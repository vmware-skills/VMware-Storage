"""Regression evals for the storage hardening fix batch (2026-06).

Each test pins one confirmed bug so it can never silently return:

1. browse_datastore query list excluded generic files → .ova/.ovf invisible
2. storage_iscsi_remove_target under-classified (medium, must be high);
   storage_iscsi_status over-classified (medium, must be low)
3. MCP write tools lacked dry_run preview
4. Audit log failure flipped a successful write into a reported failure
5. SKILL.md claimed 6R/5W but the server exposes 7R/4W
6. get_vsan_capacity returned healthy-looking zeros when the vSAN datastore
   was missing
7. CLI surfaced raw tracebacks for expected operational errors
8. Not-found errors gave no suggestion of valid names
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyVmomi import vim

REPO_ROOT = Path(__file__).resolve().parents[3]


# ── Fix 1: generic file query so .ova/.ovf are returned ────────────────────


def test_browse_datastore_query_includes_generic_file_query(monkeypatch):
    """search_spec.query must include the generic vim.host.DatastoreBrowser.Query
    (the base FileQuery); without it ISO/VMDK/folders are the only results and
    .ova/.ovf files are silently dropped."""
    from vmware_storage.ops import datastore_browser as dsb

    captured: dict = {}

    def fake_search(datastorePath, searchSpec):  # noqa: N803 — pyVmomi API names
        captured["spec"] = searchSpec
        return SimpleNamespace(
            info=SimpleNamespace(
                state=vim.TaskInfo.State.success, result=[], error=None
            )
        )

    fake_ds = SimpleNamespace(
        browser=SimpleNamespace(SearchDatastoreSubFolders_Task=fake_search)
    )
    monkeypatch.setattr(dsb, "find_datastore_by_name", lambda si, name: fake_ds)

    result = dsb.browse_datastore(object(), "ds1", pattern="*.ova")
    assert result == []

    queries = captured["spec"].query
    assert any(type(q) is vim.host.DatastoreBrowser.Query for q in queries), (
        "search_spec.query must contain the generic "
        "vim.host.DatastoreBrowser.Query — without it .ova/.ovf files are "
        f"never returned. Got: {[type(q).__name__ for q in queries]}"
    )
    # The specific queries must still be present (folder recursion, ISO, VMDK).
    for qtype in (
        vim.host.DatastoreBrowser.IsoImageQuery,
        vim.host.DatastoreBrowser.VmDiskQuery,
        vim.host.DatastoreBrowser.FolderQuery,
    ):
        assert any(type(q) is qtype for q in queries)


def test_pyvmomi_has_no_filequery_alias():
    """Guard against 'fixing' this with the nonexistent FileQuery name."""
    assert not hasattr(vim.host.DatastoreBrowser, "FileQuery")
    assert hasattr(vim.host.DatastoreBrowser, "Query")


# ── Fix 2: risk levels + dry_run on MCP write tools ────────────────────────

_WRITE_TOOLS = (
    "storage_iscsi_enable",
    "storage_iscsi_add_target",
    "storage_iscsi_remove_target",
    "storage_rescan",
)


def test_remove_target_risk_level_is_high():
    from mcp_server import server

    assert server.storage_iscsi_remove_target._risk_level == "high", (
        "storage_iscsi_remove_target is destructive (LUNs become inaccessible) "
        "and must be risk_level='high' to hit the policy confirmation gate."
    )


def test_iscsi_status_risk_level_is_low():
    from mcp_server import server

    assert server.storage_iscsi_status._risk_level == "low", (
        "storage_iscsi_status is read-only and must be risk_level='low'."
    )


@pytest.mark.parametrize("tool_name", _WRITE_TOOLS)
def test_write_tools_accept_dry_run(tool_name):
    import inspect

    from mcp_server import server

    fn = getattr(server, tool_name)
    sig = inspect.signature(inspect.unwrap(fn))
    assert "dry_run" in sig.parameters, f"{tool_name} must accept dry_run"
    param = sig.parameters["dry_run"]
    assert param.default is False
    assert param.annotation in (bool, "bool")


@pytest.mark.parametrize(
    ("tool_name", "kwargs"),
    [
        ("storage_iscsi_enable", {"host_name": "esxi-01"}),
        ("storage_iscsi_add_target", {"host_name": "esxi-01", "address": "10.0.0.1"}),
        ("storage_iscsi_remove_target", {"host_name": "esxi-01", "address": "10.0.0.1"}),
        ("storage_rescan", {"host_name": "esxi-01"}),
    ],
)
def test_dry_run_previews_without_connecting(tool_name, kwargs, monkeypatch):
    """dry_run=True must return a preview and never touch the connection."""
    import inspect

    from mcp_server import server

    def explode(*a, **k):  # pragma: no cover - failure path
        raise AssertionError("dry_run must not open a vSphere connection")

    monkeypatch.setattr(server, "_get_connection", explode)
    fn = inspect.unwrap(getattr(server, tool_name))
    result = fn(dry_run=True, **kwargs)
    assert isinstance(result, str)
    assert result.startswith("[DRY-RUN]")
    assert "No changes made" in result


# ── Fix 3: audit failure must not flip a successful write ──────────────────


def test_audit_oserror_does_not_flip_tool_success(monkeypatch):
    """If the operation succeeded but audit logging raises OSError, the tool
    must still report the operation's success string."""
    import inspect

    from mcp_server import server

    monkeypatch.setattr(server, "_get_connection", lambda target=None: object())
    success_msg = "Software iSCSI enabled on host 'esxi-01'."
    monkeypatch.setattr(server, "enable_software_iscsi", lambda si, host: success_msg)

    def broken_log(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(server._audit, "log", broken_log)

    fn = inspect.unwrap(server.storage_iscsi_enable)
    result = fn(host_name="esxi-01")
    assert result == "Software iSCSI enabled on host 'esxi-01'."
    assert not result.startswith("Error")


def test_safe_audit_swallows_any_audit_exception(monkeypatch):
    from mcp_server import server

    def broken_log(**kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(server._audit, "log", broken_log)
    # Must not raise.
    server._safe_audit(target="t", operation="op", resource="r", parameters={}, result="ok")


def test_audit_logger_write_failure_degrades_to_stderr(tmp_path, capsys):
    """AuditLogger.log() must never raise on file I/O failure (踩坑 family
    rule: audit failure never blocks the main operation)."""
    from vmware_storage.notify.audit import AuditLogger

    # Make the log path a directory so open(..., "a") raises OSError.
    log_path = tmp_path / "audit.log"
    log_path.mkdir()
    logger = AuditLogger(log_file=str(log_path))
    logger.log(target="t", operation="op", resource="r", parameters={}, result="ok")
    captured = capsys.readouterr()
    assert "audit log write failed" in captured.err


def test_audit_logger_init_failure_degrades_to_stderr(tmp_path, capsys):
    """Constructor must survive an un-creatable log directory."""
    from vmware_storage.notify.audit import AuditLogger

    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    # parent of log file is a regular file → mkdir raises OSError
    AuditLogger(log_file=str(blocker / "sub" / "audit.log"))
    captured = capsys.readouterr()
    assert "audit log dir setup failed" in captured.err


# ── Fix 4: SKILL.md tool-count parity (11 tools, 7R/4W) ─────────────────────


def _list_tools():
    from mcp_server.server import mcp

    return asyncio.run(mcp.list_tools())


def test_mcp_exposes_11_tools_7_read_4_write():
    tools = _list_tools()
    assert len(tools) == 11, f"Expected 11 MCP tools, got {len(tools)}: {[t.name for t in tools]}"
    read = [t.name for t in tools if t.annotations and t.annotations.readOnlyHint]
    write = [t.name for t in tools if not (t.annotations and t.annotations.readOnlyHint)]
    assert len(read) == 7, f"Expected 7 read tools, got {len(read)}: {read}"
    assert len(write) == 4, f"Expected 4 write tools, got {len(write)}: {write}"
    assert sorted(write) == sorted(_WRITE_TOOLS)


def test_skill_md_claims_match_actual_tool_counts():
    skill_md = (REPO_ROOT / "skills" / "vmware-storage" / "SKILL.md").read_text()
    tools = _list_tools()
    read = sum(1 for t in tools if t.annotations and t.annotations.readOnlyHint)
    write = len(tools) - read
    expected = f"({len(tools)} — {read} read, {write} write)"
    assert expected in skill_md, (
        f"SKILL.md must declare '{expected}' in the MCP Tools heading "
        "(踩坑 #34: declared counts must match the actual exposed surface)."
    )
    assert "6 read, 5 write" not in skill_md
    assert "6 of 11 tools" not in skill_md


# ── Fix 5: vSAN capacity must not fake zeros when datastore missing ────────


def test_vsan_capacity_missing_datastore_is_explicit(monkeypatch):
    from vmware_storage.ops import vsan

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
    assert result["total_gb"] is None
    assert result["used_gb"] is None
    assert result["free_gb"] is None
    assert result["usage_pct"] is None
    assert "no vSAN datastore" in result["message"]


def test_vsan_capacity_normal_path_still_works(monkeypatch):
    from vmware_storage.ops import vsan

    gib = 1024**3
    fake_cluster = SimpleNamespace(
        configurationEx=SimpleNamespace(vsanConfigInfo=SimpleNamespace(enabled=True)),
        datastore=[
            SimpleNamespace(
                summary=SimpleNamespace(type="vsan", capacity=100 * gib, freeSpace=40 * gib),
                name="vsanDatastore",
            )
        ],
    )
    monkeypatch.setattr(vsan, "find_cluster_by_name", lambda si, name: fake_cluster)

    result = vsan.get_vsan_capacity(object(), "prod-cluster")
    assert result["datastore_name"] == "vsanDatastore"
    assert result["total_gb"] == 100.0
    assert result["free_gb"] == 40.0
    assert result["used_gb"] == 60.0
    assert result["usage_pct"] == 60.0


# ── Fix 6: CLI prints teaching error, no traceback ──────────────────────────


def test_cli_expected_error_no_traceback(monkeypatch):
    from typer.testing import CliRunner

    from vmware_storage import cli
    from vmware_storage.ops.iscsi_config import ISCSIError

    def boom(target, config_path=None):
        raise ISCSIError(
            "Software iSCSI is not enabled on host 'esxi-01'. "
            "Enable it first with: vmware-storage iscsi enable"
        )

    monkeypatch.setattr(cli, "_get_connection", boom)
    result = CliRunner().invoke(cli.app, ["iscsi", "status", "esxi-01"])
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "not enabled on host" in result.output
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, ISCSIError)


def test_cli_value_error_no_traceback(monkeypatch):
    from typer.testing import CliRunner

    from vmware_storage import cli

    def boom(target, config_path=None):
        raise ValueError("Datastore 'ds-x' not found. Did you mean 'ds-1'?")

    monkeypatch.setattr(cli, "_get_connection", boom)
    result = CliRunner().invoke(cli.app, ["datastore", "browse", "ds-x"])
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "Did you mean" in result.output
    assert "Traceback" not in result.output


def test_all_cli_commands_are_wrapped():
    """Every leaf command must go through handle_cli_errors."""
    from vmware_storage import cli

    for sub_app in (cli.app, cli.ds_app, cli.iscsi_app, cli.vsan_app):
        for cmd in sub_app.registered_commands:
            fn = cmd.callback
            assert getattr(fn, "__wrapped__", None) is not None, (
                f"CLI command '{fn.__name__}' is not wrapped by handle_cli_errors"
            )


# ── Fix 7: teaching not-found hints ─────────────────────────────────────────


def test_not_found_hint_close_match():
    from vmware_storage.ops.inventory import not_found_hint

    hint = not_found_hint("esxi-1", ["esxi-01", "esxi-02"])
    assert "Did you mean 'esxi-01'?" in hint


def test_not_found_hint_lists_available_when_no_close_match():
    from vmware_storage.ops.inventory import not_found_hint

    hint = not_found_hint("zzz", ["alpha", "beta"])
    assert "Available: alpha, beta" in hint


def test_not_found_hint_empty_inventory():
    from vmware_storage.ops.inventory import not_found_hint

    assert not_found_hint("anything", []) == ""


def test_not_found_hint_truncates_long_inventories():
    from vmware_storage.ops.inventory import not_found_hint

    names = [f"zzz-host-{i:02d}" for i in range(25)]
    hint = not_found_hint("qqq", names, limit=10)
    assert "15 more" in hint


# ── Fix 8: list_cached_images error hint points at local cache ─────────────


def test_list_cached_images_hint_mentions_rescan(monkeypatch):
    import inspect

    from mcp_server import server

    def boom(image_type=None, datastore=None):
        raise ValueError("registry corrupt")

    monkeypatch.setattr(server.datastore_browser, "list_images", boom)
    fn = inspect.unwrap(server.list_cached_images)
    result = fn()
    assert "re-run scan_datastore_images" in result[0]["hint"]
    # The old hint pointed users at connectivity diagnostics, which is wrong
    # for a local-cache-only tool.
    assert "doctor" not in result[0]["hint"]
