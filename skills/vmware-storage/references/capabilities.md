# VMware Storage Capabilities

All 11 MCP tools exposed by `vmware-storage-mcp`, organized by category.

## Automation Level Reference

Each operation is classified by autonomy level per the Enterprise Harness Engineering framework:

| Level | Meaning | Agent autonomy | Examples in this skill |
|:-:|---|---|---|
| **L1** | Read-only, raw data | Always auto-run | `list_all_datastores`, `browse_datastore`, `scan_datastore_images`, `list_cached_images`, `storage_iscsi_status`, vSAN status queries |
| **L2** | Read + analysis / recommendation | Always auto-run | datastore capacity analysis, image registry queries, iSCSI target health correlation |
| **L3** | Single write — user must approve | Only after explicit confirmation; high-risk ops require double-confirm + `--dry-run` (see Confirm column) | `storage_iscsi_enable`, `storage_iscsi_add_target`, `storage_iscsi_remove_target`, vSAN cluster ops |
| **L4** | Multi-step plan / apply workflow | Plan generation auto; apply gated by user approval | *(roadmap — multi-host iSCSI rollout, vSAN expansion plans)* |
| **L5** | Auto-remediation from learned pattern | Pattern library only; requires `risk:low` + `reversible:true` + `repeatable:true` + signed approval | **PoC pattern (v1.5.16+)**: [`patterns/iscsi-target-stale-rescan.yaml`](../../../patterns/iscsi-target-stale-rescan.yaml) — scans for stale iSCSI devices (`devices_inaccessible_count > 0`, missing expected devices, or `last_rescan_age_minutes > 60`); action: invoke `storage_rescan` on the affected `(host, target)`; classified low-risk because the rescan is idempotent and non-destructive (no data, config, or VM state is modified). Schema only — **not yet enforced by the runtime**. |

**Notes**:
- L1/L2 tools are always safe for agents to call without confirmation.
- L3 tools always pass through the `@vmware_tool` decorator: connection check → policy check → audit log → double-confirm.
- L5 PoC pattern (`patterns/iscsi-target-stale-rescan.yaml`, v1.5.16+) is a **reference design**: it documents the candidate trigger / action / validation / circuit-breaker shape under `schema_version: 1` (see [vmware-policy auto-remediation pattern docs](https://github.com/vmware-skills/VMware-Policy/blob/main/docs/auto-remediation-patterns.md)). The pattern is `approval.status: poc_unsigned` and will only become live after `success_count_required: 5` + `failure_count_max: 0` + `distinct_operators_required: 2` + `days_observed: 90` are met and the pattern is signed.

## Datastore (4 tools)

| Tool | Description | Parameters | Risk | Confirm |
|------|-------------|------------|:----:|:-------:|
| `list_all_datastores` | List datastores with capacity, usage %, VM count | `target` (string, optional) | Low | No |
| `browse_datastore` | Browse files with optional path and glob pattern | `datastore` (string, **required**), `path` (string, optional), `pattern` (string, optional), `target` (string, optional) | Low | No |
| `scan_datastore_images` | Find OVA/ISO/OVF/VMDK deployable images in a datastore | `datastore` (string, **required**), `target` (string, optional) | Low | No |
| `list_cached_images` | Query local image registry with type/datastore filters | `image_type` (string, optional), `datastore` (string, optional) | Low | No |

**List envelope**: all four return `{items, returned, limit, total, truncated, hint}` instead of a bare array, so an agent can tell a complete answer from a first page rather than inferring it (VMware-AIops issue #31). Each enumerates its collection in full — a PropertyCollector walk, a datastore browse task, or the on-disk registry — so `total` is the real count and `truncated` is always `false`. On error the tools return `{error, hint}` (a dict, not a one-element list).

## iSCSI (5 tools)

| Tool | Description | Parameters | Risk | Confirm |
|------|-------------|------------|:----:|:-------:|
| `storage_iscsi_status` | Show adapter status, HBA device, IQN, configured send targets | `host` (string, **required**), `target` (string, optional) | Low | No |
| `storage_iscsi_enable` | Enable software iSCSI adapter on a host | `host` (string, **required**), `target` (string, optional) | Medium | Yes |
| `storage_iscsi_add_target` | Add iSCSI send target (IP + port) and rescan storage | `host` (string, **required**), `address` (string, **required**), `port` (integer, default: 3260), `target` (string, optional) | Medium | Yes |
| `storage_iscsi_remove_target` | Remove iSCSI send target and rescan storage | `host` (string, **required**), `address` (string, **required**), `port` (integer, default: 3260), `target` (string, optional) | Medium | Yes |
| `storage_rescan` | Rescan all HBAs and VMFS volumes on a host | `host` (string, **required**), `target` (string, optional) | Low | No |

## vSAN (3 tools)

| Tool | Description | Parameters | Risk | Confirm |
|------|-------------|------------|:----:|:-------:|
| `vsan_health` | Cluster health summary with disk group details per host | `cluster` (string, **required**), `target` (string, optional) | Low | No |
| `vsan_capacity` | Total/used/free capacity in GB and usage percentage | `cluster` (string, **required**), `target` (string, optional) | Low | No |
| `vsan_efficiency` | Dedup + compression status (vSAN Management SDK) | `cluster_name` (string, **required**), `target` (string, optional) | Low | No |

## Fibre Channel / multipath (2 tools, read-only)

| Tool | Description | Parameters | Risk | Confirm |
|------|-------------|------------|:----:|:-------:|
| `fc_adapter_list` | FC and FCoE HBAs per host: vmhba, model, driver, status, port type, WWPN/WWNN, `speed_reported` | `cluster` or `host` (optional; neither = every host on the target), `limit` (1-200, default 50), `offset`, `target` | Low | No |
| `storage_device_paths` | Per-device (NAA) SCSI multipath state across the hosts of one scope: `shared`, `seen_by`, `not_seen_on`, `path_count_differs`, `states_needing_attention`, per-host path counts by state, working paths, adapters, PSP/SATP, VMFS datastores | exactly one of `cluster` / `host` / `datastore` (**required**); `device`, `adapter`, `only_differences`, `limit`, `offset`, `target` | Low | No |

Source: each host's `config.storageDevice` (HBAs, SCSI LUNs, `multipathInfo`) and `config.fileSystemVolume.mountInfo`, fetched in one PropertyCollector call per scope. No SSH, no array or switch credentials, no rescans. Requested in [VMware-Storage#18](https://github.com/vmware-skills/VMware-Storage/issues/18).

- **Unread is not empty.** A host whose storage view could not be read — `NoPermission`, not connected (vCenter keeps a lost host's last-known config, so it is not treated as current), or missing from vCenter's reply — is listed in `hosts_not_read` with the reason, `complete` is `false`, and that host is never counted in `not_seen_on` or `hosts_without_fc`.
- **Observed state, not verdicts.** `states_needing_attention` holds only `dead` and `disabled`. `standby` is reported in `by_state` but not flagged (it is normal on active/passive arrays), and a path count does not prove independent fabrics.
- **Only shared devices can be missing.** `not_seen_on` is filled only when `shared` is true: the device is reached over FC or iSCSI (judged by the path's adapter type, so a dead path that lost its transport still counts) or already seen by more than one host. A local-marked device is exempt even on a SAN — that is how a boot-from-SAN LUN zoned to one host is usually marked. A disk inside one host — local, or a SAS/NVMe drive ESXi marks non-local — is never reported missing elsewhere. `mpx.*` names, non-disk LUNs (CD-ROM) and unresolved LUNs are per-host and never merged across hosts, since every host can have its own `mpx.vmhba0:C0:T0:L0`. A side effect: an array LUN that has no NAA identifier and is named `mpx.*` is shown once per host rather than as one shared device. Each path row carries `protocol` (`fc`, `iscsi` or null).
- **`adapter`** matches by vmhba name; in a cluster scope `vmhba2` can be a different card on each host.
- **`complete`** is false when any host was not read, or when a datastore's backing devices are unknown (no readable host reports the VMFS volume mounted).
- **`speed_reported`** is the raw vSphere value. The API documents bits per second, but hosts commonly report Gbit/s (e.g. `16`), so it is not converted.
- **Scope**: `storage_device_paths` requires one scope and refuses to read every host at once — multipath data is large on estates with hundreds of hosts. Per-path detail is returned only when `device` or `datastore` is given.
- **Not covered**: NVMe-oF namespaces may not appear (they are not in `multipathInfo`); zoning, array masking and switch health are out of scope.
- **Privileges**: reads host configuration only. Expected to work with the Read-Only role; not yet validated against a restricted account.
- **Typical response**: `fc_adapter_list` ~80 tokens per adapter; `storage_device_paths` ~120 tokens per device per host in summary form, 2–3× that with per-path detail.

## Risk Level Definitions

| Level | Meaning | Examples |
|-------|---------|---------|
| **Low** | Read-only query, no state change | `list_all_datastores`, `browse_datastore`, `vsan_health`, `storage_iscsi_status`, `storage_rescan` |
| **Medium** | State change affecting storage configuration, but recoverable | `storage_iscsi_enable`, `storage_iscsi_add_target`, `storage_iscsi_remove_target` |

## Tool Counts by Risk Level

| Risk | Count | Tools |
|------|:-----:|-------|
| Low | 11 | All read-only tools + `storage_rescan` |
| Medium | 3 | `storage_iscsi_enable`, `storage_iscsi_add_target`, `storage_iscsi_remove_target` |

> Note: `storage_rescan` triggers a host-level HBA rescan which is non-destructive (discovery only) and classified as Low risk. The iSCSI write tools (`enable`, `add_target`, `remove_target`) are Medium risk because they modify the host's iSCSI configuration, but changes are reversible.

## Input Validation

| Parameter | Validation | Error on Invalid |
|-----------|-----------|-----------------|
| `address` (IP) | `ipaddress.ip_address()` — accepts IPv4 and IPv6 | `ISCSIError: Invalid IP address` |
| `port` | Integer in range 1-65535 | `ISCSIError: Port must be 1-65535` |
| `host` | Looked up by exact name match in vSphere inventory | `HostNotFoundError` |
| `cluster` | Looked up by exact name match in vSphere inventory | `VSANError: Cluster not found` |
| `datastore` | Looked up by exact name match (case-sensitive) | `Datastore not found` |

## Audit Coverage

All 14 tools are wrapped with `@vmware_tool` from vmware-policy, which provides:

- **Pre-execution**: Policy rule check against `~/.vmware/rules.yaml` (deny rules, maintenance windows)
- **Post-execution**: Audit log entry written to `~/.vmware/audit.db` (SQLite WAL mode)
- **Input sanitization**: All vSphere API response text processed through `sanitize()` (truncation + control character cleanup)

## Read/Write Split

| Type | Count | Tools |
|------|:-----:|-------|
| Read | 10 | `list_all_datastores`, `browse_datastore`, `scan_datastore_images`, `list_cached_images`, `storage_iscsi_status`, `vsan_health`, `vsan_capacity`, `vsan_efficiency`, `fc_adapter_list`, `storage_device_paths` |
| Write | 4 | `storage_iscsi_enable`, `storage_iscsi_add_target`, `storage_iscsi_remove_target`, `storage_rescan` |

> Write tools require explicit parameters (host name, IP address) and support `--dry-run` in CLI mode. All write operations are audit-logged with timestamp, user, target, operation, parameters, and result.

## Connection Requirements

| Requirement | Datastore Tools | iSCSI Tools | vSAN Tools |
|-------------|:---------------:|:-----------:|:----------:|
| vCenter connection | Required | Not required (direct ESXi OK) | Required |
| ESXi host access | Via vCenter | Direct or via vCenter | Via vCenter |
| pyVmomi | Required | Required | Required |
| vSAN SDK | Not required | Not required | Recommended (for full health) |

## Runtime Requirements

| Requirement | Minimum | Notes |
|-------------|---------|-------|
| Python | 3.10+ | Lowered from 3.11 in v1.5.27 for Goose sandbox / Ubuntu 22.04 compatibility. Tested on 3.10 / 3.11 / 3.12. |
| OS | macOS, Linux | stdio MCP transport — no network listener required |
