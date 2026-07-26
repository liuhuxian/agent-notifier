# Agent Notifier

Agent Notifier 让终端中的原生 Coding Agent 与 cc-connect/飞书共享同一会话，
并提供可靠的任务完成通知和远程权限审批。

当前同时支持 Codex 和 OpenCode；安装器会根据本机可用的 agent 选择后端。

## 当前兼容版本

| 组件 | 状态 | 已验证版本 |
|---|---|---|
| Codex CLI | 支持 | **0.145.0** |
| cc-connect | 支持 | **1.3.2**，commit `19406df9` |
| Python | 支持 | 3.10+ |
| Linux systemd user service | 支持 | 当前主路径 |
| OpenCode | 支持 | 当前插件/API 组合，安装时检测 |

完整 multi 后端依赖 Codex App Server、OpenCode 和 cc-connect ACP 协议。这些协议仍在
演进，因此 Codex/cc-connect 默认拒绝未经验证的版本；OpenCode 当前做存在性和
`--version` 检查，具体 API 兼容性仍需通过冒烟测试确认。升级任一组件后，先运行：

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
- 终端发起的权限请求会显示在终端，并主动推送到绑定的飞书会话；飞书发起的
  权限请求只显示 Agent Notifier 交互卡片，不再重复生成 cc-connect 原生卡片。
  第一份有效响应生效，晚到响应会提示已经处理。
- 飞书向已绑定 thread 提交任务时，同一 thread 的已打开终端会同步显示
  `turn/*`、`item/*` 和工具执行进度；普通 JSON-RPC 响应仍只返回原请求方。
- 飞书发起的 turn 中，Agent Notifier 直接投递完整 commentary 进度；最终回复只经
  ACP 返回 cc-connect，由 cc-connect 发送并写入会话历史，避免重复回复和
  Reply chain 中的 `(empty response)`。
- 飞书来源的中间进度在每个完整 commentary 消息结束后投递一次，不发送
  token 级碎片，并按 thread/turn/item 去重。每条新中间消息会获得 `OnIt`
  reaction，并删除上一条中间消息的 reaction，使最新进度始终可见。
- cc-connect 收到任务后立即在用户原消息上添加 `OnIt` 表情；该 reaction 仍由
  cc-connect 在回合结束时移除，与中间消息上移动的 `OnIt` 相互独立。
- Codex 的思考、工具执行和测试阶段通过标准 ACP 事件更新同一张进度卡片。
  流式正文预览保留为可选能力，默认关闭。
- Agent Notifier 不转发模型内部推理文本，只发送“正在思考”“正在执行工具”
  “正在运行测试”等安全状态，以及用户最终可见的回复文本。
- App Server 在活动 turn 中异常退出时，Agent Notifier 会发送明确的中断通知，
  随后自动重启共享服务。

## 安装

```bash
cd /path/to/agent-notifier
./install.sh                         # 自动选择 codex/opencode/multi
# 也可以明确选择：./install.sh --backend multi
```

安装包不会保存飞书密钥、群聊 ID 或旧会话 ID。密钥继续由 cc-connect 管理；首次
部署到新电脑后运行初始化向导：

```bash
agent-notifier setup
```

向导会配置：

- cc-connect project
- 交互群（Group A）chat ID
- 通知群（Group B）chat ID
- `[chat_routes]` 和 `notification_routes`

如果需要脚本化部署，可以显式传参：

```bash
agent-notifier setup --non-interactive \
  --project le-wm-codex \
  --interactive-chat-id oc_group_a \
  --notification-chat-id oc_group_b
```

然后配置 cc-connect 项目并执行严格检查：

```bash
agent-notifier configure-cc --project le-wm-codex
agent-notifier doctor --strict
```

`doctor --strict` 会检查已验证的 Codex/cc-connect 版本、OpenCode、服务状态以及
默认通知路由和 ACP 路由。OpenCode 服务默认使用稳定的 `$HOME` 工作目录；如果
需要指定项目目录，安装前设置：

```bash
export AGENT_NOTIFIER_OPENCODE_WORK_DIR=/path/to/project
```

远程机器需要在用户退出 SSH 后继续运行时，额外启用 user service linger：

```bash
loginctl enable-linger "$USER"
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
~/.config/agent-notifier/config.toml
~/.config/systemd/user/agent-notifier.service
~/.config/systemd/user/opencode-serve.service   (需要 opencode)
~/.config/opencode/plugins/feishu-notify.js      (需要 opencode)
```

安装包内包含带注释的默认配置模板。首次安装会复制到
`~/.config/agent-notifier/config.toml`；重复安装不会覆盖用户已有配置。

### OpenCode 配置

安装器会自动复制 opencode 插件和 systemd 服务（如果检测到 `opencode`）。
额外需要手动配置的步骤：

**1. 在 `~/.cc-connect/config.toml` 中注册审批按钮命令：**

```toml
[[commands]]
  name = "opencode-approve"
  exec = "sh -c 'echo once > /tmp/oc-perm-reply-{{1}}.json && agent-notifier opencode-reply-result --perm {{1}} allow >/dev/null 2>&1 && date \"+%Y-%m-%d %H:%M:%S\"'"

[[commands]]
  name = "opencode-deny"
  exec = "sh -c 'echo reject > /tmp/oc-perm-reply-{{1}}.json && agent-notifier opencode-reply-result --perm {{1}} deny >/dev/null 2>&1 && date \"+%Y-%m-%d %H:%M:%S\"'"
```

**2. 在 `~/.config/agent-notifier/config.toml` 中配置聊天路由和通知目标：**

```toml
[chat_routes]
oc_YOUR_GROUP_A_CHAT_ID = "opencode:<session_id>"
oc_YOUR_GROUP_B_CHAT_ID = "silent"

[notification_routes.default]
project = "le-wm-codex"
receive_id_type = "chat_id"
receive_id = "oc_YOUR_GROUP_B_CHAT_ID"
message_format = "markdown"
session_key = "feishu:oc_YOUR_GROUP_B_CHAT_ID:terminal:<session_id>"

[notification_routes.acp]
project = "le-wm-codex"
receive_id_type = "chat_id"
receive_id = "oc_YOUR_GROUP_A_CHAT_ID"
message_format = "markdown"
session_key = "feishu:oc_YOUR_GROUP_A_CHAT_ID:terminal:<session_id>"
```

在每个群聊中发送 `/whoami` 获取 chat_id。然后将本地 opencode session 注册到 cc connect 进行卡片识别：

```bash
agent-notifier bind-terminal --thread-id <session_id> --route default
agent-notifier bind-terminal --thread-id <session_id> --route acp
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

安装器会自动配置原生 Codex 的 `Stop` hook。它只负责完成通知，不安装、删除或
修改 `PermissionRequest` hook：

```text
原生 Codex Stop
    -> agent-notifier native-hook stop
    -> agent-notifier notify --route default
    -> 配置的通知群（默认 Group B）
```

安装时会先备份已有 `~/.codex/hooks.json`。如果需要手动重新配置完成 hook，运行：

```bash
agent-notifier configure-native-hooks
```

该命令只替换 Agent Notifier 之前生成的旧完成 hook，保留其他用户自定义 hook。
普通 `codex` 执行 Stop hook；Agent Notifier 管理的 App Server 会通过
`AGENT_NOTIFIER_MANAGED=1` 跳过 hook，由服务本身发送完成通知，避免重复。

旧的 `configure-hooks` 命令现在也只处理 `Stop`，不会触碰 `PermissionRequest`。

## 配置 cc-connect

Agent Notifier 的显示配置位于：

```toml
[progress]
onit = true
progress_card = true
stream_preview = false
stream_update_interval_ms = 2000
moving_onit = true
notify_interruption = true
```

- `onit`：收到飞书任务后立即添加 `OnIt` 表情。
- `progress_card`：展示思考、工具执行和测试阶段。
- `stream_preview`：逐步展示用户最终可见的回复正文。
- `moving_onit`：将 `OnIt` 移到飞书 turn 最新的中间回复；最终完成、中断或服务
  退出时移除。
- `notify_interruption`：App Server 异常退出时发送中断通知。

终端回合、终端审批和外部 pipeline 使用独立通知群：

```toml
[notification_routes.default]
project = "le-wm-codex"
receive_id_type = "chat_id"
receive_id = "oc_your_notification_group_chat_id"
message_format = "markdown" # or "text"
session_key = "feishu:oc_your_notification_chat_id:terminal:ses_your_session_id"
```

`session_key` 是 OpenCode 双端审批卡片需要的字段，格式为 `feishu:{chat_id}:terminal:{session_id}`。
群A 的 ACP 路由也需要加这个字段（见下方 cc-connect 命令配置）。

### OpenCode 审批按钮命令

OpenCode 交互审批卡片依赖 cc-connect 自定义命令来处理按钮点击。
在 `~/.cc-connect/config.toml` 的 `[[commands]]` 中添加：

```toml
[[commands]]
name = "opencode-approve"
description = "Approve an OpenCode permission request"
exec = "sh -c 'echo once > /tmp/oc-perm-reply-{{1}}.json && agent-notifier opencode-reply-result --perm {{1}} allow >/dev/null 2>&1 && date \"+%Y-%m-%d %H:%M:%S\"'"

[[commands]]
name = "opencode-deny"
description = "Deny an OpenCode permission request"
exec = "sh -c 'echo reject > /tmp/oc-perm-reply-{{1}}.json && agent-notifier opencode-reply-result --perm {{1}} deny >/dev/null 2>&1 && date \"+%Y-%m-%d %H:%M:%S\"'"
```

`markdown` sends a text-only interactive card so Feishu renders emphasis,
lists, line breaks, and fenced code blocks. `text` sends a plain Feishu text
message and displays Markdown syntax literally.

When OpenCode is used through the ACP bridge, the installed plugin also forwards
`tool.execute.before/after` lifecycle events through a local Unix datagram socket.
The ACP backend converts them to `tool_call` and `tool_call_update`, so Feishu can
show separate intermediate tool cards with the moving OnIt reaction. This path is
best effort; if the ACP bridge or plugin is unavailable, the normal final result
card still works.

该群可同时在 `[chat_routes]` 中配置为 `silent`，从而只接收通知、不启动
Codex/OpenCode 对话。分流规则为：

- 终端发起的 Codex turn：完成、异常和审批发送到 `default` 通知群。
- 飞书 P2P 或交互群发起的 turn：回复、进度和审批留在原聊天。
- 外部工具使用下列命令显式发送到通知群：

```bash
printf '%s\n' 'pipeline completed' |
  agent-notifier notify --route default --stdin
```

修改配置后重新运行 `configure-cc`，再重启 cc-connect，使飞书显示设置生效：

```bash
agent-notifier configure-cc --project le-wm-codex
cc-connect daemon restart
```

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
/agent-cmd <status|model|usage|session>
/agent-help
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
/agent-cmd <status|model|usage|session>
/agent-help
```

`/agent-new codex` 通过 Agent Notifier 管理的共享 Codex App Server 创建一个
全新的 Codex thread。创建后会自动执行一次静默初始化 turn，使 rollout 立即
落盘；初始化回复和完成通知不会发送到飞书。初始化成功后才会订阅通知，并将
当前飞书聊天的普通任务目标切换到新 thread；任一步骤失败都不会改变原来的
活动路由。

当前只实现 `codex` provider；后续接入 OpenCode 后沿用相同命令，例如
`/agent-new opencode` 和 `/agent-list opencode`。`/agent-current` 会统一显示各
provider 当前的普通任务目标。

`/agent-cmd` 不需要重复指定 provider，它会读取当前飞书聊天由
`/agent-new` 或 `/agent-switch` 选中的 Agent 类型并自动分发。为避免把飞书命令
变成任意终端入口，当前只允许四个只读命令：

- `/agent-cmd status`：活动会话、运行状态、模型和用量摘要
- `/agent-cmd model`：模型与推理强度
- `/agent-cmd usage`：最近/累计 token 与账户限额窗口
- `/agent-cmd session`：完整会话 ID、目录和运行状态

`usage` 的 thread token 数据来自 Codex App Server 的实时通知；服务重启后若当前
thread 尚未完成新回合，会明确显示暂无缓存。账户限额则在查询时实时读取。
使用 `/agent-help` 可在飞书中查看当前已注册命令及其参数。

本机对应命令：

```bash
agent-notifier agent-list codex --project le-wm-codex
agent-notifier agent-current --project le-wm-codex
agent-notifier agent-new codex --project le-wm-codex
agent-notifier agent-switch codex <THREAD_ID或唯一短ID> --project le-wm-codex
agent-notifier agent-cmd status --project le-wm-codex
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
