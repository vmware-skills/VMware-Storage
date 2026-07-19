"""Behavioral (mocked-pyVmomi) unit tests for the storage ops modules.

踩坑 #9: the ops modules previously had only static/AST conformance checks —
nothing exercised their actual branching. These tests drive the real ops code
against SimpleNamespace stand-ins for pyVmomi managed objects and assert
behavior: idempotency, validation, error paths, the #10 enable poll, the vSAN
missing-datastore path, and the datastore-browser query/path handling.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyVmomi import vim
from vmware_policy import paginated

from vmware_storage.ops import datastore_browser as dsb
from vmware_storage.ops import iscsi_config, vsan


# ── helpers ────────────────────────────────────────────────────────────────


def _send_target(address: str, port: int) -> SimpleNamespace:
    return SimpleNamespace(address=address, port=port)


def _fake_hba(targets=None, device="vmhba64", iqn="iqn.1998-01.com.vmware:host"):
    """A software iSCSI HBA double that satisfies isinstance + isSoftwareBased."""
    hba = SimpleNamespace(
        device=device,
        iScsiName=iqn,
        isSoftwareBased=True,
        configuredSendTarget=list(targets or []),
    )
    # add_iscsi_target uses isinstance(hba, vim.host.InternetScsiHba); rather
    # than build a real pyVmomi object, patch _get_iscsi_hba directly in tests.
    return hba


def _fake_storage_system():
    calls: list[tuple] = []

    def _record(name):
        def _fn(**kwargs):
            calls.append((name, kwargs))
        return _fn

    ss = SimpleNamespace(
        calls=calls,
        UpdateSoftwareInternetScsiEnabled=_record("UpdateSoftwareInternetScsiEnabled"),
        AddInternetScsiSendTargets=_record("AddInternetScsiSendTargets"),
        RemoveInternetScsiSendTargets=_record("RemoveInternetScsiSendTargets"),
        RescanAllHba=_record("RescanAllHba"),
        RescanVmfs=_record("RescanVmfs"),
    )
    return ss


@pytest.fixture
def patch_host(monkeypatch):
    """Patch _require_host/_get_storage_system to return controllable doubles."""
    host = SimpleNamespace(name="esxi-01")
    ss = _fake_storage_system()
    monkeypatch.setattr(iscsi_config, "_require_host", lambda si, name: host)
    monkeypatch.setattr(iscsi_config, "_get_storage_system", lambda h: ss)
    return SimpleNamespace(host=host, ss=ss)


# ── iscsi_config: validation ───────────────────────────────────────────────


@pytest.mark.unit
def test_add_target_rejects_invalid_ip(patch_host, monkeypatch):
    monkeypatch.setattr(iscsi_config, "_get_iscsi_hba", lambda h: _fake_hba())
    with pytest.raises(iscsi_config.ISCSIError, match="Invalid IP address"):
        iscsi_config.add_iscsi_target(object(), "esxi-01", "not-an-ip")


@pytest.mark.unit
@pytest.mark.parametrize("bad_port", [0, 65536, -1, 99999])
def test_add_target_rejects_out_of_range_port(patch_host, monkeypatch, bad_port):
    monkeypatch.setattr(iscsi_config, "_get_iscsi_hba", lambda h: _fake_hba())
    with pytest.raises(iscsi_config.ISCSIError, match="Port must be 1-65535"):
        iscsi_config.add_iscsi_target(object(), "esxi-01", "10.0.0.1", port=bad_port)


# ── iscsi_config: duplicate-target idempotency ─────────────────────────────


@pytest.mark.unit
def test_add_target_is_idempotent_for_existing_target(patch_host, monkeypatch):
    hba = _fake_hba(targets=[_send_target("10.0.0.1", 3260)])
    monkeypatch.setattr(iscsi_config, "_get_iscsi_hba", lambda h: hba)

    msg = iscsi_config.add_iscsi_target(object(), "esxi-01", "10.0.0.1", port=3260)

    assert "already configured" in msg
    # idempotent path must NOT touch the storage system (no add / no rescan)
    assert patch_host.ss.calls == []


@pytest.mark.unit
def test_add_target_adds_and_rescans_when_new(patch_host, monkeypatch):
    hba = _fake_hba(targets=[_send_target("10.0.0.1", 3260)])
    monkeypatch.setattr(iscsi_config, "_get_iscsi_hba", lambda h: hba)

    msg = iscsi_config.add_iscsi_target(object(), "esxi-01", "10.0.0.2", port=3260)

    assert "added to host" in msg
    op_names = [c[0] for c in patch_host.ss.calls]
    assert op_names == ["AddInternetScsiSendTargets", "RescanAllHba", "RescanVmfs"]


# ── iscsi_config: disabled-HBA error path ──────────────────────────────────


@pytest.mark.unit
def test_add_target_errors_when_hba_disabled(patch_host, monkeypatch):
    monkeypatch.setattr(iscsi_config, "_get_iscsi_hba", lambda h: None)
    with pytest.raises(iscsi_config.ISCSIError, match="not enabled"):
        iscsi_config.add_iscsi_target(object(), "esxi-01", "10.0.0.1")


@pytest.mark.unit
def test_remove_target_errors_when_target_absent(patch_host, monkeypatch):
    hba = _fake_hba(targets=[_send_target("10.0.0.1", 3260)])
    monkeypatch.setattr(iscsi_config, "_get_iscsi_hba", lambda h: hba)
    with pytest.raises(iscsi_config.ISCSIError, match="not found"):
        iscsi_config.remove_iscsi_target(object(), "esxi-01", "10.0.0.9")


# ── iscsi_config: #10 — enable waits for the HBA to materialize ─────────────


@pytest.mark.unit
def test_enable_already_enabled_short_circuits(patch_host, monkeypatch):
    monkeypatch.setattr(iscsi_config, "_get_iscsi_hba", lambda h: _fake_hba())
    msg = iscsi_config.enable_software_iscsi(object(), "esxi-01")
    assert "already enabled" in msg
    assert patch_host.ss.calls == []


@pytest.mark.unit
def test_enable_polls_until_hba_appears(patch_host, monkeypatch):
    # HBA is absent for the first two probes, then materializes — enable must
    # poll rather than returning before the adapter is usable.
    materialized = _fake_hba()
    seq = [None, None, materialized]
    probes = {"n": 0}

    def fake_get(h):
        i = probes["n"]
        probes["n"] += 1
        return seq[i] if i < len(seq) else materialized

    monkeypatch.setattr(iscsi_config, "_get_iscsi_hba", fake_get)
    monkeypatch.setattr(iscsi_config.time, "sleep", lambda s: None)

    msg = iscsi_config.enable_software_iscsi(object(), "esxi-01")

    assert "enabled on host 'esxi-01'" in msg
    assert materialized.device in msg
    # the enable call fired, and we polled past the initial absences
    assert patch_host.ss.calls[0][0] == "UpdateSoftwareInternetScsiEnabled"
    assert probes["n"] >= 3


@pytest.mark.unit
def test_enable_reports_pending_when_hba_never_appears(patch_host, monkeypatch):
    # If the adapter never surfaces within the timeout, report "did not appear"
    # rather than raising — the enable was requested successfully.
    monkeypatch.setattr(iscsi_config, "_get_iscsi_hba", lambda h: None)
    monkeypatch.setattr(iscsi_config.time, "sleep", lambda s: None)
    monkeypatch.setattr(iscsi_config, "_HBA_POLL_TIMEOUT_SEC", 0.0)

    msg = iscsi_config.enable_software_iscsi(object(), "esxi-01")

    assert "did not appear" in msg
    assert patch_host.ss.calls[0][0] == "UpdateSoftwareInternetScsiEnabled"


@pytest.mark.unit
def test_wait_for_iscsi_hba_returns_none_on_timeout(monkeypatch):
    monkeypatch.setattr(iscsi_config.time, "sleep", lambda s: None)
    host = SimpleNamespace(config=SimpleNamespace(storageDevice=None))
    assert iscsi_config._wait_for_iscsi_hba(host, timeout=0.0) is None


# ── vsan: missing-datastore message path ───────────────────────────────────


@pytest.mark.unit
def test_vsan_capacity_missing_datastore_message(monkeypatch):
    fake_cluster = SimpleNamespace(
        configurationEx=SimpleNamespace(vsanConfigInfo=SimpleNamespace(enabled=True)),
        datastore=[
            SimpleNamespace(
                summary=SimpleNamespace(type="VMFS", capacity=1, freeSpace=1),
                name="local-ds",
            )
        ],
    )
    monkeypatch.setattr(vsan, "find_cluster_by_name", lambda si, name: fake_cluster)

    result = vsan.get_vsan_capacity(object(), "prod-cluster")

    assert result["vsan_enabled"] is True
    assert result["datastore_name"] is None
    assert all(result[k] is None for k in ("total_gb", "used_gb", "free_gb", "usage_pct"))
    assert "no vSAN datastore" in result["message"]


@pytest.mark.unit
def test_vsan_capacity_disabled_returns_not_enabled(monkeypatch):
    fake_cluster = SimpleNamespace(
        configurationEx=SimpleNamespace(vsanConfigInfo=SimpleNamespace(enabled=False)),
        datastore=[],
    )
    monkeypatch.setattr(vsan, "find_cluster_by_name", lambda si, name: fake_cluster)

    result = vsan.get_vsan_capacity(object(), "prod-cluster")

    assert result["vsan_enabled"] is False
    assert "not enabled" in result["message"]


# ── datastore_browser: generic FileQuery inclusion + path validation ───────


@pytest.mark.unit
def test_browse_datastore_includes_generic_and_specific_queries(monkeypatch):
    captured: dict = {}

    def fake_search(datastorePath, searchSpec):  # noqa: N803 — pyVmomi API names
        captured["spec"] = searchSpec
        captured["path"] = datastorePath
        return SimpleNamespace(
            info=SimpleNamespace(state=vim.TaskInfo.State.success, result=[], error=None)
        )

    fake_ds = SimpleNamespace(
        browser=SimpleNamespace(SearchDatastoreSubFolders_Task=fake_search)
    )
    monkeypatch.setattr(dsb, "find_datastore_by_name", lambda si, name: fake_ds)

    result = dsb.browse_datastore(object(), "ds1", path="images", pattern="*.ova")

    assert result["items"] == []
    assert captured["path"] == "[ds1] images"
    qtypes = {type(q) for q in captured["spec"].query}
    assert vim.host.DatastoreBrowser.Query in qtypes
    assert vim.host.DatastoreBrowser.IsoImageQuery in qtypes
    assert vim.host.DatastoreBrowser.VmDiskQuery in qtypes
    assert vim.host.DatastoreBrowser.FolderQuery in qtypes


@pytest.mark.unit
@pytest.mark.parametrize("bad_path", ["../escape", "/abs/path", "a/../b", "x\x00y"])
def test_browse_datastore_rejects_traversal_paths(monkeypatch, bad_path):
    fake_ds = SimpleNamespace(
        browser=SimpleNamespace(
            SearchDatastoreSubFolders_Task=lambda **k: pytest.fail(
                "must reject before issuing a browse task"
            )
        )
    )
    monkeypatch.setattr(dsb, "find_datastore_by_name", lambda si, name: fake_ds)

    with pytest.raises(ValueError, match="Invalid datastore path"):
        dsb.browse_datastore(object(), "ds1", path=bad_path)


@pytest.mark.unit
def test_browse_datastore_not_found_raises_with_hint(monkeypatch):
    monkeypatch.setattr(dsb, "find_datastore_by_name", lambda si, name: None)
    monkeypatch.setattr(
        dsb,
        "list_datastores",
        lambda si: paginated([{"name": "ds-1"}, {"name": "ds-2"}], total=2),
    )
    with pytest.raises(ValueError, match="not found"):
        dsb.browse_datastore(object(), "ds-x")


@pytest.mark.unit
def test_browse_datastore_parses_files(monkeypatch):
    file_a = SimpleNamespace(path="app.ova", fileSize=2 * 1024 * 1024, modification="2026-06-01")
    file_b = SimpleNamespace(path="boot.iso", fileSize=None, modification=None)
    folder = SimpleNamespace(folderPath="[ds1] images/", file=[file_a, file_b])

    def fake_search(datastorePath, searchSpec):  # noqa: N803
        return SimpleNamespace(
            info=SimpleNamespace(state=vim.TaskInfo.State.success, result=[folder], error=None)
        )

    fake_ds = SimpleNamespace(
        browser=SimpleNamespace(SearchDatastoreSubFolders_Task=fake_search)
    )
    monkeypatch.setattr(dsb, "find_datastore_by_name", lambda si, name: fake_ds)

    files = dsb.browse_datastore(object(), "ds1", path="images")["items"]

    assert [f["name"] for f in files] == ["app.ova", "boot.iso"]
    by_name = {f["name"]: f for f in files}
    assert by_name["app.ova"]["size_mb"] == 2.0
    assert by_name["boot.iso"]["size_mb"] == 0  # None fileSize → 0, no crash
    assert by_name["app.ova"]["ds_path"] == "[ds1] images/app.ova"
