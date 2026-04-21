"""Inventory queries for vCenter/ESXi storage resources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import sanitize

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance


def _get_objects(si: ServiceInstance, obj_type: list, recursive: bool = True) -> list:
    """Generic container view helper."""
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder, obj_type, recursive
    )
    try:
        return list(container.view)
    finally:
        container.Destroy()


def list_datastores(si: ServiceInstance) -> list[dict]:
    """List all datastores with capacity info."""
    datastores = _get_objects(si, [vim.Datastore])
    results = []
    for ds in datastores:
        summary = ds.summary
        total_gb = round(summary.capacity / (1024**3), 1) if summary.capacity else 0
        free_gb = round(summary.freeSpace / (1024**3), 1) if summary.freeSpace else 0
        used_gb = round(total_gb - free_gb, 1)
        usage_pct = round((used_gb / total_gb) * 100, 1) if total_gb > 0 else 0
        results.append({
            "name": sanitize(ds.name),
            "type": summary.type,
            "free_gb": free_gb,
            "used_gb": used_gb,
            "total_gb": total_gb,
            "usage_pct": usage_pct,
            "accessible": summary.accessible,
            "url": sanitize(summary.url) if summary.url else "",
            "vm_count": len(ds.vm) if ds.vm else 0,
        })
    return sorted(results, key=lambda x: x["name"])


def list_hosts(si: ServiceInstance) -> list[dict]:
    """List ESXi hosts (minimal, for storage context)."""
    hosts = _get_objects(si, [vim.HostSystem])
    return [
        {
            "name": sanitize(host.name),
            "connection_state": str(host.runtime.connectionState),
        }
        for host in sorted(hosts, key=lambda h: h.name)
    ]


def find_host_by_name(si: ServiceInstance, host_name: str) -> vim.HostSystem | None:
    """Find a host by name. Returns None if not found."""
    hosts = _get_objects(si, [vim.HostSystem])
    for host in hosts:
        if host.name == host_name:
            return host
    return None


def find_datastore_by_name(
    si: ServiceInstance, ds_name: str
) -> vim.Datastore | None:
    """Find a datastore by name. Returns None if not found."""
    datastores = _get_objects(si, [vim.Datastore])
    for ds in datastores:
        if ds.name == ds_name:
            return ds
    return None


def find_cluster_by_name(
    si: ServiceInstance, cluster_name: str
) -> vim.ClusterComputeResource | None:
    """Find a cluster by exact name. Returns None if not found."""
    clusters = _get_objects(si, [vim.ClusterComputeResource])
    for cluster in clusters:
        if cluster.name == cluster_name:
            return cluster
    return None
