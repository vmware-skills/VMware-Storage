"""MCP server wrapping VMware Storage operations.

This module exposes VMware vSphere datastore browsing, iSCSI configuration,
and vSAN health/capacity tools via the Model Context Protocol (MCP) using
stdio transport.

Tool categories
---------------
* **Read-only**: list_all_datastores, browse_datastore, scan_datastore_images,
  list_cached_images, storage_iscsi_status, vsan_health, vsan_capacity
* **Write**: storage_iscsi_enable, storage_iscsi_add_target,
  storage_iscsi_remove_target, storage_rescan

Security considerations
-----------------------
* Credentials are loaded from environment variables / .env file.
* Transport: Uses stdio transport (local only); no network listener.
* iSCSI operations modify host storage configuration; confirmation recommended.

Source: https://github.com/zw008/VMware-Storage
License: MIT
"""


import logging
import os
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from vmware_policy import (
    apply_read_only_gate,
    mtime_cached_loader,
    sanitize,
    set_environment_resolver,
    vmware_tool,
)

from vmware_storage.config import CONFIG_FILE, load_config
from vmware_storage.connection import ConnectionManager
from vmware_storage.ops import datastore_browser
from vmware_storage.ops.inventory import list_datastores
from vmware_storage.ops.iscsi_config import (
    add_iscsi_target,
    enable_software_iscsi,
    get_iscsi_status,
    remove_iscsi_target,
    rescan_storage,
)
from vmware_storage.ops.vsan import get_vsan_capacity, get_vsan_health
from vmware_storage.notify.audit import AuditLogger

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vmware-storage.mcp")

def _safe_error(exc: Exception, tool: str) -> str:
    """Return an agent-safe error string; log full detail server-side only.

    Raw exception text can carry API response bodies, internal paths, or
    host:port pairs. Full traceback goes to the server log; the agent sees only
    a control-char-stripped, length-capped message. Intentional validation
    errors (ValueError/FileNotFoundError/KeyError/PermissionError) pass through.
    """
    logger.error("Tool %s failed", tool, exc_info=True)
    if isinstance(exc, (ValueError, FileNotFoundError, KeyError, PermissionError)):
        return sanitize(str(exc), 300)
    return f"{type(exc).__name__}: operation failed."


mcp = FastMCP("VMware Storage")

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
        config_path = os.environ.get("VMWARE_STORAGE_CONFIG")
        config = load_config(Path(config_path) if config_path else None)
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

    Returns the list envelope: 'items' holds one row per datastore, and
    'returned'/'total'/'truncated' state whether the listing is complete.
    Every datastore is enumerated in one pass, so truncated is always false.

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

    Returns the list envelope: 'items' holds one row per file, and
    'returned'/'total'/'truncated' state whether the listing is complete.
    Every match in the searched folders is returned, so truncated is
    always false.

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

    Returns the list envelope: 'items' holds one row per image, and
    'returned'/'total'/'truncated' state whether the listing is complete.
    Every image pattern is browsed in full, so truncated is always false.

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
    scans; results may be stale or empty if no scan has run. For a live,
    authoritative listing of one datastore use scan_datastore_images instead.
    Returns the list envelope: 'items' holds {datastore, name, ds_path,
    size_mb, type, modified} and is empty if nothing matches, while
    'returned'/'total'/'truncated' state whether the listing is complete. The
    whole registry is filtered in memory, so truncated is always false. No
    side effects.

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
        target: Optional vCenter/ESXi target name from config; omit to use
            the default target.
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
        return f"Error: {_safe_error(e, 'storage')} Run 'vmware-storage doctor' to verify connectivity."


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def storage_iscsi_status(
    host_name: str,
    target: Optional[str] = None,
) -> dict:
    """[READ] Get the software iSCSI adapter state and configured send targets for an ESXi host.

    Returns {host, enabled, hba_device, iqn, send_targets: [{address, port}]};
    when the adapter is disabled, enabled=false with null device/IQN and an
    empty target list. No side effects. Use this before storage_iscsi_enable /
    storage_iscsi_add_target / storage_iscsi_remove_target to check
    prerequisites, and afterwards to verify the change took effect.

    Args:
        host_name: ESXi host name exactly as shown in vCenter inventory
            (FQDN or IP). Errors if not found.
        target: Optional vCenter/ESXi target name from config; omit to use
            the default target.
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
        target: Optional vCenter/ESXi target name from config; omit for the
            default target.
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
        return f"Error: {_safe_error(e, 'storage')} Run 'vmware-storage doctor' to verify connectivity."


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
        target: Optional vCenter/ESXi target name from config; omit for the
            default target.
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
        return f"Error: {_safe_error(e, 'storage')} Run 'vmware-storage doctor' to verify connectivity."


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def storage_rescan(
    host_name: str,
    dry_run: bool = False,
    target: Optional[str] = None,
) -> str:
    """[WRITE] Rescan all HBAs and VMFS volumes on an ESXi host to discover newly presented LUNs and datastores.

    Use after a storage array presents new LUNs or after out-of-band SAN
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
        target: Optional vCenter/ESXi target name from config; omit for the
            default target.
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
        return f"Error: {_safe_error(e, 'storage')} Run 'vmware-storage doctor' to verify connectivity."


# ---------------------------------------------------------------------------
# vSAN tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def vsan_health(
    cluster_name: str,
    target: Optional[str] = None,
) -> dict:
    """[READ] Get vSAN enablement status, host count, and per-host disk-group layout for a cluster.

    Returns {cluster_name, vsan_enabled, overall_health, host_count,
    disk_groups: [{host, cache_disk, cache_size_gb, capacity_disks}]}.
    Note: overall_health is reported as "unknown" — full health checks require
    vCenter's VsanVcClusterHealthSystem; use the vCenter UI for detailed
    health. If vSAN is not enabled, returns vsan_enabled=false with a message
    rather than an error. Use vsan_capacity for space usage instead. No side
    effects.

    Args:
        cluster_name: Cluster name exactly as shown in vCenter. Errors if the
            cluster is not found.
        target: Optional vCenter/ESXi target name from config; omit to use
            the default target.
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

    Returns {cluster_name, vsan_enabled, datastore_name, total_gb, used_gb,
    free_gb, usage_pct}. If vSAN is not enabled on the cluster, returns
    vsan_enabled=false with an explanatory message rather than an error;
    errors only if the cluster name is not found. Use vsan_health for
    disk-group layout and host details; use list_all_datastores for non-vSAN
    (VMFS/NFS) datastore usage. No side effects.

    Args:
        cluster_name: Cluster name exactly as shown in vCenter. Errors if the
            cluster is not found.
        target: Optional vCenter/ESXi target name from config; omit to use
            the default target.
    """
    try:
        si = _get_connection(target)
        return get_vsan_capacity(si, cluster_name)
    except Exception as e:
        logger.error("vsan_capacity failed: %s", e)
        return {"error": _safe_error(e, "storage"), "hint": "Run 'vmware-storage doctor' to verify connectivity."}


# ---------------------------------------------------------------------------
# Read-only gate
# ---------------------------------------------------------------------------


def _config_read_only() -> Optional[bool]:
    """Best-effort read of ``read_only`` from the config file.

    Runs at import time, when no config file need exist yet (tests, ``--help``,
    smoke checks), so every failure degrades to "not configured" and lets the
    env vars decide. None and False are equivalent here — config is the last
    link in the precedence chain — but None keeps 'not configured'
    distinguishable from 'configured off' in logs and debugging.

    Resolved through the same VMWARE_STORAGE_CONFIG override the connection layer
    uses. Reading the default path instead would silently ignore settings in an
    operator's custom config file — a control that appears configured and does
    nothing, which is the exact failure this work exists to remove.
    """
    try:
        _cfg_path = os.environ.get("VMWARE_STORAGE_CONFIG")
        return load_config(Path(_cfg_path) if _cfg_path else None).read_only
    except Exception:  # noqa: BLE001 — absent/unreadable config is not an error here
        return None


# Applied once, after every tool module above has registered. In read-only mode
# the write tools are removed from the registry, so list_tools() never offers
# them — the guarantee is structural rather than a prompt instruction the model
# may ignore (VMware-AIops issue #31).
WITHHELD_WRITE_TOOLS: list[str] = apply_read_only_gate(
    mcp, "vmware-storage", config_flag=_config_read_only()
)


# ---------------------------------------------------------------------------
# Environment declaration
# ---------------------------------------------------------------------------


_cached_config = mtime_cached_loader("VMWARE_STORAGE_CONFIG", CONFIG_FILE, load_config)


def _environment_for(target: Optional[str]) -> str:
    """Report the environment a target declares, for policy scoping.

    Policy rules scope by environment ("irreversible work in production needs a
    second person"), and vmware-policy cannot read this skill's config itself.
    Registering this lookup is what lets those rules fire at all. Reloaded on
    config.yaml mtime change so an edit takes effect without restarting the
    server. The config is cached via :func:`vmware_policy.mtime_cached_loader`,
    so repeated tool calls pay one ``os.stat`` instead of a full YAML parse.
    """
    try:
        return _cached_config().environment_for(target)
    except Exception:  # noqa: BLE001 — an unreadable config means "undeclared"
        return ""


set_environment_resolver(_environment_for)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Run the MCP server."""
    mcp.run(transport="stdio")
