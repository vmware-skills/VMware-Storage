"""2.0.0: MCP tools renamed verb-first, plus the three parameter changes.

The spec is the family's ``design/tool-rename-2.0.json`` (vmware-storage
entry and its ``_param_changes``). Three things can go wrong silently:

* a tool keeps its old name, or is registered under both names;
* a renamed tool shares its name with the ops function it wraps and, without
  the ``_ops_`` import alias, calls itself instead of vSphere;
* ``browse_datastore`` / ``scan_datastore_images`` keep ``ds_name`` (AIops'
  ``browse_datastore`` takes ``datastore_name``; one name, one schema), or
  ``list_all_datastores`` lacks Monitor's ``limit``.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from vmware_storage.mcp_server import server
from vmware_storage.ops import inventory

RENAMES = {
    "fc_adapter_list": "list_fc_adapters",
    "storage_device_paths": "list_storage_device_paths",
    "storage_iscsi_add_target": "add_iscsi_target",
    "storage_iscsi_enable": "enable_iscsi",
    "storage_iscsi_remove_target": "remove_iscsi_target",
    "storage_iscsi_status": "get_iscsi_status",
    "storage_rescan": "rescan_storage",
    "vsan_capacity": "get_vsan_capacity",
    "vsan_efficiency": "get_vsan_efficiency",
    "vsan_health": "get_vsan_health",
}


def _tools() -> dict:
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


@pytest.mark.unit
def test_only_new_names_are_registered() -> None:
    names = set(_tools())
    assert set(RENAMES.values()) <= names, set(RENAMES.values()) - names
    assert not (set(RENAMES) & names), set(RENAMES) & names


@pytest.mark.unit
@pytest.mark.parametrize("new", sorted(RENAMES.values()))
def test_renamed_tool_does_not_call_itself(new: str) -> None:
    """The tool body must reach the ops layer, never the tool's own name."""
    fn = inspect.unwrap(getattr(server, new))
    called = set(fn.__code__.co_names)
    assert new not in called, f"{new} calls {new} — the ops import is shadowed"


@pytest.mark.unit
@pytest.mark.parametrize("tool", ["browse_datastore", "scan_datastore_images"])
def test_datastore_tools_take_datastore_name(tool: str) -> None:
    props = _tools()[tool].inputSchema["properties"]
    assert "datastore_name" in props
    assert "ds_name" not in props
    assert "datastore_name" in _tools()[tool].inputSchema.get("required", [])


@pytest.mark.unit
def test_list_all_datastores_takes_optional_limit() -> None:
    schema = _tools()["list_all_datastores"].inputSchema
    assert "limit" in schema["properties"]
    assert "limit" not in schema.get("required", [])


def _stub(monkeypatch, names: list[str]) -> None:
    props = {
        "summary.type": "VMFS",
        "summary.capacity": 10 * 1024**3,
        "summary.freeSpace": 5 * 1024**3,
        "summary.accessible": True,
        "summary.url": "u",
    }
    monkeypatch.setattr(
        inventory, "_collect",
        lambda si, types, paths: [(object(), {**props, "name": n}) for n in names],
    )


@pytest.mark.unit
def test_limit_none_returns_every_datastore(monkeypatch) -> None:
    _stub(monkeypatch, ["c", "a", "b"])
    out = inventory.list_datastores(object())
    assert [r["name"] for r in out["items"]] == ["a", "b", "c"]
    assert out["total"] == 3 and out["truncated"] is False


@pytest.mark.unit
def test_limit_cuts_rows_but_keeps_the_real_total(monkeypatch) -> None:
    _stub(monkeypatch, ["c", "a", "b"])
    out = inventory.list_datastores(object(), limit=2)
    assert [r["name"] for r in out["items"]] == ["a", "b"]
    assert out["returned"] == 2
    assert out["total"] == 3
    assert out["truncated"] is True


@pytest.mark.unit
def test_tool_passes_limit_through(monkeypatch) -> None:
    _stub(monkeypatch, ["a", "b", "c"])
    monkeypatch.setattr(server, "_get_connection", lambda target=None: object())
    out = inspect.unwrap(server.list_all_datastores)(limit=1)
    assert out["returned"] == 1 and out["total"] == 3
