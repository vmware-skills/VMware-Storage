"""The confirmation gate in front of the iSCSI and rescan write tools (HLD §7, revised 2026-09-16).

The executors in ``iscsi_config`` stay what the CLI calls. This module is what
the four MCP tools put in front of them:

* ``decide`` turns ``confirm`` plus the deprecated ``dry_run`` into one
  decision — the conservative reading wins (L2);
* ``measure_*`` reads what the call would change from the host's own storage
  view (``config.storageDevice`` and ``config.fileSystemVolume``, the same
  data the multipath tools read) and lists every part it could not read in
  ``unmeasured`` instead of guessing (L1);
* ``refuse_if_blocked`` raises a teaching error on a blocker or an unmeasured
  field, on the acting path only (L3).

No new vSphere call: every value comes from properties this skill already
reads. The executors re-find the host and re-check their own preconditions.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyVmomi import vim
from vmware_policy import sanitize

from vmware_storage.ops import iscsi_config as _iscsi
from vmware_storage.ops.iscsi_config import ISCSIError

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

#: Identifiers listed individually in a blast radius; the counts cover the rest.
MAX_LISTED = 16

_SEND_TARGET_METHOD = "sendTargetMethod"
_DEFAULT_PORT = 3260
_HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")

#: One iSCSI portal, normalised: ``(host, port)``.
Portal = tuple[str, int]


class GateRefusedError(ISCSIError):
    """``confirm=True`` was refused: a blocker, or a blast radius not measured."""


@dataclass(frozen=True)
class Decision:
    """Whether the call acts, and the deprecation note if the alias was used."""

    act: bool
    deprecated: str | None


def decide(confirm: bool, dry_run: bool | None) -> Decision:
    """Act iff ``confirm`` or an explicit ``dry_run=False``, unless ``dry_run=True``.

    The old contract acted on ``dry_run=False`` — and, because that was the
    default, on a bare call. A bare call now previews.
    """
    if dry_run is None:
        return Decision(act=confirm is True, deprecated=None)
    return Decision(
        act=dry_run is False,
        deprecated="dry_run is deprecated; use confirm. Removed in the next minor release.",
    )


def refuse_if_blocked(tool: str, host_name: str, radius: dict[str, Any]) -> None:
    """Raise before any write when the blast radius has a blocker or a gap.

    The remedy comes first: the MCP layer caps the message at 300 characters.
    """
    if radius["blockers"]:
        raise GateRefusedError(f"{tool} refused on '{host_name}': {radius['blockers'][0]}")
    if radius["unmeasured"]:
        raise GateRefusedError(
            f"{tool} refused on '{host_name}': could not read "
            f"{', '.join(radius['unmeasured'])}, so what it changes is unknown. Reconnect "
            "the host or run 'vmware-storage doctor', then preview again. Nothing was changed."
        )


# ---------------------------------------------------------------------------
# Reading the host
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _HostView:
    """One host's storage view, or the fields that could not be read."""

    name: str
    host_id: str | None
    hbas: list
    storage_device: Any
    mounts: list
    unmeasured: list[str]
    blockers: list[str]


def _read_host(si: ServiceInstance, host_name: str) -> tuple[Any, _HostView]:
    """Find the host (a teaching error if absent) and read its storage view."""
    host = _iscsi._require_host(si, host_name)
    unmeasured: list[str] = []
    blockers: list[str] = []
    if str(getattr(host.runtime, "connectionState", "") or "") != "connected":
        unmeasured.append("host_connection")
    try:
        _iscsi._get_storage_system(host)
    except ISCSIError as exc:
        blockers.append(str(exc))
    storage_device, mounts, hbas = None, [], []
    try:
        config = _iscsi._require_host_config(host)
        storage_device = config.storageDevice
        hbas = list(storage_device.hostBusAdapter or [])
        mounts = list(config.fileSystemVolume.mountInfo or [])
    except (ISCSIError, AttributeError, TypeError):
        unmeasured.append("host_storage_config")
    view = _HostView(
        name=host_name,
        host_id=getattr(host, "_moId", None),
        hbas=hbas,
        storage_device=storage_device,
        mounts=mounts,
        unmeasured=unmeasured,
        blockers=blockers,
    )
    return host, view


def _software_hba(view: _HostView):
    for hba in view.hbas:
        if isinstance(hba, vim.host.InternetScsiHba) and hba.isSoftwareBased:
            return hba
    return None


def _adapter(hba) -> dict | None:
    if hba is None:
        return None
    return {"device": hba.device, "iqn": sanitize(hba.iScsiName or "") or None}


def _base(view: _HostView, **fields: Any) -> dict[str, Any]:
    return {
        "host": view.name,
        "host_id": view.host_id,
        **fields,
        "blockers": list(view.blockers),
        "unmeasured": list(view.unmeasured),
    }


def _endpoint(address: str, port: int) -> str:
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def _norm_host(text: str) -> str | None:
    """One canonical spelling of an IP literal or host name; None if it is neither.

    IPv6 case and zero groups, IPv4 leading zeros and letter case all collapse.
    """
    h = text.strip().lower()
    if not h:
        return None
    try:
        return ipaddress.ip_address(h).compressed
    except ValueError:
        pass
    parts = h.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        nums = [int(p) for p in parts]
        return ".".join(map(str, nums)) if all(n <= 255 for n in nums) else None
    return h if _HOSTNAME.fullmatch(h) else None


def _portal(address: str, port: int) -> Portal | None:
    """A portal from a data object's separate ``address`` and ``port`` fields."""
    host = _norm_host(str(address or "").strip("[] "))
    return (host, int(port)) if host is not None and port else None


def _portals(text: str | None) -> set[Portal]:
    """Every portal one ``address[:port]`` string may denote; empty when unparseable.

    vSphere writes portals as ``a.b.c.d:port``, ``[v6]:port``, and — seen in
    the field — ``v6:port`` unbracketed or without a port at all. An
    unbracketed IPv6 string is ambiguous (is the last group the port?), so
    both readings are returned: matching either one counts, which errs toward
    "this path is lost" — the side that blocks.
    """
    s = (text or "").strip()
    found: set[Portal] = set()

    def _add(host_text: str, port_text: str | None) -> None:
        host = _norm_host(host_text)
        if port_text is not None and not port_text.isdigit():
            return
        port = int(port_text) if port_text else _DEFAULT_PORT
        if host is not None and 0 < port < 65536:
            found.add((host, port))

    if s.startswith("["):
        m = re.fullmatch(r"\[([^\]]+)\](?::(\d+))?", s)
        if m:
            _add(m.group(1), m.group(2))
    elif s.count(":") == 1:
        _add(*s.split(":"))
    elif s.count(":") > 1:
        _add(s, None)
        _add(*s.rsplit(":", 1))
    else:
        _add(s, None)
    return found


def _not_enabled(host_name: str) -> str:
    return (
        f"software iSCSI is not enabled on host '{host_name}'. Run storage_iscsi_enable "
        "first, then preview again."
    )


# ---------------------------------------------------------------------------
# storage_iscsi_enable
# ---------------------------------------------------------------------------


def measure_enable(si: ServiceInstance, host_name: str) -> dict[str, Any]:
    """Whether the software adapter is already there, and the host's adapters."""
    _host, view = _read_host(si, host_name)
    hba = _software_hba(view)
    readable = _readable(view)
    return _base(
        view,
        change="enable the software iSCSI adapter",
        already_enabled=(hba is not None) if readable else None,
        adapter=_adapter(hba),
        adapters=sorted(h.device for h in view.hbas) if readable else None,
    )


# ---------------------------------------------------------------------------
# storage_iscsi_add_target
# ---------------------------------------------------------------------------


def _readable(view: _HostView) -> bool:
    """False when the storage view was not read: counts are then None, not 0."""
    return "host_storage_config" not in view.unmeasured


def _rescan_scope(view: _HostView) -> dict[str, Any]:
    return {
        "adapters": sorted(h.device for h in view.hbas) if _readable(view) else None,
        "vmfs": True,
    }


def measure_add_target(
    si: ServiceInstance, host_name: str, address: str, port: int
) -> dict[str, Any]:
    """The send target to add, what is configured now, and the rescan it triggers."""
    _iscsi._validate_address(address)
    _iscsi._validate_port(port)
    _host, view = _read_host(si, host_name)
    hba = _software_hba(view)
    send = list(hba.configuredSendTarget or []) if hba is not None else []
    readable = _readable(view)
    wanted = _portal(address, port)
    radius = _base(
        view,
        target=_endpoint(address, port),
        adapter=_adapter(hba),
        send_target_count=len(send) if readable else None,
        send_targets=[_endpoint(t.address, t.port) for t in send][:MAX_LISTED]
        if readable else None,
        already_configured=any(_portal(t.address, t.port) == wanted for t in send)
        if readable else None,
        rescan=_rescan_scope(view),
    )
    if hba is None and readable:
        radius["blockers"].append(_not_enabled(host_name))
    return radius


# ---------------------------------------------------------------------------
# storage_iscsi_remove_target
# ---------------------------------------------------------------------------


def _static_pairs(hba) -> set[tuple[str, Portal]]:
    """Every ``(iqn, portal)`` the adapter has a static target for, however discovered."""
    pairs = set()
    for st in hba.configuredStaticTarget or []:
        portal = _portal(st.address, st.port)
        if portal is not None:
            pairs.add((st.iScsiName, portal))
    return pairs


def _lost_static_targets(hba, address: str, port: int) -> tuple[set, bool]:
    """``(iqn, portal)`` pairs only this send target discovered; and whether readable.

    A static target discovered through send targets names its discovering
    portal in ``parent``. One whose parent is absent or matches no configured
    send target cannot be attributed, so the answer is "unknown", not "none".
    A portal also discovered through another send target is kept. Portals are
    compared normalised, never as strings.
    """
    removed = _portal(address, port)
    known = {_portal(t.address, t.port) for t in hba.configuredSendTarget or []}
    lost, kept, readable = set(), set(), True
    for st in hba.configuredStaticTarget or []:
        if st.discoveryMethod != _SEND_TARGET_METHOD:
            continue
        parents = _portals(st.parent)
        portal = _portal(st.address, st.port)
        if not parents & known or portal is None:
            readable = False
            continue
        (lost if removed in parents else kept).add((st.iScsiName, portal))
    return lost - kept, readable


def _path_is_lost(path, lost: set, static: set) -> bool | None:
    """Whether a path with an iSCSI transport rides on a lost static target.

    None — fail closed — when the path names a removed target's IQN but its
    address is unparseable or matches no static target of that IQN: it may
    be the lost portal spelled a way this code does not know.
    """
    iqn = path.transport.iScsiName
    if iqn not in {i for i, _ in lost}:
        return False
    pairs = {(iqn, p) for a in path.transport.address for p in _portals(a)}
    if pairs & lost:
        return True
    return False if pairs & static else None


def _vmfs_by_disk(mounts: list) -> dict[str, list[str]]:
    by_disk: dict[str, list[str]] = {}
    for m in mounts:
        vol = getattr(m, "volume", None)
        if isinstance(vol, vim.host.VmfsVolume):
            for e in vol.extent or []:
                by_disk.setdefault(e.diskName, []).append(sanitize(vol.name or ""))
    return by_disk


def _dependents(view: _HostView, hba, lost: set) -> tuple[dict, bool]:
    """Paths, devices and datastores that ride on the lost static targets."""
    names = {lun.key: lun.canonicalName for lun in view.storage_device.scsiLun or []}
    static = _static_pairs(hba)
    lost_paths, all_lost, some_lost, unattributed, unmatched = [], [], [], 0, 0
    for lu in view.storage_device.multipathInfo.lun or []:
        verdicts = []
        for p in lu.path or []:
            if p.adapter != hba.key:
                verdict = False
            elif not isinstance(p.transport, vim.host.InternetScsiTargetTransport) or not (
                p.transport.address
            ):
                unattributed += 1
                verdict = None
            else:
                verdict = _path_is_lost(p, lost, static)
                if verdict is None:
                    unmatched += 1
            if verdict:
                lost_paths.append(sanitize(p.name or ""))
            verdicts.append(bool(verdict))
        device = names.get(lu.lun, sanitize(lu.id or ""))
        if verdicts and all(verdicts):
            all_lost.append(device)
        elif any(verdicts):
            some_lost.append(device)
    vmfs = _vmfs_by_disk(view.mounts)
    ds_all = sorted({d for dev in all_lost for d in vmfs.get(dev, [])})
    ds_some = sorted({d for dev in some_lost for d in vmfs.get(dev, [])} - set(ds_all))
    deps = {
        "paths_lost_count": len(lost_paths),
        "paths_lost": sorted(lost_paths)[:MAX_LISTED],
        "devices_losing_all_paths": sorted(all_lost)[:MAX_LISTED],
        "devices_losing_some_paths": sorted(some_lost)[:MAX_LISTED],
        "datastores_losing_all_paths": ds_all[:MAX_LISTED],
        "datastores_losing_some_paths": ds_some[:MAX_LISTED],
        "paths_without_transport": unattributed,
        "paths_unmatched_address": unmatched,
    }
    return deps, unattributed == 0 or not lost


def measure_remove_target(
    si: ServiceInstance, host_name: str, address: str, port: int
) -> dict[str, Any]:
    """The send target to remove and the paths, devices and datastores behind it."""
    _iscsi._validate_address(address)
    _iscsi._validate_port(port)
    _host, view = _read_host(si, host_name)
    hba = _software_hba(view)
    radius = _base(view, target=_endpoint(address, port), adapter=_adapter(hba))
    if not _readable(view):
        return radius
    if hba is None:
        radius["blockers"].append(_not_enabled(host_name))
        return radius
    wanted = _portal(address, port)
    if not any(_portal(t.address, t.port) == wanted for t in hba.configuredSendTarget or []):
        radius["blockers"].append(
            f"send target {_endpoint(address, port)} is not configured. Run "
            "storage_iscsi_status for the exact address:port pairs."
        )
        return radius
    lost, parents_readable = _lost_static_targets(hba, address, port)
    if not parents_readable:
        radius["unmeasured"].append("static_target_parent")
    try:
        deps, attributed = _dependents(view, hba, lost)
    except (AttributeError, TypeError):
        radius["unmeasured"].append("multipath_info")
        return radius
    if not attributed:
        radius["unmeasured"].append("paths_without_transport")
    if deps["paths_unmatched_address"]:
        radius["unmeasured"].append("path_address")
    radius["static_targets_removed"] = [
        {"iqn": iqn, "address": _endpoint(*portal)} for iqn, portal in sorted(lost)
    ][:MAX_LISTED]
    radius.update(deps)
    if deps["datastores_losing_all_paths"]:
        radius["blockers"].append(
            "Unmount these datastores first, they would lose every path: "
            + ", ".join(deps["datastores_losing_all_paths"][:5]) + "."
        )
    return radius


# ---------------------------------------------------------------------------
# storage_rescan
# ---------------------------------------------------------------------------


def measure_rescan(si: ServiceInstance, host_name: str) -> dict[str, Any]:
    """The rescan's scope: this one host, every adapter on it, and its VMFS volumes."""
    _host, view = _read_host(si, host_name)
    if not _readable(view):
        return _base(view, hosts=[host_name], adapters=None, adapter_count=None,
                     vmfs_rescan=True, mounted_vmfs=None)
    adapters = sorted(h.device for h in view.hbas)
    return _base(
        view,
        hosts=[host_name],
        adapters=adapters,
        adapter_count=len(adapters),
        vmfs_rescan=True,
        mounted_vmfs=sorted({d for ds in _vmfs_by_disk(view.mounts).values() for d in ds}),
    )
