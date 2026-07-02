"""Inventory queries for vCenter/ESXi storage resources."""

from __future__ import annotations

import difflib
from typing import TYPE_CHECKING

from pyVmomi import vim, vmodl
from vmware_policy import sanitize

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance


def not_found_hint(name: str, available: list[str], limit: int = 10) -> str:
    """Teaching suffix for not-found errors: closest match or available names.

    Returns '' when there is nothing useful to suggest, so callers can append
    the result unconditionally.
    """
    matches = difflib.get_close_matches(name, available, n=1)
    if matches:
        return f" Did you mean '{matches[0]}'?"
    if available:
        shown = ", ".join(sorted(available)[:limit])
        more = f", … ({len(available) - limit} more)" if len(available) > limit else ""
        return f" Available: {shown}{more}."
    return ""


# Server-side page size for PropertyCollector. Large inventories are streamed in
# batches of this many objects; the helper transparently follows continuation
# tokens, so the caller always gets the full result set.
_PC_PAGE_SIZE = 1000


def _collect(
    si: ServiceInstance, obj_type: list, paths: list[str]
) -> list[tuple[object, dict]]:
    """Batch-retrieve ``paths`` for every ``obj_type`` object in one operation.

    Uses ``PropertyCollector.RetrievePropertiesEx`` so all requested properties
    for all matching objects are fetched in a single server-side call (paged via
    continuation tokens), instead of one lazy SOAP round-trip per property per
    object. This is the difference between seconds and minutes on inventories
    with thousands of datastores/hosts (GitHub issue #31).

    Args:
        si: vSphere ServiceInstance.
        obj_type: Single-element list with the managed-object type to collect,
            e.g. ``[vim.Datastore]``.
        paths: Property paths to fetch, e.g. ``["name", "summary.capacity"]``.
            Array properties (e.g. ``vm``) come back as lists; unset properties
            are simply absent from the returned dict.

    Returns:
        List of ``(managed_object, {path: value})`` tuples in server order.
    """
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(
        content.rootFolder, obj_type, True
    )
    try:
        traversal = vmodl.query.PropertyCollector.TraversalSpec(
            name="traverseView", type=vim.view.ContainerView, path="view", skip=False
        )
        obj_spec = vmodl.query.PropertyCollector.ObjectSpec(
            obj=view, skip=True, selectSet=[traversal]
        )
        prop_spec = vmodl.query.PropertyCollector.PropertySpec(
            type=obj_type[0], pathSet=list(paths), all=False
        )
        filter_spec = vmodl.query.PropertyCollector.FilterSpec(
            objectSet=[obj_spec], propSet=[prop_spec]
        )
        options = vmodl.query.PropertyCollector.RetrieveOptions(
            maxObjects=_PC_PAGE_SIZE
        )
        pc = content.propertyCollector
        results: list[tuple[object, dict]] = []
        batch = pc.RetrievePropertiesEx([filter_spec], options)
        while batch is not None:
            for obj_content in batch.objects:
                props = {p.name: p.val for p in (obj_content.propSet or [])}
                results.append((obj_content.obj, props))
            token = getattr(batch, "token", None)
            if not token:
                break
            batch = pc.ContinueRetrievePropertiesEx(token)
        return results
    finally:
        view.Destroy()


_DS_PROPS = [
    "name",
    "summary.type",
    "summary.freeSpace",
    "summary.capacity",
    "summary.accessible",
    "summary.url",
]


def list_datastores(si: ServiceInstance, include_vm_count: bool = False) -> list[dict]:
    """List all datastores with capacity info.

    ``vm_count`` is opt-in (``include_vm_count=True``) because the backing
    datastore-to-VM linkage (``vm``) is an extra property to fetch. This is the
    most-called read path, so the default skips it; the ``vm_count`` key is
    present only when requested. All properties are fetched in a single batched
    PropertyCollector call regardless (GitHub issue #31).
    """
    paths = list(_DS_PROPS)
    if include_vm_count:
        paths.append("vm")
    results = []
    for _obj, p in _collect(si, [vim.Datastore], paths):
        cap = p.get("summary.capacity")
        free = p.get("summary.freeSpace")
        total_gb = round(cap / (1024**3), 1) if cap else 0
        free_gb = round(free / (1024**3), 1) if free else 0
        used_gb = round(total_gb - free_gb, 1)
        usage_pct = round((used_gb / total_gb) * 100, 1) if total_gb > 0 else 0
        url = p.get("summary.url")
        entry = {
            "name": sanitize(p.get("name", "")),
            "type": p.get("summary.type"),
            "free_gb": free_gb,
            "used_gb": used_gb,
            "total_gb": total_gb,
            "usage_pct": usage_pct,
            "accessible": p.get("summary.accessible"),
            "url": sanitize(url) if url else "",
        }
        if include_vm_count:
            entry["vm_count"] = len(p.get("vm") or [])
        results.append(entry)
    return sorted(results, key=lambda x: x["name"])


def list_hosts(si: ServiceInstance) -> list[dict]:
    """List ESXi hosts (minimal, for storage context)."""
    rows = _collect(si, [vim.HostSystem], ["name", "runtime.connectionState"])
    return [
        {
            "name": sanitize(p.get("name", "")),
            "connection_state": str(p.get("runtime.connectionState", "N/A")),
        }
        for _obj, p in sorted(rows, key=lambda r: r[1].get("name", ""))
    ]


def _find_by_name(si: ServiceInstance, obj_type: list, name: str):
    """Return the first managed object of ``obj_type`` whose name matches.

    Fetches every object's ``name`` in one batched call rather than touching
    ``obj.name`` per object (each of which would be a round-trip).
    """
    for obj, p in _collect(si, obj_type, ["name"]):
        if p.get("name") == name:
            return obj
    return None


def find_host_by_name(si: ServiceInstance, host_name: str) -> vim.HostSystem | None:
    """Find a host by name. Returns None if not found."""
    return _find_by_name(si, [vim.HostSystem], host_name)


def find_datastore_by_name(
    si: ServiceInstance, ds_name: str
) -> vim.Datastore | None:
    """Find a datastore by name. Returns None if not found."""
    return _find_by_name(si, [vim.Datastore], ds_name)


def find_cluster_by_name(
    si: ServiceInstance, cluster_name: str
) -> vim.ClusterComputeResource | None:
    """Find a cluster by exact name. Returns None if not found."""
    return _find_by_name(si, [vim.ClusterComputeResource], cluster_name)
