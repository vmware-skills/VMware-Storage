"""Which VMs take the space on one datastore.

"datastore1 is 74% full — which VMs take the most?" had no direct answer: the
only route was browse_datastore, summing each folder's VMDK sizes. Those are
provisioned sizes, not space used; a folder name is not a VM name; and files
no VM owns (an orphaned nested copy, an ISO) were folded into some VM's total
(2026-10-24 conversation round, storage-datastore-top-consumers).

vCenter already keeps the number per VM and datastore:
``VirtualMachine.storage.perDatastoreUsage`` — ``committed`` (bytes on disk),
``uncommitted`` (thin headroom it may still grow into) and ``unshared``. This
reads it for every VM in one PropertyCollector pass and keeps the rows for the
requested datastore. Whatever the datastore holds beyond the VMs' committed
bytes is reported as ``unattributed_gb``, not spread across VMs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import sanitize

from vmware_storage.ops.inventory import (
    _collect,
    find_datastore_by_name,
    list_datastores,
    not_found_hint,
)

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

_GB = 1024**3

_VM_PROPS = [
    "name", "datastore", "storage.perDatastoreUsage", "storage.timestamp", "config.template",
]


def _gb(n: int | None) -> float | None:
    return None if n is None else round(n / _GB, 1)


def _moid(obj: object) -> str | None:
    return getattr(obj, "_moId", None)


def datastore_vm_usage(si: ServiceInstance, datastore_name: str, top_n: int = 10) -> dict:
    """Per-VM space on one datastore, largest committed first.

    Returns the datastore's own capacity figures, ``items`` (the top ``top_n``
    VMs: committed / uncommitted / unshared GB and when vCenter measured them),
    ``total`` (every VM with files there), ``attributed_gb`` (the VMs'
    committed sum), ``unattributed_gb`` (used space no VM's committed bytes
    account for) and ``unmeasured_vms`` (VMs that list the datastore but carry
    no usage row for it, so their space is unknown, not zero).
    """
    if top_n < 1:
        raise ValueError(f"top_n must be at least 1; got {top_n}.")
    ds = find_datastore_by_name(si, datastore_name)
    if ds is None:
        names = [d["name"] for d in list_datastores(si)["items"]]
        raise ValueError(
            f"Datastore '{datastore_name}' not found on this target. Run "
            f"list_all_datastores and copy an exact name (case-sensitive)."
            f"{not_found_hint(datastore_name, names)}"
        )
    ds_id = _moid(ds)
    summary = ds.summary
    capacity, free = summary.capacity or 0, summary.freeSpace or 0

    rows, unmeasured = [], []
    for vm, p in _collect(si, [vim.VirtualMachine], _VM_PROPS):
        if ds_id not in {_moid(d) for d in (p.get("datastore") or [])}:
            continue
        name = sanitize(p.get("name", ""))
        usage = next(
            (u for u in (p.get("storage.perDatastoreUsage") or []) if _moid(u.datastore) == ds_id),
            None,
        )
        if usage is None:
            unmeasured.append(name)
            continue
        stamp = p.get("storage.timestamp")
        rows.append({
            "name": name,
            "vm_id": _moid(vm),
            "template": bool(p.get("config.template")),
            "committed_gb": _gb(usage.committed),
            "uncommitted_gb": _gb(usage.uncommitted),
            "unshared_gb": _gb(usage.unshared),
            "measured_at": stamp.isoformat() if stamp else None,
        })

    rows.sort(key=lambda r: (-(r["committed_gb"] or 0), r["name"]))
    attributed = round(sum(r["committed_gb"] or 0 for r in rows), 1)
    used = _gb(capacity - free)
    total = len(rows)
    out = {
        "datastore": {
            "name": sanitize(datastore_name),
            "type": summary.type,
            "total_gb": _gb(capacity),
            "used_gb": used,
            "free_gb": _gb(free),
            "usage_pct": round((capacity - free) / capacity * 100, 1) if capacity else None,
        },
        "items": rows[:top_n],
        "returned": min(top_n, total),
        "total": total,
        "truncated": total > top_n,
        "attributed_gb": attributed,
        # committed counts space a linked clone shares with its parent once per
        # VM, so the sum can pass used; that is overlap, not negative space.
        "unattributed_gb": max(round(used - attributed, 1), 0.0) if used is not None else None,
        "unmeasured_vms": sorted(unmeasured),
        "note": (
            "committed_gb is space on disk; uncommitted_gb is thin-provisioned "
            "headroom the VM may still grow into. unattributed_gb is used space "
            "no VM's committed bytes account for: unregistered VMs, orphaned "
            "disks, ISOs, logs. Find those files with browse_datastore."
        ),
    }
    if used is not None and attributed > used:
        out["note"] += (
            " The VMs' committed sum exceeds the datastore's used space: some "
            "disks are shared (linked clones), so committed_gb overlaps."
        )
    if unmeasured:
        out["note"] += (
            f" {len(unmeasured)} VM(s) list this datastore but vCenter holds no usage "
            "for them here, so their space is unknown, not zero; part of "
            "unattributed_gb may be theirs."
        )
    return out
