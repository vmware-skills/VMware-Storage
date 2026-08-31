"""Regression: per-target username is overridable from the environment.

A deployment that injects credentials from a secret store (systemd
``EnvironmentFile``, container secret, vault sidecar) could previously
externalise only half the pair — the password came from the environment while
the username was pinned in config.yaml. Pointing the env password at a
different service account than the configured username logs in as nobody.

Both halves must resolve at the same moment, so both are properties. Binding
the username at load time while the password stays a property is the specific
bug this guards: a sidecar rotating both mid-process moves the password and
leaves the username behind, and the login uses a combination that was never
issued together.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from vmware_storage.config import load_config


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "targets:\n"
        "  - name: test-vc\n"
        "    host: 10.0.0.1\n"
        "    username: config-file-user\n"
        "    type: vcenter\n"
    , encoding="utf-8")
    return path


def test_username_and_password_rotate_together(config_file: Path, monkeypatch) -> None:
    monkeypatch.setenv("VMWARE_TEST_VC_USERNAME", "svc-a@vsphere.local")
    monkeypatch.setenv("VMWARE_TEST_VC_PASSWORD", "pw-a")
    target = load_config(config_file).targets[0]
    assert (target.username, target.password) == ("svc-a@vsphere.local", "pw-a")

    monkeypatch.setenv("VMWARE_TEST_VC_USERNAME", "svc-b@vsphere.local")
    monkeypatch.setenv("VMWARE_TEST_VC_PASSWORD", "pw-b")
    assert (target.username, target.password) == ("svc-b@vsphere.local", "pw-b"), (
        "the pair came apart — one half is bound at load time and the other at access"
    )


def test_username_env_overrides_config_file(config_file: Path, monkeypatch) -> None:
    monkeypatch.setenv("VMWARE_TEST_VC_USERNAME", "svc-account@vsphere.local")
    target = load_config(config_file).targets[0]
    assert target.config_username == "config-file-user"
    assert target.username == "svc-account@vsphere.local"


def test_username_falls_back_to_config_file(config_file: Path, monkeypatch) -> None:
    monkeypatch.delenv("VMWARE_TEST_VC_USERNAME", raising=False)
    target = load_config(config_file).targets[0]
    assert target.username == "config-file-user"
