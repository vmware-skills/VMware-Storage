## v1.5.20 (2026-05-08)

**Family alignment** — no source changes in this skill.

- **align:** Tracks v1.5.20 family bump driven by vmware-nsx-security and vmware-aria PyPI README `mcp-name:` ownership marker fix required by MCP Registry validation. Other 7 skills already had the marker; this release re-publishes them to keep the family version aligned per CLAUDE.md policy.
- **registry:** All 9 skills now registered on registry.modelcontextprotocol.io as `isLatest=true`.

## v1.5.19 (2026-05-06)

**Family alignment** — no source changes in this skill.

- **build:** Bumped `requires-python` from `>=3.10` to `>=3.11` (regression eval uses `tomllib`).
- **smoke:** Family `scripts/family_smoke.sh` adds Check 3b — recursive `--help` on every subcommand to surface broken lazy imports (yjs review 2026-05-06; 踩坑 #27).
- **align:** Tracks v1.5.19 fixes in vmware-nsx (CRITICAL CLI imports), vmware-vks (ApiClient leak), vmware-harden (Twin indexes + LEFT JOIN), vmware-policy (approval gate + singleton lock).

## v1.5.18 (2026-05-02)

**Family alignment + tooling normalization** — no source changes in this skill.

- **dev:** Migrated `[project.optional-dependencies] dev` → `[dependency-groups] dev` (PEP 735) so `uv sync --group dev` works uniformly across the family. Canonical set: `pytest>=8.0,<10.0`, `pytest-cov`, `ruff`.
- **test:** New `tests/eval/regression/test_release_blockers.py` (5 evals) catches the v1.5.x release blockers — missing `mcp_server` in wheel, AST-detected unimported runtime names, Typer app load failure, module import errors. Run via `pytest tests/eval/regression/`.
- **align:** Family version bump to v1.5.18.

## v1.5.17 (2026-05-01)

**Family alignment** — no source changes in this skill.

This release tracks vmware-pilot v1.5.17 (new `investigate_alert` template + `review_workflow` MCP tool + `parallel_group` step type) and vmware-policy v1.5.17 (L5 pattern matcher integrated into `@vmware_tool`). Both work with the existing skill MCP surface unchanged.

- **align:** Family version bump to v1.5.17.

## v1.5.16 (2026-04-30)

**Enterprise Harness Engineering alignment** — adapted from the Linkloud × addxai framework articles ([part 1](https://mp.weixin.qq.com/s/hz4W7ILHJ1yz_pG0Z1xP-A), [part 2](https://mp.weixin.qq.com/s/F3qYbyB3S8oIqx-Y4BrWNQ)).

- **feat (PoC):** New `patterns/` directory with first L5 auto-remediation candidate `iscsi-target-stale-rescan.yaml` — iSCSI HBA rescan as repeatable, low-risk, idempotent operation. Schema and lifecycle docs in companion vmware-policy. Runtime integration tracked separately.
- **docs:** "Automation Level Reference" section in `references/capabilities.md` — every tool tagged L1-L5 per the EHE framework.
- **docs:** Common Workflows in `SKILL.md` rewritten with pre-flight judgment for iSCSI setup, image scan, and vSAN health.
- **align:** Family version bump to v1.5.16.

## v1.5.15 (2026-04-29)

**UX improvements from real user feedback**

- **feat:** New top-level CLI subcommand `vmware-storage mcp` starts the MCP server. Single command, single binary on PATH after `uv tool install vmware-storage` — no more `uvx --from`, no PyPI re-resolve, no TLS-proxy issues.
- **feat:** Default `verify_ssl: true` on new targets (was `false`). Self-signed cert environments must now opt in explicitly with `verify_ssl: false` in `config.yaml`.
- **docs:** README, SKILL.md, setup-guide.md, and `examples/mcp-configs/*.json` switched to `command: "vmware-storage"`, `args: ["mcp"]`. uvx form moved to fallback with TLS-proxy troubleshooting note.
- **compat:** Legacy `vmware-storage-mcp` console script kept — existing user configs continue to work.

## v1.5.14 (2026-04-21)

**Bug fixes from code review by @yjs-2026 (follow-up)**

- **fix(P0):** `__init__.py` — version synced to match pyproject.toml (was stuck at 1.5.12)
- **fix(security):** `vsan.py` — log message now uses `sanitize(host.name)` to prevent prompt injection via log output

## v1.5.13 (2026-04-21)

**Bug fixes from code review 2026-04-20**

- **fix:** `vsan.py` — `overall_health` now returns `"unknown"` instead of hardcoded `"green"` when VsanVcClusterHealthSystem is not available; removed dead code `_get_vsan_cluster_system`
- **fix(security):** `inventory.py` — `list_hosts` now sanitizes `host.name` via `vmware_policy.sanitize()` to prevent prompt injection (consistent with `list_datastores`)

## v1.5.12 (2026-04-17)

- Align with VMware skill family v1.5.12 (security & bug fixes from code review by @yjs-2026)

## v1.5.11 (2026-04-17)

- Align with VMware skill family v1.5.11 (AVI 22.x fixes from @timwangbc)

## v1.5.10 (2026-04-16)

- Security: bump python-multipart 0.0.22→0.0.26 (DoS via large multipart preamble/epilogue)
- Align with VMware skill family v1.5.10

## v1.5.8 (2026-04-15)

- Align with VMware skill family v1.5.8 (NSX/AVI/Aria/AIops bug fixes)

## v1.5.7 (2026-04-15)

- Align with VMware skill family v1.5.7 (Pilot `__from_step_N__` fix + VKS SSL/timeout fix)

## v1.5.6 (2026-04-15)

- Fix: CRITICAL — `mcp_server` module missing from PyPI wheel. Added hatch packages config
- Align with VMware skill family v1.5.6

## v1.5.5 (2026-04-15)

- Align with VMware skill family v1.5.5

## v1.5.4 (2026-04-14)

- Security: bump pytest 9.0.2→9.0.3 (CVE-2025-71176, insecure tmpdir handling)

## v1.5.0 (2026-04-12)

### Anthropic Best Practices Integration

- **[READ]/[WRITE] tool prefixes**: All MCP tool descriptions now start with [READ] or [WRITE] to clearly indicate operation type
- **Read/write split counts**: SKILL.md MCP Tools section header shows exact read vs write tool counts
- **Negative routing**: Description frontmatter includes "Do NOT use when..." clause to prevent misrouting
- **Broadcom author attestation**: README.md, README-CN.md, and pyproject.toml include VMware by Broadcom author identity (wei-wz.zhou@broadcom.com) to resolve Snyk E005 brand warnings

### Storage-specific

- **Workflow failure branches**: Datastore and vSAN workflows include error handling steps

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

- `list_all_datastores` — List all datastores with capacity, usage 