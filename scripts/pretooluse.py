#!/usr/bin/env python3
"""PreToolUse hook: block destructive shell commands and risky file edits.

Reads the hook JSON from stdin and prints the documented PreToolUse decision
JSON on stdout (exit 0). Never silent: every deny/ask carries a reason that
is shown to Claude (permissionDecisionReason) and to the user
(systemMessage and stderr).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from guardrails_common import (  # noqa: E402
    PATH_NOTE,
    classify_path,
    emit_pretooluse,
    load_config,
    log,
    normalize_path,
)
from shell_guard import evaluate_command  # noqa: E402

FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _file_path(tool_name, tool_input):
    for key in ("file_path", "notebook_path", "path"):
        if tool_input.get(key):
            return tool_input[key]
    return None


def _edit_deletes(tool_name, tool_input):
    """True if the edit removes content (empty replacement) or overwrites a file."""
    if tool_name == "Edit":
        return tool_input.get("new_string", None) == ""
    if tool_name == "MultiEdit":
        return any(e.get("new_string", None) == "" for e in tool_input.get("edits", []) or [])
    if tool_name == "NotebookEdit":
        return tool_input.get("edit_mode") == "delete"
    return False


def handle_file_tool(tool_name, tool_input, cwd, config, extra_roots):
    path = _file_path(tool_name, tool_input)
    if not path:
        emit_pretooluse("allow", "")
    resolved = normalize_path(path, cwd)
    verdict, detail = classify_path(resolved, cwd, config, extra_roots)
    if verdict == "secret":
        emit_pretooluse(
            "deny",
            "Guardrails blocked: %s would modify a secrets file (%s). Credentials are never edited by Claude. %s"
            % (tool_name, detail, PATH_NOTE),
        )
    if verdict == "protected":
        emit_pretooluse(
            "deny",
            "Guardrails blocked: %s to %s (%s). %s" % (tool_name, path, detail, PATH_NOTE),
        )
    if verdict == "outside":
        emit_pretooluse(
            "deny",
            "Guardrails blocked: %s to %s, which is %s. Claude only edits files inside the project. %s"
            % (tool_name, path, detail, PATH_NOTE),
        )
    if verdict == "ask":
        is_migration = "migrations" in detail
        if is_migration:
            deletes = _edit_deletes(tool_name, tool_input)
            overwrite = tool_name == "Write" and os.path.exists(resolved)
            if not (deletes or overwrite):
                emit_pretooluse("allow", "")
            what = "overwrites an existing migration" if overwrite else "deletes content from a migration"
            emit_pretooluse(
                "ask",
                "Guardrails asks for approval: %s %s (%s). Approve if this is intended. %s"
                % (tool_name, what, path, PATH_NOTE),
            )
        emit_pretooluse(
            "ask",
            "Guardrails asks for approval: %s touches deployment or infrastructure config %s (%s). %s"
            % (tool_name, path, detail, PATH_NOTE),
        )
    emit_pretooluse("allow", "")


def handle_bash(tool_input, cwd, config):
    command = tool_input.get("command") or ""
    decision, reason = evaluate_command(command, cwd, config)
    if decision == "allow":
        emit_pretooluse("allow", "")
    emit_pretooluse(decision, reason)


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception as exc:
        emit_pretooluse("ask", "Guardrails could not parse the hook input (%s); please review this action yourself." % exc)
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    cwd = os.path.normpath(cwd)
    extra_roots = [r for r in (data.get("scratchpad_dir"), os.environ.get("CLAUDE_PLUGIN_DATA")) if r]

    config, warnings, _ = load_config(cwd)
    config["_extra_roots"] = extra_roots
    for w in warnings:
        log("guardrails: " + w)

    try:
        if tool_name == "Bash":
            handle_bash(tool_input, cwd, config)
        elif tool_name in FILE_TOOLS:
            handle_file_tool(tool_name, tool_input, cwd, config, extra_roots)
        else:
            emit_pretooluse("allow", "")
    except SystemExit:
        raise
    except Exception as exc:  # fail safe: hand the decision to the user
        emit_pretooluse(
            "ask",
            "Guardrails hook hit an internal error (%s: %s); review this action yourself." % (type(exc).__name__, exc),
        )


if __name__ == "__main__":
    main()
