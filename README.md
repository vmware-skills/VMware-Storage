<!-- mcp-name: io.github.vmware-skills/vmware-storage -->

# VMware-Storage

Understand datastore capacity and storage paths before making changes.

English · [简体中文](README-CN.md)

Community-maintained · MIT · Not an official VMware/Broadcom product.

[![18-second illustrative investigation demo](https://raw.githubusercontent.com/vmware-skills/marketplace/main/assets/demos/vmware-skills-en.gif)](https://github.com/vmware-skills/marketplace/blob/main/assets/demos/vmware-skills-en.mp4)

Synthetic-data animation, not a live recording. [Watch the narrated family introduction (~2 min)](https://github.com/vmware-skills/marketplace/blob/main/assets/demos/vmware-skills-en.mp4)

## Why use it?

- Inspect datastores, files and deployment images.
- Read capacity, health and FC/device-path evidence.
- Manage supported iSCSI configuration explicitly.

## Get started

Install the published CLI and inspect its commands:

```sh
uv tool install vmware-storage==2.6.2
vmware-storage --help
```

Requires uv and Python >=3.10. Configure targets and credentials locally before remote queries. [Setup guide](skills/vmware-storage/references/setup-guide.md).

For Claude Code and other host installation paths, see the [family portal](https://github.com/vmware-skills/marketplace).

## Know the boundaries

Includes iSCSI configuration writes. Inventory and path evidence do not establish unmeasured historical trends or universal array compatibility.

## Go deeper

- [Capabilities](skills/vmware-storage/references/capabilities.md)
- [CLI reference](skills/vmware-storage/references/cli-reference.md)
- [Agent guardrails](skills/vmware-storage/references/agent-guardrails.md)
- [Detailed usage and architecture](skills/vmware-storage/references/detailed-guide.md)
- [Canonical skill instructions](skills/vmware-storage/SKILL.md)

Part of [VMware Skills](https://github.com/vmware-skills/marketplace): 15 skills, 14 MCP servers and one shared Policy library. If it helps your work, star the family portal.

[Issues and feedback](https://github.com/vmware-skills/VMware-Storage/issues) · [MIT](LICENSE)
