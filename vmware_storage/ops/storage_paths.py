"""Read-only Fibre Channel adapter inventory and SCSI multipath diagnostics.

Everything here comes from the host's own storage view in vCenter
(``config.storageDevice`` and ``config.fileSystemVolume``) over the existing
pyVmomi connection — no SSH, no array or switch credentials, and no rescans.

Two rules shape the output (GitHub VMware-Storage#18):

* **A host that could not be read is not a host with zero paths.** Unread hosts
  are listed in ``hosts_not_read`` with the reason, and are never counted as
  "not seeing" a device.
* **Observed state, not verdicts.** Path states are reported as vSphere reports
  them. A standby path can be normal (active/passive arrays), and a path count
  says nothing about whether those paths cross independent fabrics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pyVmomi import vim, vmodl
from vmware_policy import paginated, sanitize

from vmware_storage.ops.inventory import _collect, not_found_hint

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_HOST_BASE = ["name", "runtime.connectionState"]
_P_HBA = "config.storageDevice.hostBusAdapter"
_P_LUN = "config.storageDevice.scsiLun"
_P_MP = "config.storageDevice.multipathInfo"
_P_MOUNT = "config.fileSystemVolume.mountInfo"

_VMFS_UUID = re.compile(r"/vmfs/volumes/([^/]+)/?$")

OBSERVATION_NOTE = (
    "Path states are as vSphere reports them. 'standby' can be normal "
    "(active/passive arrays), and a path count does not show whether the paths "
    "cross independent fabrics."
)


class StoragePathError(Exception):
    """Raised with a corrected next step when a query cannot be scoped."""


# ─── Collection ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HostRead:
    """One host's properties, or the reason they could not be read."""

    name: str
    props: dict = field(default_factory=dict)
    not_read_reason: str | None = None


def _collect_from(si: ServiceInstance, objs: list, obj_type, paths: list[str]) -> list:
    """Retrieve ``paths`` for exactly ``objs`` in one paged call.

    Returns ``(obj, props, missing)`` where ``missing`` maps each property the
    server could not return to its fault type name (e.g. ``NoPermission``).
    """
    if not objs:
        return []
    obj_specs = [vmodl.query.PropertyCollector.ObjectSpec(obj=o, skip=False) for o in objs]
    prop_spec = vmodl.query.PropertyCollector.PropertySpec(
        type=obj_type, pathSet=list(paths), all=False
    )
    filter_spec = vmodl.query.PropertyCollector.FilterSpec(objectSet=obj_specs, propSet=[prop_spec])
    options = vmodl.query.PropertyCollector.RetrieveOptions(maxObjects=1000)
    pc = si.RetrieveContent().propertyCollector
    out: list = []
    batch = pc.RetrievePropertiesEx([filter_spec], options)
    while batch is not None:
        for oc in batch.objects:
            props = {p.name: p.val for p in (oc.propSet or [])}
            missing = {
                m.path: type(m.fault).__name__ if m.fault is not None else "unknown"
                for m in (oc.missingSet or [])
            }
            out.append((oc.obj, props, missing))
        token = getattr(batch, "token", None)
        if not token:
            break
        batch = pc.ContinueRetrievePropertiesEx(token)
    return out


def _not_read_reason(props: dict, missing: dict, needed: list[str]) -> str | None:
    """Why a returned host does not count as read, or None if it does."""
    state = str(props.get("runtime.connectionState", "unknown"))
    if state != "connected":
        # vCenter keeps the last-known config of a host it lost; reporting those
        # paths as current would describe a host nobody reached.
        return f"host is not connected (connectionState={state}); its storage view may be stale"
    absent = [p for p in needed if p not in props]
    if not absent:
        return None
    faults = sorted({missing[p] for p in absent if p in missing})
    if faults:
        return f"vCenter refused the read ({', '.join(faults)})"
    return "host reported no storage information"


def _read_hosts(si: ServiceInstance, hosts: list, needed: list[str]) -> list[HostRead]:
    """Read ``needed`` host properties; every requested host appears exactly once."""
    reads = []
    returned = set()
    for obj, props, missing in _collect_from(si, hosts, vim.HostSystem, _HOST_BASE + needed):
        returned.add(getattr(obj, "_moId", id(obj)))
        name = sanitize(props.get("name", "") or "unknown")
        reason = _not_read_reason(props, missing, needed)
        reads.append(
            HostRead(name=name, props=props if reason is None else {}, not_read_reason=reason)
        )
    lost = [o for o in hosts if getattr(o, "_moId", id(o)) not in returned]
    names = _names_for(si, lost) if lost else {}
    for obj in lost:
        moid = getattr(obj, "_moId", id(obj))
        reason = "vCenter returned nothing for this host"
        reads.append(HostRead(name=names.get(moid, f"[{moid}]"), not_read_reason=reason))
    return sorted(reads, key=lambda r: r.name)


def _names_for(si: ServiceInstance, objs: list) -> dict:
    """Best-effort names for hosts the main read did not return (moId otherwise)."""
    wanted = {getattr(o, "_moId", id(o)) for o in objs}
    try:
        rows = _collect(si, [vim.HostSystem], ["name"])
    except Exception:  # noqa: BLE001 — naming is cosmetic; the host is already reported unread
        return {}
    return {
        getattr(o, "_moId", id(o)): sanitize(p.get("name", ""))
        for o, p in rows
        if getattr(o, "_moId", id(o)) in wanted and p.get("name")
    }


# ─── Scope ───────────────────────────────────────────────────────────────────


def _pick_one(kind: str, rows: list, name: str):
    hits = [(obj, p) for obj, p in rows if p.get("name") == name]
    if len(hits) == 1:
        return hits[0]
    if hits:
        advice = {
            "Cluster": "Scope by host instead, or rename one of the clusters.",
            "Datastore": "Scope by a host that mounts it, or rename one of the datastores.",
            "Host": "Use the other name vCenter shows for it (FQDN or IP), or scope by cluster.",
        }.get(kind, "Rename one of them.")
        raise StoragePathError(
            f"{len(hits)} objects of type {kind.lower()} are named '{name}' on this "
            f"target (names can repeat across datacenters). {advice}"
        )
    names = [p.get("name", "") for _o, p in rows]
    hint = not_found_hint(name, names)
    raise StoragePathError(f"{kind} '{name}' not found on this target.{hint}")


def _datastore_scope(si: ServiceInstance, datastore: str) -> tuple[list, dict]:
    rows = _collect(si, [vim.Datastore], ["name", "host", "summary.type", "summary.url"])
    _obj, p = _pick_one("Datastore", rows, datastore)
    ds_type = str(p.get("summary.type", ""))
    match = _VMFS_UUID.search(str(p.get("summary.url", "")))
    info = {"datastore": datastore, "type": ds_type, "vmfs_uuid": match.group(1) if match else None}
    return [m.key for m in (p.get("host") or [])], info


def resolve_hosts(
    si: ServiceInstance,
    cluster: str | None = None,
    host: str | None = None,
    datastore: str | None = None,
    allow_whole_target: bool = False,
) -> tuple[list, dict]:
    """Return ``(host objects, scope description)`` for exactly one scope."""
    given = [k for k, v in (("cluster", cluster), ("host", host), ("datastore", datastore)) if v]
    if len(given) > 1:
        raise StoragePathError(
            f"Give only one of cluster, host or datastore (got {', '.join(given)}). "
            "To look at one host inside a cluster, pass just host."
        )
    if cluster:
        rows = _collect(si, [vim.ClusterComputeResource], ["name", "host"])
        _obj, p = _pick_one("Cluster", rows, cluster)
        return list(p.get("host") or []), {"cluster": cluster}
    if host:
        obj, _p = _pick_one("Host", _collect(si, [vim.HostSystem], ["name"]), host)
        return [obj], {"host": host}
    if datastore:
        hosts, info = _datastore_scope(si, datastore)
        return hosts, info
    if allow_whole_target:
        return [obj for obj, _p in _collect(si, [vim.HostSystem], ["name"])], {"whole_target": True}
    raise StoragePathError(
        "Scope required: pass cluster, host or datastore. Multipath data is "
        "per-host and large, so this tool does not read every host on a target "
        "at once. Cluster names come from vmware-monitor list_all_clusters."
    )


def check_paging(limit: int, offset: int) -> None:
    """Validate paging before any host is read."""
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}, got {limit}.")
    if offset < 0:
        raise ValueError(f"offset must be 0 or greater, got {offset}.")


def _paginate(rows: list[dict], limit: int, offset: int, **extra) -> dict:
    """The family envelope, with truncation judged from the offset, not the page."""
    check_paging(limit, offset)
    page = rows[offset : offset + limit]
    nxt = offset + limit if offset + limit < len(rows) else None
    env = paginated(page, limit=limit, total=len(rows), offset=offset, next_offset=nxt, **extra)
    if nxt is not None:
        hint = f"More results: call again with offset={nxt}."
    elif offset and offset >= len(rows):
        hint = f"offset {offset} is past the end ({len(rows)} results); start again at offset 0."
    else:
        hint = None
    return {**env, "truncated": nxt is not None, "hint": hint}


def _not_read(reads: list[HostRead]) -> list[dict]:
    return [{"host": r.name, "reason": r.not_read_reason} for r in reads if r.not_read_reason]


# ─── FC adapters ─────────────────────────────────────────────────────────────


def format_wwn(value: int | None) -> str | None:
    """64-bit WWN as colon-separated hex; xsd:long arrives signed."""
    if value is None:
        return None
    raw = f"{value & 0xFFFFFFFFFFFFFFFF:016x}"
    return ":".join(raw[i : i + 2] for i in range(0, 16, 2))


def _fc_row(host: str, hba) -> dict:
    return {
        "host": host,
        "device": hba.device,
        "model": sanitize(hba.model or ""),
        "driver": hba.driver,
        "status": hba.status,
        "port_type": str(hba.portType) if hba.portType is not None else None,
        "speed_reported": hba.speed,
        "wwpn": format_wwn(hba.portWorldWideName),
        "wwnn": format_wwn(hba.nodeWorldWideName),
    }


def list_fc_adapters(
    si: ServiceInstance,
    cluster: str | None = None,
    host: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    """List Fibre Channel HBAs (FC and FCoE) per host."""
    check_paging(limit, offset)
    hosts, scope = resolve_hosts(si, cluster=cluster, host=host, allow_whole_target=True)
    reads = _read_hosts(si, hosts, [_P_HBA])
    rows: list[dict] = []
    without_fc: list[str] = []
    for r in reads:
        if r.not_read_reason:
            continue
        fc = [h for h in (r.props.get(_P_HBA) or []) if isinstance(h, vim.host.FibreChannelHba)]
        if not fc:
            without_fc.append(r.name)
        rows.extend(_fc_row(r.name, h) for h in sorted(fc, key=lambda h: h.device))
    return _paginate(
        rows,
        limit,
        offset,
        scope=scope,
        hosts_in_scope=len(reads),
        hosts_without_fc=without_fc,
        hosts_not_read=_not_read(reads),
        speed_note=(
            "speed_reported is the raw value from vSphere. The API documents it "
            "as bits per second, but hosts commonly report the link rate in "
            "Gbit/s (e.g. 16); it is not converted."
        ),
    )
