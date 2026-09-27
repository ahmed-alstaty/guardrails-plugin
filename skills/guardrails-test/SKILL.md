---
name: guardrails-test
description: Dry-run the guardrails blocklist against sample destructive commands (and any commands or paths you pass) to see what the PreToolUse hooks would allow, ask about or block. Nothing is executed.
argument-hint: "[\"command\" ...] [--path FILE]"
disable-model-invocation: true
allowed-tools: Bash(python3 *)
---

# Guardrails test

Show the enforcement working without running anything dangerous. Every command is judged by the same engine the PreToolUse hook uses, in the current directory with the current `.claude/guardrails.json`.

## Procedure

1. Run the dry run. Pass the user's arguments straight through; with none, a built-in sample set of destructive and safe commands is used:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/dry_run.py" --cwd "$PWD" $ARGUMENTS
   ```

   Examples the user might type:
   - `/guardrails:guardrails-test` shows the sample set.
   - `/guardrails:guardrails-test "rm -rf build" "git push -f origin main"` judges those two commands.
   - `/guardrails:guardrails-test --path .env --path Dockerfile` judges file edits.

2. Show the output verbatim in a code block. Each line starts with `ALLOW`, `ASK` or `DENY` followed by the command and the reason the hook would give.

3. If any built-in sample differs from its expectation, the script marks it with `<-- expected ...`; explain that a rule in `.claude/guardrails.json` (allow_commands, block_commands, allow_paths, ...) changed the outcome and show that rule with Read.

4. Point out, in one sentence, that these are the exact decisions the hook returns to Claude Code; a DENY is enforced before the tool runs and cannot be talked around, while an ASK shows a permission prompt.
