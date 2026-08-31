"""The doctor must diagnose the config file the tools will actually load.

Real-hardware finding, 2026-08-30, first hit on the sibling Aria skill and swept
across the family. This skill had the defect in its own variant: ``load_config``
resolved ``config_path or CONFIG_FILE`` and never looked at
``VMWARE_STORAGE_CONFIG`` at all, while the MCP server read the variable itself
and passed the result down. So the agent-facing tools opened the file named by
the variable, and the CLI and the doctor opened ``~/.vmware-storage/config.yaml``
— and the doctor reported every check green about it.

``config.py`` had been advertising the variable in an error message
("default {CONFIG_FILE}, overridden by VMWARE_STORAGE_CONFIG") the whole time.

A diagnostic that green-lights a file the tools do not open is worse than no
diagnostic: it converts "my tools fail" into "my tools fail and the checker says
they should not", which is where the operator stops trusting the checker. The
first test below is that exact sentence — the doctor exits 0 while
``load_config`` raises ``FileNotFoundError``.

The precedence now lives in exactly one function, ``resolve_config_path``, which
``load_config`` and every check in the doctor go through — two copies of a rule
do not disagree loudly, they disagree slowly, which is how this one drifted
(CLAUDE.md 形态 #6).
"""

from __future__ import annotations

import inspect
import socket

import pytest

from vmware_storage import config as cfg
from vmware_storage import doctor as doc

# Deliberately different target counts: the count the report prints is what
# tells us which of the two files the doctor actually opened.
_ONE_TARGET = """
targets:
  - name: only-in-the-default
    host: 127.0.0.1
    port: {port}
    username: admin
"""

_THREE_TARGETS = """
targets:
  - name: a
    host: 127.0.0.1
    port: {port}
    username: admin
  - name: b
    host: 127.0.0.1
    port: {port}
    username: admin
  - name: c
    host: 127.0.0.1
    port: {port}
    username: admin
"""


def _flat(text: str) -> str:
    """The report with whitespace and table drawing removed.

    Rich wraps a long path across cells; flattening keeps the assertions about
    *which file* independent of the table layout.
    """
    return "".join(ch for ch in text if not ch.isspace() and ch not in "│┃")


@pytest.fixture()
def listener():
    """A real listening socket, so the doctor's connectivity check passes.

    Without it the report is red for an unrelated reason and cannot show the
    thing worth showing: every check green about a file nothing will open.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    yield sock.getsockname()[1]
    sock.close()


@pytest.fixture()
def sandbox(tmp_path, monkeypatch, listener):
    """A default config and .env that are both entirely valid, so the only way
    the doctor can report on the env var's file is by resolving it."""
    default = tmp_path / "default.yaml"
    default.write_text(_ONE_TARGET.format(port=listener), encoding="utf-8")
    env_file = tmp_path / "dot.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)

    monkeypatch.setattr(cfg, "CONFIG_FILE", default)
    monkeypatch.setattr(doc, "ENV_FILE", env_file)
    monkeypatch.delenv("VMWARE_STORAGE_CONFIG", raising=False)
    # Rich elides long details at 80 columns, so an assertion about a tmp_path
    # would be measuring the terminal rather than the doctor.
    monkeypatch.setenv("COLUMNS", "300")
    return default


def test_the_env_var_decides_which_file_is_resolved(sandbox, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text(_THREE_TARGETS.format(port=1), encoding="utf-8")
    monkeypatch.setenv("VMWARE_STORAGE_CONFIG", str(elsewhere))

    assert cfg.resolve_config_path() == elsewhere
    assert len(cfg.load_config().targets) == 3, (
        "load_config ignored $VMWARE_STORAGE_CONFIG, so the CLI reads one file "
        "and the MCP server another"
    )


def test_an_explicit_path_still_beats_the_env_var(sandbox, tmp_path, monkeypatch):
    """The control on precedence: an explicit path is the caller saying which
    file they mean, and it has to keep winning."""
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(_ONE_TARGET.format(port=1), encoding="utf-8")
    monkeypatch.setenv("VMWARE_STORAGE_CONFIG", str(tmp_path / "ignored.yaml"))

    assert cfg.resolve_config_path(explicit) == explicit
    assert len(cfg.load_config(explicit).targets) == 1


def test_with_neither_it_is_the_default(sandbox):
    assert cfg.resolve_config_path() == cfg.CONFIG_FILE
    assert len(cfg.load_config().targets) == 1


def test_doctor_does_not_pass_while_the_tools_cannot_load_the_config(
    sandbox, tmp_path, monkeypatch, capsys
):
    """The reported failure, in full: the doctor exits 0 — every check green —
    while every tool call raises FileNotFoundError.

    The default config here exists, parses, and points at a socket that is
    genuinely listening. It is simply not the file the tools will open.
    """
    missing = tmp_path / "not-there.yaml"
    monkeypatch.setenv("VMWARE_STORAGE_CONFIG", str(missing))

    with pytest.raises(FileNotFoundError):
        cfg.load_config()

    rc = doc.run_doctor(skip_auth=True)
    out = _flat(capsys.readouterr().out)

    assert rc != 0, (
        "doctor exited 0 against a config file that does not exist; this is the "
        "report that tells an operator their broken setup is fine"
    )
    assert str(missing) in out, (
        "the report must name the file it looked at — a verdict about an "
        "unnamed file is what made this take real hardware to find"
    )
    assert "1target(s)configured" not in out, (
        "doctor parsed the default config and called it green while every tool "
        "call raises FileNotFoundError on the path in $VMWARE_STORAGE_CONFIG"
    )


def test_doctor_reads_the_env_vars_file_not_the_default(
    sandbox, tmp_path, monkeypatch, capsys
):
    """The positive half: pointed at a real file elsewhere, the doctor reports
    on that one — three targets, not the default's one."""
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text(_THREE_TARGETS.format(port=1), encoding="utf-8")
    monkeypatch.setenv("VMWARE_STORAGE_CONFIG", str(elsewhere))

    doc.run_doctor(skip_auth=True)
    out = _flat(capsys.readouterr().out)

    assert str(elsewhere) in out, "the report must name the file it looked at"
    assert "3target(s)configured" in out, (
        "the doctor counted the default file's targets, so it parsed the file "
        "the tools will never open"
    )


def test_load_config_and_the_doctor_cannot_disagree():
    """Structural, not behavioural: every reader goes through the one resolver,
    so a future edit cannot silently desynchronise them again.

    The doctor is six independent check functions, four of which open the
    config. Asserting on each one by name would go stale the moment a seventh is
    added, so the assertion is that the module does not name the default path at
    all: whichever check needs to know, asks.
    """
    assert "resolve_config_path" in inspect.getsource(cfg.load_config), (
        "load_config resolves the config path by itself again; that is the "
        "duplication this test exists to prevent"
    )
    assert "CONFIG_FILE" not in inspect.getsource(doc), (
        "a doctor check names the default config path directly, so it can "
        "diagnose a file the tools will not open"
    )


def test_the_mcp_server_does_not_keep_its_own_copy_of_the_precedence():
    """The third copy. The MCP server read $VMWARE_STORAGE_CONFIG itself and
    passed the result down explicitly — which is why the tools and the CLI
    disagreed about which file was in play."""
    from vmware_storage.mcp_server import server

    source = inspect.getsource(server._get_conn_mgr)
    assert "os.environ" not in source, (
        "_get_conn_mgr resolves the config path itself; let load_config do it"
    )
