"""vSAN health, capacity, and disk group queries.

Requires pyVmomi >= 8.0.3 which includes the vSAN Management SDK.
Older pyVmomi versions need the vSAN SDK installed separately.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pyVmomi import vim

from vmware_policy import sanitize

from vmware_storage.ops.inventory import find_cluster_by_name

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

_log = logging.getLogger("vmware-storage.vsan")


class VSANError(Exception):
    """Raised on vSAN operation failures."""



def get_vsan_health(
    si: ServiceInstance,
    cluster_name: str,
) -> dict:
    """Get vSAN cluster health summary.

    Args:
        si: vSphere ServiceInstance.
        cluster_name: Name of the vSAN-enabled cluster.

    Returns:
        dict with overall_health, test_groups (list of group results),
        and cluster_name.
    """
    cluster = find_cluster_by_name(si, cluster_name)
    if cluster is None:
        raise VSANError(f"Cluster '{cluster_name}' not found")

    # Check if vSAN is enabled
    vsan_config = cluster.configurationEx.vsanConfigInfo
    if not vsan_config or not vsan_config.enabled:
        return {
            "cluster_name": cluster_name,
            "vsan_enabled": False,
            "overall_health": "N/A",
            "message": f"vSAN is not enabled on cluster '{cluster_name}'",
        }

    # Collect disk groups per host
    disk_groups = []
    for host in cluster.host or []:
        vsan_sys = host.configManager.vsanSystem
        if vsan_sys is None:
            continue
        try:
            disk_mapping = vsan_sys.config.storageInfo.diskMapping
            if disk_mapping:
                for dg in disk_mapping:
                    cache_disk = dg.ssd
                    capacity_disks = dg.nonSsd
                    disk_groups.append({
                        "host": sanitize(host.name),
                        "cache_disk": sanitize(cache_disk.displayName) if cache_disk else "N/A",
                        "cache_size_gb": round(
                            cache_disk.capacity.block * cache_disk.capacity.blockSize / (1024**3), 1
                        ) if cache_disk and cache_disk.capacity else 0,
                        "capacity_disks": len(capacity_disks) if capacity_disks else 0,
                    })
        except Exception as e:
            _log.warning("Failed to read disk groups from host %s: %s", sanitize(host.name), e)

    return {
        "cluster_name": cluster_name,
        "vsan_enabled": True,
        "overall_health": "unknown",  # Full health check requires VsanVcClusterHealthSystem
        "host_count": len(cluster.host) if cluster.host else 0,
        "disk_groups": disk_groups,
        "message": "vSAN is enabled. overall_health is 'unknown' because full health check "
                   "requires VsanVcClusterHealthSystem. Use vCenter UI for detailed status.",
    }


def get_vsan_capacity(
    si: ServiceInstance,
    cluster_name: str,
) -> dict:
    """Get vSAN capacity overview for a cluster.

    Args:
        si: vSphere ServiceInstance.
        cluster_name: Name of the vSAN-enabled cluster.

    Returns:
        dict with total/used/free capacity in GB.
    """
    cluster = find_cluster_by_name(si, cluster_name)
    if cluster is None:
        raise VSANError(f"Cluster '{cluster_name}' not found")

    vsan_config = cluster.configurationEx.vsanConfigInfo
    if not vsan_config or not vsan_config.enabled:
        return {
            "cluster_name": cluster_name,
            "vsan_enabled": False,
            "message": f"vSAN is not enabled on cluster '{cluster_name}'",
        }

    # Get capacity from vSAN datastores
    total_gb = 0.0
    free_gb = 0.0
    vsan_ds_name = None

    for ds in cluster.datastore or []:
        summary = ds.summary
        if summary.type == "vsan":
            total_gb = round(summary.capacity / (1024**3), 1) if summary.capacity else 0
            free_gb = round(summary.freeSpace / (1024**3), 1) if summary.freeSpace else 0
            vsan_ds_name = ds.name
            break

    used_gb = round(total_gb - free_gb, 1)
    usage_pct = round((used_gb / total_gb) * 100, 1) if total_gb > 0 else 0

    return {
        "cluster_name": cluster_name,
        "vsan_enabled": True,
        "datastore_name": vsan_ds_name,
        "total_gb": total_gb,
        "used_gb": used_gb,
        "free_gb": free_gb,
        "usage_pct": usage_pct,
    }
