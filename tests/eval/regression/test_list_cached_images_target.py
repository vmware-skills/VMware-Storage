"""list_cached_images can answer for one target (review 2026-10-18).

Rows carry ``target`` since 2026-10-17 so two hosts' "datastore1" stay apart,
but the tool had no way to ask for one target's rows: a caller saw every
target's images mixed together and had to filter by hand.
"""

from __future__ import annotations

import json

import pytest

from vmware_storage.config import AppConfig, TargetConfig
from vmware_storage.connection import ConnectionManager
from vmware_storage.mcp_server import server
from vmware_storage.ops import datastore_browser as dsb

_ROWS = [
    {"target": "vc", "datastore": "datastore1", "name": "a.iso",
     "ds_path": "[datastore1] a.iso", "size_mb": 1.0, "type": "File", "modified": ""},
    {"target": "esx", "datastore": "datastore1", "name": "b.ova",
     "ds_path": "[datastore1] b.ova", "size_mb": 1.0, "type": "File", "modified": ""},
    # Written before targets were recorded: it cannot be attributed to either.
    {"datastore": "datastore1", "name": "old.iso", "ds_path": "[datastore1] old.iso",
     "size_mb": 1.0, "type": "File", "modified": ""},
]


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "image_registry.json"
    path.write_text(json.dumps({"images": _ROWS, "last_scan": None}), encoding="utf-8")
    monkeypatch.setattr(dsb, "IMAGE_REGISTRY_FILE", path)
    monkeypatch.setattr(dsb, "CONFIG_DIR", tmp_path)
    return path


def _with_targets(monkeypatch, *names: str) -> None:
    cfg = AppConfig(targets=tuple(
        TargetConfig(name=n, host=f"{n}.example.com", config_username="u") for n in names))
    mgr = ConnectionManager(cfg)
    monkeypatch.setattr(server, "_get_conn_mgr", lambda: mgr)


@pytest.mark.unit
def test_ops_filter_returns_only_that_targets_rows(registry) -> None:
    out = dsb.list_images(target="esx")
    assert [i["name"] for i in out["items"]] == ["b.ova"]
    assert out["total"] == 1


@pytest.mark.unit
def test_ops_filter_combines_with_the_other_filters(registry) -> None:
    assert dsb.list_images(target="vc", image_type="ova")["items"] == []
    assert [i["name"] for i in dsb.list_images(target="vc", datastore="datastore1")["items"]] == [
        "a.iso"]


@pytest.mark.unit
def test_tool_with_target_returns_only_that_target(registry, monkeypatch) -> None:
    _with_targets(monkeypatch, "vc", "esx")
    out = server.list_cached_images(target="vc")
    assert [i["name"] for i in out["items"]] == ["a.iso"]


@pytest.mark.unit
def test_tool_without_target_is_unchanged_and_needs_no_config(registry, monkeypatch) -> None:
    def no_config():
        raise AssertionError("an unfiltered cache read must not load config.yaml")

    monkeypatch.setattr(server, "_get_conn_mgr", no_config)
    out = server.list_cached_images()
    assert sorted(i["name"] for i in out["items"]) == ["a.iso", "b.ova", "old.iso"]


@pytest.mark.unit
def test_tool_with_an_unknown_target_names_the_configured_ones(registry, monkeypatch) -> None:
    _with_targets(monkeypatch, "vc", "esx")
    out = server.list_cached_images(target="nope")
    assert "error" in out
    assert "nope" in out["error"] and "vc" in out["error"]
