"""Regression: inventory reads must use PropertyCollector, never per-object
lazy property access (GitHub issue #31).

Storage's ``ops/inventory.py`` previously used ``CreateContainerView`` +
per-object attribute reads (``ds.summary``, ``host.name``, ``len(ds.vm)``),
which is one SOAP round-trip per property per object — seconds vs minutes on
large inventories, and it backs the most-called path
(``datastore_browser.scan_all_datastores``, not-found hints, iscsi/vsan hints).

These tests lock in the fix by wiring a fake ServiceInstance whose managed
objects **raise on ANY attribute access**. The only way the ops code can pass
is by going through the fake PropertyCollector (RetrievePropertiesEx + paging).
If anyone reintroduces a lazy ``obj.summary`` / ``obj.name`` read, these tests
raise immediately.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyVmomi import vim

from vmware_storage.ops import inventory

# ── fakes ───────────────────────────────────────────────────────────────────


class ExplodingMO:
    """Managed-object stand-in that raises on ANY attribute access.

    Represents an object returned by PropertyCollector. Ops code must read its
    properties from the batched propSet, never by touching the object directly.
    """

    def __init__(self, moid: str) -> None:
        object.__setattr__(self, "_moid", moid)

    def __getattr__(self, name: str):  # noqa: D401
        raise AssertionError(
            f"lazy per-object attribute access '{name}' on {self._moid} — "
            "inventory must batch via PropertyCollector (issue #31)"
        )

    def __repr__(self) -> str:
        return f"ExplodingMO({object.__getattribute__(self, '_moid')})"


class _PropVal:
    def __init__(self, name: str, val: object) -> None:
        self.name = name
        self.val = val


class _ObjContent:
    def __init__(self, obj: object, props: dict) -> None:
        self.obj = obj
        self.propSet = [_PropVal(k, v) for k, v in props.items()]


class _Batch:
    def __init__(self, objects: list, token: str | None) -> None:
        self.objects = objects
        self.token = token


class FakePropertyCollector:
    """Serves batched property results keyed by requested vim type, with paging.

    ``data[vim_type]`` is a list of ``(ExplodingMO, {path: value})``. Only the
    ``pathSet`` requested in the FilterSpec is returned (mirrors real behavior:
    unrequested/unset properties are absent). Paging is exercised with a small
    page size so ContinueRetrievePropertiesEx is covered.
    """

    def __init__(self, data: dict, page_size: int = 2) -> None:
        self._data = data
        self._page_size = page_size
        self._pending: dict[str, list] = {}
        self._seq = 0

    def _page(self, rows: list) -> _Batch:
        first = rows[: self._page_size]
        rest = rows[self._page_size :]
        token = None
        if rest:
            self._seq += 1
            token = f"tok-{self._seq}"
            self._pending[token] = rest
        return _Batch(first, token)

    def RetrievePropertiesEx(self, filter_specs, options):  # noqa: N802
        spec = filter_specs[0]
        prop_spec = spec.propSet[0]
        vim_type = prop_spec.type
        paths = set(prop_spec.pathSet)
        rows = [
            _ObjContent(obj, {k: v for k, v in props.items() if k in paths})
            for obj, props in self._data.get(vim_type, [])
        ]
        return self._page(rows)

    def ContinueRetrievePropertiesEx(self, token):  # noqa: N802
        return self._page(self._pending.pop(token))


class _FakeStub:
    """Minimal SOAP stub so a real ContainerView moref can be Destroy()'d."""

    def InvokeMethod(self, mo, info, args):  # noqa: N802 - pyVmomi contract
        return None


class _FakeViewManager:
    def CreateContainerView(self, root, types, recursive):  # noqa: N802
        # Real ContainerView moref (satisfies PropertyCollector FilterSpec
        # typing), backed by a no-op stub so _collect's Destroy() call succeeds.
        return vim.view.ContainerView("cv-fake", _FakeStub())


def make_si(data: dict, page_size: int = 2) -> object:
    """Build a fake ServiceInstance backed by FakePropertyCollector."""
    content = SimpleNamespace(
        rootFolder=object(),
        viewManager=_FakeViewManager(),
        propertyCollector=FakePropertyCollector(data, page_size=page_size),
    )
    return SimpleNamespace(RetrieveContent=lambda: content)


GB = 1024**3


# ── list_datastores ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_list_datastores_batches_and_shapes_correctly() -> None:
    data = {
        vim.Datastore: [
            (
                ExplodingMO("ds-b"),
                {
                    "name": "ds-b",
                    "summary.type": "VMFS",
                    "summary.capacity": 100 * GB,
                    "summary.freeSpace": 40 * GB,
                    "summary.accessible": True,
                    "summary.url": "ds:///vmfs/b",
                    "vm": [object(), object(), object()],
                },
            ),
            (
                ExplodingMO("ds-a"),
                {
                    "name": "ds-a",
                    "summary.type": "vsan",
                    "summary.capacity": 200 * GB,
                    "summary.freeSpace": 50 * GB,
                    "summary.accessible": True,
                    "summary.url": "",
                    "vm": [object()],
                },
            ),
        ]
    }
    result = inventory.list_datastores(make_si(data))

    # sorted by name; no vm_count key by default (opt-in)
    assert [d["name"] for d in result] == ["ds-a", "ds-b"]
    assert all("vm_count" not in d for d in result)

    ds_b = result[1]
    assert ds_b["type"] == "VMFS"
    assert ds_b["total_gb"] == 100.0
    assert ds_b["free_gb"] == 40.0
    assert ds_b["used_gb"] == 60.0
    assert ds_b["usage_pct"] == 60.0
    assert ds_b["accessible"] is True
    assert ds_b["url"] == "ds:///vmfs/b"

    # empty url stays "", not the sanitized falsy value
    assert result[0]["url"] == ""


@pytest.mark.unit
def test_list_datastores_vm_count_opt_in() -> None:
    data = {
        vim.Datastore: [
            (
                ExplodingMO("ds-1"),
                {
                    "name": "ds-1",
                    "summary.type": "VMFS",
                    "summary.capacity": 10 * GB,
                    "summary.freeSpace": 5 * GB,
                    "summary.accessible": True,
                    "summary.url": "u",
                    "vm": [object(), object()],
                },
            ),
        ]
    }
    result = inventory.list_datastores(make_si(data), include_vm_count=True)
    assert result[0]["vm_count"] == 2


@pytest.mark.unit
def test_list_datastores_handles_paging() -> None:
    data = {
        vim.Datastore: [
            (
                ExplodingMO(f"ds-{i}"),
                {
                    "name": f"ds-{i:02d}",
                    "summary.type": "VMFS",
                    "summary.capacity": 10 * GB,
                    "summary.freeSpace": 5 * GB,
                    "summary.accessible": True,
                    "summary.url": "u",
                },
            )
            for i in range(5)
        ]
    }
    # page_size=2 forces two ContinueRetrievePropertiesEx round-trips
    result = inventory.list_datastores(make_si(data, page_size=2))
    assert [d["name"] for d in result] == [f"ds-{i:02d}" for i in range(5)]


# ── list_hosts ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_list_hosts_batches_and_sorts() -> None:
    data = {
        vim.HostSystem: [
            (ExplodingMO("h-z"), {"name": "esxi-z", "runtime.connectionState": "connected"}),
            (ExplodingMO("h-a"), {"name": "esxi-a", "runtime.connectionState": "disconnected"}),
        ]
    }
    result = inventory.list_hosts(make_si(data))
    assert [h["name"] for h in result] == ["esxi-a", "esxi-z"]
    assert result[0]["connection_state"] == "disconnected"
    assert result[1]["connection_state"] == "connected"


# ── find_*_by_name ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_find_datastore_by_name_matches_in_memory() -> None:
    target = ExplodingMO("ds-target")
    data = {
        vim.Datastore: [
            (ExplodingMO("ds-other"), {"name": "other"}),
            (target, {"name": "wanted"}),
        ]
    }
    found = inventory.find_datastore_by_name(make_si(data), "wanted")
    assert found is target


@pytest.mark.unit
def test_find_datastore_by_name_returns_none_when_absent() -> None:
    data = {vim.Datastore: [(ExplodingMO("ds-1"), {"name": "ds-1"})]}
    assert inventory.find_datastore_by_name(make_si(data), "nope") is None
