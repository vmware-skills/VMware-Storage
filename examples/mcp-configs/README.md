# MCP Configuration Templates

Copy the relevant config snippet into your AI agent's MCP configuration file.

## Prerequisites

```bash
# Install vmware-storage
uv tool install vmware-storage
# or: pip install vmware-storage

# Configure credentials
mkdir -p ~/.vmware-storage
cp config.example.yaml ~/.vmware-storage/config.yaml
# Edit config.yaml with your vCenter/ESXi host and username

echo "VMWARE_MY_VCENTER_PASSWORD=your_password" > ~/.vmware-storage/.env
chmod 600 ~/.vmware-storage/.env

# Verify setup
vmware-storage doctor
```

## Agent Configuration Files

| Agent | Config File | Template |
|-------|------------|----------|
| Claude Code | `~/.claude/settings.json` | [claude-code.json](claude-code.json) |
| Cursor | Cursor MCP settings | [cursor.json](cursor.json) |
| Goose | `goose configure` or UI | [goose.json](goose.json) |
| Continue | `~/.continue/config.yaml` | [continue.yaml](continue.yaml) |
| LocalCowork | MCP config panel | [localcowork.json](localcowork.json) |
| mcp-agent | `mcp_agent.config.yaml` | [mcp-agent.yaml](mcp-agent.yaml) |
| VS Code Copilot | `.vscode/mcp.json` | [vscode-copilot.json](vscode-copilot.json) |

## Using with Local Models (Ollama / LM Studio)

vmware-storage has only 11 tools — ideal for 7B-14B local models with limited context windows.

```bash
# Example: Continue + Ollama + vmware-storage MCP server
# 1. Configure Continue with your Ollama model
# 2. Add vmware-storage MCP config from continue.yaml
# 3. Ask naturally: "list all datastores" or "show iSCSI status on esxi-01"
```

## Combining with Other VMware Skills

vmware-storage can run alongside other VMware MCP skills simultaneously:

```json
{
  "mcpServers": {
    "vmware-monitor": {
      "command": "vmware-monitor",
      "args": ["mcp"],
      "env": { "VMWARE_MONITOR_CONFIG": "~/.vmware-monitor/config.yaml" }
    },
    "vmware-storage": {
      "command": "vmware-storage",
      "args": ["mcp"],
      "env": { "VMWARE_STORAGE_CONFIG": "~/.vmware-storage/config.yaml" }
    }
  }
}
```
