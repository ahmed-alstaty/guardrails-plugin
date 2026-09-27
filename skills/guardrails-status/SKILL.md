---
name: guardrails-status
description: Re-run the guardrails session report on demand. Shows which instruction files (CLAUDE.md, CLAUDE.local.md, AGENTS.md, .claude/rules), skills, hooks and plugins load for this project, with OK / WARN / SKIPPED markers and warnings.
disable-model-invocation: true
allowed-tools: Bash(python3 *)
---

# Guardrails status

Print the same checklist the plugin shows at session start, for the current directory.

## Procedure

1. Run the report in text mode:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_report.py" --text --cwd "$PWD"
   ```

2. Show the user the output verbatim inside a code block; do not summarise away the WARN lines.

3. Below it, add at most three sentences: how many warnings there are, which one to fix first (over-length or contradictory instruction files come before cosmetic ones), and that `/guardrails:guardrails-lint` produces suggested trims for the instruction files.

## What the markers mean

- `OK`: the file or hook is present and nothing looks wrong.
- `WARN`: something the docs advise against (over 200 lines, missing @import, contradictory rules, skill without description, invalid settings JSON, plugin enabled but not installed).
- `SKIPPED`: not present, or present but not loaded (for example an `AGENTS.md` that is ignored because a `CLAUDE.md` exists).
