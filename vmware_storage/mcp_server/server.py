"""MCP server wrapping VMware Storage operations.

This module exposes VMware vSphere datastore browsing, iSCSI configuration,
and vSAN health/capacity tools via the Model Context Protocol (MCP) using
stdio transport.

Tool categories
---------------
* **Read-only**: list_all_datastores, browse_datastore, scan_datastore_images,
  list_cached_images, storage_iscsi_status, vsan_health, vsan_capacity,
  vsan_efficiency, fc_adapter_list, storage_device_paths
* **Write**: storage_iscsi_enable, storage_iscsi_add_target,
  storage_iscsi_remove_target, storage_rescan

Security considerations
-----------------------
* Credentials are loaded from environment variables / .env file.
* Transport: Uses stdio transport (local only); no network listener.
* iSCSI operations modify host storage configuration; confirmation recommended.

Source: https://github.com/vmware-skills/VMware-Storage
License: MIT
"""


import logging
import ssl
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from vmware_policy import (
    describe_tool_parameters,
    report_tool_failure,
    sanitize,
    vmware_tool,
)

from vmware_storage import __version__
from vmware_storage.config import ConfigError, load_config
from vmware_storage.connection import ConnectionManager
from vmware_storage.notify.audit import AuditLogger
from vmware_storage.ops import datastore_browser
from vmware_storage.ops.datastore_browser import DatastoreBrowseError
from vmware_storage.ops.inventory import list_datastores
from vmware_storage.ops.iscsi_config import (
    HostNotFoundError,
    ISCSIError,
    add_iscsi_target,
    enable_software_iscsi,
    get_iscsi_status,
    remove_iscsi_target,
    rescan_storage,
)
from vmware_storage.ops.multipath import device_paths
from vmware_storage.ops.storage_paths import StoragePathError, list_fc_adapters
from vmware_storage.ops.vsan import VSANError, get_vsan_capacity, get_vsan_health
from vmware_storage.ops.vsan_efficiency import get_vsan_efficiency

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vmware-storage.mcp")

def _safe_error(exc: Exception, tool: str) -> str:
    """Return an agent-safe error string; log full detail server-side only.

    Raw exception text can carry API response bodies, internal paths, or
    host:port pairs. Full traceback goes to the server log; the agent sees only
    a control-char-stripped, length-capped message.

    Every exception this skill raises on purpose passes through: the builtin
    validation errors, and the domain exceptions defined under
    ``vmware_storage.ops``. Those domain types exist to carry a corrected next
    step, so omitting them replaced ten of this skill's rewritten messages with
    ``ISCSIError: operation failed.`` on the way to the agent — teaching text
    that the CLI printed in full and the MCP surface silently threw away.

    ``TimeoutError`` and ``ConnectionError`` are here for the same reason: the
    CLI path catches ``OSError`` and prints a retry hint, and the two surfaces
    should not disagree about what a dropped connection means.

    The one configuration error this skill raises on purpose — the
    missing-password error, this family's most common first-run failure, whose
    entire remedy is the env var name it carries — passes through as the narrow
    ``ConfigError``. Bare ``OSError`` was briefly listed here for it and was too
    wide a door: ``sanitize`` strips control characters and truncates, it does
    not redact, so ``ssl.SSLCertVerificationError`` (certificate subject and
    hostname), ``socket.gaierror`` (the name that failed to resolve) and
    ``requests``-style connection errors (full scheme://host:port/path) all
    reached the agent verbatim through it. Only the narrow ``OSError``
    subclasses that were allowed before it remain.

    Anything else is reduced to its type — an unplanned exception's text was
    written for a developer reading a traceback, not for an agent choosing what
    to do next, and it is the one that can carry credentials.
    """
    logger.error("Tool %s failed", tool, exc_info=True)
    # Checked ahead of the allowlist: ssl.SSLCertVerificationError inherits from
    # ValueError as well as OSError, so it matches the ValueError entry — which
    # predates the OSError one — and narrowing the OSError side does not keep it
    # out. Its text quotes the certificate subject and the host. Self-signed
    # certs are this family's most common connection problem, and the operator
    # reads the remedy for them from the CLI, which catches ssl.SSLError itself.
    if isinstance(exc, ssl.SSLError):
        return f"{type(exc).__name__}: operation failed."
    _passthrough = (
        ValueError,
        FileNotFoundError,
        KeyError,
        PermissionError,
        TimeoutError,
        ConnectionError,
        ConfigError,
        DatastoreBrowseError,
        HostNotFoundError,
        ISCSIError,
        StoragePathError,
        VSANError,
    )
    if isinstance(exc, _passthrough):
        return sanitize(str(exc), 300)
    return f"{type(exc).__name__}: operation failed."


def _error_reply(exc: Exception, tool: str) -> str:
    """Agent-safe error string for a write tool, recorded as a *failed* call.

    The four write tools return their result as a string, so their ``except``
    block swallows the exception and the ``@vmware_tool`` wrapper above sees an
    ordinary return. Left alone it records the failed operation as ``ok``,
    writes an undo token for a change that never landed (vmware-pilot would
    then offer to reverse it), and reports success to the circuit breaker, so
    repeated failures never trip it. ``report_tool_failure`` is what tells it
    otherwise.

    The read tools need no equivalent: they return the ``{"error": ...}``
    envelope, which vmware-policy detects on its own.

    Must be called from the tool body — that is inside the ``@vmware_tool``
    wrapper's dynamic extent, which is where the signal is read.
    """
    msg = _safe_error(exc, tool)
    report_tool_failure(msg)
    return f"Error: {msg} Run 'vmware-storage doctor' to verify connectivity."


mcp = FastMCP("VMware Storage")

# FastMCP takes no version argument and leaves the lowlevel server's at
# None, which makes `initialize` answer with the MCP SDK's version rather
# than ours. Set it so a client can tell which release it is talking to.
mcp._mcp_server.version = __version__

_audit = AuditLogger()


def _safe_audit(**kwargs: Any) -> None:
    """Audit a write operation; never let audit failure mask the op result.

    Family rule: audit failure never blocks — the operation already happened,
    so the agent must see the authoritative result, not an audit traceback.
    """
    try:
        _audit.log(**kwargs)
    except Exception as e:
        logger.warning("Audit logging failed (operation succeeded): %s", e)

# ---------------------------------------------------------------------------
# Connection management (lazy-init singleton)
# ---------------------------------------------------------------------------

_conn_mgr: Optional[ConnectionManager] = None


def _get_conn_mgr() -> ConnectionManager:
    global _conn_mgr
    if _conn_mgr is None:
        # No env-var read here: load_config resolves the path (explicit arg,
        # then the environment, then the default). This copy was the reason the
        # server and the CLI opened different files — load_config did not look
        # at the variable at all, so only this path honoured it (形态 #6).
        config = load_config()
        _conn_mgr = ConnectionManager(config)
    return _conn_mgr


def _get_connection(target: Optional[str] = None):
    return _get_conn_mgr().connect(target)


# ---------------------------------------------------------------------------
# Datastore tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_all_datastores(target: Optional[str] = None) -> dict:
    """[READ] List all datastores with capacity, usage percentage, and accessibility.

    Use this first for the ds_name that browse_datastore and
    scan_datastore_images require.

    Returns the list envelope: 'items' holds one row per datastore, and
    'returned'/'total'/'truncated' state whether the listing is complete.
    Enumerated in one pass, so truncated is always false.

    Args:
        target: Optional vCenter/ESXi target name from config.
    """
    try:
        si = _get_connection(target)
        return list_datastores(si)
    except Exception as e:
        logger.error("list_all_datastores failed: %s", e)
        return {"error": _safe_error(e, "storage"), "hint": "Run 'vmware-storage doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def browse_datastore(
    ds_name: str,
    path: str = "",
    pattern: str = "*",
    target: Optional[str] = None,
) -> dict:
    """[READ] Browse files in a datastore directory.

    Use this for arbitrary files or a glob; prefer scan_datastore_images for
    deployable images. ds_name comes from list_all_datastores.

    Returns the list envelope: 'items' holds one row per file, and
    'returned'/'total'/'truncated' state whether the listing is complete.
    All matches are returned, so truncated is always false.

    Args:
        ds_name: Datastore name.
        path: Subdirectory path (empty for root).
        pattern: Glob pattern to filter files (e.g. "*.ova", "*.iso").
        target: Optional vCenter/ESXi target name from config.
    """
    try:
        si = _get_connection(target)
        return datastore_browser.browse_datastore(si, ds_name, path=path, pattern=pattern)
    except Exception as e:
        logger.error("browse_datastore failed: %s", e)
        return {"error": _safe_error(e, "storage"), "hint": "Run 'vmware-storage doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def scan_datastore_images(
    ds_name: str,
    path: str = "",
    target: Optional[str] = None,
) -> dict:
    """[READ] Scan a datastore for deployable images (OVA, ISO, OVF, VMDK).

    Use this for a live scan of one datastore; prefer list_cached_images for
    a cached answer. ds_name comes from list_all_datastores.

    Returns the list envelope: 'items' holds one row per image, and
    'returned'/'total'/'truncated' state whether the listing is complete.
    All patterns are browsed, so truncated is always false.

    Args:
        ds_name: Datastore name.
        path: Subdirectory path (empty for root).
        target: Optional vCenter/ESXi target name from config.
    """
    try:
        si = _get_connection(target)
        return datastore_browser.scan_images(si, ds_name, path=path)
    except Exception as e:
        logger.error("scan_datastore_images failed: %s", e)
        return {"error": _safe_error(e, "storage"), "hint": "Run 'vmware-storage doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_cached_images(
    image_type: Optional[str] = None,
    datastore: Optional[str] = None,
) -> dict:
    """[READ] List deployable images (OVA/OVF/ISO/VMDK) from the local cache registry — instant, no vCenter connection or datastore I/O.

    Reads ~/.vmware-storage/image_registry.json, populated by prior datastore
    scans; results may be stale or empty if no scan has run. For a live
    listing use scan_datastore_images instead.
    Returns the list envelope: 'items' holds {datastore, name, ds_path,
    size_mb, type, modified} and is empty if nothing matches, while
    'returned'/'total'/'truncated' state whether the listing is complete. The
    whole registry is filtered in memory, so truncated is always false.

    Args:
        image_type: Filter by file extension without the dot, e.g. "ova",
            "iso", "ovf", "vmdk" (case-insensitive). Omit for all types.
        datastore: Filter by exact datastore name. Omit for all datastores.
    """
    try:
        return datastore_browser.list_images(image_type=image_type, datastore=datastore)
    except Exception as e:
        logger.error("list_cached_images failed: %s", e)
        return {"error": _safe_error(e, "storage"),
                "hint": "Local image registry may be missing or corrupt — re-run scan_datastore_images to rebuild it (no vCenter connectivity is involved here)."}


# ---------------------------------------------------------------------------
# iSCSI tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def storage_iscsi_enable(
    host_name: str,
    dry_run: bool = False,
    target: Optional[str] = None,
) -> str:
    """[WRITE] Enable the software iSCSI adapter (vmhba) on an ESXi host.

    Required prerequisite before storage_iscsi_add_target. Idempotent: if the
    adapter is already enabled, returns its HBA device and IQN without making
    changes. Modifies host storage configuration but is non-disruptive (no
    reboot, no impact on existing datastores). Audit-logged to
    ~/.vmware/audit.db. Check current state first with storage_iscsi_status.
    Returns a confirmation string; errors include remediation hints.

    Args:
        host_name: ESXi host name exactly as shown in vCenter inventory
            (FQDN or IP). Errors if not found.
        dry_run: If true, return a preview of the change without executing it.
        target: Optional vCenter/ESXi target name from config.
    """
    try:
        if dry_run:
            return f"[DRY-RUN] Would enable software iSCSI on host '{host_name}'. No changes made."
        si = _get_connection(target)
        result = enable_software_iscsi(si, host_name)
        _safe_audit(target=target or "default", operation="iscsi_enable",
                    resource=host_name, parameters={"host_name": host_name}, result=result)
        return result
    except Exception as e:
        logger.error("storage_iscsi_enable failed: %s", e)
        return _error_reply(e, "storage")


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def storage_iscsi_status(
    host_name: str,
    target: Optional[str] = None,
) -> dict:
    """[READ] Get the software iSCSI adapter state and configured send targets for an ESXi host.

    Returns {host, enabled, hba_device, iqn, send_targets: [{address, port}]};
    when the adapter is disabled, enabled=false with null device/IQN and an
    empty target list. Use this before storage_iscsi_enable /
    storage_iscsi_add_target / storage_iscsi_remove_target to check
    prerequisites, and afterwards to verify the change took effect.

    Args:
        host_name: ESXi host name exactly as shown in vCenter inventory
            (FQDN or IP). Errors if not found. No host listing here — get
            names from vmware-monitor list_esxi_hosts.
        target: Optional vCenter/ESXi target name from config.
    """
    try:
        si = _get_connection(target)
        return get_iscsi_status(si, host_name)
    except Exception as e:
        logger.error("storage_iscsi_status failed: %s", e)
        return {"error": _safe_error(e, "storage"), "hint": "Run 'vmware-storage doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(
    risk_level="medium",
    undo=lambda params, result: {
        "tool": "storage_iscsi_remove_target",
        "params": {
            "host_name": params.get("host_name"),
            "address": params.get("address"),
            "port": params.get("port", 3260),
            "target": params.get("target"),
        },
        "skill": "storage",
        "note": "Inverse of storage_iscsi_add_target: remove the iSCSI send target that was added.",
    },
)
def storage_iscsi_add_target(
    host_name: str,
    address: str,
    port: int = 3260,
    dry_run: bool = False,
    target: Optional[str] = None,
) -> str:
    """[WRITE] Add an iSCSI send (dynamic discovery) target to an ESXi host's software iSCSI adapter, then automatically rescan all HBAs and VMFS volumes to discover new LUNs.

    Prerequisite: software iSCSI must be enabled first (storage_iscsi_enable);
    otherwise returns an error with guidance. Idempotent: a duplicate
    address:port returns "already configured" without changes. No separate
    storage_rescan call is needed afterwards. Audit-logged to
    ~/.vmware/audit.db. Returns a confirmation string.

    Args:
        host_name: ESXi host name as shown in vCenter inventory.
        address: iSCSI portal IP address (IPv4/IPv6 literal; hostnames are
            rejected with a validation error).
        port: iSCSI TCP port, 1-65535 (default 3260).
        dry_run: If true, return a preview of the change without executing it.
        target: Optional vCenter/ESXi target name from config.
    """
    try:
        if dry_run:
            return (f"[DRY-RUN] Would add iSCSI target {address}:{port} to host "
                    f"'{host_name}' and rescan storage. No changes made.")
        si = _get_connection(target)
        result = add_iscsi_target(si, host_name, address, port)
        _safe_audit(target=target or "default", operation="iscsi_add_target",
                    resource=host_name,
                    parameters={"host_name": host_name, "address": address, "port": port},
                    result=result)
        return result
    except Exception as e:
        logger.error("storage_iscsi_add_target failed: %s", e)
        return _error_reply(e, "storage")


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(
    risk_level="high",
    undo=lambda params, result: {
        "tool": "storage_iscsi_add_target",
        "params": {
            "host_name": params.get("host_name"),
            "address": params.get("address"),
            "port": params.get("port", 3260),
            "target": params.get("target"),
        },
        "skill": "storage",
        "note": "Inverse of storage_iscsi_remove_target: re-add the iSCSI send target that was removed.",
    },
)
def storage_iscsi_remove_target(
    host_name: str,
    address: str,
    port: int = 3260,
    dry_run: bool = False,
    target: Optional[str] = None,
) -> str:
    """[WRITE] Remove an iSCSI send target from an ESXi host's software iSCSI adapter, then rescan all HBAs and VMFS volumes.

    Destructive: LUNs served only by this target become inaccessible after the
    rescan — first verify the target exists (storage_iscsi_status) and that no
    datastores depend on it (list_all_datastores). Errors if the address:port
    pair is not configured or software iSCSI is disabled. Reversible only by
    re-adding via storage_iscsi_add_target. Audit-logged to
    ~/.vmware/audit.db. Returns a confirmation string.

    Args:
        host_name: ESXi host name as shown in vCenter inventory.
        address: Configured iSCSI portal IP (IPv4/IPv6 literal; hostnames
            rejected). Must match the existing entry exactly.
        port: Configured iSCSI TCP port, 1-65535 (default 3260).
        dry_run: If true, return a preview of the change without executing it.
        target: Optional vCenter/ESXi target name from config.
    """
    try:
        if dry_run:
            return (f"[DRY-RUN] Would remove iSCSI target {address}:{port} from host "
                    f"'{host_name}' and rescan storage. No changes made.")
        si = _get_connection(target)
        result = remove_iscsi_target(si, host_name, address, port)
        _safe_audit(target=target or "default", operation="iscsi_remove_target",
                    resource=host_name,
                    parameters={"host_name": host_name, "address": address, "port": port},
                    result=result)
        return result
    except Exception as e:
        logger.error("storage_iscsi_remove_target failed: %s", e)
        return _error_reply(e, "storage")


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def storage_rescan(
    host_name: str,
    dry_run: bool = False,
    target: Optional[str] = None,
) -> str:
    """[WRITE] Rescan all HBAs and VMFS volumes on an ESXi host to discover newly presented LUNs and datastores.

    Use this when a storage array presents new LUNs, or after out-of-band SAN
    changes. Not needed after storage_iscsi_add_target /
    storage_iscsi_remove_target — those rescan automatically. Non-destructive:
    only triggers device and VMFS discovery (deletes nothing), but it is
    I/O-visible on the host and may take a minute or two with many paths.
    Audit-logged to ~/.vmware/audit.db. Returns a confirmation string; errors
    include remediation hints.

    Args:
        host_name: ESXi host name as shown in vCenter inventory (FQDN or IP).
            Errors if not found.
        dry_run: If true, return a preview of the change without executing it.
        target: Optional vCenter/ESXi target name from config.
    """
    try:
        if dry_run:
            return (f"[DRY-RUN] Would rescan all HBAs and VMFS volumes on host "
                    f"'{host_name}'. No changes made.")
        si = _get_connection(target)
        result = rescan_storage(si, host_name)
        _safe_audit(target=target or "default", operation="storage_rescan",
                    resource=host_name, parameters={"host_name": host_name}, result=result)
        return result
    except Exception as e:
        logger.error("storage_rescan failed: %s", e)
        return _error_reply(e, "storage")


# ---------------------------------------------------------------------------
# vSAN tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def vsan_health(
    cluster_name: str,
    target: Optional[str] = None,
) -> dict:
    """[READ] Get vSAN overall health, per-check-group health, and per-host disk-group layout for a cluster.

    Returns {cluster_name, vsan_enabled, overall_health,
    overall_health_description, health_queried, health_not_queried_reason,
    health_checked_at, test_groups: [{group_id, group_name, group_health}],
    host_count, hosts_read, hosts_not_read: [{host, reason}],
    disk_groups_complete,
    disk_groups: [{host, cache_disk, cache_size_gb, capacity_disks}]}.
    overall_health is what vSAN reports — green / yellow / red, and sometimes
    "unknown", which is vSAN's own answer. When the health service could not be
    asked at all, overall_health is null and health_queried is false with the
    reason: a null is "not measured", never a measurement. health_checked_at is
    the age of vCenter's cached summary (the same one Skyline Health shows);
    this tool reads the cache rather than triggering a full health run.
    Check disk_groups_complete before treating disk_groups as the cluster's
    inventory: vCenter cannot read a disconnected or notResponding host, and an
    empty disk_groups on such a cluster does NOT mean it has none — the unread
    hosts are named in hosts_not_read and in the message.
    If vSAN is not enabled, returns vsan_enabled=false with a message
    rather than an error. Use vsan_capacity for space usage instead. No side
    effects.

    Args:
        cluster_name: Cluster name exactly as shown in vCenter. Errors if the
            cluster is not found.
        target: Optional vCenter/ESXi target name from config.
    """
    try:
        si = _get_connection(target)
        return get_vsan_health(si, cluster_name)
    except Exception as e:
        logger.error("vsan_health failed: %s", e)
        return {"error": _safe_error(e, "storage"), "hint": "Run 'vmware-storage doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def vsan_capacity(
    cluster_name: str,
    target: Optional[str] = None,
) -> dict:
    """[READ] Get space usage of a cluster's vSAN datastore for capacity planning.

    Returns {cluster_name, vsan_enabled, datastore_name, accessible, total_gb,
    used_gb, free_gb, usage_pct}. When the vSAN datastore is inaccessible
    (accessible=false) the four figures are null with a message, not 0 — vCenter
    answers 0 for a datastore it cannot reach, and 0 GB used reads as a healthy
    empty datastore. accessible=null means the summary did not say. If vSAN is
    not enabled on the cluster, returns vsan_enabled=false with an explanatory
    message rather than an error; errors only if the cluster name is not found. Use vsan_health for
    disk-group layout and host details; use list_all_datastores for non-vSAN
    (VMFS/NFS) datastore usage. No side effects.

    Args:
        cluster_name: Cluster name exactly as shown in vCenter. Errors if the
            cluster is not found.
        target: Optional vCenter/ESXi target name from config.
    """
    try:
        si = _get_connection(target)
        return get_vsan_capacity(si, cluster_name)
    except Exception as e:
        logger.error("vsan_capacity failed: %s", e)
        return {"error": _safe_error(e, "storage"), "hint": "Run 'vmware-storage doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def vsan_efficiency(
    cluster_name: str,
    target: Optional[str] = None,
) -> dict:
    """[READ] Get vSAN data-efficiency (deduplication + compression) status for a cluster.

    Returns {cluster_name, vsan_enabled, dedup_enabled, compression_enabled}.
    Reads it via the vSAN Management SDK (VsanVcClusterConfigSystem), not base
    pyVmomi. When vSAN reports no data-efficiency config (space efficiency off,
    or an OSA cluster without it), dedup_enabled/compression_enabled come back
    null with a message rather than a fabricated false. Errors only if the
    cluster name is not found. Use vsan_capacity for space usage and
    vsan_health for disk-group layout. No side effects.

    Note: vSAN Global Deduplication and vSAN-to-vSAN replication are NOT
    exposed here — neither has a verified SDK object (global dedup has no
    distinct field; v2v replication lives in the separate vSAN Data Protection
    plane). Use the vCenter/vSAN UI for those.

    Args:
        cluster_name: Cluster name exactly as shown in vCenter (case-sensitive).
            Errors if the cluster is not found — vmware-storage has no
            cluster-listing tool; get names from vmware-monitor list_all_clusters.
        target: Optional vCenter/ESXi target name from config.
    """
    try:
        si = _get_connection(target)
        return get_vsan_efficiency(si, cluster_name)
    except Exception as e:
        logger.error("vsan_efficiency failed: %s", e)
        return {"error": _safe_error(e, "storage"), "hint": "Run 'vmware-storage doctor' to verify connectivity."}


# ---------------------------------------------------------------------------
# Fibre Channel / multipath tools (read-only)
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def fc_adapter_list(
    cluster: Optional[str] = None,
    host: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    target: Optional[str] = None,
) -> dict:
    """[READ] List Fibre Channel HBAs (FC and FCoE) per ESXi host: vmhba, model, driver, status, port type, WWPN/WWNN and reported link speed.

    Use this for "which FC adapters does each host have" or to find a
    host's WWPNs; use storage_device_paths for devices and paths behind them.
    Scope with cluster OR host; with neither, every host on the target is
    read (only the adapter list is fetched, so this stays cheap).

    Returns the list envelope ('items', 'returned', 'total', 'truncated',
    'next_offset') plus hosts_without_fc (read, no FC HBA) and
    hosts_not_read [{host, reason}]. A host in hosts_not_read was NOT read —
    never report it as having no FC adapters. speed_reported is the raw
    vSphere value: the API documents bits per second, but hosts commonly report
    Gbit/s, so it is not converted. Reads host config only; no rescans.

    Args:
        cluster: Cluster name exactly as in vCenter. Omit to use host or the whole target.
        host: ESXi host name exactly as in vCenter inventory (FQDN or IP).
        limit: Rows per page, 1-200 (default 50).
        offset: Rows to skip; pass the previous page's next_offset.
        target: Optional vCenter/ESXi target name from config.
    """
    try:
        si = _get_connection(target)
        return list_fc_adapters(si, cluster=cluster, host=host, limit=limit, offset=offset)
    except Exception as e:
        logger.error("fc_adapter_list failed: %s", e)
        return {"error": _safe_error(e, "storage"), "hint": "Run 'vmware-storage doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def storage_device_paths(
    cluster: Optional[str] = None,
    host: Optional[str] = None,
    datastore: Optional[str] = None,
    device: Optional[str] = None,
    adapter: Optional[str] = None,
    only_differences: bool = False,
    limit: int = 50,
    offset: int = 0,
    target: Optional[str] = None,
) -> dict:
    """[READ] SCSI multipath state per device (NAA) across the hosts of one scope: which hosts see it, path counts and states, working paths, adapters, target WWPN, PSP/SATP policy, and the VMFS datastores on it.

    Use for: "does datastore X have dead or disabled paths on any host"
    (datastore=X), "which hosts see naa.… and through which adapters"
    (cluster + device), "do hosts see different numbers of paths"
    (cluster + only_differences=true), "which devices and datastores depend on
    vmhba2" (host + adapter). Exactly one of cluster, host or datastore is
    required — this tool will not read every host at once.

    Per device: shared (reached over FC/iSCSI or seen by 2+ hosts), seen_by,
    not_seen_on (hosts that WERE read and do not see a shared device; a disk
    inside one host is never listed), path_count_differs,
    states_needing_attention
    (dead/disabled only) and per-host {paths_total, by_state, working_paths,
    policy, satp, adapters}; with adapter set, also paths_via_adapter and
    only_paths_via_adapter (matched by vmhba name, which can be a different
    card on each host of a cluster). Per-path detail is included when device or
    datastore is given. Devices needing attention sort first; 'summary'
    counts the whole result, not just this page.

    Gotchas: hosts in hosts_not_read were NOT read (refused, not connected —
    vCenter's copy of a lost host's config may be stale — or missing from the
    reply) and are never in
    not_seen_on — when complete is false, say which hosts are unknown instead
    of concluding a device is missing. States are as vSphere reports them:
    'standby' can be normal (active/passive arrays), and a path count does not
    prove independent fabrics. NFS/vSAN/vVol datastores have no SCSI paths
    (scope_note says so). NVMe-oF namespaces may not appear. No rescans.

    Args:
        cluster: Cluster name exactly as in vCenter.
        host: ESXi host name exactly as in vCenter inventory.
        datastore: Datastore name; scopes to the hosts that mount it and its VMFS extents.
        device: Canonical name (e.g. naa.60060e80...) or display name; case-insensitive.
        adapter: vmhba name (e.g. vmhba2) to keep devices with a path through it.
        only_differences: Keep only devices whose visibility or path count
            differs across the hosts that were read.
        limit: Devices per page, 1-200 (default 50).
        offset: Devices to skip; pass the previous page's next_offset.
        target: Optional vCenter/ESXi target name from config.
    """
    try:
        si = _get_connection(target)
        return device_paths(
            si, cluster=cluster, host=host, datastore=datastore, device=device,
            adapter=adapter, only_differences=only_differences, limit=limit, offset=offset,
        )
    except Exception as e:
        logger.error("storage_device_paths failed: %s", e)
        return {"error": _safe_error(e, "storage"), "hint": "Run 'vmware-storage doctor' to verify connectivity."}


# ---------------------------------------------------------------------------
# Environment declaration
# ---------------------------------------------------------------------------

# The environment resolver lives in policy_environment so the CLI registers
# it too (its @guarded writes go through the same guard()); importing it here
# registers it for the MCP surface.
from vmware_storage.policy_environment import _cached_config, _environment_for  # noqa: E402,F401

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


#: How long a stop signal waits for the session logout before exiting anyway.
#: A logout to a vCenter that answers takes well under a second.
_STOP_LOGOUT_SECONDS = 5.0


def _exit_on_stop_signals() -> None:
    """Turn the signals a client stops this server with into a normal exit.

    Claude Code stops a stdio MCP server with SIGINT and then SIGTERM about a
    millisecond later (measured 2026-09-15). Python's default SIGTERM ends the
    process on the spot, before ``atexit`` runs, so the ``Disconnect`` the
    connection layer registered never happened and every conversation left its
    vCenter session open. ``SystemExit`` is not enough: raised from the handler it unwinds the event
    loop, but interpreter shutdown then waits for anyio's worker thread blocked
    reading stdin, which the client keeps open, so ``atexit`` still never ran
    (independent review, 2026-09-15; a test driving the real stdio loop hung in
    all five skills). So the first stop signal ignores the rest, runs the
    ``atexit`` callbacks, and leaves with ``os._exit`` — nothing waits on that
    thread. The callbacks run on a worker thread with a deadline: pyVmomi
    connects with ``httpConnectionTimeout=None``, so a logout to a vCenter that
    stopped answering, or one waiting on the SOAP stub lock a tool call held
    when the signal landed, otherwise left a server that ignored every stop
    signal and only SIGKILL ended (second independent review, 2026-09-15).
    """
    import atexit
    import os
    import signal
    import threading

    stop_signals = [
        getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, name)
    ]

    def _stop(signum: int, _frame: object) -> None:
        for sig in stop_signals:
            signal.signal(sig, signal.SIG_IGN)
        try:
            logout = threading.Thread(
                target=atexit._run_exitfuncs, name="logout-on-stop", daemon=True
            )
            logout.start()
            logout.join(_STOP_LOGOUT_SECONDS)
            if logout.is_alive():
                # Non-blocking, straight to fd 2: a client that keeps stderr open
                # but stops reading it would otherwise park this write, and the
                # exit, on a full pipe (independent review, 2026-09-15). A
                # message that cannot be written is dropped — exiting matters more.
                message = (
                    f"Session logout did not finish within {_STOP_LOGOUT_SECONDS:.0f}s; "
                    "exiting without it. vCenter or ESXi ends the session when it "
                    "idles out.\n"
                )
                try:
                    os.set_blocking(2, False)
                    os.write(2, message.encode("utf-8", "replace"))
                except OSError:
                    pass
        finally:
            os._exit(128 + signum)

    for sig in stop_signals:
        signal.signal(sig, _stop)


def main():
    """Run the MCP server."""
    _exit_on_stop_signals()
    mcp.run(transport="stdio")

# The docstrings above are the schema. `describe_tool_parameters` copies each
# `Args:` entry into the JSON schema an agent actually reads, and closes the
# object. Without it every parameter reaches the model as a bare name and a
# type, which is how a wrong guess becomes an unfiltered result or a silent
# zero-row answer instead of an error (real-hardware round, 2026-08-30).
_DESCRIBED_PARAMS = describe_tool_parameters(mcp._tool_manager._tools)
