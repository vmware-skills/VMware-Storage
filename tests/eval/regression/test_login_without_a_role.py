"""A login that works but has no vSphere role (AIops#47).

Reported on vCenter 8.0.3: an account with no vSphere role logs in, vCenter
shows no session for it (``currentSession`` is ``None``), and every inventory
call then fails with ``vim.fault.NotAuthenticated``. ``doctor`` passed, and the
error told the user to check credentials, which were fine.

Driven through the real ConnectionManager, doctor, CLI handler and MCP tool;
only ``SmartConnect`` / ``Disconnect`` are fakes, so nothing touches a network.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import typer
from pyVmomi import vim

from vmware_storage import doctor
from vmware_storage.config import AppConfig, TargetConfig
from vmware_storage.connection import _SI_ATEXIT, ConnectionManager, NoVsphereRoleError
from vmware_storage.mcp_server import server

ROLE_WORDS = ("no vSphere role", "Read-Only")


def _config() -> AppConfig:
    return AppConfig(
        targets=(
            TargetConfig(name="lab", host="vc.example.com", config_username="u@vsphere.local"),
        )
    )


def _si(has_session: bool, *, root_readable: bool = False) -> MagicMock:
    """A login as vCenter answers it.

    No role (AIops#47) takes both signals: ``currentSession`` reads ``None`` and
    a container view on the root folder is refused with NotAuthenticated.
    ``root_readable`` is the scoped account (one folder, or Supervisor namespace
    permissions only): no session either, but the root view opens.
    """
    si = MagicMock(name="si")
    si.content.sessionManager.currentSession = MagicMock() if has_session else None
    if not has_session and not root_readable:
        si.content.viewManager.CreateContainerView.side_effect = vim.fault.NotAuthenticated()
    return si


@pytest.fixture
def fake_login(monkeypatch):
    monkeypatch.setenv("VMWARE_LAB_PASSWORD", "pw")

    def install(has_session: bool, root_readable: bool = False):
        si = _si(has_session, root_readable=root_readable)
        return si, patch("pyVim.connect.SmartConnect", return_value=si), patch(
            "pyVim.connect.Disconnect"
        )

    return install


@pytest.mark.unit
def test_role_less_login_is_refused_closed_and_not_cached(fake_login):
    si, connect, disconnect = fake_login(has_session=False)
    mgr = ConnectionManager(_config())
    with connect as smart, disconnect as dis:
        with pytest.raises(NoVsphereRoleError) as excinfo:
            mgr.connect("lab")
        msg = str(excinfo.value)
        for word in ROLE_WORDS:
            assert word in msg
        assert "'lab'" in msg
        dis.assert_called_once_with(si)  # the session is logged out
        assert id(si) not in _SI_ATEXIT  # and its exit handler unpinned
        assert mgr.list_connected() == []  # nothing cached
        with pytest.raises(NoVsphereRoleError):
            mgr.connect("lab")
        assert smart.call_count == 2  # the second call logged in again


@pytest.mark.unit
def test_normal_login_is_unaffected(fake_login):
    si, connect, disconnect = fake_login(has_session=True)
    mgr = ConnectionManager(_config())
    with connect, disconnect as dis:
        assert mgr.connect("lab") is si
        assert mgr.list_connected() == ["lab"]
        dis.assert_not_called()
        mgr.disconnect_all()


@pytest.mark.unit
def test_scoped_account_without_a_session_still_connects(fake_login):
    """``currentSession`` alone is not a verdict: a readable root view lets it through."""
    si, connect, disconnect = fake_login(has_session=False, root_readable=True)
    mgr = ConnectionManager(_config())
    with connect, disconnect as dis:
        assert mgr.connect("lab") is si  # no NoVsphereRoleError
        assert mgr.list_connected() == ["lab"]  # cached like any session
        dis.assert_not_called()
        view = si.content.viewManager.CreateContainerView.return_value
        view.Destroy.assert_called_once_with()  # the probe leaves no view behind
        mgr.disconnect_all()


@pytest.mark.unit
def test_doctor_auth_row_names_the_missing_role(fake_login, monkeypatch):
    _, connect, disconnect = fake_login(has_session=False)
    monkeypatch.setattr(type(doctor.resolve_config_path()), "exists", lambda self: True)
    monkeypatch.setattr("vmware_storage.config.load_config", _config)
    with connect, disconnect:
        ok, row = doctor._check_auth()
    assert ok is False
    for word in ROLE_WORDS:
        assert word in row


@pytest.mark.unit
def test_mcp_tool_returns_the_role_message_whole(fake_login, monkeypatch):
    _, connect, disconnect = fake_login(has_session=False)
    mgr = ConnectionManager(_config())
    monkeypatch.setattr(server, "_get_conn_mgr", lambda: mgr)
    with connect, disconnect:
        out = server.list_all_datastores(target="lab")
    for word in ROLE_WORDS:
        assert word in out["error"]
    # The remedy sits at the end of the message; the 300-character cap cut it.
    assert "vmware-storage doctor" in out["error"]
    # The hint points at the role check, not at connectivity, which is fine.
    assert "session" in out["hint"] and "verify connectivity" not in out["hint"]


@pytest.mark.unit
def test_cli_handler_prints_the_role_message(fake_login, monkeypatch, capsys):
    from vmware_storage import cli

    _, connect, disconnect = fake_login(has_session=False)
    monkeypatch.setattr(cli, "load_config", lambda path=None: _config())

    @cli.handle_cli_errors
    def run():
        cli._get_connection("lab")

    with connect, disconnect, pytest.raises(typer.Exit):
        run()
    out = " ".join(capsys.readouterr().out.split())
    for word in ROLE_WORDS:
        assert word in out


# ── NotAuthenticated raised later: expired session, or a role removed ─────────


def _says_both(text: str) -> None:
    text = " ".join(text.split())
    assert "expired" in text and "run the command again" in text
    assert "no vSphere role" in text and "Read-Only" in text


@pytest.mark.unit
def test_cli_not_authenticated_says_both_causes(capsys):
    from vmware_storage.cli import handle_cli_errors

    @handle_cli_errors
    def boom():
        raise vim.fault.NotAuthenticated()

    with pytest.raises(typer.Exit):
        boom()
    _says_both(capsys.readouterr().out)


@pytest.mark.unit
def test_mcp_not_authenticated_says_both_causes(monkeypatch):
    def _boom(target=None):
        raise vim.fault.NotAuthenticated()

    monkeypatch.setattr(server, "_get_connection", _boom)
    out = server.list_all_datastores(target="lab")
    _says_both(out["error"])
    assert "operation failed" not in out["error"]
    assert "session" in out["hint"] and "verify connectivity" not in out["hint"]


@pytest.mark.unit
def test_get_access_info_hint_names_the_role_check(fake_login, monkeypatch):
    _, connect, disconnect = fake_login(has_session=False)
    mgr = ConnectionManager(_config())
    monkeypatch.setattr(server, "_get_conn_mgr", lambda: mgr)
    with connect, disconnect:
        out = server.get_access_info(target="lab")
    for word in ROLE_WORDS:
        assert word in out["error"]
    assert "session" in out["hint"] and "verify connectivity" not in out["hint"]


@pytest.mark.unit
def test_write_tool_error_reply_hint_names_the_role_check():
    out = server._error_reply(vim.fault.NotAuthenticated(), "storage")
    assert "session" in out["hint"] and "verify connectivity" not in out["hint"]
    other = server._error_reply(TimeoutError("slow"), "storage")
    assert "verify connectivity" in other["hint"]


@pytest.mark.unit
def test_doctor_row_is_not_a_class_name(fake_login, monkeypatch):
    """The row must carry the text, not 'NoVsphereRoleError'."""
    _, connect, disconnect = fake_login(has_session=False)
    monkeypatch.setattr(type(doctor.resolve_config_path()), "exists", lambda self: True)
    monkeypatch.setattr("vmware_storage.config.load_config", _config)
    with connect, disconnect:
        _, row = doctor._check_auth()
    assert "NoVsphereRoleError" not in row


# 2026-10-18, lab vCenter 8.0.3, SSO account with no permission anywhere:
# vCenter refuses the *login itself* with NoPermission (System.View on the root
# folder). Before the fix that raw fault reached doctor and every tool.
def _login_refused(exc: Exception, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VMWARE_LAB_PASSWORD", "pw")
    mgr = ConnectionManager(_config())
    with patch("pyVim.connect.SmartConnect", side_effect=exc):
        try:
            mgr.connect("lab")
        except Exception as e:  # noqa: BLE001 — the test inspects whatever is raised
            return e
    raise AssertionError("connect did not raise")


def test_login_refused_with_no_permission_names_the_missing_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = vim.fault.NoPermission(
        privilegeId="System.View", msg="Permission to perform this operation was denied."
    )
    err = _login_refused(fault, monkeypatch)
    assert isinstance(err, NoVsphereRoleError)
    msg = str(err)
    assert "no vSphere role" in msg and "Read-Only" in msg and "'lab'" in msg
    assert "dynamicType" not in msg and "operation failed" not in msg
    assert len(msg) <= 300
    assert err.__cause__ is fault


def test_wrong_password_is_not_reported_as_a_missing_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err = _login_refused(vim.fault.InvalidLogin(), monkeypatch)
    assert not isinstance(err, NoVsphereRoleError)
    assert isinstance(err, vim.fault.InvalidLogin)


@pytest.mark.parametrize(
    ("privilege", "esxi"),
    [("System.View", True), ("Sessions.ValidateSession", False), (None, False)],
)
def test_other_login_refusals_get_a_neutral_remedy(
    privilege, esxi, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review 2026-10-18 (M3): an ESXi host (lockdown mode, host permissions) or
    # another privilege must not be sent to vCenter Global Permissions.
    from dataclasses import replace

    fault = vim.fault.NoPermission(privilegeId=privilege)
    mgr = ConnectionManager(_config())
    if esxi:
        esxi_targets = tuple(replace(t, type="esxi") for t in mgr._config.targets)
        mgr._config = replace(mgr._config, targets=esxi_targets)
    monkeypatch.setenv("VMWARE_LAB_PASSWORD", "pw")
    monkeypatch.setenv("VMWARE_PRIVATEAI_LAB_PASSWORD", "pw")
    with patch("pyVim.connect.SmartConnect", side_effect=fault):
        with pytest.raises(Exception) as info:
            mgr.connect("lab")
    msg = str(info.value)
    assert "Global Permissions" not in msg and "lockdown mode" in msg
    if privilege:
        assert f"({privilege})" in msg
    assert info.value.__cause__ is fault and len(msg) <= 300
