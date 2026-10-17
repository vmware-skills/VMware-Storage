"""browse_datastore must return a ds_path that deploy/attach tools can use.

Regression source: real ESXi 8 host (home-esxi, 2026-10-17). The browse task
reports each folder's ``folderPath`` with no trailing separator —
``"[datastore1]"`` for the root, ``"[datastore1] backup"`` for a folder —
and the old code glued the file name straight on, producing
``"[datastore1] backupVMware-VCSA.iso"`` and ``"[datastore1]Hermers - TT"``.
Agents pass that string into deploy_vm_from_ova / attach_iso_to_vm, so a
malformed path breaks the very flow the browse exists for. The earlier tests
mocked ``"[ds1] images/"`` (trailing slash), the one shape that hid the bug.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyVmomi import vim

from vmware_storage.ops import datastore_browser as dsb


@pytest.mark.unit
@pytest.mark.parametrize(
    ("folder", "name", "expected"),
    [
        # Root, as ESXi reports it: no space after the bracket.
        ("[datastore1]", "Hermers - TT", "[datastore1] Hermers - TT"),
        ("[datastore1]", "x.iso", "[datastore1] x.iso"),
        # Root, as vCenter reports it: trailing space.
        ("[datastore1] ", "x.iso", "[datastore1] x.iso"),
        # Folder without a trailing slash (ESXi).
        ("[datastore1] backup", "x.iso", "[datastore1] backup/x.iso"),
        (
            "[datastore1] Hermers - TT",
            "Hermers - TT.vmdk",
            "[datastore1] Hermers - TT/Hermers - TT.vmdk",
        ),
        ("[datastore1] a/b", "x.iso", "[datastore1] a/b/x.iso"),
        # Folder with a trailing slash (vCenter) — must not double it.
        ("[datastore1] backup/", "x.iso", "[datastore1] backup/x.iso"),
    ],
)
def test_join_ds_path(folder, name, expected):
    assert dsb._join_ds_path(folder, name) == expected


@pytest.mark.unit
def test_browse_root_returns_well_formed_paths_for_every_folder(monkeypatch):
    """Root browse is recursive: subfolder rows come back too, each well formed."""
    def _file(path):
        return SimpleNamespace(path=path, fileSize=1024 * 1024, modification="2026-10-17")

    results = [
        SimpleNamespace(folderPath="[datastore1]", file=[_file("backup")]),
        SimpleNamespace(folderPath="[datastore1] backup", file=[_file("x.iso")]),
    ]
    seen = {}

    def fake_search(datastorePath, searchSpec):  # noqa: N803 — pyVmomi API names
        seen["path"] = datastorePath
        return SimpleNamespace(
            info=SimpleNamespace(state=vim.TaskInfo.State.success, result=results, error=None)
        )

    monkeypatch.setattr(
        dsb,
        "find_datastore_by_name",
        lambda si, name: SimpleNamespace(
            browser=SimpleNamespace(SearchDatastoreSubFolders_Task=fake_search)
        ),
    )

    rows = dsb.browse_datastore(object(), "datastore1")["items"]

    assert seen["path"] == "[datastore1]"
    assert {r["name"]: r["ds_path"] for r in rows} == {
        "backup": "[datastore1] backup",
        "x.iso": "[datastore1] backup/x.iso",
    }
