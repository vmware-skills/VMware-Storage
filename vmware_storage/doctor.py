"""vmware-storage doctor — environment and connectivity diagnostics."""

from __future__ import annotations

import json
import socket
import stat
from typing import Callable

from rich.console import Console
from rich.table import Table

from vmware_storage.config import CONFIG_DIR, CONFIG_FILE, ENV_FILE

console = Console()

_PASS = "[green]\u2713[/]"  # nosec B105 — rich color markup, not a password
_FAIL = "[red]\u2717[/]"
_INFO = "[cyan]i[/]"


def _check(label: str, fn: Callable[[], tuple[bool, str]]) -> tuple[bool, str, str]:
    try:
        ok, msg = fn()
        return ok, label, msg
    except Exception as e:
        return False, label, f"Error: {e}"


def _check_config_file() -> tuple[bool, str]:
    if CONFIG_FILE.exists():
        return True, f"Config found: {CONFIG_FILE}"
    return False, (
        f"Config not found: {CONFIG_FILE} — Run: vmware-storage init "
        f"(or manually: mkdir -p {CONFIG_DIR} && cp config.example.yaml {CONFIG_FILE})"
    )


def _check_env_file() -> tuple[bool, str]:
    if not ENV_FILE.exists():
        return False, (
            f".env not found: {ENV_FILE} — Run: vmware-storage init "
            f"(or manually create it and: chmod 600 {ENV_FILE})"
        )
    mode = ENV_FILE.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return (
            False,
            f".env permissions too open ({oct(stat.S_IMODE(mode))}) — Run: chmod 600 {ENV_FILE}",
        )
    return True, f".env found with correct permissions (600): {ENV_FILE}"


def _check_targets() -> tuple[bool, str]:
    if not CONFIG_FILE.exists():
        return False, "Config file missing"
    import yaml

    with open(CONFIG_FILE) as f:
        raw = yaml.safe_load(f) or {}
    targets = raw.get("targets", [])
    if not targets:
        return False, "No targets configured in config.yaml"
    names = [t.get("name", "?") for t in targets]
    return True, f"{len(targets)} target(s) configured: {', '.join(names)}"


def _check_connectivity() -> tuple[bool, str]:
    if not CONFIG_FILE.exists():
        return False, "Config file missing"
    import yaml

    with open(CONFIG_FILE) as f:
        raw = yaml.safe_load(f) or {}
    targets = raw.get("targets", [])
    if not targets:
        return False, "No targets to check"

    results = []
    all_ok = True
    for t in targets:
        host = t.get("host", "")
        port = t.get("port", 443)
        try:
            sock = socket.create_connection((host, port), timeout=5)
            sock.close()
            results.append(f"{host}:{port} \u2713")
        except OSError as e:
            results.append(f"{host}:{port} \u2717 ({e})")
            all_ok = False
    return all_ok, "  ".join(results)


def _check_auth() -> tuple[bool, str]:
    if not CONFIG_FILE.exists():
        return False, "Config file missing"
    try:
        from vmware_storage.config import load_config
        from vmware_storage.connection import ConnectionManager

        config = load_config()
        if not config.targets:
            return False, "No targets configured"
        conn_mgr = ConnectionManager(config)
        target = config.default_target
        conn_mgr.connect(target.name)
        conn_mgr.disconnect_all()
        return True, f"Authentication OK for target '{target.name}'"
    except KeyError as e:
        return False, f"Missing password env var: {e}"
    except Exception as e:
        return False, f"Auth failed: {e}"


def _check_mcp_server() -> tuple[bool, str]:
    try:
        import importlib

        importlib.import_module("vmware_storage.mcp_server.server")
        return True, "MCP server module loads OK"
    except ImportError as e:
        return False, f"MCP server import failed: {e}"


_CHECKS: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
    ("Config file", _check_config_file),
    (".env file", _check_env_file),
    ("Targets configured", _check_targets),
    ("Network connectivity", _check_connectivity),
    ("vSphere authentication", _check_auth),
    ("MCP server", _check_mcp_server),
]


def run_doctor(skip_auth: bool = False) -> int:
    """Run all checks and print results. Returns exit code (0 = all pass)."""
    console.print("\n[bold]vmware-storage doctor[/]\n")

    table = Table(show_header=True, header_style="bold")
    table.add_column("", width=3)
    table.add_column("Check", style="bold", min_width=25)
    table.add_column("Result")

    failures = 0
    for label, fn in _CHECKS:
        if skip_auth and label == "vSphere authentication":
            table.add_row(_INFO, label, "[dim]skipped (--skip-auth)[/]")
            continue
        ok, lbl, msg = _check(label, fn)
        icon = _PASS if ok else _FAIL
        if not ok:
            failures += 1
        table.add_row(icon, lbl, msg)

    console.print(table)

    if failures == 0:
        console.print("\n[green bold]\u2713 All checks passed.[/]\n")
    else:
        console.print(f"\n[red bold]\u2717 {failures} check(s) failed.[/]\n")

    return 0 if failures == 0 else 1
