"""Connection management for vCenter and ESXi hosts.

Handles multi-target connections via pyVmomi with session reuse.
"""

from __future__ import annotations

import atexit
import ssl
from collections.abc import Callable
from typing import TYPE_CHECKING

from pyVmomi import vim

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

from vmware_storage.config import CONFIG_FILE, AppConfig, ConfigError, TargetConfig, load_config

# ServiceInstance is a pyVmomi ManagedObject — its __setattr__ rejects any
# attribute not in its allowed list (raises "Managed object attributes are
# read-only" on pyVmomi 8.x). We keep per-connection metadata in this module
# dict, keyed by id(si). Cleared via atexit when the SI is disconnected.
# 踩坑 #32 (2026-05-19, 客户 vCenter 8.0U3 现场).
_SI_VERIFY_SSL: dict[int, bool] = {}

# atexit cleanups for live connections, keyed by id(si) so a connection dropped
# before interpreter exit can take its handler with it.
_SI_ATEXIT: dict[int, Callable[[], None]] = {}


def _release_si(si: ServiceInstance) -> None:
    """Unregister the atexit cleanup registered for ``si``.

    Every connect() registers a cleanup that closes over si, and atexit holds
    that closure -- and therefore si -- until the process exits. A long-running
    MCP server that reconnects after each session expiry (踩坑 #40) accumulates
    one dead ServiceInstance and one handler per reconnect, and at exit runs a
    Disconnect against every session it ever opened.

    Measured before this existed: 50 evict-and-reconnect cycles left 50 handlers
    registered and all 50 evicted ServiceInstance objects still reachable, while
    the id(si) side stores stayed correctly at one entry -- the side-store
    discipline was never the leak, the registration was.
    """
    fn = _SI_ATEXIT.pop(id(si), None)
    if fn is not None:
        atexit.unregister(fn)



def get_verify_ssl(si: ServiceInstance) -> bool:
    """Return verify_ssl flag stashed by the connect() that created ``si``.

    Defaults to True (strict) if the SI was created outside this manager, so a
    downstream SDK caller (e.g. the vSAN Management SDK) never silently drops to
    an unverified TLS context by accident.
    """
    return _SI_VERIFY_SSL.get(id(si), True)


class ConnectError(ConfigError):
    """A session could not be opened — with an authored, leak-free explanation.

    ``SmartConnect`` reports a refused connection through the transport layer,
    and that text names the resolved host and port, and for a TLS failure the
    certificate subject too. Handing that string on meant handing it to the
    agent, while the message said nothing the operator could act on.

    :attr:`cause_name` keeps the transport class recoverable — "ConnectError"
    diagnoses nothing, "SSLCertVerificationError" or "gaierror" says which knob
    to reach for. Only the class name of the original travels, never its message.
    """

    def __init__(self, message: str, cause_name: str = "") -> None:
        super().__init__(message)
        self.cause_name = cause_name


def _connect_failed(target: TargetConfig, exc: BaseException) -> ConnectError:
    """Authored replacement for a transport error, safe to show an agent.

    Names the target as it is written in config.yaml, its current ``verify_ssl``
    setting, and the file to edit — and interpolates nothing from ``exc``, whose
    text is the thing being withheld. The original survives as ``__cause__`` for
    the server-side log.

    ``CONFIG_FILE`` comes last because the message is capped at 300 characters
    on the way to an agent and its length is set by the operator's home
    directory, not by anything this code controls: the closing 'run doctor'
    step survived on the author's ``/Users/zw`` and was cut off on the
    ``C:\\Users\\Administrator`` that ran the VCF 9.1 hardware pass. Losing the
    path still leaves an operator who can act; losing the remedy does not.
    """
    return ConnectError(
        f"Could not open a session to target '{target.name}' (its config says "
        f"verify_ssl: {str(target.verify_ssl).lower()}). A self-signed certificate "
        f"needs verify_ssl: false; otherwise check that target's host and port. "
        f"Then run 'vmware-storage doctor'. The file to edit is {CONFIG_FILE}.",
        cause_name=type(exc).__name__,
    )


class ConnectionManager:
    """Manages connections to multiple vCenter/ESXi targets."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._connections: dict[str, ServiceInstance] = {}

    @classmethod
    def from_config(cls, config: AppConfig | None = None) -> ConnectionManager:
        cfg = config or load_config()
        return cls(cfg)

    def connect(self, target_name: str | None = None) -> ServiceInstance:
        """Connect to a target by name, or the default target."""
        target = (
            self._config.get_target(target_name)
            if target_name
            else self._config.default_target
        )

        if target.name in self._connections:
            si = self._connections[target.name]
            try:
                # Probe liveness; expired tokens can surface as a None
                # currentSession instead of raising.
                alive = si.content.sessionManager.currentSession is not None
            except Exception:
                # Any failure (NotAuthenticated, socket error, …) means the
                # cached session is unusable — drop it and reconnect below.
                alive = False
            if alive:
                return si
            # Evict the id(si)-keyed side store NOW rather than waiting for
            # atexit: once the old si is GC'd, a new si for a DIFFERENT target
            # can reuse the same id() value and read stale verify_ssl
            # (id-reuse hazard).
            _SI_VERIFY_SSL.pop(id(si), None)
            _release_si(si)
            del self._connections[target.name]

        si = self._create_connection(target)
        self._connections[target.name] = si
        return si

    def disconnect(self, target_name: str) -> None:
        """Disconnect from a specific target."""
        if target_name in self._connections:
            from pyVim.connect import Disconnect

            _release_si(self._connections[target_name])
            Disconnect(self._connections[target_name])
            del self._connections[target_name]

    def disconnect_all(self) -> None:
        """Disconnect from all targets."""
        for name in list(self._connections):
            self.disconnect(name)

    def list_targets(self) -> list[str]:
        """List all configured target names."""
        return [t.name for t in self._config.targets]

    def list_connected(self) -> list[str]:
        """List currently connected target names."""
        return list(self._connections.keys())

    @staticmethod
    def _create_connection(target: TargetConfig) -> ServiceInstance:
        """Create a new pyVmomi connection."""
        from pyVim.connect import Disconnect, SmartConnect

        context = None
        if not target.verify_ssl:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        # Resolved before the try: a missing password raises ConfigError, which
        # is an OSError subclass, and it must not be mistaken below for a
        # transport failure and rewritten into a message about certificates.
        user, pwd = target.username, target.password

        try:
            si = SmartConnect(
                host=target.host,
                user=user,
                pwd=pwd,
                port=target.port,
                sslContext=context,
                disableSslCertValidation=not target.verify_ssl,
            )
        except OSError as exc:
            # TLS, DNS and socket failures only — every one of them stringifies
            # with the host, the port, or the certificate subject. Authentication
            # faults are vmodl types, not OSError, so they pass through
            # untouched to the handlers that already explain them.
            raise _connect_failed(target, exc) from exc

        # Stash verify_ssl in the id(si)-keyed side store so downstream SDK
        # callers (e.g. the vSAN Management SDK via GetVsanVcMos) can rebuild a
        # matching TLS context instead of falling back to Python's default
        # verifying one on a self-signed target. Side-store, never setattr
        # (踩坑 #32).
        _SI_VERIFY_SSL[id(si)] = target.verify_ssl

        def _cleanup(_si: ServiceInstance = si) -> None:
            # Guarded: a dead session at interpreter exit must not raise.
            try:
                Disconnect(_si)
            except Exception:
                pass
            finally:
                _SI_VERIFY_SSL.pop(id(_si), None)

        _SI_ATEXIT[id(si)] = _cleanup
        atexit.register(_cleanup)
        return si


def get_content(si: ServiceInstance) -> vim.ServiceInstanceContent:
    """Shortcut to get ServiceContent from a ServiceInstance."""
    return si.RetrieveContent()
