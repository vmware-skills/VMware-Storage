"""Verify vmware-storage has zero VM lifecycle operations.

This test ensures the storage skill never gains VM create/delete/power
functionality — those belong in vmware-aiops.
"""

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
# mcp_server now lives inside the package, so rglob over vmware_storage covers it.
SOURCE_DIRS = [REPO_ROOT / "vmware_storage"]

FORBIDDEN_PATTERNS = [
    "PowerOnVM_Task",
    "PowerOffVM_Task",
    "Destroy_Task",
    "CreateVM_Task",
    "CloneVM_Task",
    "MigrateVM_Task",
    "ReconfigVM_Task",
    "CreateSnapshot_Task",
    "RemoveSnapshot_Task",
    "RevertToSnapshot_Task",
]


@pytest.mark.unit
def test_no_vm_lifecycle_code():
    """Source code must not contain VM lifecycle API calls."""
    violations = []
    for source_dir in SOURCE_DIRS:
        for py_file in source_dir.rglob("*.py"):
            content = py_file.read_text()
            for pattern in FORBIDDEN_PATTERNS:
                if pattern in content:
                    violations.append(f"{py_file.relative_to(REPO_ROOT)}: contains '{pattern}'")

    assert not violations, "VM lifecycle code found in storage skill:\n" + "\n".join(violations)
