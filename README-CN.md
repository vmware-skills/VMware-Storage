<!-- mcp-name: io.github.vmware-skills/vmware-storage -->
# VMware Storage

> **作者**: Wei Zhou, VMware by Broadcom — wei-wz.zhou@broadcom.com
> 本项目由 VMware 工程师维护的社区项目，非 VMware 官方产品。
> VMware 官方开发者工具请访问 [developer.broadcom.com](https://developer.broadcom.com)。

[English](README.md) | [中文](README-CN.md)

VMware vSphere 存储管理：数据存储、iSCSI、vSAN，以及只读的光纤通道（FC）/ 多路径诊断 — 14 个 MCP 工具，领域专注、轻量级。

> 从 vmware-aiops 拆分，更轻量的上下文，兼容本地小模型。

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## 伴生 Skills

| Skill | 范围 | 工具数 | 安装 |
|-------|------|:-----:|------|
| **[vmware-aiops](https://github.com/vmware-skills/VMware-AIops)** ⭐ 统一入口 | VM 生命周期、部署、Guest Ops、集群 | 49 | `uv tool install vmware-aiops` |
| **[vmware-monitor](https://github.com/vmware-skills/VMware-Monitor)**（只读） | 只读监控、告警、事件、VM 信息 | 27 | `uv tool install vmware-monitor` |
| **[vmware-vks](https://github.com/vmware-skills/VMware-VKS)** | Tanzu 命名空间、TKC 集群生命周期 | 20 | `uv tool install vmware-vks` |
| **[vmware-nsx](https://github.com/vmware-skills/VMware-NSX)** | NSX 网络：段、网关、NAT、IPAM | 33 | `uv tool install vmware-nsx-mgmt` |
| **[vmware-nsx-security](https://github.com/vmware-skills/VMware-NSX-Security)** | DFW 微分段、安全组、Traceflow | 21 | `uv tool install vmware-nsx-security` |
| **[vmware-aria](https://github.com/vmware-skills/VMware-Aria)** | Aria Ops 指标、告警、容量规划 | 28 | `uv tool install vmware-aria` |

## 快速安装

```bash
# 通过 PyPI
uv tool install vmware-storage

# 或 pip
pip install vmware-storage
```

## 离线 / 气隙环境安装（从源码）

本项目采用现代 PEP 517 构建系统（hatchling），因此**故意不提供 `setup.py`**
——这是预期行为，不是缺失文件。如果你克隆源码后遇到
`ERROR: File "setup.py" or "setup.cfg" not found ... editable mode
currently requires a setuptools-based build`，说明你的 `pip` 版本低于 21.3，
无法对非 setuptools 后端做*可编辑*（`-e`）安装。可编辑模式只是开发便利，
运行工具并不需要它——任选其一：

```bash
# 在源码树中——普通（非可编辑）安装会构建 wheel：
pip install .              # 不是  pip install -e .

# ……或先升级 pip，可编辑安装也能用：
pip install --upgrade pip && pip install -e .
```

若目标是**完全气隙的主机**，在联网机器上构建 wheel 再拷贝过去
——目标主机全程无需联网：

```bash
# 在联网机器上，把本包及其依赖收集为 wheel：
pip wheel . -w dist        # → dist/*.whl   （或：uv build，仅构建本包）

# 把 dist/ 拷到气隙主机，然后离线安装：
pip install --no-index --find-links dist vmware-storage
```

## 配置

```bash
mkdir -p ~/.vmware-storage
cp config.example.yaml ~/.vmware-storage/config.yaml
# 编辑 config.yaml，填入 vCenter/ESXi 地址和用户名

echo "VMWARE_MY_VCENTER_PASSWORD=your_password" > ~/.vmware-storage/.env
chmod 600 ~/.vmware-storage/.env

# 验证环境
vmware-storage doctor
```

### config.yaml 示例

```yaml
default_target: vcenter1
targets:
  vcenter1:
    host: 192.168.1.10       # vCenter IP
    user: administrator@vsphere.local
    password_env: VMWARE_VCENTER1_PASSWORD
  esxi-prod:
    host: 192.168.1.20       # 直连 ESXi
    user: root
    password_env: VMWARE_ESXIPROD_PASSWORD
```

## MCP 工具（14 个）

| 类别 | 工具 | 类型 |
|------|------|------|
| 数据存储 | `list_all_datastores`、`browse_datastore`、`scan_datastore_images`、`list_cached_images` | 只读 |
| iSCSI | `storage_iscsi_enable`、`storage_iscsi_status`、`storage_iscsi_add_target`、`storage_iscsi_remove_target`、`storage_rescan` | 读/写 |
| vSAN | `vsan_health`、`vsan_capacity`、`vsan_efficiency` | 只读 |
| FC / 多路径 | `fc_adapter_list`、`storage_device_paths` | 只读 |

### 工具说明

**数据存储**
- `list_all_datastores` — 列出所有数据存储，含容量、使用率、可达性
- `browse_datastore` — 浏览数据存储目录下的文件（支持 glob 过滤）
- `scan_datastore_images` — 扫描可部署镜像（OVA、ISO、OVF、VMDK）
- `list_cached_images` — 查询本地镜像注册表（支持按类型/数据存储过滤）

**iSCSI**
- `storage_iscsi_enable` — 在 ESXi 主机上启用软件 iSCSI 适配器
- `storage_iscsi_status` — 查看 iSCSI 适配器状态和已配置的发送目标
- `storage_iscsi_add_target` — 添加 iSCSI 发送目标并自动重扫
- `storage_iscsi_remove_target` — 移除 iSCSI 发送目标并自动重扫
- `storage_rescan` — 强制重扫所有 HBA 和 VMFS 卷

**vSAN**
- `vsan_health` — 获取 vSAN 集群健康摘要和磁盘组详情
- `vsan_capacity` — 获取 vSAN 容量概览（总量/已用/空闲）
- `vsan_efficiency` — 获取集群的 vSAN 数据效率（去重 + 压缩）状态

**FC / 多路径（只读）**
- `fc_adapter_list` — 按主机列出 FC / FCoE HBA：vmhba、型号、驱动、状态、端口类型、WWPN/WWNN、上报的速率
- `storage_device_paths` — 在一个集群 / 主机 / 数据存储范围内，按设备（NAA）查看多路径状态：哪些主机看得到、各状态路径数、工作路径、适配器、PSP/SATP、所在 VMFS 数据存储

## 自动修复模式（PoC）

[`patterns/`](patterns/) 目录存放企业级 Harness Engineering 框架下的 L5 自动修复**候选**模式。首个 PoC 模式 [`patterns/iscsi-target-stale-rescan.yaml`](patterns/iscsi-target-stale-rescan.yaml) 描述了 iSCSI HBA 重扫这一低风险、可逆、可重复的操作。当前仅定义了模式 schema，**运行时尚未集成**，因此是参考设计，并非生产可用的自动修复。

## 常见工作流

### 在主机上配置 iSCSI 存储

1. 启用 iSCSI 适配器：`vmware-storage iscsi enable esxi-01`
2. 添加目标：`vmware-storage iscsi add-target esxi-01 10.0.0.100`
3. 验证：`vmware-storage iscsi status esxi-01`

`add-target` 命令会自动重扫存储。任何写操作前可加 `--dry-run` 预览。

### 查找可部署镜像

1. 列出所有数据存储：`vmware-storage datastore list`
2. 扫描镜像：`vmware-storage datastore scan-images datastore01`
3. 按模式浏览：`vmware-storage datastore browse datastore01 --pattern "*.iso"`

### vSAN 健康评估

1. 检查健康：`vmware-storage vsan health Cluster-Prod`
2. 检查容量：`vmware-storage vsan capacity Cluster-Prod`
3. 若发现问题，用 `vmware-monitor` 查看告警和事件

### 检查光纤通道路径

1. 某个数据存储背后的 dead / disabled 路径：`vmware-storage paths devices --datastore ds-fc-01`
2. 看某个共享设备的路径数与同伴主机不同、或根本看不到它的主机：`vmware-storage paths devices --cluster Cluster-Prod --only-differences`
3. 给 SAN 团队的 WWPN：`vmware-storage paths fc-adapters --cluster Cluster-Prod`

读不到的主机列在 `hosts_not_read` 中，绝不会被报成"缺设备"。路径状态按 vSphere 上报的原样给出——`standby` 不会被标为问题。

## CLI

```bash
# 数据存储
vmware-storage datastore list
vmware-storage datastore browse datastore01
vmware-storage datastore scan-images datastore01

# iSCSI（破坏性操作有双重确认 + --dry-run 预览）
vmware-storage iscsi status esxi-01
vmware-storage iscsi enable esxi-01 --dry-run
vmware-storage iscsi enable esxi-01
vmware-storage iscsi add-target esxi-01 192.168.1.100 --dry-run
vmware-storage iscsi add-target esxi-01 192.168.1.100
vmware-storage iscsi remove-target esxi-01 192.168.1.100
vmware-storage iscsi rescan esxi-01

# vSAN
vmware-storage vsan health Cluster-Prod
vmware-storage vsan capacity Cluster-Prod

# 光纤通道 / 多路径（只读）
vmware-storage paths fc-adapters --cluster Cluster-Prod
vmware-storage paths devices --datastore ds-fc-01

# 环境诊断
vmware-storage doctor
```

## MCP Server

**v1.5.15+ 推荐方式**：完成 `uv tool install vmware-storage` 后，**一条命令启动 MCP**：

```bash
# 推荐 — 单命令，无网络依赖
vmware-storage mcp

# 指定配置路径
VMWARE_STORAGE_CONFIG=/path/to/config.yaml vmware-storage mcp

# 或通过 Docker
docker compose up -d
```

### Agent 配置

将以下内容添加到 AI Agent 的 MCP 配置文件：

```json
{
  "mcpServers": {
    "vmware-storage": {
      "command": "vmware-storage",
      "args": ["mcp"],
      "env": {
        "VMWARE_STORAGE_CONFIG": "~/.vmware-storage/config.yaml"
      }
    }
  }
}
```

<details>
<summary>备选方案：uvx（不安装）或 legacy 入口</summary>

```bash
# 不想安装，临时运行（每次需要联网 resolve 依赖）
uvx --from vmware-storage vmware-storage mcp

# 旧 entry point（仍可用，向后兼容）
vmware-storage-mcp
```

> **公司 TLS 代理网络下？** uvx 可能报 `invalid peer certificate: UnknownIssuer`。
> 推荐使用上面的 `vmware-storage mcp`（无需联网），或 `export UV_NATIVE_TLS=true`。

</details>

更多 Agent 配置模板（Claude Code、Cursor、Goose、Continue 等）见 [examples/mcp-configs/](examples/mcp-configs/)。

## 为什么独立成一个 Skill？

`vmware-aiops` 有 60 个 MCP 工具——对本地小模型（7B-14B）来说上下文占用太重。独立拆分后：

- **14 个工具** — 完全适合小模型上下文窗口
- **领域专注** — 存储管理员只看到需要的工具
- **最小权限** — 可以配置只有存储只读权限的 vCenter 服务账号
- **可组合** — 可与 vmware-monitor 或 vmware-aiops 同时运行

## 版本兼容性

**Python**: 3.10+ （自 v1.5.27 起；此前要求 3.11+）。已在 3.10 / 3.11 / 3.12 测试通过。

| vSphere | 支持 | 说明 |
|---------|------|------|
| 8.0 | 完整 | vSAN SDK 内置于 pyVmomi 8.0.3+ |
| 7.0 | 完整 | 所有存储 API 均可用 |
| 6.7 | 兼容 | iSCSI + 数据存储功能正常；vSAN 功能有限 |

## 安全

| 功能 | 说明 |
|------|------|
| 只读为主 | 14 个工具中 10 个只读 |
| 默认只预览（MCP） | 4 个写工具接受 `confirm`（默认 `false`）：不带它的调用不做任何变更，只返回 `blast_radius`（主机、适配器；移除目标时还有该目标背后的路径、设备和数据存储）。某个数据存储会失去全部路径、或影响范围有任何部分读不到时，`confirm=true` 会被拒绝。`dry_run` 为已弃用的别名 |
| 输入验证 | iSCSI 操作前验证 IP 地址和端口 |
| 审计日志 | 所有操作记录到 `~/.vmware-storage/audit.log`（JSON Lines） |
| 双重确认 | CLI iSCSI 写操作需两次确认 |
| --dry-run | CLI iSCSI 写操作支持预览模式 |
| 无 VM 操作 | 无法创建、删除或修改 VM |
| 凭据安全 | 密码只从环境变量读取，不存于配置文件 |
| Prompt 注入防护 | 来自 vSphere 的文件名和路径经过控制字符清理 |
| TLS 说明 | 默认对 ESXi 自签名证书禁用 TLS 验证；生产环境建议启用 |

## 常见问题排查

| 问题 | 原因与解决 |
|------|-----------|
| iSCSI enable 报 "already enabled" | 不是错误 — 适配器已启用。运行 `iscsi status` 查看已配置的目标。 |
| 浏览时报 "Datastore not found" | 数据存储名称**区分大小写**。运行 `datastore list` 获取准确名称。 |
| `vsan_health` 返回 `overall_health: null` | `null` 表示**没有查询**（原因见 `health_not_queried_reason`），不是测量结果；只要是字符串就一定是 vSAN 自己给的答案，包括它自己的 `"unknown"`。常见原因：连的是独立 ESXi，而健康服务跑在 **vCenter** 上。 |
| 重扫后未发现新 LUN | 添加目标后等待 15-30 秒再重扫。确认 ESXi 能访问目标 IP。 |
| "Password not found" 错误 | 变量名规则：`VMWARE_<目标名大写>_PASSWORD`（连字符→下划线）。检查 `~/.vmware-storage/.env`。 |
| 连接 vCenter 超时 | 使用 `vmware-storage doctor --skip-auth` 跳过高延迟网络的认证检查。 |

## 许可证

[MIT](LICENSE)
