## v1.8.14 — the healthy datastore was the one that crashed

Two crashes and a placeholder that stood in for a real answer.

`datastore list` failed every time on a *healthy* datastore: an empty style
string rendered `[]12.5%[/]`, which Rich parses as markup and rejects. Only the
normal case crashed, which is how it shipped. A datastore legitimately named
`[SSD] prod` hit the same parser.

`vsan_health` hardcoded `overall_health: "unknown"` under a note claiming a full
health check was out of reach. It is not — `vsanapiutils` ships inside pyvmomi
and the health system sits beside the config system this skill already uses. The
placeholder was the worst possible one, because vSAN returns "unknown" itself:
"we did not ask" was spelled identically to "vSAN does not know", and here it
stood in for **red**.

The suite also stopped writing to the operator's real audit database.

**The `vmware-policy` floor moves to >=1.11.0.** Policy 1.11.0 stops the engine
failing open: on a host whose locale is not UTF-8, reading `rules.yaml` raised a
decode error that was swallowed, and a `freeze-production-writes` rule came back
ALLOW. No new API is used here, so the floor could have stayed — it is raised
because leaving it low means a user resolving 1.10.0 keeps the permissive engine
and the fix never reaches them. One behaviour travels with it: on a host whose
rules file cannot be read, operations move from all-allowed to all-denied.
`VMWARE_POLICY_DISABLED=1` is checked above the rules, so the escape hatch does
not itself depend on them loading.

Also in this release: the suite no longer appends to the operator's real
`~/.vmware/audit.db`. It held over 30,000 rows dominated by tool names nobody
had invoked, including 1,400 entries for a destructive operation that never
happened — an audit trail carrying test fiction cannot answer the question it is
kept for.

## v1.8.13 — the schema an agent reads now carries the descriptions

Parameter descriptions reach the JSON schema for the first time. An MCP client
sees the schema, not the docstring, and this repo's coverage of `description`
and `additionalProperties` was 0% — while nearly every parameter was already
described in an `Args:` block no client ever receives.

Measured on a real VCF 9.1 estate, the gap produced a silent failure with no
error at any stage: a parameter name guessed wrong is discarded and the tool
returns the full unfiltered result; a value guessed wrong (`power_state=
"running"`) returns 0 rows where there were 11.

vmware-policy 1.10.0's `describe_tool_parameters` copies what is already
written, so the docstring is now load-bearing and the two cannot drift apart. It
removes the `Args:` block from the description once copied — both travel in
every `tools/list` response, so leaving it bills the same sentences twice
against the manifest's token budget. `additionalProperties` is closed: an open
schema is room for a model to invent arguments that are then silently
discarded, which is the other half of the same failure.

**The `vmware-policy` floor moves to >=1.10.0.** Older releases have no
`describe_tool_parameters`, and resolving one gives an ImportError at server
start rather than a missing feature.

The Chinese README also said 11 tools where there are 12, and quoted
vmware-aiops at 49 tools where it now has 60 — a cross-skill reference drifts
just as quietly as an internal one.

## v1.8.12 — reporting what was not read, instead of answering for it

Found against a real VCF 9.1 estate where four of eight ESXi hosts were
`notResponding`. vCenter keeps answering for such a host out of its own cache,
with no error and no marker, so a read "succeeds" and looks authoritative.

**`vsan_health` returned `disk_groups: []` and said nothing about the four hosts
it could not read** — the exception was logged and the host skipped. An empty
list is indistinguishable from "this cluster has no disk groups", and this
cluster's real `overallHealth` was red. Every host that does not contribute is
now recorded with its `connectionState` and the reason, counted in
`hosts_read`/`hosts_not_read`, flagged by `disk_groups_complete`, and named in
the message — the part a chat client reliably renders.

**`vsan_capacity` returned total/used/free all `0`** with no message, having
never consulted `summary.accessible`: a healthy-looking empty datastore. The
flag is now read as three states — `false` returns nulls and says why, `null`
(an older summary that did not say) leaves the figures standing and reports the
unknown rather than resolving it to true.

**`storage_iscsi_status` crashed** with `AttributeError: 'NoneType' object has
no attribute 'storageDevice'`, because `HostSystem.config` is `None` for an
unreachable host. The obvious repair was the wrong one: `_get_iscsi_hba`'s
`None` already means "no software iSCSI adapter" and renders as
`enabled: false`, so guarding the attribute would have traded a crash for a
confident false claim about a machine nobody reached. It raises a teaching error
naming the state instead.

**`doctor` cleared an estate it had not checked** — it authenticated only the
default target, and it inspected a different config file from the one the tools
load. `load_config` never consulted `VMWARE_STORAGE_CONFIG` at all while the MCP
server did, so the agent's tools opened one file and the CLI and doctor opened
another. The precedence now lives in one `resolve_config_path`. **This changes
CLI behaviour**: `vmware-storage` now honours that variable, where it previously
ignored it.

Also: `server.json` never started the MCP server, and the Dockerfile could not
build the wheel it installs.

## v1.8.11 — two wrong numbers: the server's own version, and the advertised tool count

Both defects were invisible to the test suites and both were user-facing.

- **The MCP server reported the SDK's version as its own.** `FastMCP` accepts no
  `version` argument and leaves the lowlevel server's at `None`; with it `None`
  the SDK answers `initialize` with its OWN version. Every skill in the family
  therefore told its client it was mcp 1.29.1 — a number that exists for no
  package here, and one that would change with an SDK bump and no code change of
  ours. Verified end to end rather than by reading: unset the field and a probe
  server reports the installed SDK's version; set it and it reports ours.
- **server.json advertised a stale tool count.** That number is what MCP Registry
  publishes and what the plugin manifest and marketplace copy, so one stale
  integer was wrong in three public places. Corrected against the registered
  tools: 11 advertised, 12 real. README and SKILL.md were already right.

Also new: this repo is installable as a Claude Code plugin
(`/plugin install vmware-storage@vmware-skills`). The skill and its MCP server arrive in
one step; nothing is duplicated, the manifest points at the existing `skills/`
tree. family_smoke gained three gates — the server's reported version, the plugin
manifest's agreement with pyproject, and the advertised tool count against the
live registration.

## v1.8.10 (2026-08-06) — vSAN data-efficiency read (VCF 9.1) + Fable5-review hardening

### Added — `vsan_efficiency`: dedup + compression status

A new read tool across ops, MCP, and CLI, taking the skill from **11 to 12 MCP
tools (8 read / 4 write)**. It reads vSAN data-efficiency state via the **vSAN
Management SDK** (`vsanapiutils.GetVsanVcMos` → `VsanVcClusterConfigSystem` →
`VsanClusterGetConfig(cluster).dataEfficiencyConfig`), **not** base pyVmomi, and
returns `{cluster_name, vsan_enabled, dedup_enabled, compression_enabled}`. When
vSAN reports no `dataEfficiencyConfig` (space efficiency off, or an OSA cluster
without it), `dedup_enabled`/`compression_enabled` come back `null` with a
`message` rather than a fabricated `False`. Read-only, `risk:low`, no side
effects. CLI: `vmware-storage vsan efficiency <cluster>`.

**Deliberately NOT built.** vSAN **Global Deduplication** and **vSAN-to-vSAN
replication** are not exposed — neither has a verified SDK object (global dedup
has no distinct field; v2v replication lives in the separate vSAN Data
Protection plane, not the vSAN Management SDK). They are pinned as
deferred/not-tool-able, and a regression test fails if either ever appears as an
ops symbol or a registered MCP tool (踩坑 #36 phantom endpoint).

### Fixed — Fable5 review

- **SSL context propagation to the vSAN SDK (HIGH).** On a `verify_ssl: false`
  (self-signed) target, `GetVsanVcMos` would otherwise build a fresh stub with
  Python's DEFAULT *verifying* context and die with `SSLCertVerificationError`
  (then masked by `_safe_error`). `verify_ssl` is now stashed in an
  `id(si)`-keyed module side store — never `setattr` on the pyVmomi
  `ManagedObject` (踩坑 #32) — and the SDK is handed a matching unverified
  context. The side store **defaults to strict (`True`)** for an SI this manager
  never created (so a downstream SDK caller never silently drops to an
  unverified context by accident), is **evicted on reconnect** to close the
  id-reuse hazard (a GC'd `si`'s `id()` reused by a different target reading
  stale `verify_ssl`), and is cleared at `atexit`.
- **Defensive response reads.** Absent SDK response fields degrade to `None` —
  never an `AttributeError`, never a healthy-looking `False` (空结果读作没问题,
  form #1).
- **Anti-phantom spec + regression (踩坑 #36).** The exact SDK
  object/method/field surface is transcribed to `tests/eval/spec/` and enforced:
  a test scans the shipped ops source and fails on any vSAN-SDK call outside the
  verified allow-list or on the write twin `VsanClusterReconfig`, and a
  conformance test resolves the method + `dataEfficiencyConfig.*` fields against
  pyVmomi's own vSAN type metadata (real, not remembered).
- **ruff per-file-ignore** (`UP007`/`UP045` off for `mcp_server/**`) keeps the
  reflected tool signatures at `Optional[X]` so pyupgrade cannot rewrite them to
  PEP 604 `X | None` and break FastMCP reflection on Python 3.10 (踩坑 #33).
  Dropped an unused `VmomiJSONEncoder` import in `connection.py`.
- **SKILL.md tool count reconciled to the live `mcp.list_tools()` (12; 8R/4W)**
  — including the two prose spots ("7 tools are read-only", "7 of 11 tools")
  the table update had left behind (踩坑 #34).

### Beta / real-hardware caveats (honest)

The vSAN SDK **call structure** — the `vsan-cluster-config-system` accessor, the
`VsanClusterGetConfig` method, and the `dataEfficiencyConfig.dedupEnabled` /
`compressionEnabled` property paths — is verified **against pyVmomi's own vSAN
type metadata**, i.e. at the path/type level, **not against a live VCF 9.1
appliance**. What is not yet verified is the runtime behaviour on real hardware:

- the **wire response field names/shape** are read defensively
  (`getattr(..., None)`) and are still `needs-real-vsan` — path-verified only;
- the unverified-SSL `context=` kwarg to `GetVsanVcMos` is unit-tested, but the
  **full self-signed round-trip against a real vSAN cluster** is still a gate
  (`needs-real-vsan`).

Confirm both on first run against a real vSAN cluster:
`vmware-storage vsan efficiency <cluster>`. (No PromQL/PAIS/collector-pending
surfaces are part of this skill — those belong to sibling skills, not
vmware-storage.)

## v1.8.9 — moved to vmware-skills org + MCP Registry namespace io.github.vmware-skills/vmware-storage

Repo transferred from github.com/zw008 to github.com/vmware-skills (redirects preserve old links).
MCP Registry server renamed to `io.github.vmware-skills/*`; the old `io.github.zw008/*` entry is deprecated.
All in-repo links updated. No functional code change on this line beyond the org move.

## v1.8.8 — CLI writes now route through policy + audit, exactly like the MCP tools

Every state-changing CLI command is now wrapped by `@guarded`, the CLI counterpart
to the MCP `@vmware_tool` decorator: it runs the same vmware-policy `guard()`
authorization and writes the same `audit_call()` row to `~/.vmware/audit.db`. A
`delete`/`disable`/destructive command run through a shell is now authorized and
recorded exactly like the equivalent MCP tool — closing the gap where CLI writes
bypassed policy and landed only in the legacy per-skill log (HLD I-1/I-8).

- a policy `deny` rule now refuses the operation on the CLI with a teaching line
  naming the rule that fired, not a traceback
- the legacy per-skill audit log is still written this release (dual-write); it is
  removed at 2.0
- **requires vmware-policy >= 1.8.8** (the release that adds the shared `guarded` core)
- a regression test derives the write-command set from the MCP `[WRITE]` markers and
  asserts every one is `@guarded`, so a new write command cannot ship unguarded

Also carries the environment-field docstring correction (an optional label a `deny`
rule may scope to — there is no "warn now / refuse next major" gate).

## v1.8.7 (2026-07-21) — the skill-level read-only switch is removed; read/write authorization is the vCenter account's job (RBAC)

### Removed: `VMWARE_READ_ONLY` / `read_only:` — give the agent a read-only service account instead

The skill-level read-only switch is gone. It was enforced only on the MCP tool
registry, and any agent with a shell (every SKILL.md grants `allowed-tools: Bash`)
could reach the same change one CLI command away — so it withheld the *tool*, not
the *capability*. It was never a real boundary.

To run an agent read-only, give it a **read-only vCenter/NSX service account
(RBAC)**. Writes are then refused at the platform, un-bypassably, regardless of
surface or shell — the one place read/write control cannot be stepped around. A
config still carrying `read_only: true` is ignored, with a one-time warning that
names the replacement (no silent behavior change).

### Removed: approval tiers and the declared-environment gate (via vmware-policy)

The graduated-autonomy approval tiers (`confirm`/`dual`/`review`) and the "declare
an environment or be refused" baseline are removed — they only ever fired on the
rarest configuration while carrying the family's most complex machinery. Opt-in
`deny` rules and the maintenance window remain, and apply identically wherever a
tool runs.

### Added: offline / air-gapped install docs

The README now covers installing from source without editable mode (for older
`pip`) and building wheels to carry onto an air-gapped host — the modern PEP 517
layout has no `setup.py` by design, which is expected, not a missing file.

This release also carries the accumulated fixes staged since 1.8.5.

## v1.8.5 (2026-07-20) — the two fixes v1.8.4 announced now actually work

Four adversarial reviews of v1.8.4 found that both of its headline fixes were
incomplete in ways the release notes did not reflect. This release makes them
real. If you are on 1.8.4, this is the one to take.

### Fixed — a failure that was *returned* was still audited as a success

vmware-policy 1.8.4 added `report_tool_failure()` for tools that catch an
exception and return an error payload instead of raising. **No skill called it.**

Every string-returning tool therefore kept doing exactly what 1.8.4 said it had
stopped doing: writing `status=ok` to `~/.vmware/audit.db` for an operation that
failed, recording an undo token for a change that never happened, and telling the
circuit breaker the call succeeded so repeated failures never tripped it.

The surface this covered is not marginal:

| Skill | What was mis-audited |
|---|---|
| vmware-aiops | 25 of 49 tools, including **every undo-bearing write** — a failed `vm_power_on` left an undo token saying "power it back off" |
| vmware-avi | all 28 tools, including `vs_toggle` and `ako_restart` |
| vmware-storage | all 4 write tools |
| vmware-nsx | the 5 delete tools |

vmware-avi is worth calling out: before 1.8.4 its exceptions propagated and the
audit was correct. 1.8.4 caught them and returned a string, so **that release made
its audit trail worse than it had been.**

Skills whose tools already return dict payloads (vmware-monitor, vmware-vks,
vmware-aria, vmware-log-insight, vmware-harden, vmware-debug, vmware-pilot) were
already detected correctly. They gained a test proving it rather than a redundant
call.

### Fixed — narrowing `OSError` did not close the leak it was meant to close

1.8.4 narrowed the `_safe_error` passthrough because bare `OSError` let TLS and
DNS failures reach the agent with hostnames and certificate subjects in them.
That narrowing had no effect on the error it was written for:

```
ssl.SSLCertVerificationError → ssl.SSLError → OSError, ValueError
```

`ValueError` has been on every allowlist since long before 1.8.4, so a
certificate failure kept passing through — the commonest self-signed-certificate
failure in this family, carrying the hostname it was checked against. An
allowlist structurally cannot express "not this one".

Where `ssl.SSLError` can actually surface — the pyVmomi skills — it is now
reduced *ahead* of the allowlist. In the httpx skills TLS arrives wrapped as
`httpx.ConnectError`, and in vmware-avi as `requests.exceptions.SSLError`, so the
guard cannot fire there; in those skills the leak was the raw exception
interpolated into an already-allowlisted `*ApiError`, and that is now authored
text naming the config target and `verify_ssl` instead of the exception.

The missing-password error — this family's most common first-run failure, whose
entire remedy is the environment variable name it carries — keeps its message
through a narrow `ConfigError(OSError)` rather than the base class. Connection
failures are translated at the connection layer into an authored remedy that
names the target and the setting to change, with the raw detail left on
`__cause__` for the server log.

### Also fixed

- **vmware-vks**: the quickstart documented a password variable the code never
  reads — following `README.md` verbatim produced "Password not found". Five
  places, plus six references to a `doctor` command this CLI has never had, two
  descriptions promising fields the tools do not return, and eight teaching
  messages that `RuntimeError` was masking.
- **vmware-nsx**: an error cited `--route-advertisement`; the flag is `--advertise`.
- **vmware-pilot**: `get_workflow_status` told the model to call `approve` — a
  tool the read-only gate withholds — as the required next step; and a hint
  pointed at a filename that could never appear in that message.
- **vmware-aiops**: `vm_task_status` polling a *failed task* returned
  `{"state": "error", "error": ...}` from a successful read, which the new
  detection read as the call itself failing. The field is now `task_error`.
  **This is a breaking change for anything parsing that payload.**
- Several remedies that were still being cut by the 300-character cap the 1.8.4
  notes claimed to have addressed.

### Known and not fixed

`ConnectionError` remains one type from two sources in several skills — a
skill's own authored message and urllib3's `HTTPSConnectionPool(host=..., port=...)`
share it, and an allowlist cannot separate them. vmware-vks is converted; the
rest need their own domain type and are deferred rather than half-done.

## v1.8.4 (2026-07-20) — errors that teach, and tool descriptions a small model can route from

A capability eval was rolled out across the family and asked two open questions:
when a call fails, is the model told enough to fix it, and can it pick the right
tool from the description alone? Both answers were worse than anyone thought, and
in several places the reason was that the measurement was looking somewhere other
than where the model reads.

### Fixed — teaching messages were being discarded on the way to the agent

`_safe_error` reduces unrecognised exceptions to `"<Class>: operation failed."`
so raw API text, credentials in URLs and internal paths cannot reach an agent.
Its allowlist held only the builtin validation errors — so this skill's **own**
domain exceptions, the ones that exist precisely to carry a corrected next step,
had their messages replaced by their class names.

The effect was invisible from the CLI, which prints those messages in full.

The worst case was shared by nine skills: `config.py` raises exactly one
`OSError`, the missing-password error, whose entire remedy is the environment
variable name it names. An agent hitting an unconfigured target received
`OSError: operation failed.` and had nothing to act on. That is the family's most
common first-run failure, and it landed one release after the documented variable
names were corrected — so the message that would have unstuck the operator was
the one being thrown away.

The rule is now the property it always meant: **every exception this skill raises
on purpose passes through**, and only genuinely unplanned ones are reduced.
`RuntimeError` stays reduced — it is the generic catch-all and in several skills
carries raw upstream text.

### Fixed — error messages now carry the correction

Every message that reported a failure without saying how to recover was
rewritten: it names the offending value, gives an imperative remedy, and names
something concrete to act on — a tool that exists, a real CLI command, a config
file, an environment variable. Recovery becomes an instruction-following problem
rather than an inference one, which is what a weak model can still do.

Three classes of defect surfaced while doing it:

- **Remedies that were never delivered.** `_safe_error` truncates with no
  ellipsis, so a message longer than the cap loses its closing sentence
  silently. One message had been shipping at 396 characters against a 300-char
  cap — its remedy had never once reached an agent. Messages now lead with the
  remedy so a long interpolated value truncates the expendable detail instead.
- **Commands that do not exist.** One skill's error hints named a `doctor`
  subcommand it does not have.
- **Tools that do not exist.** A tool description pointed at two sibling-skill
  tools that had been renamed, and another named a tool that had moved to a
  different skill entirely.

### Improved — tool descriptions state when to use them and what to call next

The description is the API for a small model: an unstated routing rule is a
routing rule that does not exist, and a tool with no stated next hop is one the
model stops at. Descriptions now say when to prefer this tool over a sibling,
what shape comes back, the caveat that bites, and which tool to call after.

**Manifest size did not grow.** Descriptions load into every session, so the
routing clauses were paid for by cutting duplicated reference material —
repeated boilerplate, examples that restated the parameter list, and prose
copies of the pagination contract.

### Note

Every tool and CLI command named anywhere in this release was verified against
the live MCP registry and the live command tree, not against documentation.

## v1.8.3 (2026-07-20) — credentials resolve as a pair; documented env vars now exist

### Added — the per-target username can come from the environment

Adapted from [VMware-AIops#33](https://github.com/vmware-skills/VMware-AIops/pull/33) by
@wright-bench, with thanks. The password already resolved from an env var; the
username did not, so a deployment injecting credentials from a secret store
(systemd `EnvironmentFile`, container secrets, a vault sidecar) could externalise
only half of the pair — and a config-file username paired with an env password
from a different account logs in as nobody.

`<PASSWORD-KEY-PREFIX>_USERNAME` now overrides the `username:` in config.yaml,
using that skill's own password-key convention. Absent, config.yaml still wins;
nothing changes for anyone not setting it.

**Resolved on every access, like the password.** The contributed version read the
username once at load time while the password stayed a property, which
reintroduces exactly the split the override exists to prevent: a sidecar rotating
both halves mid-process moves the password and leaves the username behind. A test
pins that both halves resolve at the same moment.

### Fixed — documented credential variables that the code never read

Rolling the above across the family surfaced a separate defect: four skills
documented a password variable their own loader does not look up. An operator
following the documentation exactly — correct file, correct place, correct-looking
name — got "Password not found".

| Skill | Documented | Actually read |
|---|---|---|
| vmware-nsx | `VMWARE_NSX_<TARGET>_PASSWORD` for target `nsx-prod` → `VMWARE_NSX_PROD_PASSWORD` | `VMWARE_NSX_NSX_PROD_PASSWORD` |
| vmware-nsx-security | `VMWARE_<TARGET>_PASSWORD` | `VMWARE_NSX_SECURITY_<TARGET>_PASSWORD` |
| vmware-aria | `VMWARE_<TARGET>_PASSWORD` | `VMWARE_ARIA_<TARGET>_PASSWORD` |
| vmware-vks | `VMWARE_<TARGET>_PASSWORD` | `VMWARE_VKS_<TARGET>_PASSWORD` |
| vmware-avi | three different forms across three files | `<CONTROLLER>_PASSWORD` |

The prefixes genuinely differ per skill, so nothing could be fixed by
standardising a pattern — each repo's docs were corrected against its own code.
The code was left alone: changing a key would break every existing deployment.

`family_smoke.sh` now compares the credential variables named in each repo's docs
against the ones that repo's code builds, so the two cannot drift apart again.

## v1.8.2 (2026-07-20) — the MCP server moves into the package namespace

### Fixed — co-installing two skills broke all but the last one

Every skill shipped its MCP server as a **top-level `mcp_server` package**. Python
has one top-level namespace, so installing any two of them into one environment let
the second overwrite the first — silently, with no error and no warning.

    uv tool install vmware-aiops   ->  49 tools   (correct)
    uv pip  install vmware-aiops   ->  27 tools   (Monitor's read-only server)

vmware-aiops depends on vmware-monitor, so this was not an edge case: **every pip
install hit it**, and the operator got 27 read-only tools where 49 were expected,
with all 35 write tools missing. Docker images, shared MCP hosts and CI runners that
install more than one skill were affected the same way.

The server now lives at `vmware_<skill>/mcp_server/`, a name only this package can
claim. Introduced 2026-02-26; it survived 70 releases because every test ran against
a single package in its own repo, where the local directory shadows site-packages —
the conflict was invisible by construction.

**Migration.** Console scripts are unchanged: `vmware-<skill>` and
`vmware-<skill>-mcp` work exactly as before, as does `"command": "vmware-<skill>",
"args": ["mcp"]` in an MCP client config. Only a direct `python -m mcp_server`
breaks; use `python -m vmware_<skill>.mcp_server`.

### Added — `references/agent-guardrails.md` in every skill

The operating rules for local and small models (Llama 3.3 70B, Qwen, Mistral via
Goose / Ollama / OpenShift AI) existed in two skills. They now ship in all 13, each
with its own tool counts and failure modes, and are linked from every SKILL.md.

## v1.8.1 (2026-07-19) — read-only mode reaches the surfaces that teach it

v1.8.0 put read-only mode in the code and documented it in the README only.
Every other layer was empty, and each serves a different reader: SKILL.md is what
the agent loads, setup-guide is what an operator reads while configuring, `doctor`
is where they verify it took. The gap had two concrete costs.

An agent read SKILL.md, called a write tool the gate had withheld, and got nothing
back — with no way to learn that the absence was a deliberate lockdown rather than
a fault. It reads as a broken tool, so the model retries or hunts for a workaround.

An operator who set the switch had no way to confirm it. The only signal was a line
in the MCP server's start-up log.

### Added — the feature is now documented where each reader looks

- **SKILL.md** — a short section telling the agent that a missing write tool is a
  lockdown, not a fault: name the blocked operation, do not retry, do not route
  around it.
- **references/setup-guide.md** — the operator's view: how to enable it, the
  precedence chain, and how to verify.
- **references/capabilities.md** — which tools the gate withholds.

### Added — `doctor` reports the read-only state

`vmware-storage doctor` now shows whether read-only mode is on, **which** of the three
switches decided it, and the value as written. A typo'd value (`ture`) is called
out as a typo rather than reported as a confident ON — it resolves to on, which is
fail-closed but almost never what was meant.

The resolution runs through `vmware_policy.read_only_status()` rather than a local
copy of the precedence chain: a doctor that disagrees with the gate it reports on is
worse than no doctor. Requires `vmware-policy>=1.8.1`.

## v1.8.0 (2026-07-18) — read-only mode, working policy defaults, declared environments

Family release driven by [VMware-AIops#31](https://github.com/vmware-skills/VMware-AIops/issues/31),
where an operator running Llama 3.3 70B (Goose / OpenShift AI, on-prem H100) had to
hand-write 17 prompt guardrails to make tool calling reliable. A prompt is advisory — a
model can ignore it. Every guardrail that could move into the harness has.

### Added
- **Read-only mode.** Set `VMWARE_READ_ONLY=true` (or `VMWARE_<SKILL>_READ_ONLY`, or
  `read_only: true` in config.yaml) and every write tool is removed from the MCP registry
  at start-up. `list_tools()` never offers them, so the model cannot call what it cannot
  see. **Off by default** — nothing changes unless you turn it on. Fail-closed: if the
  mode is requested but cannot be guaranteed, the server refuses to start rather than
  running open.
- **`environment:` on each config target**, declaring which environment it is
  (production / staging / lab). Policy rules scope by this value.

### Added — list results now state whether they are complete

Every `[READ]` list tool returns the family envelope instead of a bare array:

    {"items": [...], "returned": 50, "limit": 50, "total": 213,
     "truncated": true, "hint": "Showing 50 of 213. Raise limit or narrow the query..."}

This closes the reported failure where long responses were summarised as "no data
returned": a bare list gives a model no way to tell a complete answer from page one, so
it guessed. `truncated: false` now positively states completeness — including when
`items` is empty, which means "checked, found none", not "the call failed".

- **4 tool(s) converted** across ops, MCP and CLI. All four report a real `total` (PropertyCollector walk, browse task, and the in-memory
  registry filter are each complete enumerations).

### Changed — migration, read this
- **Approval tiers now actually run.** They shipped in v1.6.0 but the engine only ever
  read `~/.vmware/rules.yaml`, and a fresh install has no such file — so every deny rule,
  maintenance window and approval tier had been inert on every install that never
  hand-authored one. A packaged baseline now loads when you have written no rules of your
  own. Writes at medium risk and above are stamped with their tier in the audit log;
  irreversible work and guest execution against a target declared `production` require a
  named approver via `VMWARE_AUDIT_APPROVED_BY`.
- **`environment:` will become required for writes.** Today a state-changing operation
  against a target that declares none still runs and logs a warning. **The next major
  release refuses it.** Declare it now and that upgrade is a no-op:

      targets:
        prod-vc01:
          host: vc01.corp.local
          environment: production

  Read-only operations are never affected, in this release or the next. Check what applies
  to your targets before upgrading: `vmware-audit policy --operation vm_delete --env <env>`.

### Fixed
- **Policy glob patterns with a leading wildcard silently matched nothing.** A rule written
  `operations: ["*_delete"]` parsed fine, read correctly, and never fired — only a trailing
  `*` was honoured. Now full glob matching, for operations and environments alike.
- Config-path overrides (`VMWARE_<SKILL>_CONFIG`) are honoured when reading `read_only`
  and `environment`, so a setting in a custom config file is no longer silently ignored.

### Notes
- Requires `vmware-policy>=1.8.0`; publish that package first.
- `vmware-audit policy` reports which rules are in force and where they came from —
  including the case where your rules file exists but failed to parse, which previously
  looked identical to "policy is working".

## v1.7.7 (2026-07-17) — session-probe None-shape fix + mcp 1.28.1

Family fix pack — no new tools, no schema changes.

### Fixed
- **A dead cached session could be returned as live** (family fix, external
  fork report VMware-AIops PR #32). An expired token can make
  `sessionManager.currentSession` return `None` without raising, and the
  raise-only liveness probe treated that dead session as alive. The probe now
  checks `currentSession is not None`; the exception path (already correct
  here) is unified on the family-standard bare `except Exception`. Three
  regression tests pin the probe shapes (raise → evict + reconnect, None →
  evict + reconnect, live → cache reuse).

### Security
- Lockfile bumps `mcp` to **1.28.1**, clearing three GHSA HIGH advisories
  against the MCP Python SDK (WebSocket Host/Origin validation, HTTP
  transport principal verification, experimental task-handler cross-client
  access). stdio-only servers are not directly exposed, and installs resolve
  `mcp` fresh from PyPI — this mainly matters for from-source checkouts.

## v1.7.5 (2026-07-13) — family version alignment (no code change)

Version-alignment release only; no functional change since v1.7.4.

## v1.7.4 (2026-07-13) — family version alignment

## v1.7.3 (2026-07-03) — family version alignment

## v1.7.2 (2026-07-02) — datastore/host inventory scale (issue #31 port)

### Fixed
- **Datastore & host inventory at scale.** `list_datastores` (the most-called
  read path, backing the datastore browser and iSCSI/vSAN hints), `list_hosts`,
  and the `find_*_by_name` helpers read lazy `.summary` / `.name` / `len(ds.vm)`
  per object — a separate SOAP round-trip each, so large estates (thousands of
  datastores/hosts) timed out. Ported the `PropertyCollector.RetrievePropertiesEx`
  batching from the AIops issue-#31 fix. Output shape unchanged (`vm_count` stays
  opt-in). This also removes the timeout that `vmware-harden` scans inherited
  through `list_datastores`.

## v1.7.1 (2026-07-02) — family version alignment

No code changes. Version bump to stay aligned with the v1.7.1 family release
(VMware-AIops + VMware-Monitor large-inventory scale fix — PropertyCollector
batching to stop per-object lazy SOAP round-trips, GitHub issue #31).

## v1.7.0 (2026-06-27) — guided onboarding + teaching auth errors

### Added
- **`vmware-storage init` — interactive first-run setup wizard.** Prompts for host /
  username / password and writes `config.yaml` + `.env` for you. The password is
  stored grep-safe (`b64:`, never plaintext on disk) and `.env` is locked to
  0600, then the connection is verified. Replaces the manual "mkdir + cp
  config.example.yaml + edit YAML + chmod 600" dance.

### Changed
- `doctor` now points to `vmware-storage init` when config/credentials are missing
  (previously suggested a command that did not exist), keeping the manual steps
  as a fallback.
- Authentication and TLS failures now print a teaching message naming the exact
  file and env var to fix (`~/.vmware-storage/.env` password var, `config.yaml`
  username) plus a `verify_ssl: false` hint for self-signed labs.

## v1.6.1 (2026-06-24)

### Added
- **`.env` passwords are auto-obfuscated to a grep-safe `b64:` form** on first
  load and decoded transparently at runtime — plaintext no longer sits in
  `~/.<skill>/.env` for a casual `grep` to find. Values are read/written through
  python-dotenv's own parser, so the stored secret never drifts from the
  configured one (handles quotes, inline comments, trailing whitespace, and a
  password that literally starts with `b64:`). **Obfuscation, not encryption** —
  for real at-rest secrecy, inject the password from a secret manager instead of
  storing `.env`. New regression suite (10 cases) covers dotenv parity, the
  `b64:`-prefixed edge case, idempotency, and 0600 preservation.

## v1.6.0 (2026-06-22) — trust architecture: undo tokens

### Added
- **Undo-token recording** (vmware-policy 1.6.0): `storage_iscsi_add_target`↔`storage_iscsi_remove_target`.
- Inherits harness budget guard, audit accountability fields, and graduated risk tiers.

### Changed
- Requires **vmware-policy >= 1.6.0**.

## v1.5.39 (2026-06-22) — datastore browse: honest timeout

### Fixed
- **Datastore browse timeout is honest and actionable.** `browse_datastore` waited 120s then raised a
  bare `TimeoutError` on a large/busy datastore (and `scan_images` browses once per image pattern, so the
  budget compounded). Bumped to 300s, and the timeout message now tells the caller to narrow the search
  (sub-path + specific pattern) instead of retrying the same broad browse — the same agent token-burn
  class fixed in AIops snapshot-delete this release. Family sweep of the sync-wait-raises-on-timeout
  pattern (踩坑 #21): VKS/NSX/NSX-Security/Aria/AVI were already safe (fire-and-forget or graceful poll).

## v1.5.38 (2026-06-12) — release alignment

No functional changes — version bumped to keep the VMware skill family aligned at v1.5.38.

## v1.5.37 (2026-06-12) — backlog: faster datastore listing, iSCSI race, ops tests

### Fixed
- `list_datastores` no longer does a per-datastore `ds.vm` round-trip on the default (busiest) path;
  `vm_count` is now opt-in. (#8)
- `enable_software_iscsi` polls for the HBA to materialize before returning success, fixing a race where
  an immediate `add_target` hit "Software iSCSI is not enabled". (#10)

### Added
- Behavioral (mocked-pyVmomi) unit tests for `iscsi_config`, `vsan`, and `datastore_browser`. (#9)

## v1.5.36 (2026-06-12) — OVA scanning fix + destructive-op gating

### Fixed
- **OVA/OVF discovery returned nothing** — the datastore-browser query list excluded generic files;
  `scan-images` and `browse` now surface `.ova`/`.ovf` (added the generic FileQuery).
- **iSCSI remove-target now hits the confirmation gate via MCP** — it was `risk_level=medium`, below
  the policy gate; raised to `high`, and the four MCP write tools gained `dry_run` preview.
- **Audit-write failure no longer flips a successful write to a reported failure** (degrades to stderr).
- `get_vsan_capacity` returns an explicit message instead of healthy-looking zeros when the vSAN
  datastore isn't found; not-found errors include name suggestions; CLI teaching-error decorator.

### Changed
- SKILL.md tool split corrected to **7 read / 4 write** (was mislabeled 6R/5W).

## v1.5.35 (2026-06-10) — security hardening: safe errors, datastore path guard

### Fixed
- **MCP tools route errors through `_safe_error()`** (no raw exception text to the agent).
- **Datastore browse path** rejects `..`, absolute paths, and null bytes.
- **Image registry** written 0600; **audit** dir 0700 / log 0600.

This release aligns the whole family back to a single version (1.5.35); vmware-policy and vmware-pilot return to the shared number after sitting at 1.5.22.

## v1.5.32 (2026-06-08) — Audit-clean confirmation + hardened test suite

The 2026-06-08 family-wide pyVmomi introspection audit verified every iSCSI
and vSAN property chain and method in this codebase against SDK metadata —
no findings (the only skill in the family to pass clean).

### Tests
- vim-attribute conformance regression added (prevents future invented
  pyVmomi names — the failure mode found in sibling skills).
- Safety test repointed at the CLI commands that own the double-confirm
  guards (was asserting the wrong layer and failing permanently).

## v1.5.30 (2026-06-07) — Tool description quality (Glama TDQS)

### Improved
- Rewrote MCP tool descriptions flagged by Glama's Tool Description Quality Score review:
  per-parameter semantics (format, defaults, valid values), return-field documentation,
  sibling-tool routing guidance, and behavioral transparency (side effects, audit logging,
  async semantics). Corrected descriptions that overstated or misstated actual behavior.
- No functional changes; descriptions only.

## v1.5.29 (2026-05-29) — Pattern Library PoC Documentation

### Documentation
- README.md / README-CN.md: new "Auto-Remediation Patterns (PoC)" section pointing at `patterns/iscsi-target-stale-rescan.yaml` (framed as schema reference design, runtime not yet wired).
- SKILL.md: safety bullet referencing `patterns/` library with `risk:low + reversible:true + repeatable:true` classification.
- capabilities.md: L5 row links the PoC YAML with scan target, action, and risk classification; new "Runtime Requirements" table with Python 3.10+ minimum (v1.5.27).

### No code changes
Documentation-only release.

## v1.5.28 (2026-05-20)

**Fix `subclass() arg 1 must be a class` in goose/old mcp environments** —
v1.5.25–1.5.27 replaced `X | None` with `Optional[X]` but kept
`from __future__ import annotations` at the top of `mcp_server/server.py`.
Under mcp 1.10–1.13 (which Goose and some sandboxes pin), `Tool.from_function`
calls `issubclass(param.annotation, Context)` without resolving forward refs,
so string annotations crash the entire server load. Removed
`from __future__ import annotations` from `mcp_server/server.py` so annotations
are real classes; verified all tools load under mcp 1.10 and 1.14.

Traceback location: `mcp/server/fastmcp/tools/base.py:67`. CLAUDE.md 踩坑 #33
updated. family_smoke.sh Check 4b now installs `mcp==1.10.0` to catch this
regression class.

## v1.5.27 (2026-05-20)

**Loosen Python requirement: now supports Python >= 3.10** — v1.5.25/26 fixed
the PEP 604 root cause in MCP tool signatures (Optional[X] instead of X | None),
but kept `requires-python = ">=3.11"` and a 3.11 hard guard in `mcp_cmd`. Both
relaxed to 3.10 so users on Python 3.10 (e.g. Goose default sandbox, Ubuntu
22.04 system python) can install and run directly without a Python upgrade.

- `pyproject.toml`: `requires-python = ">=3.10"` (was `>=3.11`; VMware-VKS
  was `>=3.12`, now also `>=3.10` for family alignment).
- `<pkg>/cli.py` `mcp_cmd()`: version guard now triggers on `< (3, 10)`.
- Behavior on Python 3.10 matches 3.11/3.12 — the Optional[X] fix from v1.5.25
  is what actually enables this; this release just stops blocking installs.

---

## v1.5.26

**Family-wide MCP server fix — Python 3.10 compatibility (踩坑 #33)** — `vmware-storage mcp`
crashed at decorator time on Python 3.10 with `subclass() arg 1 must be a class`.
Root cause: `mcp_server/server.py` used PEP 604 `X | None` in tool signatures
plus `from __future__ import annotations`; on Python 3.10 + older mcp/pydantic
combos, `typing.get_type_hints()` evaluates `"str | None"` to a
`types.UnionType` instance, which FastMCP/Pydantic then feeds to `issubclass()`.
Reported by a goose user (qwen3.6:27, Python 3.10).

- `mcp_server/server.py`: all `X | None` → `Optional[X]`; ops layer untouched.
- `<pkg>/cli.py` `mcp_cmd()`: hard guard — exits with installation fix command
  if Python < 3.11 (defense in depth, our actual lower bound).
- `pyproject.toml`: `mcp[cli]>=1.10,<2.0` (was `>=1.0`) so uv doesn't pick
  an ancient version that has the same issubclass bug.

**Tooling — family smoke gains MCP schema-build check** — `scripts/family_smoke.sh`
new Check 4b runs `asyncio.run(mcp.list_tools())` per skill, forcing FastMCP to
build Pydantic models for every declared tool. Supports both module-level `mcp`
and `build_server()` factory patterns.

**Docs — CLAUDE.md gains 踩坑 #33 (PEP 604 / Python 3.10) and #34 (CLI/MCP exposure parity).**

---

## v1.5.24 (2026-05-19)

**Family version alignment** — no code changes in this skill. Bumped together
with VMware-AIops and VMware-VKS, which received a pyVmomi 8.x `ManagedObject`
setattr fix (踩坑 #32). `family_smoke.sh` now enforces the no-setattr rule
across all 9 skills.

## v1.5.23 (2026-05-19)

**VCF 9.0 / 9.1 compatibility declared** — family-wide docs sync.

- **docs:** README + `references/setup-guide.md` version-compatibility tables now list vSphere 9.0 / 9.1 as ✅ Full. vSAN Management SDK (bundled in pyVmomi 8.0.3+) continues to work against vSphere 9. Note: vSAN ESA full feature coverage in VCF 9 may require future SDK updates.
- **docs:** Added `Official Broadcom References` pointer to [VCF Python SDK](https://developer.broadcom.com/sdks) and [vSAN Management API docs](https://developer.broadcom.com/xapis).
- **align:** Family v1.5.23 — all 9 skills tracking VCF 9.0 / 9.1 compatibility declaration.

## v1.5.22 (2026-05-08)

**Family alignment** — no source changes in this skill.

- **align:** Tracks v1.5.22 family bump driven by Smithery onboarding for vmware-avi / vmware-harden / vmware-pilot.

## v1.5.21 (2026-05-08)

**Family alignment** — no source changes in this skill.

- **deps:** Bumped `python-multipart` 0.0.26 → 0.0.27 (transitive, fixes GHSA HIGH DoS via unbounded multipart headers).
- **align:** Tracks v1.5.21 family bump driven by vmware-monitor folder_path feature (community PR #11).

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