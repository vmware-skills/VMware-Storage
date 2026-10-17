"""Error hints must fit the error, and the surface that shows them.

Real hardware, 2026-10-17: every MCP error carried "Run 'vmware-storage doctor'
to verify connectivity." -- also for an invalid path or a datastore name that
does not resolve, where the message already says what to change. And the
unknown-target error said "Pass --target", a CLI flag; the MCP name is the
``target`` parameter.
"""

from __future__ import annotations

import pytest

from vmware_storage.config import AppConfig
from vmware_storage.mcp_server import server


def _unknown_target():
    from vmware_storage.config import UnknownTargetError

    return UnknownTargetError("Target 'x' not found.")


@pytest.mark.unit
@pytest.mark.parametrize("exc", [ValueError("bad"), _unknown_target()])
def test_an_authored_request_error_does_not_send_the_agent_to_doctor(exc) -> None:
    assert "doctor" not in server._hint_for(exc)


@pytest.mark.unit
@pytest.mark.parametrize("exc", [ConnectionError("x"), TimeoutError("x"), RuntimeError("x")])
def test_connectivity_and_unplanned_errors_keep_the_doctor_hint(exc) -> None:
    assert server._hint_for(exc) == server._DOCTOR_HINT


@pytest.mark.unit
def test_unknown_target_names_the_mcp_parameter() -> None:
    with pytest.raises(KeyError) as info:
        AppConfig(targets=()).get_target("nope")
    assert "target=" in str(info.value)



@pytest.mark.unit
@pytest.mark.parametrize("exc", [
    __import__("json").JSONDecodeError("Expecting value", "<html>", 0),
    KeyError("summary.capacity"),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
])
def test_parse_errors_and_bare_key_errors_keep_the_doctor_hint(exc) -> None:
    """Independent review 2026-10-17: a reverse proxy answering HTML raises
    JSONDecodeError (a ValueError), and a programming slip raises a bare KeyError;
    neither message says what to change, so they must not be told it does."""
    assert server._hint_for(exc) == server._DOCTOR_HINT


@pytest.mark.unit
def test_an_unknown_target_is_still_a_key_error() -> None:
    """Callers catch KeyError from get_target; the subclass must not break them."""
    assert issubclass(type(_unknown_target()), KeyError)
