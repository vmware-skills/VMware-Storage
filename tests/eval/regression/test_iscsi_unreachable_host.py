"""iSCSI reads on an unreachable host must teach, not crash — and not lie.

Real-hardware finding, 2026-08-30 (VCF 9.1): ``storage_iscsi_status`` against a
``notResponding`` host raised ``AttributeError: 'NoneType' object has no
attribute 'storageDevice'``. vCenter answers ``HostSystem.config`` with ``None``
for a host it has lost contact with, and ``_get_iscsi_hba`` dereferenced it.

The obvious repair is the wrong one. ``_get_iscsi_hba`` returns ``None`` for
"this host has no software iSCSI adapter", and ``get_iscsi_status`` renders that
as ``enabled: False`` — so guarding the attribute with a ``None`` return would
turn a crash into a confident false answer about a machine nobody reached. Worse
than the crash, and invisible.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vmware_storage.ops import iscsi_config as ops


def _host(name: str, *, config, state: str = "notResponding"):
    return SimpleNamespace(
        name=name,
        config=config,
        runtime=SimpleNamespace(connectionState=state),
        configManager=SimpleNamespace(storageSystem=object()),
    )


@pytest.mark.unit
def test_status_on_an_unreachable_host_raises_a_teaching_error(monkeypatch):
    host = _host("esx-gone", config=None)
    monkeypatch.setattr(ops, "_require_host", lambda si, name: host)

    with pytest.raises(ops.ISCSIError) as exc:
        ops.get_iscsi_status(None, "esx-gone")

    message = str(exc.value)
    assert "esx-gone" in message
    assert "notResponding" in message, (
        "the state vCenter reported is the whole diagnosis; without it the "
        "reader cannot tell this from a permissions problem"
    )
    # It must not be an AttributeError, and it must not be silence.
    assert not isinstance(exc.value, AttributeError)


@pytest.mark.unit
def test_an_unreachable_host_is_never_reported_as_iscsi_disabled(monkeypatch):
    """The regression that matters more than the crash.

    ``enabled: False`` is a claim about the host's configuration. A host whose
    config vCenter could not supply has made no such claim.
    """
    host = _host("esx-gone", config=None)
    monkeypatch.setattr(ops, "_require_host", lambda si, name: host)

    try:
        out = ops.get_iscsi_status(None, "esx-gone")
    except ops.ISCSIError:
        return  # raising is the intended behaviour
    pytest.fail(
        f"returned a verdict for a host that was never read: {out}. "
        "enabled=False here says software iSCSI is off, which was not observed."
    )


@pytest.mark.unit
def test_a_reachable_host_with_no_adapter_still_reports_disabled(monkeypatch):
    """The control. A connected host that genuinely has no software iSCSI HBA
    is a real 'disabled' answer and must keep returning one."""
    host = _host(
        "esx-01",
        state="connected",
        config=SimpleNamespace(storageDevice=SimpleNamespace(hostBusAdapter=[])),
    )
    monkeypatch.setattr(ops, "_require_host", lambda si, name: host)

    out = ops.get_iscsi_status(None, "esx-01")

    assert out["enabled"] is False
    assert out["hba_device"] is None


@pytest.mark.unit
def test_a_host_whose_config_lacks_storage_device_is_also_not_a_verdict(monkeypatch):
    """``config`` present but ``storageDevice`` absent is the same unknown, and
    was the other half of the original dereference."""
    host = _host("esx-partial", state="connected", config=SimpleNamespace(storageDevice=None))
    monkeypatch.setattr(ops, "_require_host", lambda si, name: host)

    out = ops.get_iscsi_status(None, "esx-partial")
    assert out["enabled"] is False, (
        "an empty adapter list on a connected host is a measurement — only an "
        "absent `config` is not"
    )
