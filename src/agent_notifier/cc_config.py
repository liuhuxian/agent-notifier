"""Comment-preserving, narrowly scoped cc-connect project configuration."""

from __future__ import annotations

import json
import re


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


def _configure_approval_commands(source: str, command: str) -> str:
    source = _upsert_command(
        source,
        "codex-approve",
        "Approve a pending Codex permission request",
        f"{command} decide --quiet allow {{{{1}}}}",
    )
    return _upsert_command(
        source,
        "codex-deny",
        "Deny a pending Codex permission request",
        f"{command} decide --quiet deny {{{{1}}}}",
    )


def configure_project(source: str, project_name: str, command: str) -> str:
    """Change one cc-connect project to the agent-notifier ACP command."""
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

        parts[index] = _replace_table_body(block, "projects.agent.options", update_options)
        break
    if not found:
        raise ValueError(f"cc-connect project not found: {project_name}")
    return _configure_approval_commands("".join(parts), command)
