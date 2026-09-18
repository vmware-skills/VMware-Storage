"""Per-device SCSI multipath view across a set of hosts (read-only).

Answers "which hosts see this device, through which adapters", "does this
datastore have dead or disabled paths anywhere", "do some hosts see fewer
paths than their peers" and "which devices and datastores ride on this
adapter". Scoping, unread-host handling and the observation note live in
``storage_paths`` (GitHub VMware-Storage#18).
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import sanitize

from vmware_storage.ops.storage_paths import (
    _P_HBA,
    _P_LUN,
    _P_MOUNT,
    _P_MP,
    DEFAULT_LIMIT,
    OBSERVATION_NOTE,
    HostRead,
    _not_read,
    _paginate,
    _read_hosts,
    check_paging,
    format_wwn,
    resolve_hosts,
)

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

_NEEDED = [_P_HBA, _P_LUN, _P_MP, _P_MOUNT]
_ATTENTION_STATES = ("dead", "disabled")


def _target_id(transport) -> str | None:
    if isinstance(transport, vim.host.FibreChannelTargetTransport):
        return format_wwn(transport.portWorldWideName)
    if isinstance(transport, vim.host.InternetScsiTargetTransport):
        return sanitize(transport.iScsiName or "") or None
    return None


def _protocol(hba, transport) -> str | None:
    """'fc' / 'iscsi' when the path runs over a SAN, from the adapter first.

    ``Path.transport`` is optional — a dead path can come back without one — but
    ``Path.adapter`` is required, so the HBA type is the dependable signal.
    FCoE HBAs and transports subclass the FC ones.
    """
    if isinstance(hba, vim.host.FibreChannelHba) or isinstance(
        transport, vim.host.FibreChannelTargetTransport
    ):
        return "fc"
    if isinstance(hba, vim.host.InternetScsiHba) or isinstance(
        transport, vim.host.InternetScsiTargetTransport
    ):
        return "iscsi"
    return None


def _path_row(path, hbas: dict) -> dict:
    hba = hbas.get(path.adapter)
    return {
        "name": sanitize(path.name or ""),
        "adapter": hba.device if hba is not None else sanitize(str(path.adapter)),
        "protocol": _protocol(hba, path.transport),
        "state": str(path.state or path.pathState or "unknown"),
        "working": bool(path.isWorkingPath),
        "target": _target_id(path.transport),
    }


def _volumes_by_disk(mounts) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Map disk → VMFS datastore names, and VMFS uuid → extent disks."""
    by_disk: dict[str, list[str]] = {}
    by_uuid: dict[str, list[str]] = {}
    for m in mounts or []:
        vol = getattr(m, "volume", None)
        if not isinstance(vol, vim.host.VmfsVolume):
            continue
        disks = [e.diskName for e in (vol.extent or [])]
        by_uuid[vol.uuid] = disks
        for d in disks:
            by_disk.setdefault(d, []).append(sanitize(vol.name or ""))
    return by_disk, by_uuid


def _lun_meta(scsi, fallback_id: str) -> dict:
    if scsi is None:
        return {
            "device": fallback_id,
            "display_name": None,
            "vendor": None,
            "model": None,
            "lun_type": None,
            "local": None,
        }
    return {
        "device": scsi.canonicalName,
        "display_name": sanitize(scsi.displayName or ""),
        "vendor": sanitize((scsi.vendor or "").strip()),
        "model": sanitize((scsi.model or "").strip()),
        "lun_type": scsi.lunType,
        "local": getattr(scsi, "localDisk", None),
    }


def _device_key(meta: dict, host: str) -> tuple[str, bool]:
    """Key across hosts, and whether the name is host-local.

    ``mpx.*`` names are made up per host (every host's boot disk can be
    ``mpx.vmhba0:C0:T0:L0``), and non-disk LUNs are per-host devices. An
    unresolved LUN (no scsiLun entry) has ``lun_type`` None, so it is per-host
    too: its id is not comparable to canonical names. Merging any of them
    across hosts would invent a shared device.
    """
    name = str(meta["device"])
    per_host = name.startswith("mpx.") or meta["lun_type"] != "disk"
    return (f"{host}\x00{name}" if per_host else name), per_host


def _host_entry(host: str, lu, hbas: dict) -> dict:
    paths = [_path_row(p, hbas) for p in (lu.path or [])]
    policy = lu.policy.policy if lu.policy is not None else None
    satp = lu.storageArrayTypePolicy.policy if lu.storageArrayTypePolicy is not None else None
    return {
        "host": host,
        "paths_total": len(paths),
        "by_state": dict(Counter(p["state"] for p in paths)),
        "working_paths": sum(1 for p in paths if p["working"]),
        "policy": policy,
        "satp": satp,
        "adapters": sorted({p["adapter"] for p in paths}),
        "paths": paths,
    }


def _gather(reads: list[HostRead]) -> tuple[dict, dict[str, list[str]]]:
    """Merge every read host's devices; also return VMFS uuid → extent disks."""
    devices: dict[str, dict] = {}
    extents: dict[str, list[str]] = {}
    for r in reads:
        if r.not_read_reason:
            continue
        hbas = {h.key: h for h in (r.props.get(_P_HBA) or [])}
        luns = {s.key: s for s in (r.props.get(_P_LUN) or [])}
        by_disk, by_uuid = _volumes_by_disk(r.props.get(_P_MOUNT))
        extents.update(by_uuid)
        for lu in r.props[_P_MP].lun or []:
            scsi = luns.get(lu.lun)
            meta = _lun_meta(scsi, sanitize(lu.id or lu.key))
            key, per_host = _device_key(meta, r.name)
            dev = devices.setdefault(
                key, {**meta, "per_host": per_host, "hosts": [], "datastores": set()}
            )
            dev["hosts"].append(_host_entry(r.name, lu, hbas))
            dev["datastores"].update(by_disk.get(meta["device"], []))
    return devices, extents


def _finish(dev: dict, hosts_read: list[str], adapter: str | None, detail: bool) -> dict:
    seen = [h["host"] for h in dev["hosts"]]
    counts = {h["host"]: h["paths_total"] for h in dev["hosts"]}
    hosts = []
    for h in dev["hosts"]:
        row = dict(h)
        if adapter:
            via = sum(1 for p in h["paths"] if p["adapter"] == adapter)
            sole = via > 0 and via == h["paths_total"]
            row = {**row, "paths_via_adapter": via, "only_paths_via_adapter": sole}
        if not detail:
            row = {k: v for k, v in row.items() if k != "paths"}
        hosts.append(row)
    shared = _is_shared(dev, len(seen))
    not_seen = [h for h in hosts_read if h not in seen] if shared else []
    attention = sorted({s for h in dev["hosts"] for s in h["by_state"] if s in _ATTENTION_STATES})
    return {
        **{k: v for k, v in dev.items() if k not in ("hosts", "datastores", "per_host")},
        "datastores": sorted(dev["datastores"]),
        "shared": shared,
        "seen_by": len(seen),
        "not_seen_on": not_seen,
        "path_count_differs": len(set(counts.values())) > 1,
        "states_needing_attention": attention,
        "hosts": hosts,
    }


def _is_shared(dev: dict, seen_by: int) -> bool:
    """Whether other hosts could be expected to see this device.

    Only then does "not seen on host X" mean anything. A disk inside one host —
    local, or a SAS/NVMe drive ESXi marks non-local — is seen by that host alone
    by design. Shared means: reached over FC or iSCSI (by adapter type, so a
    path without a transport still counts), or already seen by more than one
    host. A local-marked device is exempt even on a SAN: that is how a
    boot-from-SAN LUN, zoned to one host on purpose, is usually marked.
    """
    if dev["per_host"] or dev["local"]:
        return False
    san = any(p["protocol"] for h in dev["hosts"] for p in h["paths"])
    return san or seen_by > 1


def _differs(d: dict) -> bool:
    return bool(d["not_seen_on"]) or d["path_count_differs"]


def _matches(dev: dict, device: str | None, adapter: str | None, keep: set | None) -> bool:
    if keep is not None and dev["device"] not in keep:
        return False
    names = (str(dev["device"]).lower(), str(dev["display_name"] or "").lower())
    if device and device.lower() not in names:
        return False
    if adapter and not any(adapter in h["adapters"] for h in dev["hosts"]):
        return False
    return True


def _summary(rows: list[dict]) -> dict:
    return {
        "devices": len(rows),
        "with_dead_paths": sum(1 for d in rows if "dead" in d["states_needing_attention"]),
        "with_disabled_paths": sum(1 for d in rows if "disabled" in d["states_needing_attention"]),
        "with_visibility_or_path_count_differences": sum(1 for d in rows if _differs(d)),
    }


def _datastore_extents(scope: dict, extents: dict) -> tuple[set | None, str | None]:
    if scope.get("type", "").upper() != "VMFS":
        return set(), (
            f"Datastore '{scope['datastore']}' is {scope.get('type') or 'not VMFS'}; "
            "only VMFS datastores are backed by SCSI devices with multipath state."
        )
    disks = extents.get(scope.get("vmfs_uuid") or "")
    if disks is None:
        return set(), (
            "No host that could be read reports this VMFS volume mounted, so its "
            "backing devices are unknown — this is not the same as having none."
        )
    return set(disks), None


def device_paths(
    si: ServiceInstance,
    cluster: str | None = None,
    host: str | None = None,
    datastore: str | None = None,
    device: str | None = None,
    adapter: str | None = None,
    only_differences: bool = False,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    """Per-device multipath state across the hosts in one scope."""
    check_paging(limit, offset)
    hosts, scope = resolve_hosts(si, cluster=cluster, host=host, datastore=datastore)
    reads = _read_hosts(si, hosts, _NEEDED)
    hosts_read = [r.name for r in reads if not r.not_read_reason]
    devices, extents = _gather(reads)
    keep, note = _datastore_extents(scope, extents) if datastore else (None, None)
    detail = bool(device or datastore)
    rows = [
        _finish(d, hosts_read, adapter, detail)
        for d in devices.values()
        if _matches(d, device, adapter, keep)
    ]
    if only_differences:
        rows = [d for d in rows if _differs(d)]
    rows.sort(key=lambda d: (not d["states_needing_attention"], not _differs(d), str(d["device"])))
    return _paginate(
        rows,
        limit,
        offset,
        scope=scope,
        hosts_read=len(hosts_read),
        hosts_not_read=_not_read(reads),
        complete=not _not_read(reads) and note is None,
        summary=_summary(rows),
        scope_note=note,
        note=OBSERVATION_NOTE,
    )
