"""``vsan_health`` must report the health vSAN reports, or say it did not ask.

Real-hardware finding, 2026-08-30 (VCF 9.1): the cluster's real ``overallHealth``
was **red** and the tool returned ``overall_health: "unknown"``, with a note
saying a full health check "requires VsanVcClusterHealthSystem". The note was
wrong about the premise. ``VsanVcClusterHealthSystem`` is reachable from what
this skill already carries: ``vsanapiutils`` ships inside pyvmomi, its
``GetVsanVcMos`` builds every vSAN managed object off the *existing* SOAP stub
and its cookie, and ``vsan-cluster-health-system`` sits in the same dict as the
``vsan-cluster-config-system`` this repo has been using for dedup/compression
since VCF 9.1 landed. Nothing extra to install, no second credential.

Established from pyVmomi's own type metadata rather than recalled — the
family's rule after 踩坑 #36, and the same evidence the AST safety whitelist
uses: ``vim.cluster.VsanVcClusterHealthSystem`` declares
``QueryClusterHealthSummary`` (wsdl ``VsanQueryVcClusterHealthSummary``)
returning ``vim.cluster.VsanClusterHealthSummary``, on which ``overallHealth``
is a required ``str``. ``test_the_sdk_surface_is_real`` re-checks that against
the installed pyVmomi so this cannot rot into a remembered API.

"unknown" was the worst available answer. It is a value vSAN itself can return,
so a caller could not tell "vSAN says it does not know" from "nobody asked" —
and on this estate it silently replaced *red*. The two are now distinct:
``overall_health`` carries what vSAN said, and is ``None`` with
``health_queried: False`` plus a reason when the query could not be made. The
same distinction ``disk_groups_complete`` already draws for the hosts.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from vmware_storage.ops import vsan as ops
from vmware_storage.ops.vsan import VSANError


def _host(name: str = "esx-01"):
    disk = SimpleNamespace(
        displayName=f"{name}-cache",
        capacity=SimpleNamespace(block=1000, blockSize=512),
    )
    mapping = [SimpleNamespace(ssd=disk, nonSsd=[disk])]
    return SimpleNamespace(
        name=name,
        runtime=SimpleNamespace(connectionState="connected"),
        configManager=SimpleNamespace(
            vsanSystem=SimpleNamespace(
                config=SimpleNamespace(
                    storageInfo=SimpleNamespace(diskMapping=mapping)
                )
            )
        ),
    )


def _cluster(hosts=(), enabled: bool = True):
    return SimpleNamespace(
        host=list(hosts) or [_host()],
        datastore=[],
        configurationEx=SimpleNamespace(
            vsanConfigInfo=SimpleNamespace(enabled=enabled)
        ),
    )


def _summary(overall="red", **over):
    data = {
        "overallHealth": overall,
        "overallHealthDescription": "Cluster has one or more red health checks",
        "timestamp": "2026-08-30T13:00:00Z",
        "groups": [
            SimpleNamespace(
                groupId="com.vmware.vsan.health.test.hcldbuptodate",
                groupName="Hardware compatibility",
                groupHealth="red",
            ),
            SimpleNamespace(
                groupId="com.vmware.vsan.health.test.network",
                groupName="Network",
                groupHealth="green",
            ),
        ],
    }
    data.update(over)
    return SimpleNamespace(**data)


class _HealthSystem:
    def __init__(self, summary=None, error: Exception | None = None) -> None:
        self._summary = summary
        self._error = error
        self.calls: list[dict] = []

    def QueryClusterHealthSummary(self, **kwargs):  # noqa: N802 — SDK name
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._summary


def _run(monkeypatch, health, cluster=None):
    monkeypatch.setattr(
        ops, "_require_cluster", lambda si, name: cluster or _cluster()
    )
    with patch(
        "vmware_storage.ops.vsan_sdk.managed_object", return_value=health
    ):
        return ops.get_vsan_health(None, "vsan-cl")


# ── the finding ─────────────────────────────────────────────────────────────


def test_the_health_the_sdk_reports_is_the_health_returned(monkeypatch) -> None:
    out = _run(monkeypatch, _HealthSystem(_summary("red")))

    assert out["overall_health"] == "red"
    assert out["health_queried"] is True
    assert "unknown" not in out["message"].lower()


@pytest.mark.parametrize("value", ["green", "yellow", "red", "unknown"])
def test_every_value_the_sdk_can_return_is_passed_through(
    monkeypatch, value
) -> None:
    """Including "unknown" — when vSAN says it, that is an answer, not a gap."""
    out = _run(monkeypatch, _HealthSystem(_summary(value)))
    assert out["overall_health"] == value
    assert out["health_queried"] is True


def test_the_group_results_the_docstring_promised_are_delivered(
    monkeypatch,
) -> None:
    """``test_groups`` has been in the return contract, and empty, since v1."""
    out = _run(monkeypatch, _HealthSystem(_summary()))

    groups = {g["group_name"]: g["group_health"] for g in out["test_groups"]}
    assert groups == {"Hardware compatibility": "red", "Network": "green"}


def test_the_summary_is_fetched_from_cache(monkeypatch) -> None:
    """A read-only tool must not kick off a full health run on production.

    ``fetchFromCache=False`` makes vCenter re-run every check, which takes
    minutes and loads the cluster. The cached summary is what the vCenter UI
    shows, and its age is reported so a caller can judge it.
    """
    health = _HealthSystem(_summary())
    out = _run(monkeypatch, health)

    assert health.calls[0]["fetchFromCache"] is True
    assert out["health_checked_at"] == "2026-08-30T13:00:00Z"


# ── the control: not asking must not look like an answer ────────────────────


def test_a_failed_health_query_is_not_reported_as_a_health_value(
    monkeypatch,
) -> None:
    out = _run(
        monkeypatch, _HealthSystem(error=RuntimeError("vsan health service down"))
    )

    assert out["overall_health"] is None, (
        "a string here is indistinguishable from a measurement; the previous "
        "'unknown' silently replaced a real red"
    )
    assert out["health_queried"] is False
    assert out["health_not_queried_reason"]


def test_a_failed_health_query_says_what_was_not_asked_and_how_to_get_it(
    monkeypatch,
) -> None:
    out = _run(
        monkeypatch, _HealthSystem(error=RuntimeError("vsan health service down"))
    )

    msg = out["message"]
    assert "VsanVcClusterHealthSystem" in msg or "health" in msg.lower()
    assert "vCenter" in msg, "name where the operator can read it instead"
    assert out["vsan_enabled"] is True
    assert out["disk_groups"], "the rest of the survey must still be returned"


def test_the_health_endpoint_being_absent_is_not_fatal(monkeypatch) -> None:
    """An ESXi target or a vCenter not managing vSAN raises VSANError from the
    accessor. The disk-group survey is still worth returning."""
    monkeypatch.setattr(ops, "_require_cluster", lambda si, name: _cluster())
    with patch(
        "vmware_storage.ops.vsan_sdk.managed_object",
        side_effect=VSANError("no vsan-cluster-health-system on this target"),
    ):
        out = ops.get_vsan_health(None, "vsan-cl")

    assert out["overall_health"] is None
    assert out["health_queried"] is False
    assert out["disk_groups"]


def test_vsan_disabled_still_short_circuits(monkeypatch) -> None:
    """Control: no health query on a cluster that has no vSAN."""
    monkeypatch.setattr(
        ops, "_require_cluster", lambda si, name: _cluster(enabled=False)
    )
    health = _HealthSystem(_summary())
    with patch(
        "vmware_storage.ops.vsan_sdk.managed_object", return_value=health
    ):
        out = ops.get_vsan_health(None, "plain-cl")

    assert out["vsan_enabled"] is False
    assert health.calls == []


# ── anti-phantom: the SDK surface is real, not remembered ───────────────────


def test_the_sdk_surface_is_real() -> None:
    """Checked against the installed pyVmomi's own type metadata (踩坑 #36).

    If a future pyVmomi renames any of this, the failure lands here with the
    reason, rather than as a 'health unavailable' message in production.
    """
    import vsanmgmtObjects  # noqa: F401 — registers the vSAN types
    from pyVmomi import vim

    system = vim.cluster.VsanVcClusterHealthSystem
    methods = {m.name: m for m in system._GetMethodList()}
    assert "QueryClusterHealthSummary" in methods
    method = methods["QueryClusterHealthSummary"]
    assert method.wsdlName == "VsanQueryVcClusterHealthSummary"
    assert method.result is vim.cluster.VsanClusterHealthSummary
    params = {p.name for p in method.params}
    assert {"cluster", "fetchFromCache"} <= params

    summary_props = {
        p.name for p in vim.cluster.VsanClusterHealthSummary._GetPropertyList()
    }
    assert {"overallHealth", "overallHealthDescription", "groups", "timestamp"} <= (
        summary_props
    )

    group_props = {
        p.name for p in vim.cluster.VsanClusterHealthGroup._GetPropertyList()
    }
    assert {"groupId", "groupName", "groupHealth"} <= group_props


# ── anti-phantom: shipped code stays inside the pinned surface ──────────────

#: Matches a call site whose callee name contains "Vsan", e.g. ``GetVsanVcMos(``.
_VSAN_CALL = __import__("re").compile(r"\b(\w*Vsan\w+)\s*\(")

#: The two modules the health path spans.
_HEALTH_PATH_MODULES = ("vsan.py", "vsan_sdk.py")


def _vsan_sdk_calls_on_the_health_path() -> set[str]:
    import inspect
    from pathlib import Path

    from vmware_storage.ops import vsan_sdk

    root = Path(inspect.getfile(vsan_sdk)).parent
    calls: set[str] = set()
    seen = []
    for name in _HEALTH_PATH_MODULES:
        path = root / name
        assert path.exists(), f"{path} is gone — this scan checks nothing"
        seen.append(name)
        calls |= set(_VSAN_CALL.findall(path.read_text(encoding="utf-8")))
    assert seen == list(_HEALTH_PATH_MODULES)
    return calls


def test_the_health_path_makes_at_least_one_vsan_sdk_call() -> None:
    """Form #1 guard: an empty scan makes the allow-list below vacuously true."""
    assert _vsan_sdk_calls_on_the_health_path()


def test_the_health_path_only_touches_spec_listed_sdk_calls() -> None:
    from tests.eval.spec import vsan_health_endpoints as spec

    extra = _vsan_sdk_calls_on_the_health_path() - spec.ALLOWED_SDK_CALLS
    assert not extra, (
        f"the health path calls vSAN SDK names outside the verified spec: "
        f"{sorted(extra)}. Add them to the spec only if verified against "
        "pyVmomi's type metadata, else remove (踩坑 #36 phantom endpoint)."
    )


def test_the_health_path_never_calls_a_write() -> None:
    from tests.eval.spec import vsan_health_endpoints as spec

    hit = _vsan_sdk_calls_on_the_health_path() & spec.FORBIDDEN_SDK_CALLS
    assert not hit, f"write call on a read-only path: {sorted(hit)}"


def test_the_spec_itself_describes_real_pyvmomi() -> None:
    """The spec is only worth anything if it is not also from memory."""
    import vsanmgmtObjects  # noqa: F401
    from pyVmomi import vim

    from tests.eval.spec import vsan_health_endpoints as spec

    for key, type_path in spec.MANAGED_OBJECTS.items():
        assert key, "empty accessor key"
        obj = vim
        for part in type_path.removeprefix("vim.").split("."):
            obj = getattr(obj, part)

    system = vim.cluster.VsanVcClusterHealthSystem
    methods = {m.name: m for m in system._GetMethodList()}
    for _owner, method_name in spec.METHODS:
        assert method_name in methods, f"{method_name} is not on the SDK type"
        assert methods[method_name].wsdlName == spec.WSDL_NAMES[method_name]
        declared = {p.name for p in methods[method_name].params}
        assert set(spec.PARAMS[method_name]) <= declared

    summary_props = {
        p.name for p in vim.cluster.VsanClusterHealthSummary._GetPropertyList()
    }
    assert set(spec.PROPERTY_PATHS) <= summary_props
    group_props = {
        p.name for p in vim.cluster.VsanClusterHealthGroup._GetPropertyList()
    }
    assert set(spec.GROUP_PROPERTY_PATHS) <= group_props

    for write in spec.FORBIDDEN_SDK_CALLS:
        assert write in methods, (
            f"{write} is listed as a forbidden SDK write but is not a method on "
            "VsanVcClusterHealthSystem — the deny list is guarding a name that "
            "does not exist"
        )
