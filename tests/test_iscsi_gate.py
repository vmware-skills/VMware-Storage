"""The confirmation gate on the four iSCSI / rescan write tools (HLD §7, revised 2026-09-16).

Before this change the four tools acted on a bare call (``dry_run`` defaulted to
False). Now ``confirm: bool = False`` previews, the preview and the acting
response carry ``blast_radius``, and ``confirm=True`` is refused on a blocker or
an unmeasured field. ``dry_run`` survives one minor cycle as a deprecated alias.

The host is built from real pyVmomi data objects (HBA, send/static targets,
multipath paths, SCSI LUNs, VMFS mounts) so the gate reads the same shapes it
reads in production; only the HostSystem and its StorageSystem are mocks, and
the StorageSystem's methods are the write APIs asserted on.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from pyVmomi import vim

from vmware_storage.mcp_server import server

HOST = "esxi-01"
SW_KEY = "key-vim.host.InternetScsiHba-vmhba65"
FC_KEY = "key-vim.host.FibreChannelHba-vmhba2"
IQN_A = "iqn.2001-05.com.array:tgt-a"
IQN_B = "iqn.2001-05.com.array:tgt-b"
WRITES = (
    "UpdateSoftwareInternetScsiEnabled",
    "AddInternetScsiSendTargets",
    "RemoveInternetScsiSendTargets",
    "RescanAllHba",
    "RescanVmfs",
)


# ---------------------------------------------------------------------------
# A host built from real data objects
# ---------------------------------------------------------------------------


def _static(iqn, address, parent, method="sendTargetMethod"):
    return vim.host.InternetScsiHba.StaticTarget(
        address=address, port=3260, iScsiName=iqn, discoveryMethod=method, parent=parent,
    )


def _path(name, adapter, iqn=None, address=None, transport=True):
    t = None
    if transport:
        t = vim.host.InternetScsiTargetTransport(iScsiName=iqn, address=[f"{address}:3260"])
    return vim.host.MultipathInfo.Path(
        key=f"path-{name}", name=name, adapter=adapter, state="active", transport=t,
    )


def _lun(key, canonical):
    return vim.host.ScsiDisk(
        key=key, canonicalName=canonical, displayName=canonical, lunType="disk"
    )


def _vmfs(name, disk):
    return vim.host.FileSystemMountInfo(
        volume=vim.host.VmfsVolume(
            name=name, uuid=f"uuid-{name}",
            extent=[vim.host.ScsiDisk.Partition(diskName=disk, partition=1)],
        )
    )


class FakeHost:
    """esxi-01 with software iSCSI on, two send targets and three LUNs.

    * send targets 10.0.0.5 (discovers tgt-a @ 10.0.0.6) and 10.0.0.9
      (discovers tgt-b @ 10.0.0.7);
    * naa.1: one path via tgt-a, one via tgt-b  -> loses some paths on remove;
    * naa.2: only via tgt-a, no datastore         -> loses every path;
    * naa.3: FC only, datastore ds-fc             -> unaffected.
    """

    def __init__(self) -> None:
        self.hba = vim.host.InternetScsiHba(
            key=SW_KEY, device="vmhba65", isSoftwareBased=True,
            iScsiName="iqn.1998-01.com.vmware:esxi-01",
            configuredSendTarget=[
                vim.host.InternetScsiHba.SendTarget(address="10.0.0.5", port=3260),
                vim.host.InternetScsiHba.SendTarget(address="10.0.0.9", port=3260),
            ],
            configuredStaticTarget=[
                _static(IQN_A, "10.0.0.6", "10.0.0.5:3260"),
                _static(IQN_B, "10.0.0.7", "10.0.0.9:3260"),
            ],
        )
        self.fc = vim.host.FibreChannelHba(key=FC_KEY, device="vmhba2")
        self.paths = {
            "naa.1": [
                _path("vmhba65:C0:T0:L1", SW_KEY, IQN_A, "10.0.0.6"),
                _path("vmhba65:C1:T0:L1", SW_KEY, IQN_B, "10.0.0.7"),
            ],
            "naa.2": [_path("vmhba65:C0:T0:L2", SW_KEY, IQN_A, "10.0.0.6")],
            "naa.3": [_path("vmhba2:C0:T0:L3", FC_KEY, transport=False)],
        }
        self.mounts = [_vmfs("ds-shared", "naa.1"), _vmfs("ds-fc", "naa.3")]
        self.enabled = True
        self.connection_state = "connected"
        self.config_readable = True
        self.storage_system = MagicMock(name="StorageSystem")
        self.storage_system.UpdateSoftwareInternetScsiEnabled.side_effect = self._enable

    def _enable(self, enabled):
        self.enabled = True

    def build(self) -> MagicMock:
        host = MagicMock(name=HOST)
        host.name = HOST
        host._moId = "host-42"
        host.runtime.connectionState = self.connection_state
        host.configManager.storageSystem = self.storage_system
        if not self.config_readable:
            host.config = None
            return host
        luns = [_lun(f"lun-{c}", c) for c in self.paths]
        mp = vim.host.MultipathInfo(lun=[
            vim.host.MultipathInfo.LogicalUnit(key=f"mp-{c}", id=c, lun=f"lun-{c}", path=p)
            for c, p in self.paths.items()
        ])
        hbas = [self.fc] + ([self.hba] if self.enabled else [])
        host.config.storageDevice = vim.host.StorageDeviceInfo(
            hostBusAdapter=hbas, scsiLun=luns, multipathInfo=mp,
        )
        host.config.fileSystemVolume.mountInfo = self.mounts
        return host

    def writes(self) -> dict:
        return {w: getattr(self.storage_system, w).call_count for w in WRITES}


@pytest.fixture
def fake(monkeypatch):
    f = FakeHost()
    monkeypatch.setattr(server, "_get_connection", lambda target=None: object())
    monkeypatch.setattr(server._audit, "log", lambda **kw: None)
    monkeypatch.setattr(
        "vmware_storage.ops.iscsi_config.find_host_by_name", lambda si, name: f.build()
    )
    # The executor polls the host it found for the new HBA; this fake host is
    # rebuilt per lookup, so the poll would only ever wait out its timeout.
    monkeypatch.setattr("vmware_storage.ops.iscsi_config._HBA_POLL_TIMEOUT_SEC", 0.0)
    return f


def _no_writes(f: FakeHost) -> None:
    assert f.writes() == {w: 0 for w in WRITES}


def _remove(**kw):
    return server.storage_iscsi_remove_target(host_name=HOST, address="10.0.0.5", **kw)


def _add(**kw):
    return server.storage_iscsi_add_target(host_name=HOST, address="10.0.0.20", **kw)


GATED = ("storage_iscsi_enable", "storage_iscsi_add_target",
         "storage_iscsi_remove_target", "storage_rescan")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", GATED)
def test_schema_confirm_defaults_to_false_and_dry_run_to_none(tool):
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    props = tools[tool].inputSchema["properties"]
    assert props["confirm"]["default"] is False
    assert props["confirm"]["description"].startswith(
        "False (default) returns the blast radius and changes nothing. True applies it."
    )
    assert props["dry_run"].get("default", "missing") is None
    assert props["dry_run"]["description"].startswith("Deprecated alias for confirm")
    assert "Do not set confirm=True on your own" in " ".join(tools[tool].description.split())


# ---------------------------------------------------------------------------
# storage_iscsi_enable
# ---------------------------------------------------------------------------


def test_enable_bare_call_previews(fake):
    fake.enabled = False
    result = server.storage_iscsi_enable(host_name=HOST)
    assert result["action"] == "preview"
    radius = result["blast_radius"]
    assert radius["host"] == HOST and radius["host_id"] == "host-42"
    assert radius["already_enabled"] is False
    assert radius["adapters"] == ["vmhba2"]
    assert radius["blockers"] == [] and radius["unmeasured"] == []
    _no_writes(fake)


def test_enable_confirm_enables_once(fake):
    fake.enabled = False
    result = server.storage_iscsi_enable(host_name=HOST, confirm=True)
    assert result["action"] == "enabled"
    assert "blast_radius" in result
    assert fake.writes()["UpdateSoftwareInternetScsiEnabled"] == 1


def test_enable_when_already_enabled_is_noop(fake):
    result = server.storage_iscsi_enable(host_name=HOST, confirm=True)
    assert result["action"] == "noop"
    assert result["blast_radius"]["adapter"]["device"] == "vmhba65"
    _no_writes(fake)


def test_enable_unreadable_host_refuses(fake):
    fake.config_readable = False
    preview = server.storage_iscsi_enable(host_name=HOST)
    assert "host_storage_config" in preview["blast_radius"]["unmeasured"]
    result = server.storage_iscsi_enable(host_name=HOST, confirm=True)
    assert "error" in result and "could not read" in result["error"].lower()
    _no_writes(fake)


def test_enable_missing_storage_system_blocks(fake):
    fake.enabled = False
    fake.storage_system = None
    preview = server.storage_iscsi_enable(host_name=HOST)
    assert preview["blast_radius"]["blockers"]
    result = server.storage_iscsi_enable(host_name=HOST, confirm=True)
    assert "error" in result and "disconnected" in result["error"]


# ---------------------------------------------------------------------------
# storage_iscsi_add_target
# ---------------------------------------------------------------------------


def test_add_bare_call_previews(fake):
    result = _add()
    assert result["action"] == "preview"
    radius = result["blast_radius"]
    assert radius["target"] == "10.0.0.20:3260"
    assert radius["adapter"]["device"] == "vmhba65"
    assert radius["send_target_count"] == 2
    assert radius["already_configured"] is False
    assert radius["rescan"]["adapters"] == ["vmhba2", "vmhba65"]
    assert radius["blockers"] == [] and radius["unmeasured"] == []
    _no_writes(fake)


def test_add_confirm_adds_once_and_rescans(fake):
    result = _add(confirm=True)
    assert result["action"] == "target_added"
    assert result["blast_radius"]["target"] == "10.0.0.20:3260"
    w = fake.writes()
    assert w["AddInternetScsiSendTargets"] == 1 and w["RescanAllHba"] == 1


def test_add_existing_target_is_noop(fake):
    result = server.storage_iscsi_add_target(host_name=HOST, address="10.0.0.5", confirm=True)
    assert result["action"] == "noop"
    _no_writes(fake)


def test_add_without_iscsi_enabled_blocks(fake):
    fake.enabled = False
    assert _add()["blast_radius"]["blockers"]
    result = _add(confirm=True)
    assert "error" in result and "storage_iscsi_enable" in result["error"]
    _no_writes(fake)


def test_add_unreadable_host_refuses(fake):
    fake.config_readable = False
    assert _add()["blast_radius"]["unmeasured"]
    assert "error" in _add(confirm=True)
    _no_writes(fake)


def test_add_on_disconnected_host_refuses(fake):
    """vCenter keeps a lost host's last-known config; only the gate notices.

    The executor's own checks (adapter present, config readable) pass on that
    stale view, so without the gate the add would be sent to a host nobody
    reached.
    """
    fake.connection_state = "notResponding"
    assert "host_connection" in _add()["blast_radius"]["unmeasured"]
    result = _add(confirm=True)
    assert "error" in result and "host_connection" in result["error"]
    _no_writes(fake)


def test_add_undo_recorded_only_after_a_real_add(fake, monkeypatch):
    recorded = []

    class _Store:
        def record(self, **kw):
            recorded.append(kw)
            return "undo-1"

    monkeypatch.setattr("vmware_policy.undo.get_undo_store", lambda: _Store())
    _add()
    assert recorded == [], "a preview recorded an undo token for a target never added"
    _add(confirm=True)
    assert len(recorded) == 1
    assert recorded[0]["undo_descriptor"]["params"]["confirm"] is True


# ---------------------------------------------------------------------------
# storage_iscsi_remove_target
# ---------------------------------------------------------------------------


def test_remove_bare_call_previews_dependents(fake):
    result = _remove()
    assert result["action"] == "preview"
    radius = result["blast_radius"]
    assert radius["target"] == "10.0.0.5:3260"
    assert radius["static_targets_removed"] == [{"iqn": IQN_A, "address": "10.0.0.6:3260"}]
    assert radius["paths_lost_count"] == 2
    assert radius["devices_losing_all_paths"] == ["naa.2"]
    assert radius["devices_losing_some_paths"] == ["naa.1"]
    assert radius["datastores_losing_all_paths"] == []
    assert radius["datastores_losing_some_paths"] == ["ds-shared"]
    assert radius["blockers"] == [] and radius["unmeasured"] == []
    _no_writes(fake)


def test_remove_confirm_removes_once(fake):
    result = _remove(confirm=True)
    assert result["action"] == "target_removed"
    assert result["blast_radius"]["paths_lost_count"] == 2
    w = fake.writes()
    assert w["RemoveInternetScsiSendTargets"] == 1 and w["RescanVmfs"] == 1


def test_remove_datastore_losing_every_path_blocks(fake):
    fake.mounts = fake.mounts + [_vmfs("ds-only-a", "naa.2")]
    preview = _remove()
    assert preview["blast_radius"]["datastores_losing_all_paths"] == ["ds-only-a"]
    assert preview["blast_radius"]["blockers"]
    result = _remove(confirm=True)
    assert "error" in result and "ds-only-a" in result["error"]
    _no_writes(fake)


def test_remove_unknown_target_blocks(fake):
    preview = server.storage_iscsi_remove_target(host_name=HOST, address="10.0.0.99")
    assert preview["blast_radius"]["blockers"]
    result = server.storage_iscsi_remove_target(host_name=HOST, address="10.0.0.99", confirm=True)
    assert "error" in result and "storage_iscsi_status" in result["error"]
    _no_writes(fake)


def test_remove_path_without_transport_is_unmeasured(fake):
    fake.paths["naa.4"] = [_path("vmhba65:C9:T0:L4", SW_KEY, transport=False)]
    preview = _remove()
    assert "paths_without_transport" in preview["blast_radius"]["unmeasured"]
    assert "error" in _remove(confirm=True)
    _no_writes(fake)


def test_remove_unrecognised_static_parent_is_unmeasured(fake):
    fake.hba.configuredStaticTarget = [_static(IQN_A, "10.0.0.6", None)]
    preview = _remove()
    assert "static_target_parent" in preview["blast_radius"]["unmeasured"]
    assert "error" in _remove(confirm=True)
    _no_writes(fake)


def test_remove_target_reached_through_another_send_target_is_kept(fake):
    fake.hba.configuredStaticTarget = list(fake.hba.configuredStaticTarget) + [
        _static(IQN_A, "10.0.0.6", "10.0.0.9:3260"),
    ]
    radius = _remove()["blast_radius"]
    assert radius["static_targets_removed"] == []
    assert radius["paths_lost_count"] == 0


def test_remove_unreadable_host_refuses(fake):
    fake.config_readable = False
    assert _remove()["blast_radius"]["unmeasured"]
    assert "error" in _remove(confirm=True)
    _no_writes(fake)


def test_remove_disconnected_host_is_unmeasured(fake):
    fake.connection_state = "notResponding"
    assert "host_connection" in _remove()["blast_radius"]["unmeasured"]
    assert "error" in _remove(confirm=True)
    _no_writes(fake)


# ---------------------------------------------------------------------------
# storage_rescan
# ---------------------------------------------------------------------------


def test_rescan_bare_call_previews_scope(fake):
    result = server.storage_rescan(host_name=HOST)
    assert result["action"] == "preview"
    radius = result["blast_radius"]
    assert radius["hosts"] == [HOST]
    assert radius["adapters"] == ["vmhba2", "vmhba65"]
    assert radius["adapter_count"] == 2
    assert radius["vmfs_rescan"] is True
    assert radius["mounted_vmfs"] == ["ds-fc", "ds-shared"]
    _no_writes(fake)


def test_rescan_confirm_rescans_once(fake):
    result = server.storage_rescan(host_name=HOST, confirm=True)
    assert result["action"] == "rescanned"
    w = fake.writes()
    assert w["RescanAllHba"] == 1 and w["RescanVmfs"] == 1


def test_rescan_unreadable_host_refuses(fake):
    fake.config_readable = False
    assert server.storage_rescan(host_name=HOST)["blast_radius"]["unmeasured"]
    assert "error" in server.storage_rescan(host_name=HOST, confirm=True)
    _no_writes(fake)


# ---------------------------------------------------------------------------
# dry_run alias — the old contract acted on dry_run=False (and by default)
# ---------------------------------------------------------------------------

CALLS = {
    "storage_iscsi_enable": (lambda **kw: server.storage_iscsi_enable(host_name=HOST, **kw),
                             "UpdateSoftwareInternetScsiEnabled", "enabled"),
    "storage_iscsi_add_target": (_add, "AddInternetScsiSendTargets", "target_added"),
    "storage_iscsi_remove_target": (_remove, "RemoveInternetScsiSendTargets", "target_removed"),
    "storage_rescan": (lambda **kw: server.storage_rescan(host_name=HOST, **kw),
                       "RescanAllHba", "rescanned"),
}
ALIASES = [
    ({}, False),                                  # was: acted. Now the default previews.
    ({"dry_run": False}, True),
    ({"dry_run": True}, False),
    ({"confirm": True, "dry_run": True}, False),  # disagree -> preview
    ({"confirm": True, "dry_run": False}, True),
    ({"confirm": True}, True),
]


@pytest.mark.parametrize("tool", GATED)
@pytest.mark.parametrize("kwargs,acts", ALIASES)
def test_dry_run_alias(fake, tool, kwargs, acts):
    fake.enabled = tool != "storage_iscsi_enable"
    call, write, action = CALLS[tool]
    result = call(**kwargs)
    assert result["action"] == (action if acts else "preview")
    assert fake.writes()[write] == (1 if acts else 0)
    if "dry_run" in kwargs:
        assert result["deprecated"] == (
            "dry_run is deprecated; use confirm. Removed in the next minor release."
        )
    else:
        assert "deprecated" not in result


def test_refusal_is_audited_as_failure(fake, monkeypatch):
    rows = []

    class _Recorder:
        def log(self, **kw):
            rows.append(kw)

    monkeypatch.setattr("vmware_policy.guard.get_engine", lambda: _Recorder())
    fake.config_readable = False
    result = _remove(confirm=True)
    assert "error" in result
    assert rows and rows[-1]["status"] == "error"


def test_host_not_found_still_teaches(monkeypatch):
    monkeypatch.setattr(server, "_get_connection", lambda target=None: object())
    monkeypatch.setattr("vmware_storage.ops.iscsi_config.find_host_by_name", lambda si, n: None)
    monkeypatch.setattr("vmware_storage.ops.iscsi_config.list_hosts", lambda si: [])
    result = server.storage_rescan(host_name="nope")
    assert "error" in result and "list_esxi_hosts" in result["error"]


# ---------------------------------------------------------------------------
# Review fixes (2026-09-19)
# ---------------------------------------------------------------------------


def _raw_path(name, iqn, *addresses):
    t = vim.host.InternetScsiTargetTransport(iScsiName=iqn, address=list(addresses))
    return vim.host.MultipathInfo.Path(
        key=f"path-{name}", name=name, adapter=SW_KEY, state="active", transport=t,
    )


@pytest.mark.parametrize("spelling", [
    "10.0.0.6",               # no port
    "010.000.000.006:3260",   # leading zeros
    " 10.0.0.6:3260 ",        # stray whitespace
])
def test_remove_ipv4_path_spelling_still_counts_as_lost(fake, spelling):
    """A path spelled differently from the static target is still that target's path."""
    fake.paths["naa.2"] = [_raw_path("vmhba65:C0:T0:L2", IQN_A, spelling)]
    fake.mounts = fake.mounts + [_vmfs("ds-only-a", "naa.2")]
    radius = _remove()["blast_radius"]
    assert radius["datastores_losing_all_paths"] == ["ds-only-a"]
    result = _remove(confirm=True)
    assert "error" in result and "ds-only-a" in result["error"]
    _no_writes(fake)


def _ipv6_host(fake):
    fake.hba.configuredSendTarget = [
        vim.host.InternetScsiHba.SendTarget(address="fd00::5", port=3260),
        vim.host.InternetScsiHba.SendTarget(address="10.0.0.9", port=3260),
    ]
    fake.hba.configuredStaticTarget = [
        _static(IQN_A, "fd00::6", "[fd00::5]:3260"),
        _static(IQN_B, "10.0.0.7", "10.0.0.9:3260"),
    ]
    fake.mounts = fake.mounts + [_vmfs("ds-only-a", "naa.2")]


@pytest.mark.parametrize("spelling", [
    "[fd00::6]:3260",          # bracketed
    "fd00::6:3260",            # unbracketed with port
    "FD00:0000::6",            # case, leading zeros, no port
    "[FD00::6]",               # bracketed, no port
])
def test_remove_ipv6_path_spelling_still_counts_as_lost(fake, spelling):
    _ipv6_host(fake)
    fake.paths["naa.1"] = [_raw_path("vmhba65:C1:T0:L1", IQN_B, "10.0.0.7:3260")]
    fake.paths["naa.2"] = [_raw_path("vmhba65:C0:T0:L2", IQN_A, spelling)]
    preview = server.storage_iscsi_remove_target(host_name=HOST, address="fd00::5")
    radius = preview["blast_radius"]
    assert radius["static_targets_removed"] == [{"iqn": IQN_A, "address": "[fd00::6]:3260"}]
    assert radius["datastores_losing_all_paths"] == ["ds-only-a"]
    result = server.storage_iscsi_remove_target(host_name=HOST, address="fd00::5", confirm=True)
    assert "error" in result and "ds-only-a" in result["error"]
    _no_writes(fake)


@pytest.mark.parametrize("address", ["not an address!", "10.0.0.66:3260"])
def test_remove_path_of_removed_iqn_with_unmatched_address_is_unmeasured(fake, address):
    """Same IQN as a removed target, but an address no static target has: fail closed."""
    fake.paths["naa.2"] = [_raw_path("vmhba65:C0:T0:L2", IQN_A, address)]
    fake.mounts = fake.mounts + [_vmfs("ds-only-a", "naa.2")]
    radius = _remove()["blast_radius"]
    assert "path_address" in radius["unmeasured"]
    assert radius["paths_unmatched_address"] == 1
    result = _remove(confirm=True)
    assert "error" in result
    _no_writes(fake)


def test_remove_path_of_kept_portal_of_same_iqn_is_not_lost(fake):
    """IQN_A also reachable at 10.0.0.8 through the kept send target: that path survives."""
    fake.hba.configuredStaticTarget = list(fake.hba.configuredStaticTarget) + [
        _static(IQN_A, "10.0.0.8", "10.0.0.9:3260"),
    ]
    fake.paths["naa.2"] = [
        _raw_path("vmhba65:C0:T0:L2", IQN_A, "10.0.0.6:3260"),
        _raw_path("vmhba65:C2:T0:L2", IQN_A, "10.0.0.8:3260"),
    ]
    radius = _remove()["blast_radius"]
    assert radius["unmeasured"] == []
    assert radius["devices_losing_some_paths"] == ["naa.1", "naa.2"]


def test_unreadable_host_previews_show_none_not_zero(fake):
    fake.config_readable = False
    enable = server.storage_iscsi_enable(host_name=HOST)["blast_radius"]
    assert enable["adapters"] is None and enable["already_enabled"] is None
    add = _add()["blast_radius"]
    assert add["send_target_count"] is None
    assert add["send_targets"] is None
    assert add["already_configured"] is None
    assert add["rescan"]["adapters"] is None
    rescan = server.storage_rescan(host_name=HOST)["blast_radius"]
    assert rescan["adapters"] is None and rescan["adapter_count"] is None
    assert rescan["mounted_vmfs"] is None


def test_add_race_already_configured_is_noop_without_undo(fake, monkeypatch):
    """The target appears between the gate read and the executor: no undo for it."""
    recorded = []

    class _Store:
        def record(self, **kw):
            recorded.append(kw)
            return "undo-1"

    monkeypatch.setattr("vmware_policy.undo.get_undo_store", lambda: _Store())
    real = fake.build

    def _build_racing():
        host = real()
        if _build_racing.calls:
            fake.hba.configuredSendTarget = list(fake.hba.configuredSendTarget) + [
                vim.host.InternetScsiHba.SendTarget(address="10.0.0.20", port=3260)
            ]
        _build_racing.calls += 1
        return host

    _build_racing.calls = 0
    monkeypatch.setattr(
        "vmware_storage.ops.iscsi_config.find_host_by_name", lambda si, name: _build_racing()
    )
    result = _add(confirm=True)
    assert result["action"] == "noop"
    assert recorded == []
    assert fake.writes()["AddInternetScsiSendTargets"] == 0
