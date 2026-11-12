<!-- mcp-name: io.github.vmware-skills/vmware-storage -->

# VMware-Storage

<a id="readme-overview"></a>

先看清数据存储容量与路径，再决定变更。

[English](README.md) · 简体中文

社区维护 · MIT 开源 · 非 VMware/Broadcom 官方产品。

[![18 秒调查示意演示](https://raw.githubusercontent.com/vmware-skills/marketplace/main/assets/demos/vmware-skills-zh.gif)](https://github.com/vmware-skills/marketplace/blob/main/assets/demos/vmware-skills-zh.mp4)

使用合成数据的示意动画，非实时录屏。[观看家族 AI 配音介绍（约 2 分钟）](https://github.com/vmware-skills/marketplace/blob/main/assets/demos/vmware-skills-zh.mp4)

<a id="readme-capabilities-and-workflows"></a>

## 能解决什么问题？

- 调查数据存储、文件与部署镜像。
- 读取容量、健康与 FC/设备路径证据。
- 明确管理受支持的 iSCSI 配置。

<a id="readme-quick-start"></a>

## 快速开始

安装已发布 CLI，并查看可用命令：

```sh
uv tool install vmware-storage==2.6.2
vmware-storage --help
```

需要 uv 与 Python >=3.10。远程查询前，在本地配置目标与凭据。[配置指南](skills/vmware-storage/references/setup-guide.md)。

Claude Code 及其他宿主安装方式见[家族门户](https://github.com/vmware-skills/marketplace/blob/main/README-CN.md)。

<a id="readme-safety-and-audit"></a>

## 使用边界

包含 iSCSI 配置写操作。清单与路径证据不能证明未测量的历史趋势或所有阵列兼容性。

<a id="readme-mcp-configuration"></a> <a id="readme-troubleshooting"></a> <a id="readme-reference"></a>

## 详细文档

- [能力范围](skills/vmware-storage/references/capabilities.md)
- [CLI 用法](skills/vmware-storage/references/cli-reference.md)
- [Agent 使用约束](skills/vmware-storage/references/agent-guardrails.md)
- [详细用法与架构](skills/vmware-storage/references/detailed-guide-CN.md)
- [MCP 启动配置](.mcp.json)
- [唯一技能指令正本](skills/vmware-storage/SKILL.md)

<a id="readme-companion-skills"></a>

属于 [VMware Skills 家族](https://github.com/vmware-skills/marketplace/blob/main/README-CN.md)：15 个 skill、14 个 MCP server 与一个共享 Policy 库。如果对你有用，欢迎 Star 家族门户。

<a id="readme-license"></a>

[问题与反馈](https://github.com/vmware-skills/VMware-Storage/issues) · [MIT](LICENSE)
