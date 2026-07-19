"""List tools state their own completeness instead of leaving it inferred.

Source: VMware-AIops issue #31. Running the family against a local Llama 3.3
70B, the operator reported that "with long tool responses, it may omit existing
information or incorrectly state that no data was returned." A bare
``list[dict]`` gives a model no way to tell a whole answer from page one, so it
guesses — and a guess that reads "no data" looks like a finding.

The four read list tools here return the family envelope. Storage enumerates
each collection in full (PropertyCollector walk, datastore browse task, or the
on-disk registry), so ``total`` is the real count and ``truncated`` is always
False — completeness stated outright rather than left to the model.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pyVmomi import vim

from vmware_storage.ops import datastore_browser as dsb
from vmware_storage.ops import inventory

ENVELOPE_KEYS = {"items", "returned", "limit", "total", "truncated", "hint"}

GB = 1024**3


# ---------------------------------------------------------------------------
# Fakes — enough vSphere shape for the two collection walks
# ---------------------------------------------------------------------------


def _ds_props(name: str) -> dict:
    return {
        "name": name,
        "summary.type": "VMFS",
        "summary.capacity": 10 * GB,
        "summary.freeSpace": 5 * GB,
        "summary.accessible": True,
        "summary.url": "u",
    }


def _stub_collect(monkeypatch, names: list[str]) -> None:
    monkeypatch.setattr(
        inventory,
        "_collect",
        lambda si, types, paths: [(object(), _ds_props(n)) for n in names],
    )


def _stub_browse(monkeypatch, file_names: list[str]) -> None:
    files = [
        SimpleNamespace(path=n, fileSize=1024 * 1024, modification="2026-06-01")
        for n in file_names
    ]
    folder = SimpleNamespace(folderPath="[ds1] ", file=files)

    def fake_search(datastorePath, searchSpec):  # noqa: N803 — pyVmomi API names
        return SimpleNamespace(
            info=SimpleNamespace(
                state=vim.TaskInfo.State.success, result=[folder], error=None
            )
        )

    monkeypatch.setattr(
        dsb,
        "find_datastore_by_name",
        lambda si, name: SimpleNamespace(
            browser=SimpleNamespace(SearchDatastoreSubFolders_Task=fake_search)
        ),
    )


def _stub_registry(monkeypatch, tmp_path, images: list[dict]) -> None:
    registry = tmp_path / "image_registry.json"
    registry.write_text(json.dumps({"images": images, "last_scan": None}))
    monkeypatch.setattr(dsb, "IMAGE_REGISTRY_FILE", registry)


# ---------------------------------------------------------------------------
# Shape — the six keys are the contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_datastores_carries_every_envelope_key(monkeypatch) -> None:
    """Explicit nulls, never missing keys — a missing key invites invention."""
    _stub_collect(monkeypatch, ["ds-a"])
    assert ENVELOPE_KEYS <= set(inventory.list_datastores(object()))


@pytest.mark.unit
def test_browse_datastore_carries_every_envelope_key(monkeypatch) -> None:
    _stub_browse(monkeypatch, ["app.ova"])
    assert ENVELOPE_KEYS <= set(dsb.browse_datastore(object(), "ds1"))


@pytest.mark.unit
def test_scan_images_carries_every_envelope_key(monkeypatch) -> None:
    _stub_browse(monkeypatch, ["app.ova"])
    assert ENVELOPE_KEYS <= set(dsb.scan_images(object(), "ds1"))


@pytest.mark.unit
def test_list_images_carries_every_envelope_key(monkeypatch, tmp_path) -> None:
    _stub_registry(monkeypatch, tmp_path, [{"name": "a.ova", "datastore": "ds1"}])
    assert ENVELOPE_KEYS <= set(dsb.list_images())


# ---------------------------------------------------------------------------
# Completeness — the whole point of the envelope on an un-paged read
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_datastores_reports_a_real_total_and_no_truncation(monkeypatch) -> None:
    """The PropertyCollector walk sees every datastore before returning."""
    _stub_collect(monkeypatch, ["ds-a", "ds-b", "ds-c"])
    result = inventory.list_datastores(object())
    assert result["returned"] == 3
    assert result["total"] == 3
    assert result["truncated"] is False
    assert result["hint"] is None
    assert result["limit"] is None


@pytest.mark.unit
def test_browse_datastore_reports_a_real_total_and_no_truncation(monkeypatch) -> None:
    _stub_browse(monkeypatch, ["app.ova", "boot.iso"])
    result = dsb.browse_datastore(object(), "ds1")
    assert result["returned"] == 2
    assert result["total"] == 2
    assert result["truncated"] is False
    assert result["hint"] is None


@pytest.mark.unit
def test_list_images_reports_a_real_total_and_no_truncation(
    monkeypatch, tmp_path
) -> None:
    images = [
        {"name": "a.ova", "datastore": "ds1"},
        {"name": "b.iso", "datastore": "ds1"},
    ]
    _stub_registry(monkeypatch, tmp_path, images)
    result = dsb.list_images()
    assert result["returned"] == 2
    assert result["total"] == 2
    assert result["truncated"] is False


@pytest.mark.unit
def test_list_images_total_follows_the_filter(monkeypatch, tmp_path) -> None:
    """A filtered listing's total is the matching count, not the registry size."""
    images = [
        {"name": "a.ova", "datastore": "ds1"},
        {"name": "b.iso", "datastore": "ds1"},
        {"name": "c.ova", "datastore": "ds2"},
    ]
    _stub_registry(monkeypatch, tmp_path, images)
    result = dsb.list_images(image_type="ova")
    assert result["total"] == 2
    assert result["returned"] == 2
    assert result["truncated"] is False


# ---------------------------------------------------------------------------
# Empty is a stated zero, not an absence
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_datastore_list_is_an_explicit_zero(monkeypatch) -> None:
    """"No datastores" must not read the same as "the call failed"."""
    _stub_collect(monkeypatch, [])
    result = inventory.list_datastores(object())
    assert result["items"] == []
    assert result["returned"] == 0
    assert result["total"] == 0
    assert result["truncated"] is False


@pytest.mark.unit
def test_empty_registry_is_an_explicit_zero(monkeypatch, tmp_path) -> None:
    _stub_registry(monkeypatch, tmp_path, [])
    result = dsb.list_images()
    assert result["items"] == []
    assert result["total"] == 0
    assert result["truncated"] is False
