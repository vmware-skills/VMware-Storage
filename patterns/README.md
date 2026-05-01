# VMware Storage — Auto-Remediation Patterns

> **Status**: PoC reference patterns. Not yet enforced by the runtime.

This directory holds candidate L5 auto-remediation patterns specific to vmware-storage operations. The pattern engine, schema, and lifecycle live in [vmware-policy/docs/auto-remediation-patterns.md](https://github.com/zw008/VMware-Policy/blob/main/docs/auto-remediation-patterns.md).

## What is here

| File | Purpose |
|---|---|
| `iscsi-target-stale-rescan.yaml` | First reference pattern. iSCSI HBA rescan in response to stale device status. PoC unsigned. |

## Why iSCSI rescan first

Per the PoC design doc, the first L5 candidate must satisfy three hard conditions:

- **Risk: low** — rescan is a read-then-refresh operation, no destructive change to storage config or VM state
- **Reversible: true** — the operation is idempotent (re-running has no additional effect)
- **Repeatable: true** — historically the most-repeated manual storage fix in our reference deployments

This pattern is intentionally NOT yet active. To activate, an operator must:

1. Verify the audit history meets thresholds (`vmware-policy/scripts/extract_patterns.py`)
2. Author the trigger predicate precisely
3. Sign the pattern (fill `approval` block)
4. Place the signed YAML in `~/.vmware/auto-remediation-patterns/`

The runtime matcher integration is tracked as a separate work item.
