"""Two processes scanning at once must not drop each other's cached images.

Review 2026-10-18: the registry's read-modify-write was guarded by a
``threading.Lock``, which only serialises one process. A CLI scan and an MCP
server scan running side by side each read the old file, and the last
``os.replace`` wrote the other's rows away. The atomic replace stops a reader
seeing half a file; it does nothing for a lost update.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from vmware_storage.ops import datastore_browser as dsb

_WRITERS = 4

_CHILD = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from vmware_storage.ops import datastore_browser as dsb

    reg, target = Path(sys.argv[1]), sys.argv[2]
    dsb.IMAGE_REGISTRY_FILE = reg
    dsb.CONFIG_DIR = reg.parent
    real_load = dsb._load_registry

    def slow_load():
        # Widen the read-modify-write window so the race is not left to luck.
        data = real_load()
        time.sleep(0.3)
        return data

    dsb._load_registry = slow_load
    (reg.parent / f"ready-{target}").touch()
    go = reg.parent / "go"
    deadline = time.monotonic() + 60
    while not go.exists():
        if time.monotonic() > deadline:
            sys.exit("parent never said go")
        time.sleep(0.01)
    img = {"name": f"{target}.iso", "ds_path": f"[datastore1] {target}.iso",
           "size_mb": 1.0, "type": "File", "modified": ""}
    dsb._record_scan("datastore1", "", [img], target)
    """
)


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    # The children must import exactly the code this test process imported.
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return env


def _run_writers(reg: Path, script: str, targets: list[str]) -> None:
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(reg), t], env=_child_env())
        for t in targets
    ]
    try:
        deadline = time.monotonic() + 60
        while not all((reg.parent / f"ready-{t}").exists() for t in targets):
            assert time.monotonic() < deadline, "writers never became ready"
            assert all(p.poll() is None for p in procs), "a writer exited early"
            time.sleep(0.02)
        (reg.parent / "go").touch()
        for p in procs:
            assert p.wait(timeout=60) == 0
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()


@pytest.mark.unit
def test_concurrent_scans_in_separate_processes_keep_every_row(tmp_path) -> None:
    reg = tmp_path / "image_registry.json"
    targets = [f"t{i}" for i in range(_WRITERS)]

    _run_writers(reg, _CHILD, targets)

    rows = json.loads(reg.read_text(encoding="utf-8"))["images"]
    assert sorted(r["target"] for r in rows) == targets


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "image_registry.json"
    monkeypatch.setattr(dsb, "IMAGE_REGISTRY_FILE", path)
    monkeypatch.setattr(dsb, "CONFIG_DIR", tmp_path)
    return path


def _hold_lock(reg: Path):
    fcntl = pytest.importorskip("fcntl")
    fh = open(reg.with_name(reg.name + ".lock"), "a+b")  # noqa: SIM115
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh


_IMG = {"name": "x.iso", "ds_path": "[ds1] x.iso", "size_mb": 1.0,
        "type": "File", "modified": ""}


@pytest.mark.unit
def test_a_held_lock_is_a_bounded_wait_that_names_the_lock_file(registry, monkeypatch) -> None:
    monkeypatch.setattr(dsb, "REGISTRY_LOCK_TIMEOUT_S", 0.3, raising=False)
    holder = _hold_lock(registry)
    try:
        started = time.monotonic()
        with pytest.raises(dsb.DatastoreBrowseError, match=r"image_registry\.json\.lock"):
            dsb._record_scan("ds1", "", [_IMG], "vc")
        assert time.monotonic() - started < 5
    finally:
        holder.close()
    assert not registry.exists(), "nothing may be written without the lock"


@pytest.mark.unit
def test_scan_reports_the_held_lock_in_cache_note(registry, monkeypatch) -> None:
    monkeypatch.setattr(dsb, "REGISTRY_LOCK_TIMEOUT_S", 0.3, raising=False)
    monkeypatch.setattr(
        dsb, "browse_datastore",
        lambda si, ds, path="", pattern="*": {"items": [_IMG] if pattern == "*.iso" else []},
    )
    holder = _hold_lock(registry)
    try:
        result = dsb.scan_images(object(), "ds1", target="vc")
    finally:
        holder.close()
    assert [i["name"] for i in result["items"]] == ["x.iso"]
    assert "image_registry.json.lock" in result.get("cache_note", "")


@pytest.mark.unit
def test_the_lock_is_released_after_a_write(registry) -> None:
    dsb._record_scan("ds1", "", [_IMG], "vc")
    holder = _hold_lock(registry)  # would raise BlockingIOError if still held
    holder.close()
