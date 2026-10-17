"""The inter-process registry lock: Windows branch and bounded wait.

The family runs on Windows, where ``fcntl`` does not exist. That branch cannot
run on the POSIX CI host, so it is driven here with a stand-in ``msvcrt`` that
records the calls and reports the lock as busy a set number of times.
"""

from __future__ import annotations

import errno
import types

import pytest

from vmware_storage import _filelock


class _FakeMsvcrt(types.SimpleNamespace):
    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self, busy: int) -> None:
        super().__init__(calls=[], busy=busy)

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((mode, nbytes))
        if mode == self.LK_NBLCK and self.busy > 0:
            self.busy -= 1
            raise OSError(errno.EACCES, "Permission denied")  # msvcrt: busy


@pytest.mark.unit
def test_windows_branch_retries_then_locks_and_unlocks(tmp_path, monkeypatch) -> None:
    fake = _FakeMsvcrt(busy=2)
    monkeypatch.setattr(_filelock, "msvcrt", fake)
    monkeypatch.setattr(_filelock, "fcntl", None)

    with _filelock.locked(tmp_path / "reg.json.lock", timeout=5, poll=0.01):
        pass

    assert fake.calls == [(2, 1), (2, 1), (2, 1), (0, 1)]


@pytest.mark.unit
def test_windows_branch_gives_up_and_names_the_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(_filelock, "msvcrt", _FakeMsvcrt(busy=10**9))
    monkeypatch.setattr(_filelock, "fcntl", None)
    lock = tmp_path / "reg.json.lock"

    with pytest.raises(_filelock.FileLockTimeoutError, match="reg.json.lock"):
        with _filelock.locked(lock, timeout=0.1, poll=0.01):
            pytest.fail("entered the block without the lock")


@pytest.mark.unit
def test_the_lock_file_is_owner_only(tmp_path) -> None:
    import os
    import stat

    lock = tmp_path / "sub" / "reg.json.lock"
    with _filelock.locked(lock, timeout=1):
        pass
    if os.name == "posix":
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600


@pytest.mark.unit
def test_a_lock_error_that_is_not_busy_is_raised_at_once(tmp_path, monkeypatch) -> None:
    """No flock on this filesystem is not "another process holds it" (review 2026-10-18)."""
    if _filelock.fcntl is None:
        pytest.skip("POSIX branch")

    def no_locks(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(_filelock.fcntl, "flock", no_locks)
    with pytest.raises(OSError) as info:
        with _filelock.locked(tmp_path / "r.lock", timeout=5.0, poll=0.01):
            pass
    assert info.value.errno == errno.ENOLCK
    assert not isinstance(info.value, _filelock.FileLockTimeoutError)
