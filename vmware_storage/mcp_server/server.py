"""MCP server wrapping VMware Storage operations.

This module exposes VMware vSphere datastore browsing, iSCSI configuration,
and vSAN health/capacity tools via the Model Context Protocol (MCP) using
stdio transport.

Tool categories
---------------
* **Read-only**: list_all_datastores, browse_datastore, scan_datastore_images,
  list_cached_images, get_iscsi_status, get_vsan_health, get_vsan_capacity,
  get_vsan_efficiency, list_fc_adapters, list_storage_device_paths,
  get_access_info
* **Write, gated** (``confirm=False`` previews the blast radius and changes
  nothing): enable_iscsi, add_iscsi_target,
  remove_iscsi_target, rescan_storage — see ``ops.iscsi_gate``

Security considerations
-----------------------
* Credentials are loaded from environment variables / .env file.
* Transport: Uses stdio transport (local only); no network listener.
* iSCSI operations modify host storage configuration; they act only with confirm=True.

Source: https://github.com/vmware-skills/VMware-Storage
License: MIT
"""


import sys as _sys
import json
import logging
import ssl
import threading
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from vmware_policy import (  # noqa: F401 — compatibility/runtime export
    describe_tool_parameters,
    sanitize,
    vmware_tool,
)
from vmware_policy.access import unknown_tool_message, vsphere_access_info  # noqa: F401 — compatibility/runtime export

from vmware_storage import __version__
from vmware_storage.access_requirements import REQUIREMENTS  # noqa: F401 — compatibility/runtime export
from vmware_storage.config import AppConfig, ConfigError, UnknownTargetError, load_config
from vmware_storage.connection import (
    NOT_AUTHENTICATED_HINT,
    ConnectionManager,
    NoVsphereRoleError,
)
from vmware_storage.notify.audit import AuditLogger
from vmware_storage.ops import datastore_browser, datastore_usage  # noqa: F401 — compatibility/runtime export
from vmware_storage.ops import iscsi_config as _iscsi_config  # noqa: F401 — compatibility/runtime export
from vmware_storage.ops import iscsi_gate as _gate
from vmware_storage.ops.datastore_browser import DatastoreBrowseError
from vmware_storage.ops.inventory import list_datastores  # noqa: F401 — compatibility/runtime export

# The 2.0 tool names reuse several ops function names (get_iscsi_status,
# rescan_storage, get_vsan_health, ...). Importing the ops functions under an
# _ops_ alias keeps each tool from shadowing — and then calling — itself.
from vmware_storage.ops.iscsi_config import (  # noqa: F401 — compatibility/runtime export
    HostNotFoundError,
    ISCSIError,
    enable_software_iscsi,
)
from vmware_storage.ops.iscsi_config import add_iscsi_target as _ops_add_iscsi_target  # noqa: F401 — compatibility/runtime export
from vmware_storage.ops.iscsi_config import get_iscsi_status as _ops_get_iscsi_status  # noqa: F401 — compatibility/runtime export
from vmware_storage.ops.iscsi_config import remove_iscsi_target as _ops_remove_iscsi_target  # noqa: F401 — compatibility/runtime export
from vmware_storage.ops.iscsi_config import rescan_storage as _ops_rescan_storage  # noqa: F401 — compatibility/runtime export
from vmware_storage.ops.multipath import device_paths  # noqa: F401 — compatibility/runtime export
from vmware_storage.ops.storage_paths import StoragePathError
from vmware_storage.ops.storage_paths import list_fc_adapters as _ops_list_fc_adapters  # noqa: F401 — compatibility/runtime export
from vmware_storage.ops.vsan import ClusterNotFoundError, VSANError
from vmware_storage.ops.vsan import get_vsan_capacity as _ops_get_vsan_capacity  # noqa: F401 — compatibility/runtime export
from vmware_storage.ops.vsan import get_vsan_health as _ops_get_vsan_health  # noqa: F401 — compatibility/runtime export
from vmware_storage.ops.vsan_efficiency import (  # noqa: F401 — compatibility/runtime export
    get_vsan_efficiency as _ops_get_vsan_efficiency,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vmware-storage.mcp")

def _safe_error(exc: Exception, tool: str) -> str:
    """Return an agent-safe error string; log full detail server-side only.

    Raw exception text can carry API response bodies, internal paths, or
    host:port pairs. Full traceback goes to the server log; the agent sees only
    a control-char-stripped, length-capped message.

    Every exception this skill raises on purpose passes through: the builtin
    validation errors, and the domain exceptions defined under
    ``vmware_storage.ops``. Those domain types exist to carry a corrected next
    step, so omitting them replaced ten of this skill's rewritten messages with
    ``ISCSIError: operation failed.`` on the way to the agent — teaching text
    that the CLI printed in full and the MCP surface silently threw away.

    ``TimeoutError`` and ``ConnectionError`` are here for the same reason: the
    CLI path catches ``OSError`` and prints a retry hint, and the two surfaces
    should not disagree about what a dropped connection means.

    The one configuration error this skill raises on purpose — the
    missing-password error, this family's most common first-run failure, whose
    entire remedy is the env var name it carries — passes through as the narrow
    ``ConfigError``. Bare ``OSError`` was briefly listed here for it and was too
    wide a door: ``sanitize`` strips control characters and truncates, it does
    not redact, so ``ssl.SSLCertVerificationError`` (certificate subject and
    hostname), ``socket.gaierror`` (the name that failed to resolve) and
    ``requests``-style connection errors (full scheme://host:port/path) all
    reached the agent verbatim through it. Only the narrow ``OSError``
    subclasses that were allowed before it remain.

    Anything else is reduced to its type — an unplanned exception's text was
    written for a developer reading a traceback, not for an agent choosing what
    to do next, and it is the one that can carry credentials.
    """
    logger.error("Tool %s failed", tool, exc_info=True)
    # Checked ahead of the allowlist: ssl.SSLCertVerificationError inherits from
    # ValueError as well as OSError, so it matches the ValueError entry — which
    # predates the OSError one — and narrowing the OSError side does not keep it
    # out. Its text quotes the certificate subject and the host. Self-signed
    # certs are this family's most common connection problem, and the operator
    # reads the remedy for them from the CLI, which catches ssl.SSLError itself.
    if isinstance(exc, ssl.SSLError):
        return f"{type(exc).__name__}: operation failed."
    # Both authored here, so nothing from the server travels. The role error is
    # longer than the 300-character cap, and its remedy is at the end (AIops#47).
    if isinstance(exc, NoVsphereRoleError):
        return sanitize(str(exc), 500)
    from pyVmomi import vim

    if isinstance(exc, vim.fault.NotAuthenticated):
        return NOT_AUTHENTICATED_HINT
    _passthrough = (
        ValueError,
        FileNotFoundError,
        KeyError,
        PermissionError,
        TimeoutError,
        ConnectionError,
        ConfigError,
        DatastoreBrowseError,
        HostNotFoundError,
        ISCSIError,
        StoragePathError,
        VSANError,
    )
    if isinstance(exc, _passthrough):
        return sanitize(str(exc), 300)
    return f"{type(exc).__name__}: operation failed."


_DOCTOR_HINT = "Run 'vmware-storage doctor' to verify connectivity."
#: For the two failures where connectivity and credentials are fine (AIops#47).
_ROLE_HINT = (
    "Run 'vmware-storage doctor': it checks every target's login and whether "
    "vCenter gives the account a session."
)


#: For an error this skill authored that is not about reaching the endpoint:
#: the message already names the fix, and doctor would report green (2026-10-17).
_FIX_IN_MESSAGE_HINT = "The error message says what to change; connectivity is not the problem."

#: Authored errors whose cause is the request, not the connection. ssl.SSLError
#: subclasses ValueError and is excluded explicitly below.
#: ClusterNotFoundError and StoragePathError joined 2026-10-24: a wrong cluster
#: name got "verify connectivity" while the host twin of the same message did not.
_REQUEST_ERRORS = (
    ValueError, UnknownTargetError, HostNotFoundError, DatastoreBrowseError,
    ClusterNotFoundError, StoragePathError,
)

#: ValueErrors whose text was not authored as a remedy: a body that was not
#: JSON (a reverse proxy answering HTML, AIops#47) or not UTF-8. Review
#: 2026-10-17: these got "connectivity is not the problem", the opposite of true.
_UNAUTHORED_VALUE_ERRORS = (ssl.SSLError, json.JSONDecodeError, UnicodeError)


def _is_request_error(exc: Exception) -> bool:
    return isinstance(exc, _REQUEST_ERRORS) and not isinstance(exc, _UNAUTHORED_VALUE_ERRORS)


def _hint_for(exc: Exception) -> str:
    from pyVmomi import vim

    if isinstance(exc, (NoVsphereRoleError, vim.fault.NotAuthenticated)):
        return _ROLE_HINT
    if _is_request_error(exc):
        return _FIX_IN_MESSAGE_HINT
    return _DOCTOR_HINT


def _error_reply(exc: Exception, tool: str) -> dict:
    """The error envelope for a write tool, which vmware-policy audits as a failure.

    The four write tools returned plain strings until the confirmation gate
    (HLD §7) made them return dicts; a string error was indistinguishable from
    a success, so this used to call ``report_tool_failure``. The ``{"error"}``
    envelope is detected on its own: the audit row says ``error``, no undo token
    is recorded, and the circuit breaker hears a failure.
    """
    msg = _safe_error(exc, tool)
    if isinstance(exc, _gate.GateRefusedError):
        hint = "Nothing was changed. Call the tool without confirm to see the blast radius."
    else:
        hint = _hint_for(exc)
    return {"error": msg, "hint": hint}


def _preview(radius: dict) -> dict:
    """The L2 answer to a call that did not confirm: the radius, nothing changed."""
    return {
        "action": "preview",
        "blast_radius": radius,
        "hint": "Nothing was changed. Show blast_radius to the user; to apply, "
                "re-run with confirm=True.",
    }


def _acted_undo(action: str, tool: str, note: str):
    """An undo callable that fires only when the call really ``action``-ed.

    vmware-policy recognises a preview only by ``dry_run=True``; a bare
    ``confirm=False`` call is a preview it would otherwise record an undo
    token for — an offer to reverse a change that never happened.
    """
    def _undo(params: dict, result: object) -> Optional[dict]:
        if not (isinstance(result, dict) and result.get("action") == action):
            return None
        return {
            "tool": tool,
            "params": {
                "host_name": params.get("host_name"),
                "address": params.get("address"),
                "port": params.get("port", 3260),
                "target": params.get("target"),
                "confirm": True,
            },
            "skill": "storage",
            "note": note,
        }
    return _undo


_BASE_INSTRUCTIONS = (
    "VMware vSphere storage: browse datastores, scan for deployable images, "
    "configure iSCSI adapters and targets, and read vSAN health/capacity plus "
    "Fibre Channel HBA and multipath state."
)

_TARGET_RULE = (
    " Choosing a target: every tool that reads or changes storage takes `target`. "
    "Choose it from what the user asked. A vCenter, the whole environment, vSAN, "
    "datastores shared by a cluster, or several hosts: a vcenter target. One ESXi "
    "host the user names: the vcenter target that manages it, because vCenter holds "
    "that host's datastores, adapters and paths alongside the rest of the cluster; "
    "use the host's own esxi target only when the user asks about the host directly "
    "or no vCenter manages it. If the request does not say which, and targets of "
    "different types could answer differently, ask the user which one before "
    "querying. A result does not name the target it came from, so say in the answer "
    "which `target` you passed. Note this skill uses the word twice: the `target` "
    "parameter is a configured vCenter or ESXi host, while the `target` field inside "
    "a path row from list_storage_device_paths is the storage array's port (a WWPN or an "
    "iSCSI IQN) — reporting one as the other names the wrong thing entirely."
)


def _target_listing(cfg: AppConfig) -> str:
    """The configured targets as ``name (type, host[, default])`` — or why there are none.

    A config that loads with zero targets is *readable*, just empty: that is a
    fresh install before ``vmware-storage init``. It needs a different remedy from
    a config that could not be read at all, so the two must not collapse into one
    sentence — and neither may collapse into silence.

    Called from inside the caller's ``try`` on purpose: ``AppConfig.default_target``
    raises when the target list is empty, so the empty case is answered here
    before that property is ever asked, and anything unforeseen still lands on the
    caller's fallback instead of stopping the server.
    """
    if not cfg.targets:
        # The documented default path. ``VMWARE_STORAGE_CONFIG`` can point somewhere else;
        # ``vmware-storage doctor`` reports which file it actually resolved, and this
        # one-line remedy does not try to restate that.
        return "none yet — add one under `targets:` in ~/.vmware-storage/config.yaml"
    default_name = cfg.default_target.name
    return "; ".join(
        f"{t.name} ({t.type}, {t.host}{', default' if t.name == default_name else ''})"
        for t in cfg.targets
    )


def _target_instructions() -> str:
    """Server instructions that name the configured targets and how to choose one.

    Until this existed the client was never told which targets are configured, so
    it called every tool with no ``target`` and got whichever one the connection
    layer defaults to. Monitor shipped that way until 2026-09-15: with a standalone
    ESXi host first in the file, "how many VMs does the vCenter have" was answered
    from that host, confidently and wrongly. This skill has the same shape — a
    vcenter target and an esxi target answer "which datastores are there" and
    "which paths are dead" differently.

    The listing is built from the loaded config rather than written out as a
    sentence, so it cannot drift the day the operator edits their file. The
    default is marked by comparing against ``default_target``, which is what
    :meth:`ConnectionManager.connect` itself falls back to when ``target`` is
    omitted — not a guess about which entry comes first.

    Never raises: a missing or broken config must not stop the server from
    starting. The tools report that error themselves, each with its own remedy.
    """
    try:
        detail = _target_listing(load_config())
    except Exception as exc:  # noqa: BLE001 — instructions are advisory, startup is not
        # Say so *under the marker* rather than dropping the listing. Shown no
        # listing at all, a client cannot tell "this skill has no targets" from
        # "this skill could not read them", and the first reading is the one that
        # produces a confident answer about a system nobody chose. Only the
        # exception's type name is interpolated — its text quotes the config path.
        detail = f"could not be read ({type(exc).__name__}) — run `vmware-storage doctor`"
    return f"{_BASE_INSTRUCTIONS} Configured targets: {detail}.{_TARGET_RULE}"


mcp = FastMCP("vmware-storage", instructions=_target_instructions())

# FastMCP takes no version argument and leaves the lowlevel server's at
# None, which makes `initialize` answer with the MCP SDK's version rather
# than ours. Set it so a client can tell which release it is talking to.
mcp._mcp_server.version = __version__

_audit = AuditLogger()


def _safe_audit(**kwargs: Any) -> None:
    """Audit a write operation; never let audit failure mask the op result.

    Family rule: audit failure never blocks — the operation already happened,
    so the agent must see the authoritative result, not an audit traceback.
    """
    try:
        _audit.log(**kwargs)
    except Exception as e:
        logger.warning("Audit logging failed (operation succeeded): %s", e)

# ---------------------------------------------------------------------------
# Connection management (lazy-init singleton)
# ---------------------------------------------------------------------------

_conn_mgr: Optional[ConnectionManager] = None
# Calls run on worker threads side by side (HLD §16 D-13(2)); two first calls
# racing here would each build a manager, and one manager's sessions would be
# orphaned — open logins nobody pools or closes.
_conn_mgr_lock = threading.Lock()


def _get_conn_mgr() -> ConnectionManager:
    global _conn_mgr
    with _conn_mgr_lock:
        if _conn_mgr is None:
            # No env-var read here: load_config resolves the path (explicit arg,
            # then the environment, then the default). This copy was the reason the
            # server and the CLI opened different files — load_config did not look
            # at the variable at all, so only this path honoured it (形态 #6).
            config = load_config()
            _conn_mgr = ConnectionManager(config)
        return _conn_mgr


def _get_connection(target: Optional[str] = None):
    return _get_conn_mgr().connect(target)


# ---------------------------------------------------------------------------
# Datastore tools
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# iSCSI tools
# ---------------------------------------------------------------------------







from vmware_storage.mcp_server._tools_datastores import register_tools as _register_datastores

(
    list_all_datastores,
    get_datastore_vm_usage,
    browse_datastore,
    scan_datastore_images,
    list_cached_images,
    enable_iscsi,
    get_iscsi_status,
    add_iscsi_target,
) = _register_datastores(mcp, _sys.modules[__name__])






# ---------------------------------------------------------------------------
# vSAN tools
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Fibre Channel / multipath tools (read-only)
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Access self-diagnosis (HLD §17 D-17)
# ---------------------------------------------------------------------------



from vmware_storage.mcp_server._tools_storage import register_tools as _register_storage

(
    remove_iscsi_target,
    rescan_storage,
    get_vsan_health,
    get_vsan_capacity,
    get_vsan_efficiency,
    list_fc_adapters,
    list_storage_device_paths,
    get_access_info,
) = _register_storage(mcp, _sys.modules[__name__])


# ---------------------------------------------------------------------------
# Environment declaration
# ---------------------------------------------------------------------------

# The environment resolver lives in policy_environment so the CLI registers
# it too (its @guarded writes go through the same guard()); importing it here
# registers it for the MCP surface.
from vmware_storage.policy_environment import _cached_config, _environment_for  # noqa: E402,F401

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


#: How long a stop signal waits for the session logout before exiting anyway.
#: A logout to a vCenter that answers takes well under a second.
_STOP_LOGOUT_SECONDS = 5.0


def _exit_on_stop_signals() -> None:
    """Turn the signals a client stops this server with into a normal exit.

    Claude Code stops a stdio MCP server with SIGINT and then SIGTERM about a
    millisecond later (measured 2026-09-15). Python's default SIGTERM ends the
    process on the spot, before ``atexit`` runs, so the ``Disconnect`` the
    connection layer registered never happened and every conversation left its
    vCenter session open. ``SystemExit`` is not enough: raised from the handler it unwinds the event
    loop, but interpreter shutdown then waits for anyio's worker thread blocked
    reading stdin, which the client keeps open, so ``atexit`` still never ran
    (independent review, 2026-09-15; a test driving the real stdio loop hung in
    all five skills). So the first stop signal ignores the rest, runs the
    ``atexit`` callbacks, and leaves with ``os._exit`` — nothing waits on that
    thread. The callbacks run on a worker thread with a deadline: pyVmomi
    connects with ``httpConnectionTimeout=None``, so a logout to a vCenter that
    stopped answering, or one waiting on the SOAP stub lock a tool call held
    when the signal landed, otherwise left a server that ignored every stop
    signal and only SIGKILL ended (second independent review, 2026-09-15).
    """
    import atexit
    import os
    import signal
    import threading

    stop_signals = [
        getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, name)
    ]

    def _stop(signum: int, _frame: object) -> None:
        for sig in stop_signals:
            signal.signal(sig, signal.SIG_IGN)
        try:
            logout = threading.Thread(
                target=atexit._run_exitfuncs, name="logout-on-stop", daemon=True
            )
            logout.start()
            logout.join(_STOP_LOGOUT_SECONDS)
            if logout.is_alive():
                # Non-blocking, straight to fd 2: a client that keeps stderr open
                # but stops reading it would otherwise park this write, and the
                # exit, on a full pipe (independent review, 2026-09-15). A
                # message that cannot be written is dropped — exiting matters more.
                message = (
                    f"Session logout did not finish within {_STOP_LOGOUT_SECONDS:.0f}s; "
                    "exiting without it. vCenter or ESXi ends the session when it "
                    "idles out.\n"
                )
                try:
                    os.set_blocking(2, False)
                    os.write(2, message.encode("utf-8", "replace"))
                except OSError:
                    pass
        finally:
            os._exit(128 + signum)

    for sig in stop_signals:
        signal.signal(sig, _stop)


def main(
    expose: Optional[str] = None,
    allow_write: Optional[str] = None,
    transport: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    cert: Optional[str] = None,
    key: Optional[str] = None,
    public_name: Optional[str] = None,
    token_file: Optional[str] = None,
) -> None:
    """Run the MCP server."""
    _exit_on_stop_signals()
    from vmware_policy.serve import run_server
    run_server(
        mcp,
        skill="storage",
        pooled=True,
        expose=expose, allow_write=allow_write, transport=transport, host=host, port=port,
        cert=cert, key=key, public_name=public_name, token_file=token_file,
    )

# The docstrings above are the schema. `describe_tool_parameters` copies each
# `Args:` entry into the JSON schema an agent actually reads, and closes the
# object. Without it every parameter reaches the model as a bare name and a
# type, which is how a wrong guess becomes an unfiltered result or a silent
# zero-row answer instead of an error (real-hardware round, 2026-08-30).
_DESCRIBED_PARAMS = describe_tool_parameters(mcp._tool_manager._tools)
