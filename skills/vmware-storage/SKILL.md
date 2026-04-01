---
name: vmware-storage
description: >
  Use this skill whenever the user needs to manage VMware storage — datastores, iSCSI targets, and vSAN clusters.
  Directly handles: browse datastores, scan for deployable images (OVA/ISO), configure iSCSI adapters and targets, check vSAN health and capacity.
  Always use this skill for "list datastores", "add iSCSI target", "check vSAN health", "browse datastore files", "scan for OVA images", or any storage-related VMware task.
  For VM operations use vmware-aiops, for networking use vmware-nsx. For load balancing/AVI/AKO use vmware-avi.
installer:
  kind: uv
  package: vmware-storage
allowed-tools:
  - Bash
metadata: {"openclaw":{"requires":{"env":["VMWARE_STORAGE_CONFIG"],"bins":["vmware-storage"],"config":["~/.vmware-storage/config.yaml","~/.vmware-storage/.env"]},"primaryEnv":"VMWARE_STORAGE_CONFIG","homepage":"https://github.com/zw008/VMware-Storage","emoji":"🗄️","os":["macos","linux"]}}
compatibility: >
  Requires vmware-policy (auto-installed). All operations audited to ~/.vmware/audit.db.
---

# VMware Storage

VMware vSphere storage management — 11 MCP tools for datastores, iSCSI, and vSAN.

> Split from vmware-aiops for lighter context and local model compatibility.
> **Companion skills**: [vmware-aiops](https://github.com/zw008/VMware-AIops) (VM lifecycle), [vmware-monitor](https://github.com/zw008/VMware-Monitor) (read-only monitoring), [vmware-vks](https://github.com/zw008/VMware-VKS) (Tanzu Kubernetes), [vmware-nsx](https://github.com/zw008/VMware-NSX) (NSX networking), [vmware-nsx-security](https://github.com/zw008/VMware-NSX-Security) (DFW/firewall), [vmware-aria](https://github.com/zw008/VMware-Aria) (metrics/alerts/capacity), [vmware-avi](https://github.com/zw008/VMware-AVI) (AVI/ALB/AKO).
> | [vmware-pilot](../vmware-pilot/SKILL.md) (workflow orchestration) | [vmware-policy](../vmware-policy/SKILL.md) (audit/policy)

## What This Skill Does

| Category | Tools | Count |
|----------|-------|:-----:|
| **Datastore** | list all datastores, browse files, scan for OVA/ISO/OVF/VMDK images, list cached images | 4 |
| **iSCSI** | enable adapter, show status, add target, remove target, rescan HBAs | 5 |
| **vSAN** | cluster health summary, capacity overview (total/used/free) | 2 |

## Quick Install

```bash
uv tool install vmware-storage
vmware-storage doctor
```

## When to Use This Skill

- Browse datastore files or scan for deployable images (OVA/ISO/VMDK)
- Configure iSCSI: enable adapter, add/remove send targets, rescan storage
- Check vSAN cluster health and capacity
- Any storage-focused VMware operation

**Use companion skills for**:
- VM lifecycle, deployment, guest ops → `vmware-aiops`
- Inventory, health, alarms, events → `vmware-monitor`
- Tanzu Kubernetes → `vmware-vks`
- Load balancing, AVI/ALB, AKO, Ingress → `vmware-avi`

## Related Skills — Skill Routing

| User Intent | Recommended Skill |
|-------------|-------------------|
| Read-only monitoring, alarms, events | **vmware-monitor** |
| Storage: iSCSI, vSAN, datastores | **vmware-storage** ← this skill |
| VM lifecycle, deployment, guest ops | **vmware-aiops** |
| Tanzu Kubernetes (vSphere 8.x+) | **vmware-vks** |
| NSX networking: segments, gateways, NAT | **vmware-nsx** |
| NSX security: DFW rules, security groups | **vmware-nsx-security** |
| Aria Ops: metrics, alerts, capacity planning | **vmware-aria** |
| Multi-step workflows with approval | **vmware-pilot** |
| Load balancer, AVI, ALB, AKO, Ingress | **vmware-avi** (`uv tool install vmware-avi`) |
| Audit log query | **vmware-policy** (`vmware-audit` CLI) |

## Common Workflows

### Set Up iSCSI Storage on a Host

1. Enable iSCSI adapter → `vmware-storage iscsi enable esxi-01`
2. Add target → `vmware-storage iscsi add-target esxi-01 10.0.0.100`
3. Verify → `vmware-storage iscsi status esxi-01`

The `add-target` command automatically rescans storage after adding the target. If you need an additional rescan later:

4. Rescan → `vmware-storage iscsi rescan esxi-01`

**Dry-run first**: Append `--dry-run` to any write command to preview without executing:
```bash
vmware-storage iscsi enable esxi-01 --dry-run
vmware-storage iscsi add-target esxi-01 10.0.0.100 --dry-run
```

### Find Deployable Images Across Datastores

1. List all datastores → `vmware-storage datastore list`
2. Scan a datastore for images → `vmware-storage datastore scan-images datastore01`
3. Browse with a pattern → `vmware-storage datastore browse datastore01 --pattern "*.iso"`

To filter cached results by type or datastore, use the `list_cached_images` MCP tool with `image_type` and `datastore` parameters.

### vSAN Health Assessment

1. Check health → `vmware-storage vsan health Cluster-Prod`
2. Check capacity → `vmware-storage vsan capacity Cluster-Prod`
3. If issues found, investigate with `vmware-monitor` for alarms and events

### Multi-Target Operations

All commands accept `--target <name>` to operate against a specific vCenter or ESXi host from your config:

```bash
# Default target (first in config.yaml)
vmware-storage datastore list

# Specific target
vmware-storage datastore list --target prod-vcenter
vmware-storage iscsi status esxi-lab --target lab-esxi
```

## Usage Mode

| Scenario | Recommended | Why |
|----------|:-----------:|-----|
| Local/small models (Ollama, Qwen) | **CLI** | ~2K tokens vs ~8K for MCP |
| Cloud models (Claude, GPT-4o) | Either | MCP gives structured JSON I/O |
| Automated pipelines | **MCP** | Type-safe parameters, structured output |

## MCP Tools (11)

All MCP tools accept an optional `target` parameter to select which vCenter/ESXi to connect to.

| Category | Tool | Type | Description |
|----------|------|:----:|-------------|
| Datastore | `list_all_datastores` | Read | List datastores with capacity, usage %, VM count |
| | `browse_datastore` | Read | Browse files with optional path and glob pattern |
| | `scan_datastore_images` | Read | Find OVA/ISO/OVF/VMDK in a datastore |
| | `list_cached_images` | Read | Query local image registry with type/datastore filters |
| iSCSI | `storage_iscsi_status` | Read | Show adapter status, HBA device, IQN, send targets |
| | `storage_iscsi_enable` | Write | Enable software iSCSI adapter on a host |
| | `storage_iscsi_add_target` | Write | Add iSCSI send target (IP + port) and rescan |
| | `storage_iscsi_remove_target` | Write | Remove iSCSI send target and rescan |
| | `storage_rescan` | Write | Rescan all HBAs and VMFS volumes |
| vSAN | `vsan_health` | Read | Cluster health summary and disk group details |
| | `vsan_capacity` | Read | Total/used/free capacity in GB and usage % |

**Read/write split**: 6 tools are read-only, 5 modify state. Write tools require explicit parameters (host name, IP address) and are audit-logged.

## CLI Quick Reference

```bash
# Datastore
vmware-storage datastore list [--target <name>]
vmware-storage datastore browse <ds_name> [--path <subdir>] [--pattern "*.ova"]
vmware-storage datastore scan-images <ds_name> [--target <name>]

# iSCSI
vmware-storage iscsi enable <host> [--dry-run]
vmware-storage iscsi status <host>
vmware-storage iscsi add-target <host> <ip> [--port 3260] [--dry-run]
vmware-storage iscsi remove-target <host> <ip> [--port 3260] [--dry-run]
vmware-storage iscsi rescan <host> [--dry-run]

# vSAN
vmware-storage vsan health <cluster> [--target <name>]
vmware-storage vsan capacity <cluster> [--target <name>]

# Diagnostics
vmware-storage doctor [--skip-auth]
```

> Full CLI reference with all options and output formats: see `references/cli-reference.md`

## Troubleshooting

### iSCSI enable fails with "already enabled"

Not an error. The software iSCSI adapter is already active on that host. The response includes the current HBA device name and IQN. Run `iscsi status` to see configured send targets.

### "Datastore not found" when browsing

Datastore names are **case-sensitive**. Run `vmware-storage datastore list` to get the exact name. Common mistakes: `Datastore1` vs `datastore1`, trailing spaces.

### vSAN health shows "unknown" status

vSAN health checks require a **vCenter connection** (not standalone ESXi). The full VsanVcClusterHealthSystem runs via vCenter's vSAN Health Service. If connected to a standalone ESXi host, vSAN queries will fail or return limited info.

### Rescan doesn't discover new LUNs

After adding iSCSI targets, the storage subsystem may need 10-30 seconds to enumerate new LUNs. Steps to resolve:
1. Verify the target IP is reachable from the ESXi host (`vmkping` from ESXi shell)
2. Check that the iSCSI target is correctly configured: `vmware-storage iscsi status <host>`
3. Wait 15-30 seconds, then rescan again: `vmware-storage iscsi rescan <host>`

### "Password not found" error

The password environment variable is missing. Variable names follow the pattern `VMWARE_<TARGET_NAME_UPPER>_PASSWORD` where hyphens become underscores. Example: target `my-vcenter` needs `VMWARE_MY_VCENTER_PASSWORD`. Check your `~/.vmware-storage/.env` file.

### Doctor reports ".env permissions too open"

The `.env` file contains passwords and must have owner-only permissions:

```bash
chmod 600 ~/.vmware-storage/.env
```

### Connection timeout to vCenter

The `doctor` command tests connectivity with a 5-second TCP timeout. If your vCenter is on a high-latency network, the check may fail even though the connection works. Use `--skip-auth` to bypass both connectivity and auth checks, then test manually.

## Safety

- **No VM operations**: This skill cannot power on/off, create, delete, or modify VMs — that scope belongs to `vmware-aiops`
- **Read-heavy**: 6 of 11 tools are read-only (list, browse, scan, status, health, capacity)
- **Audit logging**: All operations (including reads) are logged to `~/.vmware-storage/audit.log` in JSON Lines format with timestamp, user, target, operation, parameters, and result
- **Double confirmation**: CLI write commands (iSCSI enable, add/remove target) require two separate "Are you sure?" prompts before executing
- **Dry-run mode**: All write commands support `--dry-run` to preview API calls without executing
- **Input validation**: IP addresses validated via `ipaddress.ip_address()`, ports checked for 1-65535 range, host/cluster/datastore names looked up before operations
- **Prompt injection defense**: Datastore file names and paths from vSphere are sanitized via `_sanitize()` — strips control characters (C0/C1), truncates to 500 chars — preventing malicious file names from injecting instructions into downstream LLM agents
- **Credential safety**: Passwords loaded only from environment variables (`.env` file), never from `config.yaml`; `.env` permissions are checked at startup

> Full security details: see `references/setup-guide.md`

## Setup

```bash
uv tool install vmware-storage
mkdir -p ~/.vmware-storage
cp config.example.yaml ~/.vmware-storage/config.yaml
# Edit config.yaml with your vCenter/ESXi targets

echo "VMWARE_MY_VCENTER_PASSWORD=your_password" > ~/.vmware-storage/.env
chmod 600 ~/.vmware-storage/.env

vmware-storage doctor
```

> All tools are automatically audited via vmware-policy. Audit logs: `vmware-audit log --last 20`

> Full setup guide with multi-target config, MCP server setup, and Docker: see `references/setup-guide.md`

## Architecture

```
User (natural language)
  ↓
AI Agent (Claude Code / Goose / Cursor)
  ↓ reads SKILL.md
vmware-storage CLI or MCP server (stdio transport)
  ↓ pyVmomi (vSphere SOAP API)
vCenter Server / ESXi
  ↓
Datastores / iSCSI / vSAN
```

The MCP server uses stdio transport (local only, no network listener). Connections to vSphere use SSL/TLS on port 443.

## Audit & Safety

All operations are automatically audited via vmware-policy (`@vmware_tool` decorator):
- Every tool call logged to `~/.vmware/audit.db` (SQLite, framework-agnostic)
- Policy rules enforced via `~/.vmware/rules.yaml` (deny rules, maintenance windows, risk levels)
- Risk classification: each tool tagged as low/medium/high/critical
- View recent operations: `vmware-audit log --last 20`
- View denied operations: `vmware-audit log --status denied`

vmware-policy is automatically installed as a dependency — no manual setup needed.

## License

MIT — [github.com/zw008/VMware-Storage](https://github.com/zw008/VMware-Storage)
