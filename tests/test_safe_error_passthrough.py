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

The same defect existed one layer earlier for the missing-password error —
this family's most common *first-run* failure, whose entire remedy is the env
var name it carries. An agent hitting an unconfigured target received
``OSError: operation failed.`` and had nothing to act on.

Admitting bare ``OSError`` fixed that and opened a wider door than intended.
``sanitize`` strips control characters and truncates; it does not redact. So
``ssl.SSLCertVerificationError`` (certificate subject *and* hostname),
``socket.gaierror`` (the name that failed to resolve), and connection errors
carrying a full ``scheme://host:port/path`` all reached the agent verbatim
through that entry — while this module's docstring went on claiming such text
is withheld. The narrow :class:`~vmware_storage.config.ConfigError` carries the
one message that needed to pass, and nothing else.

The second half of the guard is length. A message is only teaching if it
survives ``sanitize(..., 300)`` intact, and several of these interpolate a
caller-supplied name *before* the remedy — so a long-but-ordinary value silently
truncated the part that says what to do.
"""

from __future__ import annotations

import socket
import ssl

import pytest

from vmware_storage.config import ConfigError, TargetConfig
from vmware_storage.connection import ConnectError, ConnectionManager
from vmware_storage.mcp_server.server import _safe_error
from vmware_storage.ops.datastore_browser import DatastoreBrowseError
from vmware_storage.ops.iscsi_config import HostNotFoundError, ISCSIError
from vmware_storage.ops.vsan import VSANError

TEACHING = "Host 'esx-01' not found. Run list_esxi_hosts (vmware-monitor) and copy an exact name."

ENV_KEY = "VMWARE_VCENTER_PROD_PASSWORD"

#: Cap applied by ``_safe_error`` before the message reaches the agent.
CAP = 300

#: A real handshake failure quotes both of these. Neither may reach an agent.
CERT_HOST = "vc-internal.corp.example.com"
CERT_TEXT = (
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed "
    f"certificate in certificate chain, subject CN={CERT_HOST} (_ssl.c:1006)"
)


def _missing_password(name: str) -> ConfigError:
    """The real message, raised by the real property — not a copy of its text."""
    target = TargetConfig(name=name, host="vc.example.com", config_username="u")
    with pytest.raises(ConfigError) as excinfo:
        target.password
    return excinfo.value


# ── the narrow passthrough ──────────────────────────────────────────────────


def test_missing_password_keeps_the_env_var_name():
    """The one config error raised on purpose — and the whole point of it is the name."""
    out = _safe_error(_missing_password("vcenter-prod"), "list_datastores")
    assert ENV_KEY in out
    assert "operation failed" not in out


@pytest.mark.parametrize(
    "exc_type", [ISCSIError, HostNotFoundError, VSANError, DatastoreBrowseError]
)
def test_domain_exceptions_keep_their_message(exc_type):
    assert _safe_error(exc_type(TEACHING), "storage_iscsi_status") == TEACHING


@pytest.mark.parametrize("exc_type", [ValueError, FileNotFoundError, KeyError, PermissionError])
def test_validation_errors_still_pass_through(exc_type):
    assert "esx-01" in _safe_error(exc_type(TEACHING), "t")


def test_dropped_connection_surfaces_its_hint():
    """The CLI path catches OSError and prints the hint; the MCP path must match."""
    assert "retry" in _safe_error(ConnectionError("Connection lost — retry the operation."), "t")


# ── what the narrow type keeps out ──────────────────────────────────────────


def test_tls_failure_does_not_leak_the_certificate_subject():
    """Reduced by the pre-check, not by the allowlist — it is also a ValueError.

    ``ssl.SSLCertVerificationError`` subclasses both ``OSError`` and
    ``ValueError``, and ``ValueError`` predates the ``OSError`` entry, so
    narrowing the ``OSError`` side alone left the certificate subject arriving
    exactly as before. An allowlist cannot express "except this one".

    Only the ``isinstance`` branches of ``_safe_error`` are under test, so the
    exception is constructed rather than provoked through a real handshake.
    """
    out = _safe_error(ssl.SSLCertVerificationError(CERT_TEXT), "list_all_datastores")
    assert out == "SSLCertVerificationError: operation failed."
    assert CERT_HOST not in out


def test_dns_failure_does_not_leak_the_hostname():
    exc = socket.gaierror(
        "[Errno 8] nodename nor servname provided, or not known: vcenter-lab.corp.internal"
    )
    out = _safe_error(exc, "list_all_datastores")
    assert out == "gaierror: operation failed."
    assert "vcenter-lab.corp.internal" not in out


def test_a_bare_oserror_is_no_longer_a_passthrough():
    """The guard, stated directly: OSError-ness alone buys nothing."""
    out = _safe_error(OSError("connect to 10.20.30.40:443 failed"), "t")
    assert out == "OSError: operation failed."
    assert "10.20.30.40" not in out


def test_unplanned_exceptions_are_still_reduced():
    """The redaction this allowlist exists for has to keep working."""
    out = _safe_error(RuntimeError("https://admin:hunter2@vc.internal/api/task-42"), "t")
    assert out == "RuntimeError: operation failed."
    assert "hunter2" not in out


# ── what replaces it: the connection layer's authored message ───────────────


def _lab_target(**overrides) -> TargetConfig:
    kwargs = dict(
        name="lab-vc",
        host=CERT_HOST,
        config_username="svc@vsphere.local",
        port=8443,
        verify_ssl=True,
    )
    kwargs.update(overrides)
    return TargetConfig(**kwargs)


def test_a_transport_failure_teaches_without_naming_the_host(monkeypatch):
    """The diagnostic must not simply be dropped — reducing to a class name tells
    an operator nothing, and self-signed certs are this family's usual cause."""
    import pyVim.connect as pvc

    def _boom(**kwargs):
        raise ssl.SSLCertVerificationError(CERT_TEXT)

    monkeypatch.setenv("VMWARE_LAB_VC_PASSWORD", "pw")
    monkeypatch.setattr(pvc, "SmartConnect", _boom)

    with pytest.raises(ConnectError) as excinfo:
        ConnectionManager._create_connection(_lab_target())

    out = _safe_error(excinfo.value, "list_all_datastores")
    assert len(out) <= CAP
    # Names what the operator can act on...
    assert "lab-vc" in out
    assert "verify_ssl" in out
    assert "vmware-storage doctor" in out
    # ...and nothing the raw exception would have leaked.
    assert CERT_HOST not in out
    assert "8443" not in out
    assert "CERTIFICATE_VERIFY_FAILED" not in out
    assert excinfo.value.cause_name == "SSLCertVerificationError"


def test_a_missing_password_is_not_answered_with_a_tls_remedy(monkeypatch):
    """``target.password`` is a property that raises inside the argument list.

    Evaluated inside the wrapped call, the family's most common first-run
    failure would be caught by the connection-failure handler — an OSError
    subclass like any other — and answered with a remedy about certificates.
    """
    monkeypatch.delenv("VMWARE_LAB_VC_PASSWORD", raising=False)

    with pytest.raises(ConfigError) as excinfo:
        ConnectionManager._create_connection(_lab_target())

    assert not isinstance(excinfo.value, ConnectError)
    out = _safe_error(excinfo.value, "list_all_datastores")
    assert "VMWARE_LAB_VC_PASSWORD" in out
    assert "verify_ssl" not in out


def test_credentials_still_resolve_as_a_pair(monkeypatch):
    """Reading them into locals must not split the pair v1.8.3 exists to keep whole."""
    import pyVim.connect as pvc

    seen: dict[str, str] = {}

    def _capture(**kwargs):
        seen.update(user=kwargs["user"], pwd=kwargs["pwd"])
        return object()

    monkeypatch.setattr(pvc, "SmartConnect", _capture)
    monkeypatch.setenv("VMWARE_LAB_VC_USERNAME", "svc-b@vsphere.local")
    monkeypatch.setenv("VMWARE_LAB_VC_PASSWORD", "pw-b")

    ConnectionManager._create_connection(_lab_target())
    assert seen == {"user": "svc-b@vsphere.local", "pwd": "pw-b"}


# ── length: the remedy has to survive the cap ───────────────────────────────


def test_message_is_still_truncated():
    """Length capping is the other half of the guard."""
    assert len(_safe_error(ISCSIError("x" * 900), "t")) <= CAP


@pytest.mark.parametrize("name_len", [2, 19, 30, 50])
def test_missing_password_remedy_survives_a_long_target_name(name_len):
    """The env var name embeds the target name, so spelling it twice cost 3x.

    At a 19-character target name the message crossed the cap and the closing
    "run doctor" step — the one that confirms the fix worked — was cut off.
    """
    out = _safe_error(_missing_password("t" * name_len), "list_all_datastores")
    assert len(out) <= CAP
    assert "vmware-storage doctor" in out
    assert f"VMWARE_{'T' * name_len}_PASSWORD" in out


@pytest.mark.parametrize("detail_len", [30, 4000])
def test_browse_failure_remedy_survives_an_unbounded_fault_string(detail_len):
    """vCenter fault text has no length bound; the remedy after it must survive."""
    from vmware_storage.ops import datastore_browser

    class _Err:
        msg = "F" * detail_len

    class _Info:
        state = "error"
        error = _Err()

    class _Task:
        info = _Info()

    with pytest.raises(DatastoreBrowseError) as excinfo:
        datastore_browser._wait_for_task(_Task())
    out = _safe_error(excinfo.value, "browse_datastore")
    assert len(out) <= CAP
    assert "list_all_datastores" in out
    assert "narrower path and pattern" in out


@pytest.mark.parametrize("name_len", [10, 30, 50])
def test_cluster_not_found_keeps_its_did_you_mean(name_len, monkeypatch):
    """The closest-match suffix is appended last, so the message ahead of it
    has to leave room — it is the only part that names the correct cluster.

    Driven through the real ``_require_cluster``: asserting against a copy of
    the message text here would keep passing however the raise site is reworded.
    """
    from vmware_storage.ops import vsan

    name = "c" * name_len
    actual = name[:-1] + "X"
    monkeypatch.setattr(vsan, "find_cluster_by_name", lambda si, n: None)
    monkeypatch.setattr(vsan, "_collect", lambda si, types, paths: [(object(), {"name": actual})])

    with pytest.raises(VSANError) as excinfo:
        vsan._require_cluster(object(), name)

    out = _safe_error(excinfo.value, "vsan_health")
    assert len(out) <= CAP
    assert "list_all_clusters" in out
    assert actual in out, "the suggested cluster name was truncated away"
