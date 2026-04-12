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

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

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

_conn_mgr: ConnectionManager | None = None


def _get_conn_mgr() -> ConnectionManager:
    global _conn_mgr
    if _conn_mgr is None:
        config_path = os.environ.get("VMWARE_STORAGE_CONFIG")
        config = load_config(Path(config_path) if config_path else None)
        _conn_mgr = ConnectionManager(config)
    return _conn_mgr


def _get_connection(target: str | None = None):
    return _get_conn_mgr().connect(target)


# ---------------------------------------------------------------------------
# Datastore tools
# ---------------------------------------------------------------------------


@mcp.tool()
@vmware_tool(risk_level="low")
def list_all_datastores(target: str | None = None) -> list[dict]:
    """[READ] List all datastores with capacity, usage percentage, and accessibility.

    Args:
        target: Optional vCenter/ESXi target name from config.
    """
    si = _get_connection(target)
    return list_datastores(si)


@mcp.tool()
@vmware_tool(risk_level="low")
def browse_datastore(
    ds_name: str,
    path: str = "",
    pattern: str = "*",
    target: str | None = None,
) -> list[dict]:
    """[READ] Browse files in a datastore directory.

    Args:
        ds_name: Datastore name.
        path: Subdirectory path (empty for root).
        pattern: Glob pattern to filter files (e.g. "*.ova", "*.iso").
        target: Optional vCenter/ESXi target name from config.
    """
    si = _get_connection(target)
    return datastore_browser.browse_datastore(si, ds_name, path=path, pattern=pattern)


@mcp.tool()
@vmware_tool(risk_level="low")
def scan_datastore_images(
    ds_name: str,
    path: str = "",
    target: str | None = None,
) -> list[dict]:
    """[READ] Scan a datastore for deployable images (OVA, ISO, OVF, VMDK).

    Args:
        ds_name: Datastore name.
        path: Subdirectory path (empty for root).
        target: Optional vCenter/ESXi target name from config.
    """
    si = _get_connection(target)
    return datastore_browser.scan_images(si, ds_name, path=path)


@mcp.tool()
@vmware_tool(risk_level="low")
def list_cached_images(
    image_type: str | None = None,
    datastore: str | None = None,
) -> list[dict]:
    """[READ] List images from the local cache registry.

    Args:
        image_type: Filter by extension (e.g. "ova", "iso").
        datastore: Filter by datastore name.
    """
    return datastore_browser.list_images(image_type=image_type, datastore=datastore)


# ---------------------------------------------------------------------------
# iSCSI tools
# ---------------------------------------------------------------------------


@mcp.tool()
@vmware_tool(risk_level="medium")
def storage_iscsi_enable(
    host_name: str,
    target: str | None = None,
) -> str:
    """[WRITE] Enable the software iSCSI adapter on an ESXi host.

    Args:
        host_name: ESXi host name.
        target: Optional vCenter/ESXi target name from config.
    """
    si = _get_connection(target)
    result = enable_software_iscsi(si, host_name)
    _audit.log(target=target or "default", operation="iscsi_enable",
               resource=host_name, parameters={"host_name": host_name}, result=result)
    return result


@mcp.tool()
@vmware_tool(risk_level="medium")
def storage_iscsi_status(
    host_name: str,
    target: str | None = None,
) -> dict:
    """[READ] Get iSCSI adapter status and configured send targets.

    Args:
        host_name: ESXi host name.
        target: Optional vCenter/ESXi target name from config.
    """
    si = _get_connection(target)
    return get_iscsi_status(si, host_name)


@mcp.tool()
@vmware_tool(risk_level="medium")
def storage_iscsi_add_target(
    host_name: str,
    address: str,
    port: int = 3260,
    target: str | None = None,
) -> str:
    """[WRITE] Add an iSCSI send target to an ESXi host and rescan storage.

    Args:
        host_name: ESXi host name.
        address: iSCSI target IP address.
        port: iSCSI target port (default 3260).
        target: Optional vCenter/ESXi target name from config.
    """
    si = _get_connection(target)
    result = add_iscsi_target(si, host_name, address, port)
    _audit.log(target=target or "default", operation="iscsi_add_target",
               resource=host_name,
               parameters={"host_name": host_name, "address": address, "port": port},
               result=result)
    return result


@mcp.tool()
@vmware_tool(risk_level="medium")
def storage_iscsi_remove_target(
    host_name: str,
    address: str,
    port: int = 3260,
    target: str | None = None,
) -> str:
    """[WRITE] Remove an iSCSI send target from an ESXi host and rescan storage.

    Args:
        host_name: ESXi host name.
        address: iSCSI target IP address.
        port: iSCSI target port (default 3260).
        target: Optional vCenter/ESXi target name from config.
    """
    si = _get_connection(target)
    result = remove_iscsi_target(si, host_name, address, port)
    _audit.log(target=target or "default", operation="iscsi_remove_target",
               resource=host_name,
               parameters={"host_name": host_name, "address": address, "port": port},
               result=result)
    return result


@mcp.tool()
@vmware_tool(risk_level="medium")
def storage_rescan(
    host_name: str,
    target: str | None = None,
) -> str:
    """[WRITE] Rescan all HBAs and VMFS volumes on an ESXi host.

    Args:
        host_name: ESXi host name.
        target: Optional vCenter/ESXi target name from config.
    """
    si = _get_connection(target)
    result = rescan_storage(si, host_name)
    _audit.log(target=target or "default", operation="storage_rescan",
               resource=host_name, parameters={"host_name": host_name}, result=result)
    return result


# ---------------------------------------------------------------------------
# vSAN tools
# ---------------------------------------------------------------------------


@mcp.tool()
@vmware_tool(risk_level="low")
def vsan_health(
    cluster_name: str,
    target: str | None = None,
) -> dict:
    """[READ] Get vSAN cluster health summary and disk groups.

    Args:
        cluster_name: Name of the vSAN-enabled cluster.
        target: Optional vCenter/ESXi target name from config.
    """
    si = _get_connection(target)
    return get_vsan_health(si, cluster_name)


@mcp.tool()
@vmware_tool(risk_level="low")
def vsan_capacity(
    cluster_name: str,
    target: str | None = None,
) -> dict:
    """[READ] Get vSAN capacity overview (total/used/free) for a cluster.

    Args:
        cluster_name: Name of the vSAN-enabled cluster.
        target: Optional vCenter/ESXi target name from config.
    """
    si = _get_connection(target)
    return get_vsan_capacity(si, cluster_name)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Run the MCP server."""
    mcp.run(transport="stdio")
