"""The remedy must outlive the config path the message quotes.

``test_safe_error_passthrough`` already pins that the closing "run doctor" step
survives a long *target name*. It does not pin the other interpolated value in
the same two messages: the absolute path of ``config.yaml`` / ``.env``. That
path is as variable as the target name, and it is the developer's home
directory that decides its length.

On ``/Users/zw`` the missing-password message at a 50-character target name is
292 characters — eight below ``sanitize``'s 300-char cap, and green. On the
Windows Server 2025 host that ran the VCF 9.1 hardware pass it is 307, so the
operator is told the password is missing and *not* told how to confirm the fix.
The suite could not see that, because the only home directory it ever ran under
was a short one — the defect was verified in the one environment where it cannot
appear.

So the rule the family already writes down — put the remedy before any
interpolated value, because the cap truncates from the end — is asserted here
against a path long enough that a message which obeys it passes on any host and
one which does not fails everywhere.
"""

from __future__ import annotations

import pytest

from vmware_storage import config as cfg
from vmware_storage import connection as conn
from vmware_storage.config import ConfigError, TargetConfig
from vmware_storage.mcp_server.server import _safe_error

CAP = 300

#: Longer than any real home, so a message that survives this survives the
#: tester's ``C:\\Users\\Administrator`` and a deep CI checkout alike.
LONG_HOME = "/very/deeply/nested/service/account/home/directory/for/this/host"


def _target(name: str) -> TargetConfig:
    return TargetConfig(
        name=name,
        host="vc.internal",
        config_username="svc@vsphere.local",
        port=443,
        verify_ssl=True,
    )


@pytest.mark.parametrize("name_len", [2, 19, 30, 50])
def test_missing_password_remedy_survives_a_long_env_file_path(
    monkeypatch, name_len
) -> None:
    monkeypatch.setattr(cfg, "ENV_FILE", f"{LONG_HOME}/.vmware-storage/.env")
    monkeypatch.delenv(
        f"VMWARE_{'T' * name_len}_PASSWORD".upper(), raising=False
    )

    with pytest.raises(ConfigError) as excinfo:
        _ = _target("t" * name_len).password

    out = _safe_error(excinfo.value, "list_all_datastores")
    assert len(out) <= CAP
    assert "vmware-storage doctor" in out, (
        "the step that confirms the fix worked was truncated away by the "
        "interpolated .env path"
    )
    assert f"VMWARE_{'T' * name_len}_PASSWORD" in out


@pytest.mark.parametrize("name_len", [2, 30, 50])
def test_connect_failure_remedy_survives_a_long_config_file_path(
    monkeypatch, name_len
) -> None:
    monkeypatch.setattr(
        conn, "CONFIG_FILE", f"{LONG_HOME}/.vmware-storage/config.yaml"
    )

    err = conn._connect_failed(_target("t" * name_len), OSError("boom"))

    out = _safe_error(err, "list_all_datastores")
    assert len(out) <= CAP
    assert "verify_ssl" in out
    assert "vmware-storage doctor" in out, (
        "the step that confirms the fix worked was truncated away by the "
        "interpolated config.yaml path"
    )
