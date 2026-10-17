"""browse_datastore must see every file, and treat a leading '/' one way.

Real hardware, 2026-10-17: vmware-aiops' search spec listed only IsoImageQuery,
VmDiskQuery and FolderQuery under a comment saying "include all file types".
A DatastoreBrowser query is a filter, not a hint -- with those three, .ova /
.ovf / .vmx / .log files are never returned. On the lab vCenter it found 0 of
the 2 OVAs on 'datastore1 (1)' that vmware-storage (which also passes the
generic Query) found, so scan_all_datastore_images could not do the one job its
description gives it: finding an OVA for deploy_vm_from_ova.

The twin also drifted on a leading slash: vmware-aiops passed '/vcsa8' through,
vmware-storage refused it as "absolute" (its 1.5.35 hardening). Both now refuse
it, and the error names the corrected path.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyVmomi import vim

from vmware_storage.ops import datastore_browser as dsb


def _browse(monkeypatch, path=""):
    seen: dict = {}

    def fake_search(datastorePath, searchSpec):  # noqa: N803 -- pyVmomi API names
        seen["path"] = datastorePath
        seen["spec"] = searchSpec
        return SimpleNamespace(
            info=SimpleNamespace(state=vim.TaskInfo.State.success, result=[], error=None)
        )

    monkeypatch.setattr(
        dsb,
        "find_datastore_by_name",
        lambda si, name: SimpleNamespace(
            browser=SimpleNamespace(SearchDatastoreSubFolders_Task=fake_search)
        ),
    )
    dsb.browse_datastore(object(), "datastore1", path=path, pattern="*.ova")
    return seen


@pytest.mark.unit
def test_search_spec_includes_the_generic_file_query(monkeypatch) -> None:
    spec = _browse(monkeypatch)["spec"]
    # Exact type: every specific query subclasses Query, so isinstance would pass
    # for the three that hid the OVAs.
    assert any(type(q) is vim.host.DatastoreBrowser.Query for q in spec.query)


@pytest.mark.unit
def test_relative_path_is_joined_under_the_datastore(monkeypatch) -> None:
    assert _browse(monkeypatch, "vcsa8")["path"] == "[datastore1] vcsa8"


@pytest.mark.unit
@pytest.mark.parametrize("path", ["/vcsa8", "//vcsa8"])
def test_leading_slash_is_refused_with_the_fix(monkeypatch, path) -> None:
    with pytest.raises(ValueError, match="path='vcsa8'"):
        _browse(monkeypatch, path)


@pytest.mark.unit
@pytest.mark.parametrize("path", ["../other", "a/../../b", "x\x00y", "\\\\server\\share", "/"])
def test_traversal_and_odd_paths_are_refused(monkeypatch, path) -> None:
    with pytest.raises(ValueError, match="datastore-relative"):
        _browse(monkeypatch, path)
