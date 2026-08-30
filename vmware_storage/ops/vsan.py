"""vSAN health, capacity, and disk group queries.

Requires pyVmomi >= 8.0.3 which includes the vSAN Management SDK.
Older pyVmomi versions need the vSAN SDK installed separately.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import sanitize

from vmware_storage.ops.inventory import _collect, find_cluster_by_name, not_found_hint

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

_log = logging.getLogger("vmware-storage.vsan")


class VSANError(Exception):
    """Raised on vSAN operation failures."""


def _require_cluster(si: ServiceInstance, cluster_name: str) -> vim.ClusterComputeResource:
    """Find a cluster or raise VSANError with a teaching hint."""
    cluster = find_cluster_by_name(si, cluster_name)
    if cluster is None:
        names = [
            sanitize(p.get("name", ""))
            for _obj, p in _collect(si, [vim.ClusterComputeResource], ["name"])
        ]
        # Same density as the host twin in ops/iscsi_config.py. The dropped
        # clause restated case-sensitivity in 78 characters, which pushed the
        # message past the MCP layer's 300-char sanitize cap as soon as
        # not_found_hint appended its "Did you mean 'x'?" — cutting off the one
        # part of the message that names the correct cluster.
        raise VSANError(
            f"Cluster '{cluster_name}' not found on this target. vmware-storage has "
            f"no cluster-listing tool — run vmware-monitor's list_all_clusters to get "
            f"exact cluster names (case-sensitive) and copy one."
            f"{not_found_hint(cluster_name, names)}"
        )
    return cluster



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
    cluster = _require_cluster(si, cluster_name)

    # Check if vSAN is enabled
    vsan_config = cluster.configurationEx.vsanConfigInfo
    if not vsan_config or not vsan_config.enabled:
        return {
            "cluster_name": cluster_name,
            "vsan_enabled": False,
            "overall_health": "N/A",
            "message": f"vSAN is not enabled on cluster '{cluster_name}'",
        }

    # Collect disk groups per host. Bounded to one cluster's hosts, and each
    # host's vsanSystem.config chain is a distinct managed object (not batchable
    # via a single PropertyCollector path), so per-host reads are acceptable here.
    #
    # Every host that does not contribute is recorded with the reason. Silently
    # skipping them returned `disk_groups: []` for a cluster whose four
    # notResponding hosts were never asked (VCF 9.1, 2026-08-30) — a shape a
    # caller cannot tell apart from "this cluster has no disk groups".
    disk_groups = []
    hosts_not_read: list[dict] = []
    hosts_read = 0
    for host in cluster.host or []:
        host_name = sanitize(getattr(host, "name", ""))
        state = str(
            getattr(getattr(host, "runtime", None), "connectionState", "") or "unknown"
        )
        vsan_sys = host.configManager.vsanSystem
        if vsan_sys is None:
            hosts_not_read.append({
                "host": host_name,
                "reason": f"host exposes no vsanSystem (connectionState={state})",
            })
            continue
        try:
            disk_mapping = vsan_sys.config.storageInfo.diskMapping
        except Exception as e:
            _log.warning("Failed to read disk groups from host %s: %s", host_name, e)
            hosts_not_read.append({
                "host": host_name,
                "reason": f"read failed (connectionState={state}): {sanitize(str(e))}",
            })
            continue
        hosts_read += 1
        for dg in disk_mapping or []:
            cache_disk = dg.ssd
            capacity_disks = dg.nonSsd
            disk_groups.append({
                "host": host_name,
                "cache_disk": sanitize(cache_disk.displayName) if cache_disk else "N/A",
                "cache_size_gb": round(
                    cache_disk.capacity.block * cache_disk.capacity.blockSize / (1024**3), 1
                ) if cache_disk and cache_disk.capacity else 0,
                "capacity_disks": len(capacity_disks) if capacity_disks else 0,
            })

    message = (
        "vSAN is enabled. overall_health is 'unknown' because full health check "
        "requires VsanVcClusterHealthSystem. Use vCenter UI for detailed status."
    )
    if hosts_not_read:
        # Named in the message, not only in the field: the message is the part a
        # chat client reliably renders, and a reader who sees only it must not
        # come away believing the cluster was surveyed.
        message += (
            f" {len(hosts_not_read)} of {len(cluster.host or [])} host(s) could not "
            f"be read, so disk_groups is incomplete and an empty list here does NOT "
            f"mean the cluster has none: "
            + "; ".join(f"{h['host']} — {h['reason']}" for h in hosts_not_read)
        )

    return {
        "cluster_name": cluster_name,
        "vsan_enabled": True,
        "overall_health": "unknown",  # Full health check requires VsanVcClusterHealthSystem
        "host_count": len(cluster.host) if cluster.host else 0,
        "hosts_read": hosts_read,
        "hosts_not_read": hosts_not_read,
        #: False when any host went unread — the flag a caller checks before
        #: treating `disk_groups` as the cluster's full inventory.
        "disk_groups_complete": not hosts_not_read,
        "disk_groups": disk_groups,
        "message": message,
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
    cluster = _require_cluster(si, cluster_name)

    vsan_config = cluster.configurationEx.vsanConfigInfo
    if not vsan_config or not vsan_config.enabled:
        return {
            "cluster_name": cluster_name,
            "vsan_enabled": False,
            "message": f"vSAN is not enabled on cluster '{cluster_name}'",
        }

    # Get capacity from vSAN datastores. Bounded to one cluster's datastores
    # (typically a handful), and we early-exit on the first vsan datastore, so
    # per-datastore summary reads are acceptable here.
    total_gb = 0.0
    free_gb = 0.0
    vsan_ds_name = None
    accessible = None

    for ds in cluster.datastore or []:
        summary = ds.summary
        if summary.type == "vsan":
            total_gb = round(summary.capacity / (1024**3), 1) if summary.capacity else 0
            free_gb = round(summary.freeSpace / (1024**3), 1) if summary.freeSpace else 0
            vsan_ds_name = ds.name
            # Tri-state on purpose. False is a measured failure; None is an
            # older/partial summary that did not say, and resolving that to True
            # is how "not asked" became "healthy".
            raw = getattr(summary, "accessible", None)
            accessible = None if raw is None else bool(raw)
            break

    if vsan_ds_name is None:
        # vSAN is flagged enabled but no vSAN datastore is attached to the
        # cluster — don't fake healthy-looking zeros; say so explicitly.
        return {
            "cluster_name": cluster_name,
            "vsan_enabled": True,
            "datastore_name": None,
            "total_gb": None,
            "used_gb": None,
            "free_gb": None,
            "usage_pct": None,
            "message": (
                f"vSAN is enabled on cluster '{cluster_name}' but no vSAN datastore "
                "was found. The datastore may still be forming or inaccessible — "
                "check vsan_health and the vCenter UI."
            ),
        }

    if accessible is False:
        # vCenter answers capacity/freeSpace for an inaccessible datastore, and
        # the answer is 0. Reported as figures, that is a healthy-looking empty
        # datastore (VCF 9.1, 2026-08-30). Nulls plus the reason instead.
        return {
            "cluster_name": cluster_name,
            "vsan_enabled": True,
            "datastore_name": sanitize(str(vsan_ds_name)),
            "accessible": False,
            "total_gb": None,
            "used_gb": None,
            "free_gb": None,
            "usage_pct": None,
            "message": (
                f"vSAN datastore '{vsan_ds_name}' is inaccessible, so its capacity "
                f"was not measured — the figures are null rather than 0, which "
                f"would read as an empty datastore. Check vsan_health for which "
                f"hosts are unreachable, then re-run."
            ),
        }

    used_gb = round(total_gb - free_gb, 1)
    usage_pct = round((used_gb / total_gb) * 100, 1) if total_gb > 0 else 0

    return {
        "cluster_name": cluster_name,
        "vsan_enabled": True,
        "datastore_name": vsan_ds_name,
        "accessible": accessible,
        "total_gb": total_gb,
        "used_gb": used_gb,
        "free_gb": free_gb,
        "usage_pct": usage_pct,
    }
