"""Successful JSON output must preserve coverage and local-cache evidence."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from vmware_storage import cli
from vmware_storage.ops import datastore_browser, inventory


@pytest.fixture
def connection(monkeypatch):
    si = object()
    monkeypatch.setattr(cli, "_get_connection", lambda *a: si)
    monkeypatch.setattr(cli, "load_config", lambda *a: object())
    monkeypatch.setattr(cli, "ConnectionManager", lambda *a: SimpleNamespace(
        resolve_target_name=lambda target: "lab", connect=lambda target: si))
    return si


def _envelope(items):
    return {"items": items, "returned": len(items), "limit": 2,
            "total": None, "truncated": True, "hint": "incomplete evidence"}


@pytest.mark.parametrize("command,owner,operation", [
    (["list"], inventory, "list_datastores"),
    (["browse", "ds"], datastore_browser, "browse_datastore"),
    (["scan-images", "ds"], datastore_browser, "scan_images"),
])
@pytest.mark.parametrize("items", [[], [{"name": "[SSD] 数据", "usage_pct": None}]])
def test_json_preserves_envelope_even_when_empty_or_unknown(
    monkeypatch, connection, command, owner, operation, items,
):
    envelope = _envelope(items)
    envelope["cache_note"] = "Scan succeeded but the local cache was not updated"
    calls = []

    def read(si, *a, **kw):
        assert si is connection
        calls.append(kw)
        return envelope

    monkeypatch.setattr(owner, operation, read)
    out = CliRunner().invoke(cli.app, ["datastore", *command, "--json"])
    assert out.exit_code == 0, out.output
    assert json.loads(out.stdout) == envelope
    assert len(calls) == 1


@pytest.mark.parametrize("command,operation", [
    (["browse", "ds"], "browse_datastore"), (["scan-images", "ds"], "scan_images"),
])
def test_legacy_json_arrays_are_unchanged_without_flag(monkeypatch, connection, command, operation):
    items = [{"name": "image.iso"}]
    monkeypatch.setattr(datastore_browser, operation, lambda *a, **kw: _envelope(items))
    out = CliRunner().invoke(cli.app, ["datastore", *command])
    assert out.exit_code == 0, out.output
    assert json.loads(out.stdout) == items
