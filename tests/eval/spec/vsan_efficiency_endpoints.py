"""Verified vSAN data-efficiency SDK surface — section D (vSAN dedup part).

Source: VCF 9.1 ``vcf91-verified-endpoints.md`` section D, line marked
``vSAN dedup VERIFIED(vSAN Management SDK, 非 base pyVmomi)``:

    vsanapiutils.GetVsanVcMos(si._stub)['vsan-cluster-config-system']
    -> VsanVcClusterConfigSystem.VsanClusterGetConfig(cluster)
       .dataEfficiencyConfig(dedupEnabled / compressionEnabled)  [READ]

This is the vSAN Management SDK (shipped with pyvmomi as ``vsanapiutils`` /
``vsanmgmtObjects``), NOT base pyVmomi — so it is out of scope for the base
``pyVmomi`` conformance test and pinned here instead.

Only the entries below are permitted in shipped ops code. ``VsanClusterReconfig``
(the WRITE twin) is deliberately excluded: this task is read-only.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# VERIFIED — the exact vSAN Management SDK surface the read tool may touch.
# ---------------------------------------------------------------------------

# vsanapiutils accessor key -> the vim managed-object type it yields.
MANAGED_OBJECTS: dict[str, str] = {
    "vsan-cluster-config-system": "vim.cluster.VsanVcClusterConfigSystem",
}

# (owning managed-object type, method name) — READ methods only.
METHODS: list[tuple[str, str]] = [
    ("vim.cluster.VsanVcClusterConfigSystem", "VsanClusterGetConfig"),
]

# Property paths read off the VsanClusterGetConfig(cluster) result
# (vim.vsan.ConfigInfoEx). Field-level shapes validated against pyVmomi type
# metadata by the regression test.
RESULT_TYPE = "vim.vsan.ConfigInfoEx"
EFFICIENCY_TYPE = "vim.vsan.DataEfficiencyConfig"
PROPERTY_PATHS: list[str] = [
    "dataEfficiencyConfig",
    "dataEfficiencyConfig.dedupEnabled",
    "dataEfficiencyConfig.compressionEnabled",
]

# The bare identifiers that may appear as vSAN-SDK *calls* in shipped ops
# source. The regression test scans the ops module and asserts every
# ``<name>(`` call whose name contains "Vsan" is a member of this set.
ALLOWED_SDK_CALLS: frozenset[str] = frozenset(
    {
        "GetVsanVcMos",         # vsanapiutils helper (accessor)
        "VsanClusterGetConfig",  # VsanVcClusterConfigSystem READ method
    }
)

# ---------------------------------------------------------------------------
# DEFERRED / NOT TOOL-ABLE — must never appear as an ops function or MCP tool.
# Per section D: neither has a verified SDK object, so building a tool for it
# would be a phantom endpoint (踩坑 #36).
# ---------------------------------------------------------------------------

DEFERRED_NOT_TOOLABLE: dict[str, str] = {
    "vsan_global_dedup": (
        "vSAN Global Deduplication has no distinct SDK field (UNVERIFIED). ESA "
        "is cluster-wide by nature and may reuse dedupEnabled, but that is "
        "unproven — no separate object/field exists to query, so no tool."
    ),
    "vsan_v2v_replication": (
        "vSAN-to-vSAN replication is UNVERIFIED and not in the vSAN SDK. It "
        "lives in the separate vSAN Data Protection + Live Recovery plane "
        "(/snapservice/ only covers local snapshots), so no tool here."
    ),
}

# Identifiers that must NOT appear as vSAN-SDK calls in shipped ops source
# (write twin + the deferred concepts above).
FORBIDDEN_SDK_CALLS: frozenset[str] = frozenset(
    {
        "VsanClusterReconfig",  # WRITE twin — out of scope for this read tool
    }
)
