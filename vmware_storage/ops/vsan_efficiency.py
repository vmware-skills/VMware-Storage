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
from typing import TYPE_CHECKING

from vmware_policy import sanitize

from vmware_storage.ops.vsan import _require_cluster

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

_log = logging.getLogger("vmware-storage.vsan-efficiency")


def _config_system(si: ServiceInstance) -> object:
    """Return the vSAN cluster-config managed object, or raise a teaching error.

    The accessor (SDK import, stub check, TLS context, missing-key message)
    moved to :mod:`vmware_storage.ops.vsan_sdk` when ``vsan_health`` became a
    second caller. One copy of the ``verify_ssl: false`` handling in
    particular: a second copy is a second chance to forget it and ship
    ``SSLCertVerificationError`` to a self-signed lab.
    """
    from vmware_storage.ops.vsan_sdk import managed_object

    return managed_object(si, "vsan-cluster-config-system")


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
