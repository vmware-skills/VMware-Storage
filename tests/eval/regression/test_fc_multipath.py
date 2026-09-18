"""FC adapter inventory and multipath diagnostics (GitHub VMware-Storage#18).

Built from real pyVmomi data objects and a real ``RetrieveResult`` — never
strings standing in for SDK types. ``Path.adapter`` and ``Path.lun`` are key
strings (``Link``) and WWNs are signed 64-bit longs; a fixture that got those
wrong would pass here and fail on a customer's array.

The invariant that matters most: **a host that could not be read is never
reported as not seeing a device.** On a 300-host estate an unread host is
routine, and turning it into "missing the LUN" sends someone chasing zoning
that is fine.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyVmomi import vim, vmodl

from vmware_storage.ops import multipath, storage_paths
from vmware_storage.ops.storage_paths import StoragePathError, format_wwn

PC = vmodl.query.PropertyCollector
MP = vim.host.MultipathInfo

WWPN_HIGH_BIT = -0x3FFF_FFFF_FFFF_FFF0  # a WWN with the top bit set arrives negative


# ─── Fixtures: real vim data objects ────────────────────────────────────────


def _fc_hba(device: str, wwpn: int = 0x2100_0024_FF12_3456) -> vim.host.FibreChannelHba:
    return vim.host.FibreChannelHba(
        key=f"key-vim.host.FibreChannelHba-{device}",
        device=device,
        model="QLE2692",
        driver="qlnativefc",
        status="online",
        bus=0,
        pci="0000:3b:00.0",
        portWorldWideName=wwpn,
        nodeWorldWideName=0x2000_0024_FF12_3456,
        portType=vim.host.FibreChannelHba.PortType.fabric,
        speed=16,
    )


def _block_hba(device: str = "vmhba0") -> vim.host.BlockHba:
    return vim.host.BlockHba(
        key=f"key-vim.host.BlockHba-{device}",
        device=device,
        model="AHCI",
        driver="vmw_ahci",
        status="unknown",
        bus=0,
    )


def _disk(canonical: str, local: bool = False) -> vim.host.ScsiDisk:
    return vim.host.ScsiDisk(
        key=f"key-{canonical}",
        uuid=f"uuid-{canonical}",
        canonicalName=canonical,
        displayName=f"HITACHI {canonical}",
        vendor="HITACHI ",
        model="OPEN-V          ",
        lunType="disk",
        localDisk=local,
    )


def _cdrom(canonical: str) -> vim.host.ScsiLun:
    return vim.host.ScsiLun(
        key=f"key-{canonical}",
        uuid=f"uuid-{canonical}",
        canonicalName=canonical,
        displayName="Local CD-ROM",
        vendor="NECVMWar",
        model="VMware SATA CD00",
        lunType="cdrom",
    )


def _fc_target() -> vim.host.FibreChannelTargetTransport:
    return vim.host.FibreChannelTargetTransport(
        portWorldWideName=0x5006_0E80_1234_5601, nodeWorldWideName=0x5006_0E80_1234_5600
    )


_FC = object()  # default: an FC target transport; pass None for a path without one


def _path(
    name: str, hba, disk, state: str = "active", working: bool = True, transport=_FC
) -> MP.Path:
    return MP.Path(
        key=f"key-{name}",
        name=name,
        pathState=state,
        state=state,
        isWorkingPath=working,
        adapter=hba.key,
        lun=f"lu-{disk.canonicalName}",
        transport=_fc_target() if transport is _FC else transport,
    )


def _lu(disk, paths) -> MP.LogicalUnit:
    return MP.LogicalUnit(
        key=f"lu-{disk.canonicalName}",
        id=disk.uuid,
        lun=disk.key,
        path=paths,
        policy=MP.LogicalUnitPolicy(policy="VMW_PSP_RR"),
        storageArrayTypePolicy=MP.LogicalUnitStorageArrayTypePolicy(policy="VMW_SATP_DEFAULT_AA"),
    )


def _vmfs_mount(name: str, uuid: str, disks: list[str]) -> vim.host.FileSystemMountInfo:
    vol = vim.host.VmfsVolume(
        name=name,
        uuid=uuid,
        type="VMFS",
        capacity=1,
        extent=[vim.host.ScsiDisk.Partition(diskName=d, partition=1) for d in disks],
    )
    return vim.host.FileSystemMountInfo(volume=vol)


def _host_props(name, hbas, disks, lus, mounts=()) -> dict:
    return {
        "name": name,
        "runtime.connectionState": "connected",
        storage_paths._P_HBA: hbas,
        storage_paths._P_LUN: disks,
        storage_paths._P_MP: MP(lun=lus),
        storage_paths._P_MOUNT: list(mounts),
    }


class _FakePC:
    """Answers RetrievePropertiesEx like vCenter: canned props plus a missingSet."""

    def __init__(self, by_moid: dict[str, tuple[dict, dict]], page: int = 1000, drop=()):
        self.by_moid, self.page, self.drop = by_moid, page, set(drop)
        self.calls = 0
        self._rest: list = []

    def _batch(self, objects: list):
        head, self._rest = objects[: self.page], objects[self.page :]
        return SimpleNamespace(objects=head, token="more" if self._rest else None)

    def ContinueRetrievePropertiesEx(self, token):  # noqa: N802
        assert token == "more"
        return self._batch(self._rest)

    def RetrievePropertiesEx(self, specs, options):  # noqa: N802 — pyVmomi name
        self.calls += 1
        wanted = specs[0].propSet[0].pathSet
        objects = []
        for spec in specs[0].objectSet:
            if spec.obj._moId in self.drop:
                continue
            props, faults = self.by_moid[spec.obj._moId]
            # The envelope is plain objects: pyVmomi's DynamicProperty rejects an
            # untyped list, and the server's typed arrays iterate the same way.
            # Everything inside it — the data objects and the faults — is real.
            objects.append(
                SimpleNamespace(
                    obj=spec.obj,
                    propSet=[
                        SimpleNamespace(name=k, val=v) for k, v in props.items() if k in wanted
                    ],
                    missingSet=[PC.MissingProperty(path=p, fault=f) for p, f in faults.items()],
                )
            )
        return self._batch(objects)


class _FakeSI:
    def __init__(self, pc):
        self._content = type("C", (), {"propertyCollector": pc})()

    def RetrieveContent(self):  # noqa: N802
        return self._content


def _env(monkeypatch, hosts: dict[str, tuple[dict, dict]], clusters=None, datastores=None, pc=None):
    """hosts: moid → (props, missing faults). Wires _collect for scope lookups."""
    objs = {moid: vim.HostSystem(moid) for moid in hosts}
    rows = {
        vim.HostSystem: [(objs[m], {"name": p.get("name", m)}) for m, (p, _f) in hosts.items()],
        vim.ClusterComputeResource: [
            (
                vim.ClusterComputeResource(f"domain-{i}"),
                {"name": n.split("#")[0], "host": [objs[m] for m in ms]},
            )
            for i, (n, ms) in enumerate((clusters or {}).items())
        ],
        vim.Datastore: [
            (
                vim.Datastore(n),
                {
                    "name": n,
                    "summary.type": t,
                    "summary.url": f"ds:///vmfs/volumes/{u}/",
                    "host": [vim.Datastore.HostMount(key=objs[m]) for m in ms],
                },
            )
            for n, (t, u, ms) in (datastores or {}).items()
        ],
    }
    monkeypatch.setattr(storage_paths, "_collect", lambda si, t, paths: rows[t[0]])
    return _FakeSI(pc or _FakePC(hosts))


def _estate(monkeypatch):
    """esx-a and esx-b are read; esx-c is refused (NoPermission)."""
    a0, a1, b1 = _fc_hba("vmhba2"), _fc_hba("vmhba3"), _fc_hba("vmhba2")
    shared, only_a = _disk("naa.600a"), _disk("naa.600b")
    boot = _disk("mpx.vmhba0:C0:T0:L0", local=True)
    host_a = _host_props(
        "esx-a",
        [_block_hba(), a0, a1],
        [shared, only_a, boot],
        [
            _lu(
                shared, [_path("vmhba2:C0:T0:L1", a0, shared), _path("vmhba3:C0:T0:L1", a1, shared)]
            ),
            _lu(only_a, [_path("vmhba2:C0:T0:L2", a0, only_a)]),
            _lu(boot, [_path("vmhba0:C0:T0:L0", _block_hba(), boot)]),
        ],
        [_vmfs_mount("ds-fc-01", "u-fc-01", ["naa.600a"])],
    )
    host_b = _host_props(
        "esx-b",
        [b1],
        [shared],
        [
            _lu(
                shared,
                [
                    _path("vmhba2:C0:T0:L1", b1, shared),
                    _path("vmhba2:C0:T1:L1", b1, shared, state="dead", working=False),
                ],
            ),
        ],
        [_vmfs_mount("ds-fc-01", "u-fc-01", ["naa.600a"])],
    )
    refused = {"name": "esx-c", "runtime.connectionState": "connected"}
    faults = {p: vim.fault.NoPermission() for p in multipath._NEEDED}
    return _env(
        monkeypatch,
        {"host-1": (host_a, {}), "host-2": (host_b, {}), "host-3": (refused, faults)},
        clusters={"prod": ["host-1", "host-2", "host-3"]},
        datastores={
            "ds-fc-01": ("VMFS", "u-fc-01", ["host-1", "host-2"]),
            "nfs-01": ("NFS", "x", ["host-1"]),
        },
    )


# ─── WWN ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_wwn_is_colon_hex_and_survives_the_sign_bit():
    assert format_wwn(0x2100_0024_FF12_3456) == "21:00:00:24:ff:12:34:56"
    signed = format_wwn(WWPN_HIGH_BIT)
    assert signed == format_wwn(WWPN_HIGH_BIT & 0xFFFF_FFFF_FFFF_FFFF)
    assert not signed.startswith("-") and len(signed) == 23
    assert format_wwn(None) is None


# ─── fc_adapter_list ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_fc_adapters_listed_with_wwns_and_raw_speed(monkeypatch):
    si = _estate(monkeypatch)
    out = storage_paths.list_fc_adapters(si, cluster="prod")
    devices = [(r["host"], r["device"]) for r in out["items"]]
    assert devices == [("esx-a", "vmhba2"), ("esx-a", "vmhba3"), ("esx-b", "vmhba2")]
    row = out["items"][0]
    assert row["wwpn"] == "21:00:00:24:ff:12:34:56"
    assert row["port_type"] == "fabric"
    assert row["speed_reported"] == 16, "the raw value, not converted"
    assert out["hosts_without_fc"] == [], "both read hosts have FC HBAs"
    assert out["truncated"] is False


@pytest.mark.unit
def test_refused_host_is_not_read_and_not_a_host_without_fc(monkeypatch):
    si = _estate(monkeypatch)
    out = storage_paths.list_fc_adapters(si, cluster="prod")
    assert [h["host"] for h in out["hosts_not_read"]] == ["esx-c"]
    assert "NoPermission" in out["hosts_not_read"][0]["reason"]
    assert "esx-c" not in out["hosts_without_fc"]


@pytest.mark.unit
def test_host_with_only_local_adapters_is_without_fc(monkeypatch):
    props = _host_props("esx-local", [_block_hba()], [], [])
    si = _env(monkeypatch, {"host-9": (props, {})})
    out = storage_paths.list_fc_adapters(si)
    assert out["items"] == [] and out["hosts_without_fc"] == ["esx-local"]
    assert out["hosts_not_read"] == []


@pytest.mark.unit
def test_disconnected_host_without_faults_is_still_not_read(monkeypatch):
    gone = {"name": "esx-gone", "runtime.connectionState": "notResponding"}
    si = _env(monkeypatch, {"host-9": (gone, {})})
    out = storage_paths.list_fc_adapters(si)
    assert out["hosts_without_fc"] == []
    assert "notResponding" in out["hosts_not_read"][0]["reason"]


# ─── storage_device_paths ────────────────────────────────────────────────────


@pytest.mark.unit
def test_unread_host_is_never_counted_as_not_seeing_a_device(monkeypatch):
    si = _estate(monkeypatch)
    out = multipath.device_paths(si, cluster="prod")
    for dev in out["items"]:
        assert "esx-c" not in dev["not_seen_on"], dev["device"]
    assert out["complete"] is False and out["hosts_read"] == 2


@pytest.mark.unit
def test_visibility_and_path_count_differences(monkeypatch):
    si = _estate(monkeypatch)
    by = {d["device"]: d for d in multipath.device_paths(si, cluster="prod")["items"]}
    assert by["naa.600b"]["not_seen_on"] == ["esx-b"]
    assert by["naa.600a"]["not_seen_on"] == [] and by["naa.600a"]["path_count_differs"] is False
    boot = by["mpx.vmhba0:C0:T0:L0"]
    assert boot["not_seen_on"] == [], "a local boot disk is seen by one host by design"


@pytest.mark.unit
def test_dead_path_is_surfaced_first_and_counted(monkeypatch):
    si = _estate(monkeypatch)
    out = multipath.device_paths(si, cluster="prod")
    first = out["items"][0]
    assert first["device"] == "naa.600a" and first["states_needing_attention"] == ["dead"]
    b = next(h for h in first["hosts"] if h["host"] == "esx-b")
    assert b["by_state"] == {"active": 1, "dead": 1} and b["working_paths"] == 1
    assert out["summary"]["with_dead_paths"] == 1
    assert "paths" not in b, "path detail only when a device or datastore is named"


@pytest.mark.unit
def test_standby_is_reported_but_not_flagged(monkeypatch):
    hba, disk = _fc_hba("vmhba2"), _disk("naa.700")
    props = _host_props(
        "esx-a",
        [hba],
        [disk],
        [
            _lu(
                disk,
                [_path("p1", hba, disk), _path("p2", hba, disk, state="standby", working=False)],
            )
        ],
    )
    si = _env(monkeypatch, {"host-1": (props, {})})
    dev = multipath.device_paths(si, host="esx-a")["items"][0]
    assert dev["hosts"][0]["by_state"] == {"active": 1, "standby": 1}
    assert dev["states_needing_attention"] == []


@pytest.mark.unit
def test_device_filter_names_hosts_and_adapters_with_detail(monkeypatch):
    si = _estate(monkeypatch)
    out = multipath.device_paths(si, cluster="prod", device="NAA.600A")
    (dev,) = out["items"]
    assert dev["seen_by"] == 2 and dev["datastores"] == ["ds-fc-01"]
    a = next(h for h in dev["hosts"] if h["host"] == "esx-a")
    assert a["adapters"] == ["vmhba2", "vmhba3"] and a["policy"] == "VMW_PSP_RR"
    assert a["paths"][0]["target"] == "50:06:0e:80:12:34:56:01"


@pytest.mark.unit
def test_adapter_filter_marks_sole_dependency(monkeypatch):
    si = _estate(monkeypatch)
    out = multipath.device_paths(si, host="esx-a", adapter="vmhba2")
    by = {d["device"]: d for d in out["items"]}
    assert set(by) == {"naa.600a", "naa.600b"}
    assert by["naa.600b"]["hosts"][0]["only_paths_via_adapter"] is True
    assert by["naa.600a"]["hosts"][0]["only_paths_via_adapter"] is False


@pytest.mark.unit
def test_only_differences(monkeypatch):
    si = _estate(monkeypatch)
    out = multipath.device_paths(si, cluster="prod", only_differences=True)
    assert [d["device"] for d in out["items"]] == ["naa.600b"]


@pytest.mark.unit
def test_datastore_scope_narrows_to_its_extents(monkeypatch):
    si = _estate(monkeypatch)
    out = multipath.device_paths(si, datastore="ds-fc-01")
    assert [d["device"] for d in out["items"]] == ["naa.600a"]
    assert "paths" in out["items"][0]["hosts"][0]
    assert out["scope_note"] is None and out["complete"] is True


@pytest.mark.unit
def test_non_vmfs_datastore_says_why_it_is_empty(monkeypatch):
    si = _estate(monkeypatch)
    out = multipath.device_paths(si, datastore="nfs-01")
    assert out["items"] == [] and "NFS" in out["scope_note"]


# ─── Scope and paging ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_device_paths_requires_a_scope(monkeypatch):
    si = _estate(monkeypatch)
    with pytest.raises(StoragePathError, match="Scope required"):
        multipath.device_paths(si)


@pytest.mark.unit
def test_two_scopes_are_refused(monkeypatch):
    si = _estate(monkeypatch)
    with pytest.raises(StoragePathError, match="only one of"):
        multipath.device_paths(si, cluster="prod", host="esx-a")


@pytest.mark.unit
def test_unknown_cluster_teaches(monkeypatch):
    si = _estate(monkeypatch)
    with pytest.raises(StoragePathError, match="Did you mean 'prod'"):
        multipath.device_paths(si, cluster="prd")


@pytest.mark.unit
def test_paging(monkeypatch):
    si = _estate(monkeypatch)
    first = multipath.device_paths(si, cluster="prod", limit=2)
    assert first["returned"] == 2 and first["total"] == 3 and first["next_offset"] == 2
    last = multipath.device_paths(si, cluster="prod", limit=2, offset=2)
    assert last["returned"] == 1 and last["next_offset"] is None
    with pytest.raises(ValueError):
        multipath.device_paths(si, cluster="prod", limit=0)


@pytest.mark.unit
def test_mcp_tool_passes_the_teaching_error_through(monkeypatch):
    """StoragePathError carries the next step; _safe_error must not reduce it to its type."""
    from vmware_storage.mcp_server import server

    si = _estate(monkeypatch)
    monkeypatch.setattr(server, "_get_connection", lambda target=None: si)
    out = server.storage_device_paths()
    assert "Scope required" in out["error"], out
    ok = server.storage_device_paths(cluster="prod", only_differences=True)
    assert [d["device"] for d in ok["items"]] == ["naa.600b"]


# ─── Review findings, 2026-09-18 ─────────────────────────────────────────────


def _two_hosts_with_their_own_disks(monkeypatch):
    """Each host: a boot mpx NOT marked local, a CD-ROM, and an internal SAS disk."""
    props = {}
    for i, name in enumerate(("esx-a", "esx-b")):
        hba = _block_hba("vmhba0")
        boot = _disk("mpx.vmhba0:C0:T0:L0")  # localDisk=False, as ESXi often reports
        sas = _disk(f"naa.5000c500a{i}")  # unique per host, non-local
        cd = _cdrom("mpx.vmhba32:C0:T0:L0")
        sas_t = vim.host.SerialAttachedTargetTransport()
        props[f"host-{i}"] = (
            _host_props(
                name,
                [hba],
                [boot, sas, cd],
                [
                    _lu(
                        boot,
                        [
                            _path(
                                "vmhba0:C0:T0:L0",
                                hba,
                                boot,
                                transport=vim.host.BlockAdapterTargetTransport(),
                            )
                        ],
                    ),
                    _lu(sas, [_path("vmhba0:C0:T1:L0", hba, sas, transport=sas_t)]),
                    _lu(
                        cd,
                        [
                            _path(
                                "vmhba32:C0:T0:L0",
                                hba,
                                cd,
                                transport=vim.host.BlockAdapterTargetTransport(),
                            )
                        ],
                    ),
                ],
            ),
            {},
        )
    return _env(monkeypatch, props, clusters={"prod": ["host-0", "host-1"]})


@pytest.mark.unit
def test_per_host_disks_are_not_merged_or_reported_missing(monkeypatch):
    si = _two_hosts_with_their_own_disks(monkeypatch)
    out = multipath.device_paths(si, cluster="prod")
    boots = [d for d in out["items"] if d["device"] == "mpx.vmhba0:C0:T0:L0"]
    assert len(boots) == 2 and all(d["seen_by"] == 1 for d in boots), "two boot disks, not one"
    assert all(d["not_seen_on"] == [] for d in out["items"])
    assert out["summary"]["with_visibility_or_path_count_differences"] == 0
    assert multipath.device_paths(si, cluster="prod", only_differences=True)["items"] == []


@pytest.mark.unit
def test_a_san_lun_seen_by_one_host_is_still_reported(monkeypatch):
    hba_a, hba_b, lun = _fc_hba("vmhba2"), _fc_hba("vmhba2"), _disk("naa.600c")
    a = _host_props("esx-a", [hba_a], [lun], [_lu(lun, [_path("p", hba_a, lun)])])
    b = _host_props("esx-b", [hba_b], [], [])
    si = _env(
        monkeypatch, {"host-1": (a, {}), "host-2": (b, {})}, clusters={"prod": ["host-1", "host-2"]}
    )
    (dev,) = multipath.device_paths(si, cluster="prod")["items"]
    assert dev["shared"] is True and dev["not_seen_on"] == ["esx-b"]


@pytest.mark.unit
def test_a_host_that_is_not_connected_is_not_read_even_with_cached_config(monkeypatch):
    stale = {
        **_host_props("esx-lost", [_fc_hba("vmhba2")], [], []),
        "runtime.connectionState": "notResponding",
    }
    si = _env(monkeypatch, {"host-9": (stale, {})})
    out = storage_paths.list_fc_adapters(si)
    assert out["items"] == [] and out["hosts_without_fc"] == []
    assert "notResponding" in out["hosts_not_read"][0]["reason"]


@pytest.mark.unit
def test_a_host_missing_from_the_reply_is_not_read(monkeypatch):
    hosts = {
        "host-1": (_host_props("esx-a", [_fc_hba("vmhba2")], [], []), {}),
        "host-2": (_host_props("esx-b", [_fc_hba("vmhba2")], [], []), {}),
    }
    si = _env(
        monkeypatch,
        hosts,
        clusters={"prod": ["host-1", "host-2"]},
        pc=_FakePC(hosts, drop={"host-2"}),
    )
    out = multipath.device_paths(si, cluster="prod")
    assert out["complete"] is False
    assert out["hosts_not_read"] == [
        {"host": "esx-b", "reason": "vCenter returned nothing for this host"}
    ]


@pytest.mark.unit
def test_duplicate_cluster_names_are_refused(monkeypatch):
    props = _host_props("esx-a", [], [], [])
    si = _env(
        monkeypatch,
        {"host-1": (props, {}), "host-2": (props, {})},
        clusters={"prod": ["host-1"], "prod#2": ["host-2"]},
    )
    with pytest.raises(StoragePathError, match="2 objects .* named 'prod'"):
        multipath.device_paths(si, cluster="prod")


@pytest.mark.unit
def test_the_last_page_is_not_reported_as_truncated(monkeypatch):
    si = _estate(monkeypatch)
    last = multipath.device_paths(si, cluster="prod", limit=2, offset=2)
    assert last["truncated"] is False and last["hint"] is None and last["limit"] == 2
    first = multipath.device_paths(si, cluster="prod", limit=2)
    assert first["truncated"] is True and "offset=2" in first["hint"]


@pytest.mark.unit
def test_unknown_datastore_backing_is_not_complete(monkeypatch):
    props = _host_props("esx-a", [], [], [])  # mounts nothing
    si = _env(
        monkeypatch, {"host-1": (props, {})}, datastores={"ds-x": ("VMFS", "u-x", ["host-1"])}
    )
    out = multipath.device_paths(si, datastore="ds-x")
    assert out["items"] == [] and out["complete"] is False and "unknown" in out["scope_note"]


@pytest.mark.unit
def test_property_collector_pages_are_followed(monkeypatch):
    props = {
        f"host-{i}": (_host_props(f"esx-{i}", [_fc_hba("vmhba2")], [], []), {}) for i in range(3)
    }
    si = _env(monkeypatch, props, pc=_FakePC(props, page=1))
    out = storage_paths.list_fc_adapters(si)
    assert [r["host"] for r in out["items"]] == ["esx-0", "esx-1", "esx-2"]


@pytest.mark.unit
def test_bad_limit_is_refused_before_any_host_is_read(monkeypatch):
    si = _estate(monkeypatch)
    with pytest.raises(ValueError):
        multipath.device_paths(si, cluster="prod", limit=500)
    assert si.RetrieveContent().propertyCollector.calls == 0


# ─── Narrow review of the fixes, 2026-09-18 ──────────────────────────────────


def _two_read_hosts(monkeypatch, a_luns, a_lus, b_hbas=None, extra=None):
    hba = _fc_hba("vmhba2")
    hosts = {
        "host-1": (_host_props("esx-a", [hba, _block_hba()], a_luns, a_lus(hba)), {}),
        "host-2": (_host_props("esx-b", b_hbas or [_fc_hba("vmhba2")], [], []), {}),
        **(extra or {}),
    }
    return _env(monkeypatch, hosts, clusters={"prod": list(hosts)})


@pytest.mark.unit
def test_a_san_lun_whose_only_path_lost_its_transport_is_still_shared(monkeypatch):
    lun = _disk("naa.600d")
    si = _two_read_hosts(
        monkeypatch,
        [lun],
        lambda hba: [_lu(lun, [_path("p", hba, lun, state="dead", working=False, transport=None)])],
    )
    (dev,) = multipath.device_paths(si, cluster="prod")["items"]
    assert dev["shared"] is True and dev["not_seen_on"] == ["esx-b"]
    assert dev["states_needing_attention"] == ["dead"]


@pytest.mark.unit
def test_a_non_disk_lun_with_an_naa_name_is_per_host(monkeypatch):
    changer = vim.host.ScsiLun(
        key="key-naa.tape", uuid="u", canonicalName="naa.tape", lunType="mediaChanger"
    )
    si = _two_read_hosts(
        monkeypatch, [changer], lambda hba: [_lu(changer, [_path("p", hba, changer)])]
    )
    (dev,) = multipath.device_paths(si, cluster="prod")["items"]
    assert dev["shared"] is False and dev["not_seen_on"] == []


@pytest.mark.unit
def test_unresolved_luns_with_the_same_id_are_not_merged(monkeypatch):
    def lus(hba):
        ghost = _disk("naa.ghost")
        return [
            MP.LogicalUnit(
                key="lu-x", id="0200abc", lun="key-nowhere", path=[_path("p", hba, ghost)]
            )
        ]

    extra = {"host-3": (_host_props("esx-c", [_fc_hba("vmhba2")], [], lus(_fc_hba("vmhba2"))), {})}
    si = _two_read_hosts(monkeypatch, [], lus, extra=extra)
    items = multipath.device_paths(si, cluster="prod")["items"]
    assert [d["device"] for d in items] == ["0200abc", "0200abc"]
    assert all(d["seen_by"] == 1 and d["not_seen_on"] == [] for d in items)


@pytest.mark.unit
def test_a_boot_from_san_lun_marked_local_is_not_reported_missing(monkeypatch):
    boot = _disk("naa.600boot", local=True)
    si = _two_read_hosts(monkeypatch, [boot], lambda hba: [_lu(boot, [_path("p", hba, boot)])])
    (dev,) = multipath.device_paths(si, cluster="prod")["items"]
    assert dev["local"] is True and dev["not_seen_on"] == []


@pytest.mark.unit
def test_a_device_without_san_paths_seen_by_two_hosts_is_shared(monkeypatch):
    jbod, blk = _disk("naa.5000jbod"), _block_hba()
    sas = vim.host.SerialAttachedTargetTransport()
    lu = _lu(jbod, [_path("p", blk, jbod, transport=sas)])
    hosts = {
        f"host-{i}": (
            _host_props(n, [blk], [jbod] if n != "esx-c" else [], [lu] if n != "esx-c" else []),
            {},
        )
        for i, n in enumerate(("esx-a", "esx-b", "esx-c"))
    }
    si = _env(monkeypatch, hosts, clusters={"prod": list(hosts)})
    (dev,) = multipath.device_paths(si, cluster="prod")["items"]
    assert dev["shared"] is True and dev["seen_by"] == 2 and dev["not_seen_on"] == ["esx-c"]
    path = multipath.device_paths(si, cluster="prod", device="naa.5000jbod")["items"][0]
    assert path["hosts"][0]["paths"][0]["protocol"] is None


@pytest.mark.unit
def test_duplicate_host_and_datastore_names_give_advice_that_applies(monkeypatch):
    props = _host_props("esx-a", [], [], [])
    si = _env(
        monkeypatch,
        {"host-1": (props, {}), "host-2": (props, {})},
        datastores={"ds": ("VMFS", "u1", ["host-1"]), "ds ": ("VMFS", "u2", ["host-2"])},
    )
    with pytest.raises(StoragePathError) as exc:
        multipath.device_paths(si, host="esx-a")
    assert "Scope by host" not in str(exc.value) and "FQDN or IP" in str(exc.value)


@pytest.mark.unit
def test_offset_past_the_end_says_so(monkeypatch):
    si = _estate(monkeypatch)
    out = multipath.device_paths(si, cluster="prod", offset=10)
    assert out["items"] == [] and out["truncated"] is False and "past the end" in out["hint"]
