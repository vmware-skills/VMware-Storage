"""vSAN reads must not answer for hosts and datastores they never reached.

Real-hardware findings, 2026-08-30 (VCF 9.1, 4 of 8 hosts ``notResponding``):

* ``vsan_health`` returned ``disk_groups: []`` and said nothing about the four
  hosts it could not read. An empty list is indistinguishable from "this
  cluster has no disk groups", and the cluster's real ``overallHealth`` was red.
* ``vsan_capacity`` returned total/used/free all ``0`` with no message and no
  mention of accessibility — a shape that reads as a healthy, empty datastore.

Both are the same failure as the phantom-violation one in vmware-harden, minus
the fabrication: not a wrong answer, but a confident answer with the uncertainty
removed. Zero is a measurement. "Not reached" is not zero.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vmware_storage.ops import vsan as ops


def _disk(name: str, blocks: int = 1000, block_size: int = 512):
    return SimpleNamespace(
        displayName=name,
        capacity=SimpleNamespace(block=blocks, blockSize=block_size),
    )


def _host(name: str, *, state: str = "connected", readable: bool = True, dgs: int = 1):
    """A cluster host. ``readable=False`` mimics a host whose vsanSystem read
    raises, which is what an unreachable host does in practice."""
    if readable:
        mapping = [
            SimpleNamespace(ssd=_disk(f"{name}-cache"), nonSsd=[_disk(f"{name}-cap")])
            for _ in range(dgs)
        ]
        vsan_sys = SimpleNamespace(
            config=SimpleNamespace(storageInfo=SimpleNamespace(diskMapping=mapping))
        )
    else:
        class _Raises:
            @property
            def config(self):
                raise RuntimeError("host is not responding")

        vsan_sys = _Raises()
    return SimpleNamespace(
        name=name,
        runtime=SimpleNamespace(connectionState=state),
        configManager=SimpleNamespace(vsanSystem=vsan_sys),
    )


def _cluster(hosts, datastores=()):
    return SimpleNamespace(
        host=list(hosts),
        datastore=list(datastores),
        configurationEx=SimpleNamespace(
            vsanConfigInfo=SimpleNamespace(enabled=True)
        ),
    )


def _patch_cluster(monkeypatch, cluster):
    monkeypatch.setattr(ops, "_require_cluster", lambda si, name: cluster)


@pytest.mark.unit
def test_health_names_the_hosts_it_could_not_read(monkeypatch):
    cluster = _cluster(
        [
            _host("esx-01"),
            _host("esx-02", state="notResponding", readable=False),
            _host("esx-03", state="notResponding", readable=False),
        ]
    )
    _patch_cluster(monkeypatch, cluster)

    out = ops.get_vsan_health(None, "vsan-cl")

    assert out["hosts_read"] == 1
    unread = {h["host"]: h["reason"] for h in out["hosts_not_read"]}
    assert set(unread) == {"esx-02", "esx-03"}
    assert "notResponding" in " ".join(unread.values())
    assert out["disk_groups_complete"] is False
    assert "esx-02" in out["message"] and "esx-03" in out["message"], (
        "the message is the only part a chat client reliably shows; a caller "
        "reading it alone must not come away thinking the cluster was surveyed"
    )


@pytest.mark.unit
def test_health_on_a_fully_readable_cluster_says_so(monkeypatch):
    """The control. A version that always reports incompleteness would pass the
    test above and make every healthy cluster look suspect."""
    _patch_cluster(monkeypatch, _cluster([_host("esx-01"), _host("esx-02")]))

    out = ops.get_vsan_health(None, "vsan-cl")

    assert out["hosts_read"] == 2
    assert out["hosts_not_read"] == []
    assert out["disk_groups_complete"] is True
    assert len(out["disk_groups"]) == 2


@pytest.mark.unit
def test_health_reports_an_empty_but_complete_survey_as_such(monkeypatch):
    """Zero disk groups across hosts that all answered is a real finding, and
    must stay distinguishable from zero because nobody answered."""
    _patch_cluster(monkeypatch, _cluster([_host("esx-01", dgs=0)]))

    out = ops.get_vsan_health(None, "vsan-cl")

    assert out["disk_groups"] == []
    assert out["disk_groups_complete"] is True
    assert out["hosts_not_read"] == []


@pytest.mark.unit
def test_capacity_does_not_report_an_inaccessible_datastore_as_empty(monkeypatch):
    ds = SimpleNamespace(
        name="vsanDatastore",
        summary=SimpleNamespace(
            type="vsan", capacity=0, freeSpace=0, accessible=False
        ),
    )
    _patch_cluster(monkeypatch, _cluster([_host("esx-01")], [ds]))

    out = ops.get_vsan_capacity(None, "vsan-cl")

    assert out["accessible"] is False
    assert out["total_gb"] is None, (
        "0 GB is a measurement; an inaccessible datastore has not been measured"
    )
    assert out["used_gb"] is None and out["free_gb"] is None
    assert out["usage_pct"] is None
    assert "message" in out and "inaccessible" in out["message"].lower()


@pytest.mark.unit
def test_capacity_of_a_reachable_datastore_is_unchanged(monkeypatch):
    ds = SimpleNamespace(
        name="vsanDatastore",
        summary=SimpleNamespace(
            type="vsan",
            capacity=100 * 1024**3,
            freeSpace=40 * 1024**3,
            accessible=True,
        ),
    )
    _patch_cluster(monkeypatch, _cluster([_host("esx-01")], [ds]))

    out = ops.get_vsan_capacity(None, "vsan-cl")

    assert out["accessible"] is True
    assert out["total_gb"] == 100.0
    assert out["free_gb"] == 40.0
    assert out["used_gb"] == 60.0
    assert out["usage_pct"] == 60.0


@pytest.mark.unit
def test_capacity_treats_an_absent_accessible_flag_as_unknown_not_true(monkeypatch):
    """Older/partial summaries omit the flag. Absent is not a claim of health,
    but it is also not evidence of failure — so the figures stand and the
    unknown is stated rather than resolved in either direction."""
    ds = SimpleNamespace(
        name="vsanDatastore",
        summary=SimpleNamespace(
            type="vsan", capacity=100 * 1024**3, freeSpace=40 * 1024**3, accessible=None
        ),
    )
    _patch_cluster(monkeypatch, _cluster([_host("esx-01")], [ds]))

    out = ops.get_vsan_capacity(None, "vsan-cl")

    assert out["accessible"] is None
    assert out["total_gb"] == 100.0
