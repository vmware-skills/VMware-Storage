## v1.4.9 (2026-04-11)

- Fix: require explicit VMware/vSphere context in skill routing triggers (prevent false triggers on generic "clone", "deploy", "alarms" etc.)
- Fix: clarify vmware-policy compatibility field (Python transitive dep, not a required standalone binary)

## v1.4.8 (2026-04-09)

- Security: bump cryptography 46.0.6→46.0.7 (CVE-2026-39892, buffer overflow)
- Security: bump urllib3 2.3.0→2.6.3 (multiple CVEs) [VMware-VKS]
- Security: bump requests 2.32.5→2.33.0 (medium CVE) [VMware-VKS]

## v1.4.7 (2026-04-08)

- Fix: align openclaw metadata with actual runtime requirements
- Fix: standardize audit log path to ~/.vmware/audit.db across all docs
- Fix: update credential env var docs to correct VMWARE_<TARGET>_PASSWORD convention
- Fix: declare .env config and vmware-policy optional dependency in metadata

# Release Notes

## v1.4.5 — 2026-04-03

- **Security**: bump pygments 2.19.2 → 2.20.0 (fix ReDoS CVE in GUID matching regex)
- **Infrastructure**: add uv.lock for reproducible builds and Dependabot security tracking


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v1.4.0 — 2026-03-29

### Architecture: Unified Audit & Policy

- **vmware-policy integration**: All MCP tools now wrapped with `@vmware_tool` decorator
- **Unified audit logging**: Operations logged to `~/.vmware/audit.db` (SQLite WAL), replacing per-skill JSON Lines logs
- **Policy enforcement**: `check_allowed()` with rules.yaml, maintenance windows, risk-level gating
- **Sanitize consolidation**: Replaced local `_sanitize()` with shared `vmware_policy.sanitize()`
- **Risk classification**: Each tool tagged with risk_level (low/medium/high) for confirmation gating
- **Agent detection**: Audit logs identify calling agent (Claude/Codex/local)
- **New family members**: vmware-policy (audit/policy infrastructure) + vmware-pilot (workflow orchestration)


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v1.3.1 — 2026-03-27

### Family expansion: NSX, NSX-Security, Aria

- Added vmware-nsx, vmware-nsx-security, vmware-aria to companion skills routing table
- README updated with complete 7-skill family table
- vmware-aiops is now the family entry point (`vmware-aiops hub status`)


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v1.3.0 — 2026-03-26

### Docs / Skill optimization

- SKILL.md restructured with progressive disclosure (3-level loading)
- Created `references/` directory: cli-reference.md, setup-guide.md
- Added trigger phrases to YAML description for better skill auto-loading
- Added Common Workflows section (iSCSI setup, image discovery, vSAN health)
- Added Troubleshooting section (6 common issues)
- README.md and README-CN.md updated with Companion Skills, Workflows, Troubleshooting


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v1.2.3 (2026-03-22)

### Docs / SKILL.md restructure

- Reorder SKILL.md: tool table and Quick Install first, routing table last — improves Skills.sh/ClawHub page readability.


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v1.2.1 (2026-03-22)

### Docs & Skill Routing / 文档与 Skill 智能路由

- SKILL.md 新增 **Related Skills — Skill Routing** 路由表：遇到 VM 操作推荐 vmware-aiops，遇到只读监控推荐 vmware-monitor。
- Added README-CN.md — full Chinese documentation.
- Added `examples/mcp-configs/` — 7 agent config templates (Claude Code, Cursor, Goose, Continue, LocalCowork, mcp-agent, VS Code Copilot).


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v1.2.0 (2026-03-22)

### Initial Release / 首次发布

Domain-focused VMware storage skill, split from vmware-aiops for lighter context and better local model compatibility.

从 vmware-aiops 中按领域拆分出的存储管理 skill，更轻量，对本地模型更友好。

### Datastore Management / 数据存储管理

- `list_all_datastores` — List all datastores with capacity, usage %, accessibility
- `browse_datastore` — Browse files in any datastore directory
- `scan_datastore_images` — Find OVA, ISO, OVF, VMDK across datastores
- `list_cached_images` — Query local image registry with filters

### iSCSI Configuration / iSCSI 配置

- `storage_iscsi_enable` — Enable software iSCSI adapter on ESXi hosts
- `storage_iscsi_status` — Show adapter status and configured targets
- `storage_iscsi_add_target` — Add iSCSI send target with auto-rescan
- `storage_iscsi_remove_target` — Remove target with auto-rescan
- `storage_rescan` — Force rescan all HBAs and VMFS volumes

### vSAN Monitoring / vSAN 监控

- `vsan_health` — Cluster health summary with disk group details
- `vsan_capacity` — Total/used/free capacity with usage percentage

### Infrastructure / 基础设施

- CLI (`vmware-storage`) with typer — datastore/iscsi/vsan subcommands
- MCP server (11 tools) via stdio transport
- Docker one-command launch
- `vmware-storage doctor` — 6-check environment diagnostics
- Audit logging (JSON Lines)

**PyPI**: `uv tool install vmware-storage==1.2.0`
