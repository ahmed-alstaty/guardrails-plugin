# Changelog

All notable changes to this plugin are documented here. The format follows Keep a Changelog and the project uses semantic versioning.

## [1.0.0] - 2026-09-27

### Added

- `PreToolUse` hook for the `Bash` tool: tokenizes commands (quotes, escapes, `&&`/`||`/`;`/`|`, subshells, `bash -c`, `eval`, `sudo`/`env`/`xargs` wrappers) and denies destructive `rm`, history-destroying `git` commands, database drops, `chmod -R 777`, `curl | sh` pipes, `mkfs`, `dd` to devices, fork bombs, and shell writes into secrets or protected paths. Safe removals inside the project are allowed.
- `PreToolUse` hook for `Edit`, `Write`, `MultiEdit` and `NotebookEdit`: denies secrets files, `.git/**` and paths outside the project; asks for deployment and infrastructure files and for edits that delete migration content.
- `.claude/guardrails.json` and `~/.claude/guardrails.json` with `allow_commands`, `block_commands`, `protected_paths`, `ask_paths`, `secrets_globs`, `allow_paths`.
- `SessionStart` hook that reports instruction files, rules, nested files, `AGENTS.md` handling, skills, hooks, plugins and guardrails config as an `OK / WARN / SKIPPED` checklist, delivered as `additionalContext`, `systemMessage` and stderr.
- Skills `guardrails-lint`, `guardrails-status` and `guardrails-test` (user-invoked only).
- Test suite `tests/run_tests.sh` with a fixture project.
