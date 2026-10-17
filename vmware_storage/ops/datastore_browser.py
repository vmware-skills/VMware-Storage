"""Datastore file browsing and image discovery.

Browses vSphere datastores to find OVA, ISO, OVF, and VMDK files.
Maintains a local image registry (cache) for quick selection during deployment.

Security: All file names and paths returned from vSphere are sanitized to
strip control characters that could be used for prompt injection attacks
when this data flows to downstream LLM agents.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import paginated, sanitize

from vmware_storage._filelock import FileLockTimeoutError, locked
from vmware_storage.config import CONFIG_DIR
from vmware_storage.ops.inventory import (
    find_datastore_by_name,
    list_datastores,
)
from vmware_storage.ops.inventory import (
    not_found_hint as _not_found_hint,
)

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

_log = logging.getLogger("vmware-storage.datastore")

IMAGE_REGISTRY_FILE = CONFIG_DIR / "image_registry.json"

# File patterns for deployable images
IMAGE_PATTERNS = ("*.ova", "*.ovf", "*.iso", "*.vmdk")


class DatastoreBrowseError(Exception):
    """Raised when a datastore browse task fails.

    A domain type for the same reason as ``ISCSIError`` and ``VSANError``: the
    message carries the corrected next step, and ``_safe_error`` only lets a
    message through when it recognises the exception. This was a ``RuntimeError``
    — deliberately off that allowlist, since an unplanned exception's text is
    what can carry credentials — so the remedy written here reached the CLI and
    was replaced by ``RuntimeError: operation failed.`` on the way to an agent.
    """


def _normalize_ds_path(path: str) -> str:
    """Return a datastore-relative browse path, or refuse it with the fix.

    The path is interpolated into a ``[datastore] <path>`` spec for the vSphere
    DatastoreBrowser API. ``..`` traversal, absolute paths (leading ``/`` or
    ``\\``) and null bytes are refused -- the rule vmware-storage adopted in its
    1.5.35 security hardening. vmware-aiops used to pass ``/vcsa8`` straight
    through while vmware-storage refused it; both now refuse it and say to drop
    the leading slash, so an agent gets one answer from either skill.
    """
    if ".." in path or path.startswith(("/", "\\")) or "\x00" in path:
        hint = (
            f" Drop the leading slash: path={path.lstrip('/')!r}."
            if path.startswith("/") and ".." not in path and "\x00" not in path
            else ""
        )
        raise ValueError(
            f"Invalid datastore path {path!r}: no '..', absolute paths, or null bytes. "
            "Pass browse_datastore a datastore-relative sub-path such as 'templates' "
            f"or 'iso/linux', or '' to browse the datastore root.{hint}"
        )
    return path


def _wait_for_task(task, timeout: int = 300) -> object:
    """Wait for a datastore-browse task to complete.

    Browsing a large or busy datastore (and scan_images, which browses once per
    image pattern) can legitimately take a while, so the budget is 300s. On
    timeout the message is actionable — narrow the search rather than retrying
    the same broad browse — because a search task has no pollable result to
    resume (unlike a write task, where the operation keeps running).
    """
    start = time.time()
    while task.info.state in (vim.TaskInfo.State.running, vim.TaskInfo.State.queued):
        if time.time() - start > timeout:
            raise TimeoutError(
                f"Datastore browse did not finish within {timeout}s — the datastore "
                "is very large or busy. Re-run browse_datastore (or "
                "scan_datastore_images) with a narrower sub-path and a specific "
                "pattern (e.g. path='templates', pattern='*.ova') instead of "
                "browsing the root; do not just retry the same broad browse."
            )
        time.sleep(1)
    if task.info.state == vim.TaskInfo.State.success:
        return task.info.result
    error_msg = str(task.info.error.msg) if task.info.error else "Unknown error"
    # Cap the vCenter fault text: it is unbounded, and the remedy that follows
    # it has to survive the MCP layer's 300-char sanitize truncation. 120 keeps
    # the whole message at 294 in the worst case (same cap as the vmware-aiops
    # twin of this message).
    raise DatastoreBrowseError(
        f"Datastore browse failed: {error_msg[:120]}. Check the datastore is "
        "reachable with list_all_datastores (accessible must be true), then "
        "re-run browse_datastore with a narrower path and pattern."
    )


def _join_ds_path(folder: str, name: str) -> str:
    """Join a browse result's ``folderPath`` and a file name into a datastore path.

    ESXi reports ``"[ds]"`` / ``"[ds] dir"`` with no trailing separator; vCenter
    reports ``"[ds] "`` / ``"[ds] dir/"``. Both must yield ``"[ds] dir/name"``.
    """
    folder = folder.rstrip()
    if folder.endswith("]"):
        return f"{folder} {name}"
    if folder.endswith("/"):
        return f"{folder}{name}"
    return f"{folder}/{name}"


def browse_datastore(
    si: ServiceInstance,
    ds_name: str,
    path: str = "",
    pattern: str = "*",
) -> dict:
    """Search a datastore directory and all of its subfolders.

    The search is recursive (``SearchDatastoreSubFolders``): browsing the root
    returns matches from every folder on the datastore. Folders themselves come
    back as rows too. Each row's ``ds_path`` is ``"[ds] dir/file"``, ready to
    pass to deploy/attach tools.

    Args:
        si: vSphere ServiceInstance
        ds_name: Datastore name
        path: Folder to start from (empty for root)
        pattern: Glob pattern to filter files (e.g. "*.ova", "*")

    Returns:
        The family list envelope; ``items`` holds file dicts with name, size,
        type, modified, ds_path. The browse task returns every match in the
        searched folders, so ``total`` is the real count and nothing is
        truncated.
    """
    ds = find_datastore_by_name(si, ds_name)
    if ds is None:
        raise ValueError(
            f"Datastore '{ds_name}' not found on this target. Run list_all_datastores "
            f"to see every datastore name and copy an exact one — names are "
            f"case-sensitive."
            f"{_not_found_hint(ds_name, [d['name'] for d in list_datastores(si)['items']])}"
        )

    browser = ds.browser
    search_spec = vim.host.DatastoreBrowser.SearchSpec()
    search_spec.matchPattern = [pattern]
    search_spec.details = vim.host.DatastoreBrowser.FileInfo.Details(
        fileType=True,
        fileSize=True,
        modification=True,
    )
    search_spec.query = [
        vim.host.DatastoreBrowser.IsoImageQuery(),
        vim.host.DatastoreBrowser.VmDiskQuery(),
        vim.host.DatastoreBrowser.FolderQuery(),
        # Generic file query — without it the browser only returns ISO/VMDK/
        # folder entries, so .ova/.ovf (and any other plain file) are silently
        # dropped from browse_datastore and scan_images results.
        vim.host.DatastoreBrowser.Query(),
    ]

    path = _normalize_ds_path(path)
    ds_path = f"[{ds_name}] {path}".rstrip()
    task = browser.SearchDatastoreSubFolders_Task(
        datastorePath=ds_path,
        searchSpec=search_spec,
    )
    results_raw = _wait_for_task(task)

    files: list[dict] = []
    for result in results_raw:
        folder = sanitize(result.folderPath)
        for f in result.file:
            file_type = type(f).__name__.replace("Info", "")
            fname = sanitize(f.path)
            files.append({
                "name": fname,
                "size_mb": round(f.fileSize / (1024 * 1024), 1) if f.fileSize else 0,
                "type": file_type,
                "modified": str(f.modification) if f.modification else "",
                "ds_path": sanitize(_join_ds_path(folder, f.path)),
            })

    rows = sorted(files, key=lambda x: x["name"])
    return paginated(rows, total=len(rows))


def scan_images(
    si: ServiceInstance,
    ds_name: str,
    path: str = "",
    target: str | None = None,
) -> dict:
    """Scan a datastore for deployable images (OVA, ISO, OVF, VMDK).

    Returns the family list envelope; every image pattern is browsed in full,
    so ``total`` is the real count and nothing is truncated.

    The result also refreshes the local image registry that list_cached_images
    reads: entries for this target's datastore under the scanned folder are
    replaced (``target`` keeps two hosts' "datastore1" apart). Before 2026-10-17
    nothing wrote the registry at all, so list_cached_images was always empty. A
    cache that cannot be read or written does not fail the scan; the envelope
    says so in ``cache_note``.
    """
    all_images: list[dict] = []
    for pattern in IMAGE_PATTERNS:
        found = browse_datastore(si, ds_name, path=path, pattern=pattern)
        all_images.extend(found["items"])

    rows = sorted(all_images, key=lambda x: x["name"])
    result = paginated(rows, total=len(rows))
    try:
        _record_scan(ds_name, path, rows, target)
    except (OSError, DatastoreBrowseError) as exc:
        _log.warning("Image registry not updated after scanning %s: %s", ds_name, exc)
        # A DatastoreBrowseError message is authored (it names the file and the
        # fix); an OSError's text is not ours, so only its type is passed on.
        why = str(exc) if isinstance(exc, DatastoreBrowseError) else type(exc).__name__
        result["cache_note"] = (
            f"The scan succeeded but the local image cache could not be updated "
            f"({why}), so list_cached_images will not show these images."
        )
    return result


def _record_scan(
    ds_name: str, path: str, images: list[dict], target: str | None = None,
) -> None:
    """Replace this target's cached entries for ``ds_name`` under ``path``."""
    rel = posixpath.normpath(_normalize_ds_path(path)) if path else ""
    rel = "" if rel == "." else rel.rstrip("/")
    prefix = f"[{ds_name}] {rel}/" if rel else f"[{ds_name}] "

    def _covered(entry: dict) -> bool:
        # A row written before targets were recorded has no "target" key; it is
        # replaced by the next scan of its datastore rather than lingering as a
        # duplicate forever (narrow review 2026-10-17).
        same_target = "target" not in entry or entry["target"] == target
        same = entry.get("datastore") == ds_name and same_target
        return same and str(entry.get("ds_path", "")).startswith(prefix)

    fresh = [
        {
            "target": target,
            "datastore": ds_name,
            "name": img["name"],
            "ds_path": img["ds_path"],
            "size_mb": img["size_mb"],
            "type": img["type"],
            "modified": img["modified"],
        }
        for img in images
    ]
    # Read-modify-write under both locks: HTTPS callers scan side by side in
    # threads, and a CLI scan is another process writing the same file.
    with _REGISTRY_LOCK, _registry_file_lock():
        registry = _load_registry()
        kept = [e for e in registry["images"] if not _covered(e)]
        _save_registry({
            "images": kept + fresh,
            "last_scan": datetime.now(timezone.utc).isoformat(),
        })


# ─── Image Registry (local cache) ────────────────────────────────────────────


#: Serialises read-modify-write of the registry within this process.
_REGISTRY_LOCK = threading.Lock()

#: How long a writer waits for another process's registry write to finish.
#: A write is one small JSON file, so this only expires if something is stuck.
REGISTRY_LOCK_TIMEOUT_S = 10.0


@contextmanager
def _registry_file_lock() -> Iterator[None]:
    """Serialise registry writes across processes (a sibling ``.lock`` file)."""
    lock_path = IMAGE_REGISTRY_FILE.with_name(IMAGE_REGISTRY_FILE.name + ".lock")
    try:
        with locked(lock_path, timeout=REGISTRY_LOCK_TIMEOUT_S):
            yield
    except FileLockTimeoutError as exc:
        raise DatastoreBrowseError(str(exc)) from exc


def _load_registry() -> dict:
    """Load the local image registry, or refuse it with the way out."""
    if not IMAGE_REGISTRY_FILE.exists():
        return {"images": [], "last_scan": None}
    try:
        with open(IMAGE_REGISTRY_FILE, encoding="utf-8") as f:
            registry = json.load(f)
    except ValueError as exc:  # JSONDecodeError, UnicodeDecodeError
        registry = exc
    if not isinstance(registry, dict) or not isinstance(registry.get("images"), list):
        raise DatastoreBrowseError(
            f"The local image cache {IMAGE_REGISTRY_FILE} is unreadable. Delete it "
            "and run scan_datastore_images again to rebuild it."
        )
    return registry


def _save_registry(registry: dict) -> None:
    """Replace the registry atomically (owner-only — infra topology metadata).

    Written to a temporary file in the same directory and moved into place, so
    a reader never sees half a file and a failed write leaves the old one.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    text = json.dumps(registry, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".image_registry.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:  # mkstemp creates it 0600
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, IMAGE_REGISTRY_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_registry() -> dict:
    """Get the current image registry (from local cache)."""
    return _load_registry()


def list_images(
    image_type: str | None = None,
    datastore: str | None = None,
    target: str | None = None,
) -> dict:
    """List images from the local registry, with optional filters.

    ``target`` keeps only rows that target's scans wrote; rows cached before
    rows carried a target cannot be attributed and appear only unfiltered.

    Returns the family list envelope. The whole registry is read from disk and
    filtered in memory, so ``total`` is the real count of matching images and
    nothing is truncated.
    """
    registry = _load_registry()
    images = registry.get("images", [])

    if image_type:
        ext = f".{image_type.lower().lstrip('.')}"
        images = [i for i in images if i["name"].lower().endswith(ext)]
    if datastore:
        images = [i for i in images if i["datastore"] == datastore]
    if target is not None:
        images = [i for i in images if i.get("target") == target]

    return paginated(images, total=len(images))
