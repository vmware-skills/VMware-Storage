"""Every CLI command that performs a write is wrapped by @guarded (HLD I-1, I-8).

A write CLI command must route through vmware_policy's guard() + audit_call() —
the same enforcement @vmware_tool gives the MCP surface — so ``vmware-storage
iscsi remove-target`` run through Bash is authorized and audited to
~/.vmware/audit.db exactly like the ``storage_iscsi_remove_target`` MCP tool.
Without @guarded a CLI write bypassed policy and landed only in the legacy
per-skill log (the gap HLD §2.1 documents).

The write set is DERIVED, never hand-listed (踩坑 #43): a tool annotated
``readOnlyHint=False`` is a write; the ops functions its body calls — reached by
a bare name OR ``module.func`` on an ops-module import — are the state-changing
ops; a CLI ``@command`` calling one is a write command and must carry @guarded.
Both call shapes are handled so this test stays correct if this skill later
aliases or module-qualifies its ops imports the way the REST skills do.

This skill keeps its CLI in a single ``cli.py`` and defines its MCP tools inline
in ``mcp_server/server.py`` (AIops uses a ``cli/`` package and a
``mcp_server/tools/`` package); ``_pyfiles`` scans either shape.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

_REPO = pathlib.Path(__file__).resolve().parents[3]
CLI_SRC = _REPO / "vmware_storage" / "cli.py"
TOOLS_SRC = _REPO / "vmware_storage" / "mcp_server" / "server.py"
assert CLI_SRC.is_file(), f"CLI module not found at {CLI_SRC} — the scan would find nothing"
assert TOOLS_SRC.is_file(), f"MCP tools not found at {TOOLS_SRC} — the derivation would be empty"


def _pyfiles(path: pathlib.Path) -> list[pathlib.Path]:
    """The .py files to scan — a package's tree, or this skill's single module."""
    if path.is_dir():
        return sorted(path.rglob("*.py"))
    return [path]


def _write_tool_names() -> frozenset[str]:
    from vmware_storage.mcp_server.server import mcp

    return frozenset(
        t.name
        for t in asyncio.run(mcp.list_tools())
        if getattr(getattr(t, "annotations", None), "readOnlyHint", None) is False
    )


def _ops_refs(tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    """(local name -> REAL ops function name, ops-module aliases).

    An aliased import (``from ops.mod import realname as _alias``) maps
    ``_alias -> realname`` so an aliased call resolves to the same op an
    un-aliased import names. REST skills alias their ops (``... as _create``)
    while the CLI imports the real name; both must derive to the real name or
    the MCP→ops→CLI intersection is empty (the shape that hid all 13 NSX writes).
    """
    func_map: dict[str, str] = {}
    mods: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            parts = n.module.split(".")
            if "ops" in parts:
                if parts[-1] == "ops":
                    mods.update(a.asname or a.name for a in n.names)
                else:
                    for a in n.names:
                        func_map[a.asname or a.name] = a.name
    return func_map, mods


def _ops_calls(node: ast.AST, func_map: dict[str, str], mods: set[str]) -> set[str]:
    """Real ops function names called in ``node`` — via ``f()`` or ``mod.f()``."""
    out: set[str] = set()
    for c in ast.walk(node):
        if not isinstance(c, ast.Call):
            continue
        f = c.func
        if isinstance(f, ast.Name) and f.id in func_map:
            out.add(func_map[f.id])
        elif (
            isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name)
            and f.value.id in mods
        ):
            out.add(f.attr)
    return out


def _write_ops() -> frozenset[str]:
    targets = _write_tool_names()
    ops: set[str] = set()
    for path in _pyfiles(TOOLS_SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        func_map, mods = _ops_refs(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in targets:
                ops |= _ops_calls(node, func_map, mods)
    return frozenset(ops)


def _decorator_names(node: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for d in node.decorator_list:
        t = d.func if isinstance(d, ast.Call) else d
        if isinstance(t, ast.Name):
            names.add(t.id)
        elif isinstance(t, ast.Attribute):
            names.add(t.attr)
    return names


def _cli_write_commands() -> tuple[list[str], list[str]]:
    """(write commands, of those the ones missing @guarded)."""
    write_ops = _write_ops()
    assert write_ops, "no write ops derived — vacuous"
    writing: list[str] = []
    unguarded: list[str] = []
    for path in _pyfiles(CLI_SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        func_map, mods = _ops_refs(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not any(
                isinstance(d, ast.Call)
                and isinstance(getattr(d, "func", None), ast.Attribute)
                and d.func.attr == "command"
                for d in node.decorator_list
            ):
                continue
            if _ops_calls(node, func_map, mods) & write_ops:
                label = f"{path.name}:{node.name}"
                writing.append(label)
                if "guarded" not in _decorator_names(node):
                    unguarded.append(label)
    return writing, unguarded


def test_every_write_cli_command_is_guarded():
    writing, unguarded = _cli_write_commands()
    assert len(writing) >= 3, (
        f"only {len(writing)} write CLI commands derived ({writing}) — the "
        f"MCP→ops→CLI derivation is likely stale; a check matching almost nothing "
        f"is worse than none."
    )
    assert not unguarded, (
        f"these CLI commands call a [WRITE] ops function but are not @guarded, so "
        f"they bypass policy + audit (HLD I-1): {unguarded}"
    )


def test_high_blast_radius_commands_are_derived_and_guarded():
    """Pin named commands so a broad-but-wrong derivation cannot pass the floor.

    ``iscsi_remove_target`` is this skill's one destructiveHint=True write — it
    tears down an iSCSI send target and can make LUNs inaccessible. Pinning it
    and ``iscsi_add_target`` proves the readOnlyHint→ops→command derivation still
    resolves the real state-changing commands, not just any command.
    """
    writing, _ = _cli_write_commands()
    names = {w.split(":", 1)[1] for w in writing}
    for must in ("iscsi_remove_target", "iscsi_add_target"):
        assert must in names, (
            f"{must} is no longer derived as a write command — the readOnlyHint→"
            f"ops→command derivation stopped resolving it"
        )
