"""Comment-preserving, narrowly scoped cc-connect project configuration."""

from __future__ import annotations

import json
import re

from agent_notifier.config import ProgressConfig


PROJECT_START = re.compile(r"(?m)(?=^\s*\[\[projects\]\]\s*$)")
COMMAND_START = re.compile(r"(?m)(?=^\s*\[\[commands\]\]\s*$)")


def _set_key(section: str, key: str, value: str) -> str:
    pattern = re.compile(rf"(?m)^(\s*){re.escape(key)}\s*=.*$")
    if pattern.search(section):
        return pattern.sub(lambda match: f"{match.group(1)}{key} = {value}", section, count=1)
    if not section.endswith("\n"):
        section += "\n"
    return section + f"{key} = {value}\n"


def _replace_table_body(block: str, table: str, update) -> str:
    header = f"[{table}]"
    start = block.find(header)
    if start < 0:
        suffix = "" if block.endswith("\n") else "\n"
        return block + suffix + header + "\n" + update("")
    body_start = block.find("\n", start)
    if body_start < 0:
        body_start = len(block)
    else:
        body_start += 1
    next_table = re.search(r"(?m)^\s*\[", block[body_start:])
    body_end = body_start + next_table.start() if next_table else len(block)
    body = update(block[body_start:body_end])
    return block[:body_start] + body + block[body_end:]


def _upsert_command(
    source: str, name: str, description: str, executable: str
) -> str:
    parts = COMMAND_START.split(source)
    for index, block in enumerate(parts):
        if not re.match(r"^\s*\[\[commands\]\]", block):
            continue
        current_name = re.search(
            r'(?m)^\s*name\s*=\s*"([^"]+)"\s*$', block
        )
        if not current_name or current_name.group(1) != name:
            continue
        block = _set_key(block, "description", json.dumps(description))
        parts[index] = _set_key(block, "exec", json.dumps(executable))
        return "".join(parts)
    suffix = "" if source.endswith("\n") else "\n"
    return (
        source
        + suffix
        + "\n[[commands]]\n"
        + f"name = {json.dumps(name)}\n"
        + f"description = {json.dumps(description)}\n"
        + f"exec = {json.dumps(executable)}\n"
    )


def _remove_command(source: str, name: str) -> str:
    parts = COMMAND_START.split(source)
    kept = []
    for block in parts:
        current_name = re.search(
            r'(?m)^\s*name\s*=\s*"([^"]+)"\s*$', block
        )
        if (
            re.match(r"^\s*\[\[commands\]\]", block)
            and current_name
            and current_name.group(1) == name
        ):
            continue
        kept.append(block)
    return "".join(kept)


def _configure_approval_commands(source: str, command: str) -> str:
    source = _upsert_command(
        source,
        "codex-approve",
        "Approve a pending Codex permission request",
        f"{command} decide allow {{{{1}}}}",
    )
    source = _upsert_command(
        source,
        "codex-deny",
        "Deny a pending Codex permission request",
        f"{command} decide deny {{{{1}}}}",
    )
    source = _upsert_command(
        source,
        "agent-list",
        "List subscribed sessions for one coding agent",
        f"{command} agent-list {{{{1}}}}",
    )
    source = _upsert_command(
        source,
        "agent-current",
        "Show current coding-agent task targets",
        f"{command} agent-current",
    )
    source = _upsert_command(
        source,
        "agent-new",
        "Create and activate a new coding-agent session",
        f"{command} agent-new {{{{1}}}}",
    )
    source = _upsert_command(
        source,
        "agent-switch",
        "Switch one coding agent to a subscribed session",
        f"{command} agent-switch {{{{1}}}} {{{{2}}}}",
    )
    source = _upsert_command(
        source,
        "agent-cmd",
        "Run a safe read-only command on the active coding agent",
        f"{command} agent-cmd {{{{1}}}}",
    )
    source = _upsert_command(
        source,
        "agent-help",
        "Show Agent Notifier chat commands",
        f"{command} agent-help",
    )
    return _remove_command(source, "codex-switch")


def _configure_feishu_progress(
    block: str, settings: ProgressConfig
) -> str:
    platform_start = re.compile(
        r"(?m)(?=^\s*\[\[projects\.platforms\]\]\s*$)"
    )
    parts = platform_start.split(block)
    for index, platform in enumerate(parts):
        if not re.match(r"^\s*\[\[projects\.platforms\]\]", platform):
            continue
        platform_type = re.search(
            r'(?m)^\s*type\s*=\s*"([^"]+)"\s*$', platform
        )
        if not platform_type or platform_type.group(1) != "feishu":
            continue

        def update_options(body: str) -> str:
            reaction = '"OnIt"' if settings.onit else '""'
            style = '"card"' if settings.progress_card else '"compact"'
            body = _set_key(body, "reaction_emoji", reaction)
            return _set_key(body, "progress_style", style)

        parts[index] = _replace_table_body(
            platform, "projects.platforms.options", update_options
        )
    return "".join(parts)


def _configure_stream_preview(
    source: str, settings: ProgressConfig
) -> str:
    def update(body: str) -> str:
        body = _set_key(
            body, "enabled", str(settings.stream_preview).lower()
        )
        body = _set_key(
            body, "interval_ms", str(settings.stream_update_interval_ms)
        )
        body = _set_key(body, "min_delta_chars", "1")
        return _set_key(body, "max_chars", "2000")

    return _replace_table_body(source, "stream_preview", update)


def configure_project(
    source: str,
    project_name: str,
    command: str,
    progress: ProgressConfig | None = None,
) -> str:
    """Change one cc-connect project to the agent-notifier ACP command."""
    progress = progress or ProgressConfig()
    parts = PROJECT_START.split(source)
    found = False
    for index, block in enumerate(parts):
        if not re.match(r"^\s*\[\[projects\]\]", block):
            continue
        name = re.search(r'(?m)^\s*name\s*=\s*"([^"]+)"\s*$', block)
        if not name or name.group(1) != project_name:
            continue
        found = True
        block = _replace_table_body(
            block,
            "projects.agent",
            lambda body: _set_key(body, "type", '"acp"'),
        )

        def update_options(body: str) -> str:
            body = _set_key(body, "command", json.dumps(command))
            body = _set_key(body, "args", '["acp"]')
            return _set_key(body, "display_name", '"Agent Notifier (Codex)"')

        block = _replace_table_body(
            block, "projects.agent.options", update_options
        )

        def update_display(body: str) -> str:
            enabled = str(progress.progress_card).lower()
            body = _set_key(body, "thinking_messages", enabled)
            return _set_key(body, "tool_messages", enabled)

        block = _replace_table_body(
            block, "projects.display", update_display
        )
        parts[index] = _configure_feishu_progress(block, progress)
        break
    if not found:
        raise ValueError(f"cc-connect project not found: {project_name}")
    updated = _configure_approval_commands("".join(parts), command)
    return _configure_stream_preview(updated, progress)
