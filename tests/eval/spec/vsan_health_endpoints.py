"""Verified vSAN health SDK surface.

Sibling of ``vsan_efficiency_endpoints``. Same reasoning (踩坑 #36): the vSAN
Management SDK is out of scope for the base ``pyVmomi`` conformance test, so
what shipped code is allowed to call is pinned here instead.

Every entry below was read off the installed pyVmomi's own type metadata, which
is also what ``test_vsan_health_is_actually_queried.test_the_sdk_surface_is_real``
re-checks:

    vsanapiutils.GetVsanVcMos(si._stub)['vsan-cluster-health-system']
    -> VsanVcClusterHealthSystem.QueryClusterHealthSummary(
           cluster=..., fetchFromCache=True)          [READ]
       .overallHealth / .overallHealthDescription / .timestamp
       .groups[].groupId / .groupName / .groupHealth

Read-only. The health system also exposes ``RepairClusterObjectsImmediate``,
``RebalanceCluster``, ``SetVsanClusterHealthCheckInterval`` and other writes;
they are listed as forbidden so a later edit cannot reach one by accident from
a module whose whole point is that it does not change anything.
"""

from __future__ import annotations

# vsanapiutils accessor key -> the vim managed-object type it yields.
MANAGED_OBJECTS: dict[str, str] = {
    "vsan-cluster-health-system": "vim.cluster.VsanVcClusterHealthSystem",
}

# (owning managed-object type, python method name) — READ methods only.
METHODS: list[tuple[str, str]] = [
    ("vim.cluster.VsanVcClusterHealthSystem", "QueryClusterHealthSummary"),
]

#: WSDL name of the method above, which differs from the python name.
WSDL_NAMES: dict[str, str] = {
    "QueryClusterHealthSummary": "VsanQueryVcClusterHealthSummary",
}

#: Parameters shipped code passes. Anything else is unverified.
PARAMS: dict[str, tuple[str, ...]] = {
    "QueryClusterHealthSummary": ("cluster", "fetchFromCache"),
}

RESULT_TYPE = "vim.cluster.VsanClusterHealthSummary"
GROUP_TYPE = "vim.cluster.VsanClusterHealthGroup"

PROPERTY_PATHS: list[str] = [
    "overallHealth",
    "overallHealthDescription",
    "timestamp",
    "groups",
]

GROUP_PROPERTY_PATHS: list[str] = [
    "groupId",
    "groupName",
    "groupHealth",
]

#: Bare identifiers that may appear as vSAN-SDK *calls* in the modules the
#: health path spans (``ops/vsan.py`` and ``ops/vsan_sdk.py``).
ALLOWED_SDK_CALLS: frozenset[str] = frozenset(
    {
        "GetVsanVcMos",  # vsanapiutils helper (accessor)
    }
)

#: Writes on the same managed object. None of them belongs in a read tool.
FORBIDDEN_SDK_CALLS: frozenset[str] = frozenset(
    {
        "RepairClusterObjectsImmediate",
        "RebalanceCluster",
        "StopRebalanceCluster",
        "SetVsanClusterHealthCheckInterval",
        "SetVsanClusterSilentChecks",
        "SetVsanClusterTelemetryConfig",
        "UploadHclDb",
        "UpdateHclDbFromWeb",
        "PurgeHclFiles",
        "DownloadAndInstallVendorTool",
        "UpdateDefaultDSPolicyRecommendation",
    }
)
