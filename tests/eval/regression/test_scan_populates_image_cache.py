"""scan_datastore_images must fill the cache that list_cached_images reads.

Real hardware, 2026-10-17: list_cached_images was always empty. Its description
says the registry is "populated by prior datastore scans", but the only writer,
``update_registry()``, had no caller anywhere -- no MCP tool, no CLI command --
so the tool could never return anything, and SKILL.md told agents to prefer it
over re-scanning.
"""

from __future__ import annotations

import json

import pytest

from vmware_storage.ops import datastore_browser as dsb


def _img(ds: str, rel: str) -> dict:
    return {
        "name": rel.rsplit("/", 1)[-1],
        "size_mb": 1.0,
        "type": "File",
        "modified": "",
        "ds_path": f"[{ds}] {rel}",
    }


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "image_registry.json"
    monkeypatch.setattr(dsb, "IMAGE_REGISTRY_FILE", path)
    monkeypatch.setattr(dsb, "CONFIG_DIR", tmp_path)
    return path


def _scan_returns(monkeypatch, rows_by_pattern: dict[str, list[dict]]) -> None:
    def fake_browse(si, ds_name, path="", pattern="*"):
        rows = rows_by_pattern.get(pattern, [])
        return {"items": rows, "total": len(rows)}

    monkeypatch.setattr(dsb, "browse_datastore", fake_browse)


@pytest.mark.unit
def test_scan_then_list_cached_returns_what_was_scanned(registry, monkeypatch) -> None:
    _scan_returns(monkeypatch, {"*.ova": [_img("ds1", "ec-wrkr.ova")]})

    dsb.scan_images(object(), "ds1")
    cached = dsb.list_images(image_type="ova")

    assert [i["ds_path"] for i in cached["items"]] == ["[ds1] ec-wrkr.ova"]
    assert cached["items"][0]["datastore"] == "ds1"
    assert json.loads(registry.read_text(encoding="utf-8"))["last_scan"]


@pytest.mark.unit
def test_rescan_replaces_that_datastore_and_keeps_the_others(registry, monkeypatch) -> None:
    _scan_returns(monkeypatch, {"*.ova": [_img("ds1", "old.ova")]})
    dsb.scan_images(object(), "ds1")
    _scan_returns(monkeypatch, {"*.iso": [_img("ds2", "x.iso")]})
    dsb.scan_images(object(), "ds2")
    _scan_returns(monkeypatch, {"*.ova": [_img("ds1", "new.ova")]})
    dsb.scan_images(object(), "ds1")

    paths = sorted(i["ds_path"] for i in dsb.list_images()["items"])
    assert paths == ["[ds1] new.ova", "[ds2] x.iso"]


@pytest.mark.unit
def test_subfolder_scan_only_replaces_that_subfolder(registry, monkeypatch) -> None:
    _scan_returns(monkeypatch, {"*.ova": [_img("ds1", "a/one.ova"), _img("ds1", "b/two.ova")]})
    dsb.scan_images(object(), "ds1")
    _scan_returns(monkeypatch, {"*.ova": [_img("ds1", "a/three.ova")]})
    dsb.scan_images(object(), "ds1", path="a")

    paths = sorted(i["ds_path"] for i in dsb.list_images()["items"])
    assert paths == ["[ds1] a/three.ova", "[ds1] b/two.ova"]


@pytest.mark.unit
def test_an_unwritable_cache_does_not_fail_the_scan(registry, monkeypatch) -> None:
    _scan_returns(monkeypatch, {"*.ova": [_img("ds1", "x.ova")]})

    def boom(_registry):
        raise OSError("read-only file system")

    monkeypatch.setattr(dsb, "_save_registry", boom)
    result = dsb.scan_images(object(), "ds1")

    assert result["total"] == 1
    assert "cache_note" in result


# ── independent review, 2026-10-17 ───────────────────────────────────────────


@pytest.mark.unit
def test_a_corrupt_cache_does_not_fail_the_scan(registry, monkeypatch) -> None:
    registry.write_text("{not json", encoding="utf-8")
    _scan_returns(monkeypatch, {"*.ova": [_img("ds1", "x.ova")]})

    result = dsb.scan_images(object(), "ds1")

    assert result["total"] == 1 and "cache_note" in result


@pytest.mark.unit
def test_a_corrupt_cache_tells_the_reader_how_to_recover(registry) -> None:
    registry.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(dsb.DatastoreBrowseError, match="scan_datastore_images"):
        dsb.list_images()


@pytest.mark.unit
def test_the_same_datastore_name_on_two_targets_is_kept_apart(registry, monkeypatch) -> None:
    """'datastore1' exists on every ESXi host; one target's scan must not erase another's."""
    _scan_returns(monkeypatch, {"*.iso": [_img("ds1", "a.iso")]})
    dsb.scan_images(object(), "ds1", target="vc-a")
    _scan_returns(monkeypatch, {"*.iso": [_img("ds1", "b.iso")]})
    dsb.scan_images(object(), "ds1", target="esx-b")

    items = dsb.list_images()["items"]
    assert sorted((i["target"], i["ds_path"]) for i in items) == [
        ("esx-b", "[ds1] b.iso"), ("vc-a", "[ds1] a.iso")]


@pytest.mark.unit
def test_a_dot_slash_path_replaces_that_folder(registry, monkeypatch) -> None:
    _scan_returns(monkeypatch, {"*.iso": [_img("ds1", "a/old.iso")]})
    dsb.scan_images(object(), "ds1")
    _scan_returns(monkeypatch, {"*.iso": [_img("ds1", "a/new.iso")]})
    dsb.scan_images(object(), "ds1", path="./a")

    assert [i["ds_path"] for i in dsb.list_images()["items"]] == ["[ds1] a/new.iso"]


@pytest.mark.unit
def test_concurrent_scans_lose_no_entries(registry, monkeypatch) -> None:
    """With the HTTPS session pool, scans run side by side."""
    import threading

    def fake_browse(si, ds_name, path="", pattern="*"):
        rows = [_img(ds_name, "x.iso")] if pattern == "*.iso" else []
        return {"items": rows, "total": len(rows)}

    monkeypatch.setattr(dsb, "browse_datastore", fake_browse)
    threads = [threading.Thread(target=dsb.scan_images, args=(object(), f"ds{i}"))
               for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert dsb.list_images()["total"] == 20


@pytest.mark.unit
def test_the_registry_is_replaced_atomically(registry, monkeypatch) -> None:
    """A write that dies half-way must leave the previous file, not a truncated one."""
    _scan_returns(monkeypatch, {"*.iso": [_img("ds1", "keep.iso")]})
    dsb.scan_images(object(), "ds1")
    before = registry.read_text(encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(dsb.json, "dump", boom)
    monkeypatch.setattr(dsb.json, "dumps", boom)
    _scan_returns(monkeypatch, {"*.iso": [_img("ds2", "y.iso")]})
    dsb.scan_images(object(), "ds2")

    assert registry.read_text(encoding="utf-8") == before
