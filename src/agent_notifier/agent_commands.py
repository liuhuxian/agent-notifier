"""Read-only command adapters for the active coding-agent session."""

from __future__ import annotations

from datetime import datetime

from .codex.client import CodexAppServerClient
from .config import Paths
from .registry import SessionMapping, SessionRegistry


SUPPORTED_AGENT_COMMANDS = frozenset({"status", "model", "usage", "session"})


def format_agent_help() -> str:
    return (
        "Agent Notifier 命令\n\n"
        "会话管理\n"
        "/agent-list codex\n"
        "  查看已订阅的 Codex 会话\n"
        "/agent-current\n"
        "  查看当前飞书聊天绑定的 Agent 会话\n"
        "/agent-new codex\n"
        "  新建 Codex 会话并自动切换过去\n"
        "/agent-switch codex <会话ID或唯一短ID>\n"
        "  切换当前飞书聊天使用的 Codex 会话\n\n"
        "只读查询\n"
        "/agent-cmd status    当前状态摘要\n"
        "/agent-cmd model     模型与推理强度\n"
        "/agent-cmd usage     Token 与账户限额\n"
        "/agent-cmd session   完整会话信息\n\n"
        "权限审批\n"
        "/codex-approve <审批ID>\n"
        "/codex-deny <审批ID>\n\n"
        "帮助\n"
        "/agent-help"
    )


def _format_number(value) -> str:
    return f"{int(value or 0):,}"


def _format_reset(timestamp) -> str:
    if not timestamp:
        return "未知"
    return datetime.fromtimestamp(int(timestamp)).astimezone().strftime(
        "%Y-%m-%d %H:%M"
    )


def _rate_limit_lines(rate_limits: dict) -> list[str]:
    snapshot = rate_limits.get("rateLimits") or {}
    lines = []
    for label, key in (("主窗口", "primary"), ("次窗口", "secondary")):
        window = snapshot.get(key)
        if not window:
            continue
        lines.append(
            f"{label}：已用 {window.get('usedPercent', 0)}%，"
            f"重置 { _format_reset(window.get('resetsAt'))}"
        )
    return lines


def _usage_lines(token_usage: dict | None) -> list[str]:
    if not token_usage:
        return ["Token：暂无缓存（完成一次 Codex turn 后更新）"]
    total = token_usage.get("total") or {}
    last = token_usage.get("last") or {}
    context = token_usage.get("modelContextWindow")
    lines = [
        "累计 Token："
        f"{_format_number(total.get('totalTokens'))} "
        f"(输入 {_format_number(total.get('inputTokens'))}，"
        f"输出 {_format_number(total.get('outputTokens'))})",
        "最近回合 Token："
        f"{_format_number(last.get('totalTokens'))} "
        f"(输入 {_format_number(last.get('inputTokens'))}，"
        f"输出 {_format_number(last.get('outputTokens'))})",
    ]
    if context:
        lines.append(f"模型上下文上限：{_format_number(context)} Token")
    return lines


def _resolve_active_codex(
    paths: Paths, project: str, external_key: str
) -> tuple[SessionMapping, dict | None]:
    registry = SessionRegistry(paths.state_db)
    try:
        active = registry.get_active_agent(project, external_key)
        if active is None:
            mapping = registry.get("cc_connect", project, external_key)
            if mapping is None:
                raise ValueError(
                    "当前聊天未绑定 Agent；请先使用 /agent-new codex 或 "
                    "/agent-switch codex <会话ID>"
                )
            provider = "codex"
        else:
            provider = active.provider
            mapping = registry.get(
                "cc_connect" if provider == "codex" else provider,
                project,
                external_key,
            )
        if provider != "codex":
            raise ValueError(f"当前 Agent 类型 {provider!r} 尚不支持 /agent-cmd")
        if mapping is None:
            raise ValueError("当前 Codex Agent 没有活动会话")
        usage = registry.get_thread_token_usage(mapping.thread_id)
        return mapping, usage
    finally:
        registry.close()


async def run_agent_command(
    paths: Paths,
    command: str,
    project: str | None,
    external_key: str | None,
) -> str:
    normalized = command.strip().lower()
    if normalized not in SUPPORTED_AGENT_COMMANDS:
        supported = "、".join(sorted(SUPPORTED_AGENT_COMMANDS))
        raise ValueError(f"不支持的 Agent 命令 {command!r}；仅支持：{supported}")
    if not project or not external_key:
        raise ValueError("/agent-cmd 需要 cc-connect 项目和会话标识")

    mapping, token_usage = _resolve_active_codex(
        paths, project, external_key
    )
    rpc = CodexAppServerClient(paths.proxy_socket, "agent-notifier-command")
    await rpc.connect()
    try:
        thread_result = await rpc.call(
            "thread/read",
            {"threadId": mapping.thread_id, "includeTurns": False},
        )
        thread = thread_result["thread"]

        if normalized == "session":
            return (
                "Agent 会话\n\n"
                "类型：codex\n"
                f"会话：{mapping.thread_id}\n"
                f"状态：{(thread.get('status') or {}).get('type', '未知')}\n"
                f"目录：{thread.get('cwd') or mapping.cwd}"
            )

        config_result = await rpc.call(
            "config/read",
            {"cwd": mapping.cwd, "includeLayers": False},
        )
        config = config_result.get("config") or {}
        model = config.get("model") or "默认模型"
        effort = config.get("model_reasoning_effort") or "默认"

        if normalized == "model":
            return (
                "Agent 模型\n\n"
                f"类型：codex\n模型：{model}\n推理强度：{effort}"
            )

        rate_limits = await rpc.call("account/rateLimits/read", {})
        usage_lines = _usage_lines(token_usage)
        rate_lines = _rate_limit_lines(rate_limits)
        if normalized == "usage":
            return "Agent 用量\n\n" + "\n".join(usage_lines + rate_lines)

        status = (thread.get("status") or {}).get("type", "未知")
        return (
            "Agent 状态\n\n"
            f"类型：codex\n"
            f"会话：{mapping.short_thread_id}\n"
            f"状态：{status}\n"
            f"模型：{model}（推理强度：{effort}）\n"
            f"目录：{thread.get('cwd') or mapping.cwd}\n"
            + "\n".join(usage_lines + rate_lines)
        )
    finally:
        await rpc.close()
