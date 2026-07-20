"""iSCSI configuration: enable adapter, manage targets, rescan storage."""

from __future__ import annotations

import ipaddress
import time
from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import sanitize

from vmware_storage.ops.inventory import find_host_by_name, list_hosts, not_found_hint

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance


# After UpdateSoftwareInternetScsiEnabled the HBA materializes asynchronously;
# poll briefly so an immediate add_target doesn't race into "not enabled".
_HBA_POLL_TIMEOUT_SEC = 5.0
_HBA_POLL_INTERVAL_SEC = 0.5


class HostNotFoundError(Exception):
    """Raised when a host is not found by name."""


class ISCSIError(Exception):
    """Raised on iSCSI operation failures."""


def _require_host(si: ServiceInstance, host_name: str) -> vim.HostSystem:
    """Find a host or raise HostNotFoundError."""
    host = find_host_by_name(si, host_name)
    if host is None:
        raise HostNotFoundError(
            f"Host '{host_name}' not found on this target. vmware-storage has no "
            f"host-listing tool — run vmware-monitor's list_esxi_hosts to get exact "
            f"ESXi host names (FQDN or IP, case-sensitive) and copy one."
            f"{not_found_hint(host_name, [h['name'] for h in list_hosts(si)])}"
        )
    return host


def _validate_address(address: str) -> None:
    """Validate IP address format."""
    try:
        ipaddress.ip_address(address)
    except ValueError:
        raise ISCSIError(
            f"Invalid IP address: '{address}'. storage_iscsi_add_target and "
            "storage_iscsi_remove_target need an IPv4 or IPv6 literal (e.g. "
            "10.0.0.5 or fd00::5), not a hostname — resolve the portal name to an "
            "address and pass that."
        ) from None


def _validate_port(port: int) -> None:
    """Validate port range."""
    if not (1 <= port <= 65535):
        raise ISCSIError(
            f"Port must be 1-65535, got {port}. Pass the iSCSI portal's TCP port to "
            "storage_iscsi_add_target / storage_iscsi_remove_target, or omit the "
            "port argument to use the default 3260."
        )


def _get_storage_system(host: vim.HostSystem) -> vim.host.StorageSystem:
    """Get the host storage system manager."""
    ss = host.configManager.storageSystem
    if ss is None:
        raise ISCSIError(
            f"Storage system manager not available on host '{host.name}' — the host "
            "is most likely disconnected, not responding, or in maintenance mode. "
            "Check its connection state with vmware-monitor's list_esxi_hosts, "
            "reconnect it in vCenter, then retry."
        )
    return ss


def _get_iscsi_hba(host: vim.HostSystem) -> vim.host.InternetScsiHba | None:
    """Find the software iSCSI HBA from host bus adapters."""
    storage_device = host.config.storageDevice
    if not storage_device or not storage_device.hostBusAdapter:
        return None
    for hba in storage_device.hostBusAdapter:
        if isinstance(hba, vim.host.InternetScsiHba) and hba.isSoftwareBased:
            return hba
    return None


# ─── Enable ───────────────────────────────────────────────────────────────────


def enable_software_iscsi(si: ServiceInstance, host_name: str) -> str:
    """Enable the software iSCSI adapter on a host."""
    host = _require_host(si, host_name)
    storage_system = _get_storage_system(host)

    hba = _get_iscsi_hba(host)
    if hba is not None:
        return (
            f"Software iSCSI is already enabled on host '{host_name}' "
            f"(HBA: {hba.device}, IQN: {hba.iScsiName})."
        )

    storage_system.UpdateSoftwareInternetScsiEnabled(enabled=True)

    # The software HBA appears asynchronously after the enable call returns.
    # Poll for it so a follow-up add_target doesn't race into "Software iSCSI
    # is not enabled". A timeout here is not fatal — the adapter is enabling,
    # it just hasn't surfaced yet, so report that rather than raising.
    hba = _wait_for_iscsi_hba(host)
    if hba is None:
        return (
            f"Software iSCSI enable requested on host '{host_name}'. The adapter "
            f"did not appear within {_HBA_POLL_TIMEOUT_SEC:.0f}s — re-check with: "
            "vmware-storage iscsi status"
        )
    return f"Software iSCSI enabled on host '{host_name}' (HBA: {hba.device})."


def _wait_for_iscsi_hba(
    host: vim.HostSystem,
    timeout: float | None = None,
    interval: float | None = None,
) -> vim.host.InternetScsiHba | None:
    """Poll for the software iSCSI HBA to materialize, up to ``timeout`` seconds.

    Returns the HBA once it appears, or None if it hasn't surfaced in time.
    Defaults read the module constants at call time so they stay overridable.
    """
    if timeout is None:
        timeout = _HBA_POLL_TIMEOUT_SEC
    if interval is None:
        interval = _HBA_POLL_INTERVAL_SEC
    deadline = time.monotonic() + timeout
    while True:
        hba = _get_iscsi_hba(host)
        if hba is not None:
            return hba
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


# ─── Status ───────────────────────────────────────────────────────────────────


def get_iscsi_status(si: ServiceInstance, host_name: str) -> dict:
    """Get iSCSI adapter status and configured targets."""
    host = _require_host(si, host_name)
    hba = _get_iscsi_hba(host)

    if hba is None:
        return {
            "host": host_name,
            "enabled": False,
            "hba_device": None,
            "iqn": None,
            "send_targets": [],
        }

    targets = []
    if hba.configuredSendTarget:
        for t in hba.configuredSendTarget:
            targets.append({
                "address": t.address,
                "port": t.port,
            })

    return {
        "host": host_name,
        "enabled": True,
        "hba_device": hba.device,
        "iqn": sanitize(hba.iScsiName) if hba.iScsiName else None,
        "send_targets": targets,
    }


# ─── Target Management ───────────────────────────────────────────────────────


def add_iscsi_target(
    si: ServiceInstance,
    host_name: str,
    address: str,
    port: int = 3260,
) -> str:
    """Add an iSCSI send target and rescan."""
    _validate_address(address)
    _validate_port(port)

    host = _require_host(si, host_name)
    hba = _get_iscsi_hba(host)
    if hba is None:
        raise ISCSIError(
            f"Software iSCSI is not enabled on host '{host_name}'. Run "
            f"storage_iscsi_enable (CLI: vmware-storage iscsi enable {host_name}) "
            "first, then retry adding the target."
        )

    if hba.configuredSendTarget:
        for t in hba.configuredSendTarget:
            if t.address == address and t.port == port:
                return f"iSCSI target {address}:{port} already configured on '{host_name}'."

    storage_system = _get_storage_system(host)
    target = vim.host.InternetScsiHba.SendTarget(address=address, port=port)
    storage_system.AddInternetScsiSendTargets(
        iScsiHbaDevice=hba.device,
        targets=[target],
    )

    storage_system.RescanAllHba()
    storage_system.RescanVmfs()

    return f"iSCSI target {address}:{port} added to host '{host_name}' and storage rescanned."


def remove_iscsi_target(
    si: ServiceInstance,
    host_name: str,
    address: str,
    port: int = 3260,
) -> str:
    """Remove an iSCSI send target and rescan."""
    _validate_address(address)
    _validate_port(port)

    host = _require_host(si, host_name)
    hba = _get_iscsi_hba(host)
    if hba is None:
        raise ISCSIError(
            f"Software iSCSI is not enabled on host '{host_name}', so it has no send "
            "targets to remove. Run storage_iscsi_status to confirm the adapter "
            "state; if you meant a different host, run vmware-monitor's "
            "list_esxi_hosts to get exact names."
        )

    found = False
    if hba.configuredSendTarget:
        for t in hba.configuredSendTarget:
            if t.address == address and t.port == port:
                found = True
                break
    if not found:
        raise ISCSIError(
            f"iSCSI target {address}:{port} not found on host '{host_name}'. Run "
            "storage_iscsi_status to see the send targets actually configured and "
            "copy an exact address:port pair."
        )

    storage_system = _get_storage_system(host)
    target = vim.host.InternetScsiHba.SendTarget(address=address, port=port)
    storage_system.RemoveInternetScsiSendTargets(
        iScsiHbaDevice=hba.device,
        targets=[target],
    )

    storage_system.RescanAllHba()
    storage_system.RescanVmfs()

    return f"iSCSI target {address}:{port} removed from host '{host_name}' and storage rescanned."


# ─── Rescan ───────────────────────────────────────────────────────────────────


def rescan_storage(si: ServiceInstance, host_name: str) -> str:
    """Rescan all HBAs and VMFS volumes on a host."""
    host = _require_host(si, host_name)
    storage_system = _get_storage_system(host)

    storage_system.RescanAllHba()
    storage_system.RescanVmfs()

    return f"Storage rescan completed on host '{host_name}'."
