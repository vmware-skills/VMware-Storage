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


@pytest.mark.unit
@pytest.mark.parametrize("exc", [ValueError("bad"), KeyError("t")])
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
