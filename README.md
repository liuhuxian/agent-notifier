# Agent Notifier

Agent Notifier 让终端中的原生 Coding Agent 与 cc-connect/飞书共享同一会话，
并提供可靠的任务完成通知和远程权限审批。

首版只实现 Codex adapter，但核心会话、通知和审批模块不依赖 Codex，后续可
增加 OpenCode adapter。

## 当前兼容版本

| 组件 | 状态 | 已验证版本 |
|---|---|---|
| Codex CLI | 支持 | **0.145.0** |
| cc-connect | 支持 | **1.3.2**，commit `19406df9` |
| Python | 支持 | 3.10+ |
| Linux systemd user service | 支持 | 当前主路径 |
| OpenCode | 尚未实现 | 计划作为后续 adapter |

Agent Notifier 依赖 Codex App Server 和 cc-connect ACP 协议。这两个协议仍在
演进，因此默认拒绝未经验证的 Codex/cc-connect 版本，而不是带着未知兼容性继续
运行。升级任一组件后，先运行：

```bash
agent-notifier doctor
```

如果 `cc-connect` 不在 `PATH` 或安装在非标准位置，可设置
`AGENT_NOTIFIER_CC_CONNECT=/absolute/path/to/cc-connect`。

## 工作方式

```text
原生 Codex TUI                         飞书
      |                                  |
      | App Server 协议                  | cc-connect
      v                                  v
+----------------------+           +--------------+
| agent-notifier proxy |<--------->| ACP adapter  |
| 会话与来源追踪       |           +--------------+
| 权限先响应者生效     |
+----------+-----------+
           |
           v
  共享 Codex App Server
```

- 普通 `codex` 命令完全不受影响。
- `agent-notifier codex` 打开完整的原生 Codex TUI。
- cc-connect 通过官方 `type = "acp"` 接入，不修改 cc-connect 源码或二进制。
- cc-connect session ID 直接保存 Codex thread ID，因此 `/list`、`/switch` 和恢复
  会话仍由 cc-connect 管理。
- 活动 turn 收到飞书消息时使用 `turn/steer`，不会并发启动另一个竞争 turn。
- 权限请求同时发送到终端和飞书，第一份有效响应生效，晚到响应被忽略。
- 飞书发起的 turn 直接收到正常回复，不再额外发送重复的“完成”通知。

## 安装

```bash
cd /path/to/agent-notifier
./install.sh
```

安装器优先使用 `uv` 创建隔离环境；没有 `uv` 时使用标准 `python3 -m venv`，
此时 Debian/Ubuntu 需要安装 `python3-venv`。

CI、容器或不希望立即安装服务时：

```bash
./install.sh --no-service
```

安装内容：

```text
~/.local/bin/agent-notifier
~/.local/share/agent-notifier/venv/
~/.config/systemd/user/agent-notifier.service
```

运行状态不会写回 Git 仓库：

```text
~/.config/agent-notifier/
~/.local/state/agent-notifier/
$XDG_RUNTIME_DIR/agent-notifier/
```

Linux 默认安装用户级 systemd 服务。若希望退出 SSH 登录后服务仍保持运行：

```bash
loginctl enable-linger "$USER"
```

安装器会把当前 `codex` 的绝对路径写入 systemd unit，因此兼容 NVM 等不在
systemd 默认 `PATH` 中的安装方式。Codex 路径变更后重新运行 `./install.sh`。

没有可用 systemd 时，首次运行 `agent-notifier codex` 或 ACP adapter 会自动启动
普通后台进程。

### 避免旧 Codex hook 重复通知

如果 `~/.codex/hooks.json` 已配置 `Stop` 或 `PermissionRequest`（例如旧的飞书
通知脚本），运行：

```bash
agent-notifier configure-hooks
```

该命令先备份原文件，再给这两类 hook 增加可回滚的 gate。普通 `codex` 仍执行
原 hook；只有 Agent Notifier 管理的 App Server 会跳过它们，避免完成通知和权限
审批重复触发。其他 hook 类型不会修改。

## 配置 cc-connect

先查看项目名：

```bash
cc-connect config path
```

将指定项目的 Agent 后端切换为 Agent Notifier：

```bash
agent-notifier configure-cc --project le-wm-codex
```

该命令只修改目标 `[[projects]]` 的 Agent 配置，保留飞书、工作目录和其他项目。
修改前会生成带时间戳的备份，例如：

```text
~/.cc-connect/config.toml.agent-notifier.20260723_120000.bak
```

然后重启 cc-connect：

```bash
cc-connect daemon restart
```

恢复最近一次备份：

```bash
agent-notifier restore-cc
cc-connect daemon restart
```

## 使用

启动或接入共享服务，并打开完整 Codex TUI：

```bash
agent-notifier codex
```

Codex 参数可以继续传入：

```bash
agent-notifier codex resume <THREAD_ID>
agent-notifier codex -C /path/to/project
```

运维命令：

```bash
agent-notifier status
agent-notifier doctor
agent-notifier logs
systemctl --user restart agent-notifier
```

飞书侧继续使用 cc-connect 原有命令：

```text
/new <name>
/list
/switch <id>
/current
```

## 卸载

如已修改 cc-connect，先恢复配置：

```bash
agent-notifier restore-cc
cc-connect daemon restart
```

如执行过 `configure-hooks`，恢复原 Codex hooks：

```bash
agent-notifier restore-hooks
```

再卸载程序：

```bash
./install.sh uninstall
```

卸载默认保留运行状态和 cc-connect 备份，避免误删会话映射。

## 开发和测试

无需安装即可运行核心测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

完整冒烟：

```bash
scripts/smoke_test.sh
```

代理测试会创建临时 Unix socket；在限制 Unix socket 的沙箱中需要在沙箱外运行。

## 已知边界

- 首版不支持 OpenCode。
- 本地 Unix socket 不对网络开放。
- cc-connect 与 Codex 升级后必须重新通过兼容性测试。
- App Server 意外退出时，在途请求会明确失败，不会自动重放可能产生副作用的操作。
