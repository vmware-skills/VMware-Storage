"""A write tool that catches its exception must still be audited as a failure.

``@vmware_tool`` records a call as failed when an exception reaches it, or when
the returned payload is a dict carrying a truthy ``error`` key. This skill's
four write tools return their result as a plain **string**: their ``except``
block swallows the exception and hands back ``"Error: ..."``, which from the
wrapper's side is indistinguishable from a successful return.

Three things went wrong because of that, and all three are asserted here or
follow directly from what is:

1. the audit row said ``status=ok`` for an operation that failed;
2. ``_record_undo`` wrote an undo token for a change that never landed, so
   vmware-pilot could offer to reverse a write that never happened;
3. the circuit breaker was told ``success=True``, so repeated failures never
   tripped it — layer three of CLAUDE.md's recovery model.

``vmware_policy.report_tool_failure`` exists for exactly this and is called from
``server._error_reply``. All three consequences key off the same ``state.status``
in ``vmware_policy.decorators``, so the audited status is the honest thing to
assert; the undo suppression is checked separately because it is the one with a
destructive failure mode.

The discovery below is deliberately structural rather than a hard-coded list of
four names: a write tool added later that returns a string and forgets to route
through ``_error_reply`` fails this test instead of shipping the same defect
again.
"""

from __future__ import annotations

import inspect

import pytest

from vmware_storage.mcp_server import server
from vmware_storage.ops.iscsi_config import HostNotFoundError

#: Enough to satisfy the required parameters of every write tool; each call is
#: filtered down to the names its own signature actually declares.
_ARGS = {"host_name": "esxi-01", "address": "10.0.0.1"}

_HOST_NOT_FOUND = (
    "Host 'esxi-01' not found on this target. vmware-storage has no host-listing "
    "tool — run vmware-monitor's list_esxi_hosts to get exact ESXi host names "
    "(FQDN or IP, case-sensitive) and copy one."
)


def _string_returning_tools() -> list[str]:
    """Names of every ``@vmware_tool`` in the server module that returns a string.

    Asserts it found something: a discovery loop that silently matches nothing
    would leave this whole file reporting green while checking nothing at all.
    """
    names = []
    for name in dir(server):
        fn = getattr(server, name)
        if not getattr(fn, "_is_vmware_tool", False):
            continue
        if inspect.signature(fn).return_annotation is str:
            names.append(name)
    assert names, "found no string-returning tools — this file would check nothing"
    return sorted(names)


class _Recorder:
    """Stands in for the audit engine; ``_CallState`` captures it at call time."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def log(self, **kwargs) -> None:
        self.rows.append(kwargs)


@pytest.fixture
def audit(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr("vmware_policy.guard.get_engine", lambda: recorder)
    return recorder


def _call(tool_name: str, **overrides):
    """Invoke the tool through its real wrapper — no ``inspect.unwrap``."""
    fn = getattr(server, tool_name)
    params = inspect.signature(fn).parameters
    kwargs = {k: v for k, v in _ARGS.items() if k in params}
    kwargs.update(overrides)
    return fn(**kwargs)


@pytest.mark.parametrize("tool_name", _string_returning_tools())
def test_returned_error_string_is_audited_as_a_failure(tool_name, audit, monkeypatch):
    def _boom(target=None):
        raise HostNotFoundError(_HOST_NOT_FOUND)

    monkeypatch.setattr(server, "_get_connection", _boom)

    result = _call(tool_name)

    # The agent-facing contract is unchanged: still a string, still teaching.
    assert result.startswith("Error:")
    assert "list_esxi_hosts" in result

    assert audit.rows, f"{tool_name} was never audited"
    assert audit.rows[-1]["status"] == "error", (
        f"{tool_name} returned {result[:40]!r} but audited as "
        f"{audit.rows[-1]['status']!r} — a failed write recorded as a success"
    )


def test_a_successful_write_still_audits_as_ok(audit, monkeypatch):
    """The other direction: reporting every call failed would be the same lie."""
    monkeypatch.setattr(server, "_get_connection", lambda target=None: object())
    monkeypatch.setattr(
        server, "enable_software_iscsi", lambda si, host: "Software iSCSI enabled."
    )
    monkeypatch.setattr(server._audit, "log", lambda **kw: None)

    result = _call("storage_iscsi_enable")

    assert result == "Software iSCSI enabled."
    assert audit.rows[-1]["status"] == "ok"


def test_a_failed_add_target_records_no_undo_token(audit, monkeypatch):
    """An undo token asserts a change happened and can be reversed.

    ``storage_iscsi_add_target`` declares an ``undo`` inverse, so a failed call
    that audits as ``ok`` also hands vmware-pilot an offer to remove a send
    target that was never added.
    """
    recorded: list[dict] = []

    class _Store:
        def record(self, **kwargs):
            recorded.append(kwargs)
            return "undo-1"

    monkeypatch.setattr("vmware_policy.undo.get_undo_store", lambda: _Store())

    def _boom(target=None):
        raise HostNotFoundError(_HOST_NOT_FOUND)

    monkeypatch.setattr(server, "_get_connection", _boom)

    result = _call("storage_iscsi_add_target")

    assert result.startswith("Error:")
    assert not recorded, "recorded an undo token for a target that was never added"
