"""Regression net for the vSAN data-efficiency read tool (section D).

Three defenses:

1. **Anti-phantom (踩坑 #36)** — scan the shipped ops module and assert every
   vSAN-SDK *call* it makes is a member of the verified spec's allow-list, and
   that no forbidden (write / deferred) call appears. A hallucinated endpoint
   fails here, not at a customer site.
2. **Metadata conformance** — the spec's SDK method + property paths must
   resolve against pyVmomi's own vSAN type metadata (they are real, not
   remembered).
3. **Behavior** — the ops function extracts dedup/compression, and degrades to
   None + message when the response has no dataEfficiencyConfig (form #1:
   an absent field must never crash and must never be read as a healthy False).

Plus a deferred-surface guard: the not-tool-able concepts (global dedup, v2v
replication) must not exist as an ops symbol or a registered MCP tool.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import ssl
from types import SimpleNamespace

import pytest

from tests.eval.spec import vsan_efficiency_endpoints as spec
from vmware_storage.ops import vsan_efficiency as ops

# ── 1. anti-phantom: ops only calls spec-listed vSAN SDK surface ────────────

# Matches a call site whose callee name contains "Vsan", e.g. ``GetVsanVcMos(``
# or ``VsanClusterGetConfig(`` — but not lowercase ``vsan_efficiency``.
_VSAN_CALL = re.compile(r"\b(\w*Vsan\w+)\s*\(")


def _vsan_sdk_calls_in_source() -> set[str]:
    source = inspect.getsource(ops)
    return set(_VSAN_CALL.findall(source))


def test_ops_makes_at_least_one_vsan_sdk_call() -> None:
    # Guard against a checker that silently verifies nothing (form #1): if the
    # scan finds zero calls, the allow-list assertion below is vacuously true.
    calls = _vsan_sdk_calls_in_source()
    assert calls, "no vSAN SDK calls found in ops/vsan_efficiency.py — scan is empty"


def test_ops_only_touches_spec_listed_sdk_calls() -> None:
    calls = _vsan_sdk_calls_in_source()
    extra = calls - spec.ALLOWED_SDK_CALLS
    assert not extra, (
        f"ops/vsan_efficiency.py calls vSAN SDK names outside the verified spec: "
        f"{sorted(extra)}. Add them to the spec only if verified, else remove "
        "(踩坑 #36 phantom endpoint)."
    )


def test_ops_never_calls_forbidden_sdk() -> None:
    calls = _vsan_sdk_calls_in_source()
    hit = calls & spec.FORBIDDEN_SDK_CALLS
    assert not hit, f"forbidden (write/deferred) vSAN SDK call present: {sorted(hit)}"


# ── 2. metadata conformance: spec paths are real pyVmomi, not remembered ────


def _register_vsan_types() -> None:
    import vsanmgmtObjects  # noqa: F401 — import registers vim.vsan.* bindings


def _props_of(t) -> set[str]:
    return {
        p.name
        for klass in getattr(t, "__mro__", [])
        for p in (vars(klass).get("_propList") or [])
    }


def _resolve(dotted: str):
    import pyVmomi

    obj = pyVmomi
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def test_spec_method_exists_on_managed_object() -> None:
    _register_vsan_types()
    for type_name, method in spec.METHODS:
        t = _resolve(type_name)
        found = any(
            method in (key, getattr(info, "wsdlName", ""))
            for klass in t.__mro__
            for key, info in (vars(klass).get("_methodInfo") or {}).items()
        )
        assert found, f"{type_name}.{method} not in pyVmomi vSAN metadata"


def test_spec_efficiency_fields_resolve() -> None:
    _register_vsan_types()
    result_props = _props_of(_resolve(spec.RESULT_TYPE))
    assert "dataEfficiencyConfig" in result_props, spec.RESULT_TYPE
    eff_props = _props_of(_resolve(spec.EFFICIENCY_TYPE))
    for field in ("dedupEnabled", "compressionEnabled"):
        assert field in eff_props, f"{spec.EFFICIENCY_TYPE}.{field} missing"


# ── 3. behavior: extraction + defensive degrade ─────────────────────────────


@pytest.fixture
def patched(monkeypatch):
    """Stub the cluster lookup + config-system so no real vCenter is needed."""
    cluster = SimpleNamespace(name="prod-cluster")
    monkeypatch.setattr(ops, "_require_cluster", lambda si, name: cluster)
    return SimpleNamespace(cluster=cluster)


def _config_system_returning(config):
    return SimpleNamespace(VsanClusterGetConfig=lambda cluster: config)


def test_efficiency_extracts_dedup_and_compression(patched, monkeypatch):
    config = SimpleNamespace(
        enabled=True,
        dataEfficiencyConfig=SimpleNamespace(dedupEnabled=True, compressionEnabled=True),
    )
    monkeypatch.setattr(ops, "_config_system", lambda si: _config_system_returning(config))

    result = ops.get_vsan_efficiency(object(), "prod-cluster")

    assert result["cluster_name"] == "prod-cluster"
    assert result["vsan_enabled"] is True
    assert result["dedup_enabled"] is True
    assert result["compression_enabled"] is True


def test_efficiency_compression_only(patched, monkeypatch):
    config = SimpleNamespace(
        enabled=True,
        dataEfficiencyConfig=SimpleNamespace(dedupEnabled=False, compressionEnabled=True),
    )
    monkeypatch.setattr(ops, "_config_system", lambda si: _config_system_returning(config))

    result = ops.get_vsan_efficiency(object(), "prod-cluster")

    assert result["dedup_enabled"] is False
    assert result["compression_enabled"] is True
    assert "message" not in result


def test_efficiency_missing_config_degrades_to_none(patched, monkeypatch):
    # Response has no dataEfficiencyConfig — must not crash, must not fake False.
    config = SimpleNamespace(enabled=True, dataEfficiencyConfig=None)
    monkeypatch.setattr(ops, "_config_system", lambda si: _config_system_returning(config))

    result = ops.get_vsan_efficiency(object(), "prod-cluster")

    assert result["dedup_enabled"] is None
    assert result["compression_enabled"] is None
    assert "dataEfficiencyConfig" in result["message"]


def test_efficiency_absent_fields_do_not_crash(patched, monkeypatch):
    # dataEfficiencyConfig present but missing both flag attributes entirely.
    config = SimpleNamespace(enabled=None, dataEfficiencyConfig=SimpleNamespace())
    monkeypatch.setattr(ops, "_config_system", lambda si: _config_system_returning(config))

    result = ops.get_vsan_efficiency(object(), "prod-cluster")

    assert result["dedup_enabled"] is None
    assert result["compression_enabled"] is None


def test_config_system_teaches_when_stub_absent(monkeypatch):
    # A session with no _stub gets a teaching VSANError, not an AttributeError.
    with pytest.raises(ops.VSANError, match="no SOAP stub"):
        ops._config_system(SimpleNamespace())


# ── SSL context propagation to the vSAN SDK (HIGH-1, 踩坑 #32) ───────────────


def _fake_vsanapiutils(recorder: dict):
    """A stand-in vsanapiutils whose GetVsanVcMos records how it was called."""

    def _get(stub, context=..., **kwargs):
        recorder["called"] = True
        recorder["context"] = context
        return {"vsan-cluster-config-system": SimpleNamespace()}

    return SimpleNamespace(GetVsanVcMos=_get)


def test_config_system_passes_unverified_context_when_verify_ssl_false(monkeypatch):
    # On a verify_ssl: false (self-signed) target the SDK must be handed an
    # explicit unverified ssl context, else GetVsanVcMos builds a new stub with
    # Python's DEFAULT verifying context and dies with SSLCertVerificationError
    # (then masked by _safe_error). Mutation check: drop the else-branch in
    # _config_system and this fails (context is the sentinel default).
    recorder: dict = {}
    monkeypatch.setitem(
        __import__("sys").modules, "vsanapiutils", _fake_vsanapiutils(recorder)
    )
    monkeypatch.setattr(ops, "get_verify_ssl", lambda si: False)

    si = SimpleNamespace(_stub=object())
    ops._config_system(si)

    assert recorder["called"] is True
    ctx = recorder["context"]
    assert isinstance(ctx, ssl.SSLContext), f"expected an ssl context, got {ctx!r}"
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_config_system_no_context_when_verify_ssl_true(monkeypatch):
    # On a verifying target we must NOT force an unverified context — leave the
    # SDK to its default verifying behavior.
    recorder: dict = {}
    monkeypatch.setitem(
        __import__("sys").modules, "vsanapiutils", _fake_vsanapiutils(recorder)
    )
    monkeypatch.setattr(ops, "get_verify_ssl", lambda si: True)

    si = SimpleNamespace(_stub=object())
    ops._config_system(si)

    # No explicit context passed → recorder holds the sentinel default (...).
    assert recorder["context"] is ...


def test_get_verify_ssl_side_store_roundtrips_and_defaults_strict():
    # The side store is keyed by id(si) and defaults to strict (True) for an SI
    # this manager never created — a downstream SDK caller must never silently
    # drop to an unverified context by accident (踩坑 #32).
    from vmware_storage import connection

    si = SimpleNamespace()
    assert connection.get_verify_ssl(si) is True  # default strict

    connection._SI_VERIFY_SSL[id(si)] = False
    try:
        assert connection.get_verify_ssl(si) is False
    finally:
        connection._SI_VERIFY_SSL.pop(id(si), None)


# ── deferred surface: not-tool-able concepts must not exist anywhere ────────


def test_deferred_concepts_are_not_ops_symbols() -> None:
    for name in spec.DEFERRED_NOT_TOOLABLE:
        assert not hasattr(ops, name), (
            f"{name} is documented as not tool-able (no verified SDK object) but "
            "exists as an ops symbol — remove it (踩坑 #36)."
        )


def test_mcp_registers_efficiency_but_not_deferred_tools() -> None:
    from vmware_storage.mcp_server import server

    tool_names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "vsan_efficiency" in tool_names, tool_names
    for name in spec.DEFERRED_NOT_TOOLABLE:
        assert name not in tool_names, f"deferred concept {name} was registered as a tool"
    # And no tool advertises global-dedup / replication under another name.
    for banned in ("global_dedup", "replication"):
        assert not any(banned in n for n in tool_names), f"a tool name contains '{banned}'"
