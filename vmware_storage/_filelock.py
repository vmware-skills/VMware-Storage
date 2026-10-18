"""An inter-process lock on a sibling lock file, with a bounded wait.

A ``threading.Lock`` serialises one process only. The CLI and the MCP server
are separate processes that read-modify-write the same JSON files, so each
could read the old file and the last ``os.replace`` would drop the other's
rows. This lock covers the read-modify-write across processes; the atomic
replace still stops a lock-free reader seeing half a file.

POSIX uses ``fcntl.flock``; Windows (the family runs there too) uses
``msvcrt.locking`` on the first byte. Both are released by the OS if the
holder dies, so a crashed writer cannot leave the file locked. A lock that is
still busy after ``timeout`` seconds raises :class:`FileLockTimeoutError` naming
the lock file instead of waiting forever.
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None  # type: ignore[assignment]


#: The errnos that mean "another holder has it": EWOULDBLOCK/EAGAIN from flock,
#: EACCES/EDEADLOCK from msvcrt.locking.
_BUSY = frozenset(
    e for e in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES,
                getattr(errno, "EDEADLOCK", None), getattr(errno, "EDEADLK", None))
    if e is not None
)


class FileLockTimeoutError(TimeoutError):
    """The lock file stayed locked by another process for the whole wait."""


def _try_lock(fd: int) -> bool:
    """Take the lock without blocking; False if another holder has it."""
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        if exc.errno in _BUSY:
            return False
        # Not "someone else holds it" (no flock on this filesystem, NFS without
        # lockd): say so now, not as a false "held by another process" after
        # the whole wait (review 2026-10-18).
        raise
    return True


def _unlock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextmanager
def locked(lock_path: Path, timeout: float, poll: float = 0.05) -> Iterator[None]:
    """Hold an exclusive lock on ``lock_path`` (created 0600) for the block."""
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise FileLockTimeoutError(
                    f"Another vmware process held the lock file {lock_path} for "
                    f"{timeout:g}s. Wait for its write to finish and retry; the "
                    "lock is released as soon as that process ends."
                )
            time.sleep(poll)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
