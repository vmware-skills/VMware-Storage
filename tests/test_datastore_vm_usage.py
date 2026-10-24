"""get_datastore_vm_usage: per-VM committed space on one datastore.

2026-10-24 conversation round (storage-datastore-top-consumers): the only route
to "which VMs take the most" was summing VMDK sizes per folder from
browse_datastore, which are provisioned sizes, keyed by folder not VM, and
counted an orphaned nested copy as the VM's.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from vmware_storage.ops import datastore_usage as ops

GB = 1024**3


class _MO(SimpleNamespace):
    """A managed object as far as this module looks: just its _moId."""


DS1 = _MO(_moId="datastore-1", summary=SimpleNamespace(capacity=800 * GB, freeSpace=200 * GB, type="VMFS"))
DS2 = _MO(_moId="datastore-2")


def _usage(ds, committed, uncommitted=0, unshared=None):
    return SimpleNamespace(datastore=ds, committed=committed, uncommitted=uncommitted,
                           unshared=committed if unshared is None else unshared)


def _vm(moid, name, datastores, usages, template=False):
    return (_MO(_moId=moid), {
        "name": name,
        "datastore": datastores,
        "storage.perDatastoreUsage": usages,
        "storage.timestamp": dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.timezone.utc),
        "config.template": template,
    })


@pytest.fixture
def lab(monkeypatch):
    vms = [
        _vm("vm-1", "Log-Insight", [DS1], [_usage(DS1, 110 * GB, 5 * GB)]),
        _vm("vm-2", "vcsa8", [DS1, DS2], [_usage(DS2, 300 * GB), _usage(DS1, 50 * GB, 200 * GB)]),
        _vm("vm-3", "elsewhere", [DS2], [_usage(DS2, 99 * GB)]),
        _vm("vm-4", "inaccessible", [DS1], []),
        _vm("vm-5", "test-llm", [DS1], [_usage(DS1, 40 * GB, 10 * GB)]),
    ]
    monkeypatch.setattr(ops, "find_datastore_by_name", lambda si, n: DS1 if n == "datastore1" else None)
    monkeypatch.setattr(ops, "_collect", lambda si, t, paths: vms)
    monkeypatch.setattr(ops, "list_datastores", lambda si: {"items": [{"name": "datastore1"}, {"name": "datastore1 (1)"}]})


def test_vms_on_the_datastore_ranked_by_committed(lab):
    out = ops.datastore_vm_usage(object(), "datastore1")
    assert [r["name"] for r in out["items"]] == ["Log-Insight", "vcsa8", "test-llm"]
    # vcsa8's 300 GB on the other datastore is not counted here.
    assert out["items"][1]["committed_gb"] == 50.0 and out["items"][1]["uncommitted_gb"] == 200.0
    assert out["total"] == 3 and out["truncated"] is False


def test_space_no_vm_accounts_for_is_reported_not_spread(lab):
    out = ops.datastore_vm_usage(object(), "datastore1")
    assert out["datastore"]["used_gb"] == 600.0
    assert out["attributed_gb"] == 200.0
    assert out["unattributed_gb"] == 400.0


def test_a_vm_without_a_usage_row_is_unknown_not_zero(lab):
    out = ops.datastore_vm_usage(object(), "datastore1")
    assert out["unmeasured_vms"] == ["inaccessible"]
    assert "inaccessible" not in [r["name"] for r in out["items"]]
    assert "unknown, not zero" in out["note"]


def test_top_n_truncates_and_says_so(lab):
    out = ops.datastore_vm_usage(object(), "datastore1", top_n=1)
    assert [r["name"] for r in out["items"]] == ["Log-Insight"]
    assert out["returned"] == 1 and out["total"] == 3 and out["truncated"] is True


def test_unknown_datastore_names_the_close_match(lab):
    with pytest.raises(ValueError, match="Did you mean 'datastore1"):
        ops.datastore_vm_usage(object(), "datastore-1")


def test_top_n_below_one_is_refused(lab):
    with pytest.raises(ValueError, match="top_n must be at least 1"):
        ops.datastore_vm_usage(object(), "datastore1", top_n=0)


def test_shared_disks_do_not_make_unattributed_negative(monkeypatch):
    vms = [
        _vm("vm-1", "parent", [DS1], [_usage(DS1, 400 * GB)]),
        _vm("vm-2", "clone", [DS1], [_usage(DS1, 400 * GB, unshared=1 * GB)]),
    ]
    monkeypatch.setattr(ops, "find_datastore_by_name", lambda si, n: DS1)
    monkeypatch.setattr(ops, "_collect", lambda si, t, paths: vms)
    out = ops.datastore_vm_usage(object(), "datastore1")
    assert out["attributed_gb"] == 800.0 and out["datastore"]["used_gb"] == 600.0
    assert out["unattributed_gb"] == 0.0
    assert "linked clones" in out["note"]
