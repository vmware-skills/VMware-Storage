"""HLD §16 D-13(2) acceptance: calls never share a pyVmomi session.

Driven through the real ConnectionManager; only the session factory
(``SmartConnect``) is replaced by fakes, so nothing touches the network.
Each MCP call on a network transport runs inside ``call_scope()``; these tests
open that scope themselves on worker threads, as vmware-policy does.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
from vmware_policy.pool import call_scope

from vmware_storage import connection
from vmware_storage.config import AppConfig, TargetConfig
from vmware_storage.connection import ConnectionManager

WAIT = 5.0


def _target(name: str) -> TargetConfig:
    return TargetConfig(
        name=name, host=f"{name}.example.com", config_username="admin@vsphere.local"
    )


class _Sessions:
    """Fake SmartConnect: hands out live fake sessions and tracks concurrent use."""

    def __init__(self) -> None:
        self.made: list[MagicMock] = []
        self.in_use: set[int] = set()
        self.shared_use: list[int] = []
        self._lock = threading.Lock()

    def smart_connect(self, **kwargs: object) -> MagicMock:
        si = MagicMock(name=f"si-{kwargs.get('host')}-{len(self.made)}")
        si.content.sessionManager.currentSession = MagicMock()
        with self._lock:
            self.made.append(si)
        return si

    def use(self, si: MagicMock) -> None:
        with self._lock:
            if id(si) in self.in_use:
                self.shared_use.append(id(si))
            self.in_use.add(id(si))

    def done(self, si: MagicMock) -> None:
        with self._lock:
            self.in_use.discard(id(si))


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VMWARE_MCP_POOL_SIZE", "2")
    monkeypatch.setenv("VMWARE_VC1_PASSWORD", "fake")
    monkeypatch.setenv("VMWARE_VC2_PASSWORD", "fake")
    sessions = _Sessions()
    mgr = ConnectionManager(AppConfig(targets=(_target("vc1"), _target("vc2"))))
    with (
        patch("pyVim.connect.SmartConnect", side_effect=sessions.smart_connect),
        patch("pyVim.connect.Disconnect") as disconnect,
    ):
        yield mgr, sessions, disconnect
        mgr.disconnect_all()


def _run(fn, *args) -> tuple[threading.Thread, list]:
    errors: list = []

    def body() -> None:
        try:
            fn(*args)
        except BaseException as exc:  # surfaced by the test, not swallowed
            errors.append(exc)

    t = threading.Thread(target=body)
    t.start()
    return t, errors


def test_concurrent_calls_on_one_target_never_share_a_session(env) -> None:
    mgr, sessions, _ = env
    both_hold = threading.Barrier(2, timeout=WAIT)
    got: list[MagicMock] = []

    def call() -> None:
        with call_scope():
            si = mgr.connect("vc1")
            sessions.use(si)
            got.append(si)
            both_hold.wait()  # both calls hold a session at the same moment
            assert mgr.connect("vc1") is si  # same call, same session
            sessions.done(si)

    runs = [_run(call) for _ in range(2)]
    for t, errors in runs:
        t.join(WAIT)
        assert not errors, errors
    assert len(got) == 2 and got[0] is not got[1]
    assert sessions.shared_use == []


def test_a_long_call_leaves_the_other_session_usable(env) -> None:
    mgr, sessions, _ = env
    long_holds = threading.Event()
    release_long = threading.Event()
    held: dict[str, MagicMock] = {}

    def long_call() -> None:
        with call_scope():
            held["long"] = mgr.connect("vc1")
            long_holds.set()
            release_long.wait(WAIT)

    t, errors = _run(long_call)
    assert long_holds.wait(WAIT)
    try:
        for n in range(3):  # short calls one after another, all while it runs

            def short_call(n: int = n) -> None:
                with call_scope():
                    held[f"short{n}"] = mgr.connect("vc1")

            s, s_errors = _run(short_call)
            s.join(WAIT)
            assert not s.is_alive(), "a short call waited behind the long one"
            assert not s_errors, s_errors
            assert held[f"short{n}"] is not held["long"]
        assert held["short0"] is held["short1"] is held["short2"]  # returned and reused
    finally:
        release_long.set()
        t.join(WAIT)
    assert not errors, errors


def test_calls_on_different_targets_overlap(env) -> None:
    mgr, _, _ = env
    overlap = threading.Barrier(2, timeout=WAIT)

    def call(name: str) -> None:
        with call_scope():
            mgr.connect(name)
            overlap.wait()  # breaks (BrokenBarrierError) if they ran one at a time

    runs = [_run(call, "vc1"), _run(call, "vc2")]
    for t, errors in runs:
        t.join(WAIT)
        assert not errors, errors


def test_outside_a_call_the_session_is_reused(env) -> None:
    mgr, sessions, _ = env
    first = mgr.connect("vc1")
    assert mgr.connect("vc1") is first
    assert len(sessions.made) == 1
    assert mgr.list_connected() == ["vc1"]


def test_a_dead_session_is_closed_and_replaced(env) -> None:
    mgr, sessions, disconnect = env
    dead = mgr.connect("vc1")
    assert id(dead) in connection._SI_ATEXIT
    dead.content.sessionManager.currentSession = None  # expired token

    fresh = mgr.connect("vc1")

    assert fresh is not dead
    disconnect.assert_any_call(dead)
    assert id(dead) not in connection._SI_ATEXIT, "atexit still pins the dead session"
    assert id(dead) not in connection._SI_VERIFY_SSL


def test_a_dead_pooled_session_is_closed_and_replaced(env) -> None:
    mgr, _, disconnect = env
    with call_scope():
        dead = mgr.connect("vc1")
    dead.content.sessionManager.currentSession = None
    with call_scope():
        fresh = mgr.connect("vc1")
    assert fresh is not dead
    disconnect.assert_any_call(dead)
    assert id(dead) not in connection._SI_ATEXIT


def test_no_target_name_pools_under_the_default_target(env) -> None:
    mgr, _, _ = env
    with call_scope():
        si = mgr.connect()
        assert mgr.connect("vc1") is si  # vc1 is the first, default target
    assert mgr.list_connected() == ["vc1"]


def test_first_calls_build_one_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Racing first calls must not each build a manager (orphaned logins)."""
    import time

    from vmware_storage.mcp_server import server

    builds: list[int] = []

    def slow_load_config() -> AppConfig:
        builds.append(1)
        time.sleep(0.05)  # widen the window two unlocked first calls would race in
        return AppConfig(targets=(_target("vc1"),))

    monkeypatch.setattr(server, "load_config", slow_load_config)
    monkeypatch.setattr(server, "_conn_mgr", None)
    got: list[ConnectionManager] = []
    runs = [_run(lambda: got.append(server._get_conn_mgr())) for _ in range(4)]
    for t, errors in runs:
        t.join(WAIT)
        assert not errors, errors
    assert len(builds) == 1
    assert all(m is got[0] for m in got)
