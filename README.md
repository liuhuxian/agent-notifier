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
- cc-connect 创建或加载的 ACP session 会保存对应的 Codex thread ID。
- 纯终端启动或恢复的 Codex thread 不会自动出现在 cc-connect 的 `/list` 中；
  Agent Notifier 会为每个终端 thread 单独保存飞书通知订阅。
- 一个飞书聊天可以同时订阅多个终端 thread 的审批和完成通知，但只维护一个
  active thread 作为普通任务目标；切换任务目标不会取消其他 thread 的通知。
- 活动 turn 收到飞书消息时使用 `turn/steer`，不会并发启动另一个竞争 turn。
- 权限请求会显示在终端，并主动推送到绑定的飞书会话；第一份有效响应生效，
  晚到响应会提示已经处理。
- 飞书向已绑定 thread 提交任务时，同一 thread 的已打开终端会同步显示
  `turn/*`、`item/*` 和工具执行进度；普通 JSON-RPC 响应仍只返回原请求方。
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

`configure-cc` 同时安装或更新审批与 Agent 会话管理命令：

```text
/codex-approve <审批ID>
/codex-deny <审批ID>
/agent-list codex
/agent-current
/agent-new codex
/agent-switch codex <THREAD_ID或唯一短ID>
```

正常情况下，飞书会收到带“允许”和“拒绝”按钮的交互卡片。点击后卡片会显示
本次选择，并通过对应的自定义命令将决定送回 Codex。审批 ID 是每次请求生成的
10 位短标识；决定通过用户私有 Unix socket 返回等待中的 Codex App Server 请求，
不开放网络端口。

如果终端或另一个客户端已经先处理该审批，之后再点击飞书卡片不会重复执行操作，
机器人会明确回复 `审批已经被处理：<审批ID>`。交互卡片发送失败时会自动降级为
包含上述两个命令的纯文本消息。

当飞书先处理审批时，代理会把 App Server 的 `serverRequest/resolved` 通知重写为
终端所见的审批 ID，并广播到同一 thread。终端会关闭旧审批界面，后续审批不会被
旧请求阻塞。

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

恢复已有 thread 时，如果注册表中只有一个飞书路由，Agent Notifier 会自动为
该 thread 添加通知订阅，不会改变飞书当前的普通任务目标。存在多个飞书路由时
必须指定项目，避免误发通知：

```bash
agent-notifier codex --notify-project le-wm-codex resume <THREAD_ID>
```

也可以只添加通知订阅而不启动 TUI：

```bash
agent-notifier bind <THREAD_ID> --project le-wm-codex
```

从飞书查询和切换 Coding Agent 会话：

```text
/agent-list codex
/agent-current
/agent-new codex
/agent-switch codex <THREAD_ID或唯一短ID>
```

`/agent-new codex` 通过 Agent Notifier 管理的共享 Codex App Server 创建一个
全新的 Codex thread，自动订阅通知，并立即将当前飞书聊天的普通任务目标切换到
新 thread。创建失败时不会改变原来的活动路由。空 thread 在收到第一条普通消息
前可能尚无 rollout 文件；会话列表会为新注册 thread 保留 5 分钟落盘保护期，
避免将其误判为已删除会话。

当前只实现 `codex` provider；后续接入 OpenCode 后沿用相同命令，例如
`/agent-new opencode` 和 `/agent-list opencode`。`/agent-current` 会统一显示各
provider 当前的普通任务目标。

本机对应命令：

```bash
agent-notifier agent-list codex --project le-wm-codex
agent-notifier agent-current --project le-wm-codex
agent-notifier agent-new codex --project le-wm-codex
agent-notifier agent-switch codex <THREAD_ID或唯一短ID> --project le-wm-codex
```

该操作只改变 active task target。其他已订阅 thread 的权限审批、完成和失败通知
仍会继续发送到同一个飞书聊天。

本机也可以直接处理一个仍在等待的审批：

```bash
agent-notifier decide allow <审批ID>
agent-notifier decide deny <审批ID>
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

这些命令只管理 cc-connect 自己创建或加载的 ACP session，不会列出所有 Codex
历史 thread。终端恢复的 thread 应通过上述自动绑定或 `agent-notifier bind`
关联飞书通知路由；使用 `/agent-list codex` 查询已订阅 thread，再通过
`/agent-switch codex <ID>` 切换任务目标。需要新建 Codex thread 时使用
`/agent-new codex`，不要使用 cc-connect 的 `/new` 代替。

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
- 审批 pending 状态包含到 App Server 的活动连接，服务重启后不能恢复；需要让
  Codex 重新发起对应操作。
