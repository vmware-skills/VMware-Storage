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
| **[vmware-aiops](https://github.com/zw008/VMware-AIops)** ⭐ entry point | VM lifecycle, deployment, guest ops, clusters | 31 | `uv tool install vmware-aiops` |
| **[vmware-monitor](https://github.com/zw008/VMware-Monitor)** | Read-only monitoring, alarms, events, VM info | 8 | `uv tool install vmware-monitor` |
| **[vmware-vks](https://github.com/zw008/VMware-VKS)** | Tanzu Namespaces, TKC cluster lifecycle | 20 | `uv tool install vmware-vks` |
| **[vmware-nsx](https://github.com/zw008/VMware-NSX)** | NSX networking: segments, gateways, NAT, IPAM | 31 | `uv tool install vmware-nsx-mgmt` |
| **[vmware-nsx-security](https://github.com/zw008/VMware-NSX-Security)** | DFW microsegmentation, security groups, Traceflow | 20 | `uv tool install vmware-nsx-security` |
| **[vmware-aria](https://github.com/zw008/VMware-Aria)** | Aria Ops metrics, alerts, capacity planning | 18 | `uv tool install vmware-aria` |

## Quick Install

```bash
# Via PyPI
uv tool install vmware-storage

# Or pip
pip install vmware-storage
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

```bash
# Run directly
uvx --from vmware-storage vmware-storage-mcp

# Or via Docker
docker compose up -d
```

### Agent Configuration

Add to your AI agent's MCP config:

```json
{
  "mcpServers": {
    "vmware-storage": {
      "command": "vmware-storage-mcp",
      "env": {
        "VMWARE_STORAGE_CONFIG": "~/.vmware-storage/config.yaml"
      }
    }
  }
}
```

## Why a Separate Skill?

`vmware-aiops` has 33 MCP tools — too heavy for local LLMs (7B-14B). By splitting storage into its own skill:

- **11 tools** — fits comfortably in small model context windows
- **Domain-focused** — storage admins get only what they need
- **Composable** — use alongside vmware-monitor or vmware-aiops as needed

## Version Compatibility

| vSphere | Support | Notes |
|---------|---------|-------|
| 8.0 | Full | vSAN SDK built into pyVmomi 8.0.3+ |
| 7.0 | Full | All storage APIs work |
| 6.7 | Compatible | iSCSI + datastore features work; vSAN limited |

## Safety

| Feature | Description |
|---------|-------------|
| Read-heavy | 6/11 tools are read-only |
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

## License

[MIT](LICENSE)
