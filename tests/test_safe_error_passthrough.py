"""A teaching message the agent never sees is not a teaching message.

``_safe_error`` reduces unrecognised exceptions to ``"<Class>: operation
failed."`` so raw vSphere text cannot leak. The allowlist it checked against
held only the builtin validation errors, so every exception this skill defines
for its own domain — ``ISCSIError``, ``HostNotFoundError``, ``VSANError`` —
had its message replaced by its class name on the way to the agent.

The effect was invisible from the CLI, which prints those messages in full, and
invisible to the error-quality eval, which reads the message at the raise site
rather than what survives the wrapper. Ten of this skill's sixteen rewritten
messages were reaching an agent as ``ISCSIError: operation failed.``

These exceptions exist precisely to carry a corrected next step, so the rule is
the inverse of the original: a domain exception defined under
``vmware_storage.ops`` passes through, and only genuinely unplanned exceptions
are reduced.

``OSError`` was the same defect one layer earlier, and survived the first fix:
``config.py`` raises exactly one — the missing-password error, this family's
most common *first-run* failure — and its entire remedy is the env var name it
carries. An agent hitting an unconfigured target received
``OSError: operation failed.`` and had nothing to act on.
"""

from __future__ import annotations

import pytest

from vmware_storage.mcp_server.server import _safe_error
from vmware_storage.ops.iscsi_config import HostNotFoundError, ISCSIError
from vmware_storage.ops.vsan import VSANError

TEACHING = "Host 'esx-01' not found. Run list_esxi_hosts (vmware-monitor) and copy an exact name."

ENV_KEY = "VMWARE_VCENTER_PROD_PASSWORD"
MISSING_PASSWORD = (
    f"Password not found for target 'vcenter-prod'. Set environment "
    f"variable {ENV_KEY} — export it, or add a {ENV_KEY}=<password> line "
    f"to ~/.vmware-storage/.env (loaded automatically, chmod 600). Then run "
    f"'vmware-storage doctor' to verify."
)


def test_missing_password_keeps_the_env_var_name():
    """The single OSError config.py raises — and the whole point of it is the name."""
    out = _safe_error(OSError(MISSING_PASSWORD), "list_datastores")
    assert ENV_KEY in out
    assert "operation failed" not in out


@pytest.mark.parametrize("exc_type", [ISCSIError, HostNotFoundError, VSANError])
def test_domain_exceptions_keep_their_message(exc_type):
    assert _safe_error(exc_type(TEACHING), "storage_iscsi_status") == TEACHING


@pytest.mark.parametrize("exc_type", [ValueError, FileNotFoundError, KeyError, PermissionError])
def test_validation_errors_still_pass_through(exc_type):
    assert "esx-01" in _safe_error(exc_type(TEACHING), "t")


def test_dropped_connection_surfaces_its_hint():
    """The CLI path catches OSError and prints the hint; the MCP path must match."""
    assert "retry" in _safe_error(ConnectionError("Connection lost — retry the operation."), "t")


def test_unplanned_exceptions_are_still_reduced():
    """The redaction this allowlist exists for has to keep working."""
    out = _safe_error(RuntimeError("https://admin:hunter2@vc.internal/api/task-42"), "t")
    assert out == "RuntimeError: operation failed."
    assert "hunter2" not in out


def test_message_is_still_truncated():
    """Length capping is the other half of the guard."""
    assert len(_safe_error(ISCSIError("x" * 900), "t")) <= 300
