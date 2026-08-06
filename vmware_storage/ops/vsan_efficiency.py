"""vSAN data-efficiency (deduplication + compression) read query.

Uses the **vSAN Management SDK** — ``vsanapiutils.GetVsanVcMos(si._stub)`` to
reach ``VsanVcClusterConfigSystem``, then ``VsanClusterGetConfig(cluster)`` —
**not** base pyVmomi. The exact SDK object/method/field surface is pinned in
``tests/eval/spec/vsan_efficiency_endpoints.py`` (section D of the verified
endpoint spec) and enforced by a regression test (anti-phantom, 踩坑 #36).

Read-only. No ``VsanClusterReconfig`` (the write twin) is called here.

.. note::
   The SDK call structure is verified against pyVmomi type metadata, but the
   *wire response* shape is accessed defensively (``getattr(..., None)``) and
   still needs validation against a real vSAN cluster (needs-real-vsan). Absent
   fields degrade to ``None`` with an explanatory message rather than crashing.

.. note::
   On a ``verify_ssl: false`` target, ``GetVsanVcMos`` is handed an unverified
   ``ssl.SSLContext`` matching the pyVmomi session (else it builds a new stub
   with Python's DEFAULT verifying context and fails with
   ``SSLCertVerificationError``). The context kwarg is unit-tested, but the full
   round-trip against a real self-signed vSAN cluster is still a gate
   (needs-real-vsan).
"""

from __future__ import annotations

import logging
import ssl
from typing import TYPE_CHECKING

from vmware_policy import sanitize

from vmware_storage.connection import get_verify_ssl
from vmware_storage.ops.vsan import VSANError, _require_cluster

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

_log = logging.getLogger("vmware-storage.vsan-efficiency")


def _config_system(si: ServiceInstance) -> object:
    """Return the vSAN cluster-config managed object, or raise a teaching error.

    The vSAN Management SDK ships as ``vsanapiutils`` / ``vsanmgmtObjects``
    alongside pyVmomi (>= 8.0.3). Imported lazily so this module still loads
    where the helper is somehow absent, and every failure mode is turned into a
    :class:`VSANError` that names the fix instead of leaking a traceback.
    """
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
        vc_mos = vsanapiutils.GetVsanVcMos(stub)
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        vc_mos = vsanapiutils.GetVsanVcMos(stub, context=ctx)
    # GetVsanVcMos returns a dict of vSAN managed objects; a missing key means
    # this endpoint is not a vSAN-managing vCenter (e.g. plain ESXi).
    system = vc_mos.get("vsan-cluster-config-system") if hasattr(vc_mos, "get") else None
    if system is None:
        raise VSANError(
            "vSAN 'vsan-cluster-config-system' endpoint was not found on this "
            "target — it may be an ESXi host or a vCenter not managing vSAN. "
            "Run 'vmware-storage doctor'."
        )
    return system


def get_vsan_efficiency(si: ServiceInstance, cluster_name: str) -> dict:
    """Get vSAN data-efficiency (dedup + compression) status for a cluster.

    Args:
        si: vSphere ServiceInstance (its ``_stub`` drives the vSAN SDK).
        cluster_name: Name of the vSAN-enabled cluster, exactly as shown in
            vCenter (case-sensitive).

    Returns:
        dict with ``cluster_name``, ``vsan_enabled`` (from the cluster config,
        may be ``None`` if the field is absent), ``dedup_enabled`` and
        ``compression_enabled``. When vSAN reports no ``dataEfficiencyConfig``
        (space efficiency off, or an OSA cluster without it), the two flags are
        ``None`` and a ``message`` explains why — no fabricated ``False``.
    """
    cluster = _require_cluster(si, cluster_name)
    system = _config_system(si)

    config = system.VsanClusterGetConfig(cluster)

    # Defensive: treat the SDK response as unverified (踩坑 形态 #1). An absent
    # field degrades to None, never an AttributeError.
    enabled = getattr(config, "enabled", None)
    dec = getattr(config, "dataEfficiencyConfig", None)

    if dec is None:
        return {
            "cluster_name": sanitize(cluster_name),
            "vsan_enabled": enabled,
            "dedup_enabled": None,
            "compression_enabled": None,
            "message": (
                "vSAN reported no dataEfficiencyConfig for this cluster. Space "
                "efficiency is likely disabled, or this is an OSA cluster with "
                "dedup/compression not configured. Confirm in the vCenter UI."
            ),
        }

    return {
        "cluster_name": sanitize(cluster_name),
        "vsan_enabled": enabled,
        "dedup_enabled": getattr(dec, "dedupEnabled", None),
        "compression_enabled": getattr(dec, "compressionEnabled", None),
    }
