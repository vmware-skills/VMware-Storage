# Security Policy

## Disclaimer

This is a community-maintained open-source project and is **not affiliated with, endorsed by, or sponsored by VMware, Inc. or Broadcom Inc.** "VMware" and "vSphere" are trademarks of Broadcom Inc.

**Author**: Wei Zhou, VMware by Broadcom — wei-wz.zhou@broadcom.com

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it privately:

- **Email**: wei-wz.zhou@broadcom.com
- **GitHub**: Open a [private security advisory](https://github.com/vmware-skills/VMware-Storage/security/advisories/new)

Do **not** open a public GitHub issue for security vulnerabilities.

## Security Design

### Credential Management

- Passwords are stored exclusively in `~/.vmware-storage/.env` (never in `config.yaml`, never in code)
- `.env` file permissions are verified at startup (`chmod 600` required)
- No credentials are logged, echoed, or included in audit entries
- Each vCenter/ESXi target uses a separate environment variable: `VMWARE_<TARGET_NAME_UPPER>_PASSWORD`

### Scope Limitation

This skill manages **storage resources only** (datastores, iSCSI adapters, vSAN). It **cannot** power on/off, create, delete, or modify virtual machines. No VM lifecycle API calls exist in the codebase.

### Destructive Operation Safeguards

All write operations pass through multiple safety layers:

1. **`@vmware_tool` decorator** — mandatory on every MCP tool; provides pre-checks, audit logging, data sanitization, and timeout control
2. **Double confirmation** — CLI destructive commands (iSCSI enable, add-target, remove-target) require two separate "Are you sure?" prompts
3. **`--dry-run` mode** — all CLI write commands support preview without execution
4. **Audit logging** — every operation (read and write) is logged to `~/.vmware/audit.db` (SQLite WAL) with timestamp, user, target, operation, parameters, and result
5. **Policy engine** — `~/.vmware/rules.yaml` can deny operations by pattern, enforce maintenance windows, and set risk-level thresholds

### Network Scope

- No webhooks, no outbound network calls beyond the vSphere API
- Communication is limited to: stdio MCP transport and direct vSphere API connections
- Datastore file names returned from the API are sanitized via `_sanitize()` before being passed to LLM agents

### SSL/TLS Verification

- TLS certificate verification is **enabled by default**
- `disableSslCertValidation: true` exists solely for ESXi hosts using self-signed certificates in isolated lab/home environments
- In production, always use CA-signed certificates with full TLS verification

### Transitive Dependencies

- `vmware-policy` is the only transitive dependency auto-installed; it provides the `@vmware_tool` decorator and audit logging
- All other dependencies are standard Python packages (pyVmomi, Click, Rich, python-dotenv)
- No post-install scripts or background services are started during installation

### Prompt Injection Protection

- All vSphere-sourced content (datastore names, file paths, iSCSI target names) is processed through `_sanitize()`
- Sanitization truncates to 500 characters and strips C0/C1 control characters
- Output is wrapped in boundary markers when consumed by LLM agents

## Static Analysis

This project is scanned with [Bandit](https://bandit.readthedocs.io/) before every release, targeting 0 Medium+ issues:

```bash
uvx bandit -r vmware_storage/
```

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.5.x   | Yes       |
| < 1.5   | No        |
