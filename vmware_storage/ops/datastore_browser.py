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
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import paginated, sanitize

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


def _validate_ds_path(path: str) -> None:
    """Reject traversal/injection in a datastore-relative browse path.

    The path is interpolated into a ``[datastore] <path>`` spec for the vSphere
    DatastoreBrowser API. Block ``..`` traversal, absolute paths, and null bytes
    so a caller cannot try to escape the datastore root.
    """
    if ".." in path or path.startswith(("/", "\\")) or "\x00" in path:
        raise ValueError(
            f"Invalid datastore path {path!r}: no '..', absolute paths, or null bytes. "
            "Pass browse_datastore a datastore-relative sub-path such as 'templates' "
            "or 'iso/linux', or '' to browse the datastore root."
        )


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


def browse_datastore(
    si: ServiceInstance,
    ds_name: str,
    path: str = "",
    pattern: str = "*",
) -> dict:
    """Browse files in a datastore directory.

    Args:
        si: vSphere ServiceInstance
        ds_name: Datastore name
        path: Subdirectory path (empty for root)
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

    _validate_ds_path(path)
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
                "ds_path": sanitize(f"{folder}{f.path}"),
            })

    rows = sorted(files, key=lambda x: x["name"])
    return paginated(rows, total=len(rows))


def scan_images(
    si: ServiceInstance,
    ds_name: str,
    path: str = "",
) -> dict:
    """Scan a datastore for deployable images (OVA, ISO, OVF, VMDK).

    Returns the family list envelope; every image pattern is browsed in full,
    so ``total`` is the real count and nothing is truncated.
    """
    all_images: list[dict] = []
    for pattern in IMAGE_PATTERNS:
        found = browse_datastore(si, ds_name, path=path, pattern=pattern)
        all_images.extend(found["items"])

    rows = sorted(all_images, key=lambda x: x["name"])
    return paginated(rows, total=len(rows))


def scan_all_datastores(si: ServiceInstance) -> dict[str, list[dict]]:
    """Scan all accessible datastores for deployable images."""
    datastores = list_datastores(si)["items"]
    result: dict[str, list[dict]] = {}
    for ds in datastores:
        if not ds["accessible"]:
            _log.info("Skipping inaccessible datastore: %s", ds["name"])
            continue
        try:
            images = scan_images(si, ds["name"])["items"]
            if images:
                result[ds["name"]] = images
        except Exception as e:
            _log.warning("Failed to scan datastore %s: %s", ds["name"], e)

    return result


# ─── Image Registry (local cache) ────────────────────────────────────────────


def _load_registry() -> dict:
    """Load the local image registry from disk."""
    if not IMAGE_REGISTRY_FILE.exists():
        return {"images": [], "last_scan": None}
    with open(IMAGE_REGISTRY_FILE) as f:
        return json.load(f)


def _save_registry(registry: dict) -> None:
    """Save the image registry to disk (owner-only — infra topology metadata)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    existed = IMAGE_REGISTRY_FILE.exists()
    with open(IMAGE_REGISTRY_FILE, "w") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)
    if not existed:
        try:
            os.chmod(IMAGE_REGISTRY_FILE, 0o600)
        except OSError:
            pass


def update_registry(si: ServiceInstance) -> dict:
    """Scan all datastores and update the local image registry."""
    scan_result = scan_all_datastores(si)
    images: list[dict] = []
    for ds_name, ds_images in scan_result.items():
        for img in ds_images:
            images.append({
                "datastore": ds_name,
                "name": img["name"],
                "ds_path": img["ds_path"],
                "size_mb": img["size_mb"],
                "type": img["type"],
                "modified": img["modified"],
            })

    registry = {
        "images": images,
        "last_scan": datetime.now(timezone.utc).isoformat(),
    }
    _save_registry(registry)
    _log.info("Image registry updated: %d images across %d datastores",
              len(images), len(scan_result))
    return registry


def get_registry() -> dict:
    """Get the current image registry (from local cache)."""
    return _load_registry()


def list_images(
    image_type: str | None = None,
    datastore: str | None = None,
) -> dict:
    """List images from the local registry, with optional filters.

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

    return paginated(images, total=len(images))
