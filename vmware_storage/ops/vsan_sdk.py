"""Access to the vSAN Management SDK's vCenter-side managed objects.

``vsanapiutils`` and ``vsanmgmtObjects`` ship inside pyvmomi (>= 8.0.3), and
``GetVsanVcMos`` builds every vSAN managed object on top of the SOAP stub this
skill already holds — same host, same port, same session cookie, against the
``/vsanHealth`` endpoint. No extra package, no second credential.

One accessor, because there is more than one caller: dedup/compression reads
``vsan-cluster-config-system`` and health reads ``vsan-cluster-health-system``.
The TLS handling in particular is not something to have two copies of — on a
``verify_ssl: false`` target ``GetVsanVcMos`` otherwise builds a fresh stub with
Python's default verifying context and dies with ``SSLCertVerificationError``,
and a second copy is a second chance to forget that.
"""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING

from vmware_storage.connection import get_verify_ssl

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance


def vc_mos(si: ServiceInstance) -> dict:
    """Return the vSAN vCenter managed-object dict for this connection.

    Raises:
        VSANError: the SDK helper is missing, the session has no SOAP stub, or
            the target does not serve the vSAN endpoints. Every message names
            the fix rather than leaking a traceback.
    """
    from vmware_storage.ops.vsan import VSANError

    try:
        import vsanapiutils
    except ImportError as exc:
        raise VSANError(
            "vSAN Management SDK helper 'vsanapiutils' is unavailable. It ships "
            "with pyvmomi>=8.0.3 — reinstall the tool with "
            "'uv tool install --force vmware-storage', then run 'vmware-storage doctor'."
        ) from exc

    # The SDK talks over the same authenticated SOAP stub as the pyVmomi
    # session. A session with no stub cannot reach the vSAN endpoints.
    stub = getattr(si, "_stub", None)
    if stub is None:
        raise VSANError(
            "The vCenter session exposes no SOAP stub for the vSAN SDK. "
            "Reconnect and run 'vmware-storage doctor' to verify connectivity."
        )

    # On a verify_ssl: false (self-signed) target, GetVsanVcMos would otherwise
    # build a fresh SoapStubAdapter with Python's DEFAULT verifying context and
    # die with SSLCertVerificationError (then masked into a generic _safe_error).
    # Pass the same unverified context the pyVmomi session was opened with.
    # 踩坑 #32: verify_ssl is read from the id(si) side store, never off the SI.
    if get_verify_ssl(si):
        return vsanapiutils.GetVsanVcMos(stub)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return vsanapiutils.GetVsanVcMos(stub, context=ctx)


def managed_object(si: ServiceInstance, key: str):
    """Return one vSAN managed object by its ``GetVsanVcMos`` key.

    Args:
        si: vSphere ServiceInstance whose ``_stub`` drives the SDK.
        key: accessor key, e.g. ``vsan-cluster-health-system``. Only keys
            pinned in ``tests/eval/spec`` may be used by shipped code —
            anything else is a phantom endpoint (踩坑 #36).

    Raises:
        VSANError: the key is absent, which means this target is not a vCenter
            managing vSAN (an ESXi host, for instance).
    """
    from vmware_storage.ops.vsan import VSANError

    mos = vc_mos(si)
    # A missing key means this endpoint is not a vSAN-managing vCenter.
    system = mos.get(key) if hasattr(mos, "get") else None
    if system is None:
        raise VSANError(
            f"vSAN '{key}' endpoint was not found on this target — it may be "
            "an ESXi host or a vCenter not managing vSAN. Run "
            "'vmware-storage doctor'."
        )
    return system
