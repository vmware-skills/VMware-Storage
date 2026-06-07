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
from vmware_policy import vmware_tool

from vmware_storage.config import load_config
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

mcp = FastMCP("VMware Storage")

_audit = AuditLogger()

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
def list_all_datastores(target: Optional[str] = None) -> list[dict]:
    """[READ] List all datastores with capacity, usage percentage, and accessibility.

    Args:
        target: Optional vCenter/ESXi target name from config.
    """
    try:
        si = _get_connection(target)
        return list_datastores(si)
    except Exception as e:
        logger.error("list_all_datastores failed: %s", e)
        return [{"error": str(e), "hint": "Run 'vmware-storage doctor' to verify connectivity."}]


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def browse_datastore(
    ds_name: str,
    path: str = "",
    pattern: str = "*",
    target: Optional[str] = None,
) -> list[dict]:
    """[READ] Browse files in a datastore directory.

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
        return [{"error": str(e), "hint": "Run 'vmware-storage doctor' to verify connectivity."}]


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def scan_datastore_images(
    ds_name: str,
    path: str = "",
    target: Optional[str] = None,
) -> list[dict]:
    """[READ] Scan a datastore for deployable images (OVA, ISO, OVF, VMDK).

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
        return [{"error": str(e), "hint": "Run 'vmware-storage doctor' to verify connectivity."}]


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="low")
def list_cached_images(
    image_type: Optional[str] = None,
    datastore: Optional[str] = None,
) -> list[dict]:
    """[READ] List deployable images (OVA/OVF/ISO/VMDK) from the local cache registry — instant, no vCenter connection or datastore I/O.

    Reads ~/.vmware-storage/image_registry.json, populated by prior datastore
    scans; results may be stale or empty if no scan has run. For a live,
    authoritative listing of one datastore use scan_datastore_images instead.
    Returns a list of {datastore, name, ds_path, size_mb, type, modified};
    empty list if nothing matches. No side effects.

    Args:
        image_type: Filter by file extension without the dot, e.g. "ova",
            "iso", "ovf", "vmdk" (case-insensitive). Omit for all types.
        datastore: Filter by exact datastore name. Omit for all datastores.
    """
    try:
        return datastore_browser.list_images(image_type=image_type, datastore=datastore)
    except Exception as e:
        logger.error("list_cached_images failed: %s", e)
        return [{"error": str(e), "hint": "Run 'vmware-storage doctor' to verify connectivity."}]


# ---------------------------------------------------------------------------
# iSCSI tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def storage_iscsi_enable(
    host_name: str,
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
        target: Optional vCenter/ESXi target name from config; omit to use
            the default target.
    """
    try:
        si = _get_connection(target)
        result = enable_software_iscsi(si, host_name)
        _audit.log(target=target or "default", operation="iscsi_enable",
                   resource=host_name, parameters={"host_name": host_name}, result=result)
        return result
    except Exception as e:
        logger.error("storage_iscsi_enable failed: %s", e)
        return f"Error: {e}. Run 'vmware-storage doctor' to verify connectivity."


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
@vmware_tool(risk_level="medium")
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
        return {"error": str(e), "hint": "Run 'vmware-storage doctor' to verify connectivity."}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def storage_iscsi_add_target(
    host_name: str,
    address: str,
    port: int = 3260,
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
        target: Optional vCenter/ESXi target name from config; omit for the
            default target.
    """
    try:
        si = _get_connection(target)
        result = add_iscsi_target(si, host_name, address, port)
        _audit.log(target=target or "default", operation="iscsi_add_target",
                   resource=host_name,
                   parameters={"host_name": host_name, "address": address, "port": port},
                   result=result)
        return result
    except Exception as e:
        logger.error("storage_iscsi_add_target failed: %s", e)
        return f"Error: {e}. Run 'vmware-storage doctor' to verify connectivity."


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def storage_iscsi_remove_target(
    host_name: str,
    address: str,
    port: int = 3260,
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
        target: Optional vCenter/ESXi target name from config; omit for the
            default target.
    """
    try:
        si = _get_connection(target)
        result = remove_iscsi_target(si, host_name, address, port)
        _audit.log(target=target or "default", operation="iscsi_remove_target",
                   resource=host_name,
                   parameters={"host_name": host_name, "address": address, "port": port},
                   result=result)
        return result
    except Exception as e:
        logger.error("storage_iscsi_remove_target failed: %s", e)
        return f"Error: {e}. Run 'vmware-storage doctor' to verify connectivity."


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@vmware_tool(risk_level="medium")
def storage_rescan(
    host_name: str,
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
        target: Optional vCenter/ESXi target name from config; omit for the
            default target.
    """
    try:
        si = _get_connection(target)
        result = rescan_storage(si, host_name)
        _audit.log(target=target or "default", operation="storage_rescan",
                   resource=host_name, parameters={"host_name": host_name}, result=result)
        return result
    except Exception as e:
        logger.error("storage_rescan failed: %s", e)
        return f"Error: {e}. Run 'vmware-storage doctor' to verify connectivity."


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
        return {"error": str(e), "hint": "Run 'vmware-storage doctor' to verify connectivity."}


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
        return {"error": str(e), "hint": "Run 'vmware-storage doctor' to verify connectivity."}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Run the MCP server."""
    mcp.run(transport="stdio")
