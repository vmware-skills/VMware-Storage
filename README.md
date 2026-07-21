<!-- mcp-name: io.github.zw008/vmware-storage -->
# VMware Storage

> **Author**: Wei Zhou, VMware by Broadcom — wei-wz.zhou@broadcom.com
> This is a community-driven project by a VMware engineer, not an official VMware product.
> For official VMware developer tools see [developer.broadcom.com](https://developer.broadcom.com).

[English](README.md) | [中文](README-CN.md)

VMware vSphere storage management: datastores, iSCSI, vSAN — 11 MCP tools, domain-focused and lightweight.

> Split from vmware-aiops for lighter context and local model compatibility.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## Companion Skills

| Skill | Scope | Tools | Install |
|-------|-------|:-----:|---------|
| **[vmware-aiops](https://github.com/zw008/VMware-AIops)** ⭐ entry point | VM lifecycle, deployment, guest ops, clusters | 49 | `uv tool install vmware-aiops` |
| **[vmware-monitor](https://github.com/zw008/VMware-Monitor)** | Read-only monitoring, alarms, events, VM info | 27 | `uv tool install vmware-monitor` |
| **[vmware-vks](https://github.com/zw008/VMware-VKS)** | Tanzu Namespaces, TKC cluster lifecycle | 20 | `uv tool install vmware-vks` |
| **[vmware-nsx](https://github.com/zw008/VMware-NSX)** | NSX networking: segments, gateways, NAT, IPAM | 33 | `uv tool install vmware-nsx-mgmt` |
| **[vmware-nsx-security](https://github.com/zw008/VMware-NSX-Security)** | DFW microsegmentation, security groups, Traceflow | 21 | `uv tool install vmware-nsx-security` |
| **[vmware-aria](https://github.com/zw008/VMware-Aria)** | Aria Ops metrics, alerts, capacity planning | 28 | `uv tool install vmware-aria` |

## Quick Install

```bash
# Via PyPI
uv tool install vmware-storage

# Or pip
pip install vmware-storage
```

## Offline / Air-Gapped Install (from source)

This project uses the modern PEP 517 build system (hatchling), so there is **no
`setup.py`** by design — that is expected, not a missing file. If you cloned the
source and hit `ERROR: File "setup.py" or "setup.cfg" not found ... editable mode
currently requires a setuptools-based build`, your `pip` is older than 21.3 and
cannot do an *editable* (`-e`) install with a non-setuptools backend. Editable
mode is a developer convenience, not needed to run the tool — do one of:

```bash
# From the source tree — a normal (non-editable) install builds a wheel:
pip install .              # NOT  pip install -e .

# ...or upgrade pip first, and editable works too:
pip install --upgrade pip && pip install -e .
```

For a **truly air-gapped host**, build the wheels on a connected machine and copy
them over — the target then needs no network:

```bash
# On a connected machine, collect this package + its dependencies as wheels:
pip wheel . -w dist        # → dist/*.whl   (or: uv build, for just this package)

# Copy dist/ to the air-gapped host, then install offline:
pip install --no-index --find-links dist vmware-storage
```

## Configuration

```bash
mkdir -p ~/.vmware-storage
cp config.example.yaml ~/.vmware-storage/config.yaml
# Edit with your vCenter/ESXi credentials

echo "VMWARE_MY_VCENTER_PASSWORD=your_password" > ~/.vmware-storage/.env
chmod 600 ~/.vmware-storage/.env

# Verify
vmware-storage doctor
```

## MCP Tools (11)

| Category | Tools | Type |
|----------|-------|------|
| Datastore | `list_all_datastores`, `browse_datastore`, `scan_datastore_images`, `list_cached_images` | Read |
| iSCSI | `storage_iscsi_enable`, `storage_iscsi_status`, `storage_iscsi_add_target`, `storage_iscsi_remove_target`, `storage_rescan` | Read/Write |
| vSAN | `vsan_health`, `vsan_capacity` | Read |

## Auto-Remediation Patterns (PoC)

The [`patterns/`](patterns/) directory hosts L5 auto-remediation candidate patterns from the Enterprise Harness Engineering framework. The first PoC pattern, [`patterns/iscsi-target-stale-rescan.yaml`](patterns/iscsi-target-stale-rescan.yaml), describes an iSCSI HBA rescan as a low-risk, reversible, repeatable operation. The pattern schema is documented here only — runtime enforcement is **not yet wired up**, so this is a reference design, not production auto-remediation.

## Common Workflows

### Set Up iSCSI Storage on a Host

1. Enable iSCSI adapter: `vmware-storage iscsi enable esxi-01`
2. Add target: `vmware-storage iscsi add-target esxi-01 10.0.0.100`
3. Verify: `vmware-storage iscsi status esxi-01`

The `add-target` command automatically rescans storage. Use `--dry-run` to preview any write command first.

### Find Deployable Images Across Datastores

1. List all datastores: `vmware-storage datastore list`
2. Scan for images: `vmware-storage datastore scan-images datastore01`
3. Browse with a pattern: `vmware-storage datastore browse datastore01 --pattern "*.iso"`

### vSAN Health Assessment

1. Check health: `vmware-storage vsan health Cluster-Prod`
2. Check capacity: `vmware-storage vsan capacity Cluster-Prod`
3. If issues found, investigate with `vmware-monitor` for alarms and events

## CLI

```bash
# Datastore
vmware-storage datastore list
vmware-storage datastore browse datastore01
vmware-storage datastore scan-images datastore01

# iSCSI
vmware-storage iscsi status esxi-01
vmware-storage iscsi enable esxi-01
vmware-storage iscsi add-target esxi-01 192.168.1.100
vmware-storage iscsi remove-target esxi-01 192.168.1.100
vmware-storage iscsi rescan esxi-01

# vSAN
vmware-storage vsan health Cluster-Prod
vmware-storage vsan capacity Cluster-Prod

# Diagnostics
vmware-storage doctor
```

## MCP Server

**After `uv tool install vmware-storage`, start the MCP server with one command** (v1.5.15+):

```bash
# Recommended — single command, no network re-resolve
vmware-storage mcp

# With a custom config path
VMWARE_STORAGE_CONFIG=/path/to/config.yaml vmware-storage mcp

# Or via Docker
docker compose up -d
```

### Agent Configuration

Add to your AI agent's MCP config:

```json
{
  "mcpServers": {
    "vmware-storage": {
      "command": "vmware-storage",
      "args": ["mcp"],
      "env": {
        "VMWARE_STORAGE_CONFIG": "~/.vmware-storage/config.yaml"
      }
    }
  }
}
```

<details>
<summary>Alternative: uvx (no install) or legacy entry point</summary>

```bash
# Run without installing (requires PyPI access each launch)
uvx --from vmware-storage vmware-storage mcp

# Legacy entry point (still works, kept for backward compatibility)
vmware-storage-mcp
```

> **Behind a corporate TLS proxy?** uvx may fail with `invalid peer certificate: UnknownIssuer`.
> Use the recommended `vmware-storage mcp` form above (no network needed), or set `UV_NATIVE_TLS=true`.

</details>

## Why a Separate Skill?

`vmware-aiops` has 49 MCP tools — too heavy for local LLMs (7B-14B). By splitting storage into its own skill:

- **11 tools** — fits comfortably in small model context windows
- **Domain-focused** — storage admins get only what they need
- **Composable** — use alongside vmware-monitor or vmware-aiops as needed

## Version Compatibility

**Python**: 3.10+ (since v1.5.27 — previously 3.11+). Tested on 3.10 / 3.11 / 3.12.

| vSphere / VCF | Support | Notes |
|---------|---------|-------|
| VCF 9.1 / vSphere 9.1 | Full | Released 2026-05-12. pyVmomi+vSAN SDK `<10.0` works via SOAP. |
| VCF 9.0 / vSphere 9.0 | Full | pyVmomi 8.0.3+ with bundled vSAN SDK connects to vSphere 9. |
| 8.0 | Full | vSAN SDK built into pyVmomi 8.0.3+ |
| 7.0 | Full | All storage APIs work |
| 6.7 | Compatible | iSCSI + datastore features work; vSAN limited |

#### Official Broadcom References

- **SDKs**: <https://developer.broadcom.com/sdks> — VCF Python SDK, vSAN Management SDK (bundled in pyVmomi)
- **REST APIs**: <https://developer.broadcom.com/xapis> — vSAN Management API, VCF API
- **CLI Tools**: <https://developer.broadcom.com/tools> — PowerCLI 9.1, ESXCLI

## Safety

| Feature | Description |
|---------|-------------|
| Read-heavy | 7/11 tools are read-only |
| Input validation | IP addresses and ports validated before iSCSI operations |
| Audit logging | All operations logged to `~/.vmware-storage/audit.log` |
| No VM operations | Cannot create, delete, or modify VMs |
| Credential safety | Passwords only from environment variables, never config files |

## Troubleshooting

| Problem | Cause & Fix |
|---------|-------------|
| iSCSI enable fails with "already enabled" | Not an error — adapter is already active. Run `iscsi status` to see configured targets. |
| "Datastore not found" when browsing | Datastore names are **case-sensitive**. Run `datastore list` to get the exact name. |
| vSAN health shows "unknown" | vSAN health requires a **vCenter connection**, not standalone ESXi. |
| Rescan doesn't discover new LUNs | Wait 15-30 seconds after adding targets, then rescan again. Verify target IP is reachable from ESXi. |
| "Password not found" error | Variable names follow `VMWARE_<TARGET_UPPER>_PASSWORD` (hyphens → underscores). Check `~/.vmware-storage/.env`. |
| Connection timeout to vCenter | Use `vmware-storage doctor --skip-auth` to bypass auth checks on high-latency networks. |
| "Datastore browse did not finish within Ns" | The datastore is very large or busy. Narrow the search with a sub-path and a specific pattern (e.g. `datastore browse ds01 --path templates --pattern "*.ova"`) instead of browsing the root — do not just retry the same broad browse. |

## License

[MIT](LICENSE)
