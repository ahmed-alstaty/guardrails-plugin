# guardrails

A Claude Code plugin that puts hard limits around what Claude can do in your project, tells you what Claude actually loads at session start, and lints your `CLAUDE.md` files against Anthropic's own guidance.

Three features, all enforced with plain `python3` scripts and no dependencies:

1. **Enforcement hooks** (`PreToolUse`): destructive shell commands and edits to secrets or protected paths are denied before the tool runs. Deployment and infrastructure files require your approval.
2. **Session start report** (`SessionStart`): a compact `OK / WARN / SKIPPED` checklist of instruction files, rules, skills, hooks and plugins, with warnings for the mistakes the docs warn about.
3. **Skills**: `/guardrails:guardrails-lint`, `/guardrails:guardrails-status` and `/guardrails:guardrails-test`.

## Install

The repository is its own marketplace (`alstaty`), so it takes two commands in a Claude Code session:

```text
/plugin marketplace add ahmed-alstaty/guardrails-plugin
/plugin install guardrails@alstaty
```

Or from your shell:

```bash
claude plugin marketplace add ahmed-alstaty/guardrails-plugin
claude plugin install guardrails@alstaty            # add --scope project to enable it for the whole repo
```

From a local checkout, replace the GitHub shorthand with the path: `claude plugin marketplace add ./guardrails-plugin`. To try it for one session without installing: `claude --plugin-dir ./guardrails-plugin`.

Requirements: Claude Code with plugin support and `python3` (3.8 or newer) on `PATH`. No pip packages.

## What it blocks

Every `Bash` command is tokenized (quotes, escapes, `&&`, `||`, `;`, `|`, `$( )`, backticks, `bash -c`, `eval`, `sudo`/`env`/`xargs` wrappers) and each simple command is judged on its own, so `true && rm -rf /` is caught and `rm build/old.log` is not.

| Category | Denied | Asks for approval | Allowed |
| --- | --- | --- | --- |
| `rm` / `shred` / `unlink` | recursive removal of `/`, `~`, `$HOME`, `.`, `..`, `*`, the project directory, anything outside it, an unexpanded `$VAR/`, `--no-preserve-root`, protected or secrets paths | non-recursive `rm` of a file outside the project; `xargs rm -r`; `find` with `-delete` and no filter | files and directories inside the project (`rm -rf build`, `rm -rf node_modules`) |
| `git` | `push --force`/`-f`/`+ref`/`--mirror` to `main` or `master` (current branch is detected when no refspec is given); pushing a deletion of `main`/`master`; `reset --hard`; `clean -fd`/`-fx`; `branch -D`; `checkout -- .`, `checkout .`, `restore .`; `filter-branch`, `filter-repo` | `push --force` with an unknown branch; `--force-with-lease` to main; `checkout -- <file>`, `restore <file>`; `clean -f`; `stash drop/clear`; `reflog expire`; `gc --prune` | everything else, including `push -f` to a feature branch |
| Databases | `DROP DATABASE`, `DROP SCHEMA`, `dropdb`, `dropDatabase()`, `FLUSHALL`/`FLUSHDB`, `db:drop`, `migrate reset` (inline, `-c`/`-e`, or heredoc, when a database client or migration tool is in the command) | `DROP TABLE`, `TRUNCATE`, `DELETE FROM` without `WHERE` | |
| Permissions | `chmod -R 777`, `chmod/chown -R` on root or outside the project | | |
| Remote code | `curl ... \| sh`, `wget ... \| sudo bash`, `bash <(curl ...)`, `sh -c "$(curl ...)"` | | `curl ... \| jq` |
| Disks and system | `mkfs*`, `wipefs`, `fdisk`, `dd of=/dev/...`, `> /dev/sd*`, fork bombs, `shutdown`/`reboot`, `kill -1`, `crontab -r`, `mv x /dev/null` | `sudo su`, `sudo -i` | |
| Shell writes | redirection (`>`, `>>`, `&>`, `tee`, `cp`/`mv` destination, `sed -i`, `truncate`) into a secrets file, a protected path, or `.git/` | writes outside the project or into an ask path | |

File tools (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`) are judged by path:

| Verdict | Paths |
| --- | --- |
| Denied | secrets: `.env`, `.env.*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `id_rsa*`, `id_ed25519*`, `credentials.json`, `service-account*.json`, `secrets/**`, `.netrc`, `.htpasswd`, `.aws/credentials`, `.npmrc`, `.pypirc`; protected: `.git/**`; anything outside the project directory (the session scratchpad is exempt) |
| Asks | `Dockerfile*`, `docker-compose*.yml`, `compose*.yml`, `.github/workflows/**`, `.gitlab-ci.yml`, `terraform/**`, `*.tf`, `k8s/**`, `kubernetes/**`, `helm/**`; `migrations/**` only when an edit deletes content or a `Write` overwrites an existing migration |
| Allowed | `.env.example`, `.env.sample`, `.env.template`, `*.example`, `*.sample`, `*.template`, `*.pub`, and everything else inside the project |

Reading is never blocked; `cat .env` goes through so Claude can still understand configuration.

A block is never silent. The hook returns the documented `permissionDecision: "deny"` with a `permissionDecisionReason` that Claude sees, plus a `systemMessage` for you, and echoes the reason to stderr. Every reason ends with how to proceed:

```text
Guardrails blocked: git push --force to main rewrites shared history. To allow once, run the command yourself in your terminal. To allow always, add it to .claude/guardrails.json allow_commands.
```

If the hook itself fails (unreadable input, internal error) it returns `ask`, not `allow`.

## Configure

Create `.claude/guardrails.json` in the project (committed, applies to everyone) or `~/.claude/guardrails.json` (just you). Both extend the defaults; lists are appended, nothing is replaced.

```json
{
  "allow_commands": ["rm -rf /tmp/scratch*", "git push --force origin main"],
  "block_commands": ["npm publish*", "terraform apply*"],
  "protected_paths": ["package-lock.json", "docs/adr/**"],
  "ask_paths": ["config/production.yml"],
  "secrets_globs": ["*.token", "config/keys/**"],
  "allow_paths": ["Dockerfile", ".env.ci"]
}
```

- `allow_commands` and `block_commands` are shell-style globs matched against the whole command and against each simple command inside it. `block_commands` wins over `allow_commands`; `allow_commands` wins over the built-in rules.
- `protected_paths` are always denied, `ask_paths` prompt you, `secrets_globs` are denied, and `allow_paths` is checked first and exempts a path from all of them.
- A pattern without `/` matches a file name anywhere (`*.pem`); a pattern with `/` matches the path relative to the project root (`secrets/**`); an absolute or `~/` pattern matches outside the project.

The session report shows which config files were loaded and warns about invalid JSON or unknown keys. A broken config never disables enforcement; the defaults keep applying.

## Session start report

At startup, resume and `/clear` the plugin prints this checklist in your terminal (as a system message) and hands Claude a short summary, so Claude knows the guardrails are on and will report a block instead of trying to route around it. Run `/guardrails:guardrails-status` to see the full report again at any time:

```text
GUARDRAILS session report  (cwd: /work/app)

Instruction files (load at launch)
  WARN    [project] CLAUDE.md (243 lines) - over 200 lines (243); @import missing: docs/setup.md
  OK      [user] ~/.claude/CLAUDE.md (31 lines)
  SKIPPED AGENTS.md present but NOT read: a CLAUDE.md/CLAUDE.local.md exists at cwd or above ...

Rules (.claude/rules)
  OK      [project] .claude/rules/api.md (12 lines) paths: src/api/**/*.ts (on demand)

Nested instruction files (load when Claude reads files there)
  OK      packages/web/CLAUDE.md (18 lines) on demand

Skills
  WARN    [project] /deploy (.claude/skills/deploy/SKILL.md): no description, Claude cannot decide when to use it

Hooks
  OK      .claude/settings.json: PostToolUse(1)
  SKIPPED .claude/settings.local.json: not present
  OK      plugin guardrails: PreToolUse, SessionStart (this plugin)

Plugins
  OK      guardrails@alstaty v1.0.1 (enabled in user settings)

Guardrails
  OK      config: .claude/guardrails.json (allow_commands 2, block_commands 1, ...)

Summary: 7 OK, 2 WARN, 2 SKIPPED. Run /guardrails:guardrails-lint to fix instruction files.
```

Warnings cover: instruction files over 200 lines or over 4 MiB (Claude Code skips those), `@imports` that point at missing files, the same subject under "always" in one file and "never" in another, repeated `IMPORTANT`/`ALWAYS`/`NEVER`, `CLAUDE.local.md` not in `.gitignore`, skills without a `name` or `description`, `SKILL.md` files that are a summary with no steps and no pointer to steps, `AGENTS.md` present but not read (or present without a `CLAUDE.md`), invalid settings JSON, and plugins enabled in settings but not installed.

## Cost

Two channels, two costs. The full report is a system message: it is shown to you and never enters the model, so it is free. What Claude receives is a few lines (the OK / WARN / SKIPPED counts, up to four warnings, and the instruction not to route around a block), about 150 to 300 tokens once per session start, cached on every later turn. Each denied or asked tool call adds one sentence, about 40 tokens. Running `/guardrails:guardrails-status` or `/guardrails:guardrails-lint` puts that output into the conversation like any other tool result, so use them when you want them, not on every session.

## Skills

| Command | What it does |
| --- | --- |
| `/guardrails:guardrails-lint [files]` | Lints every instruction file that loads (or the files you name) against the documented rules: line count vs 200, procedures that should be skills, path-by-path directory descriptions Claude can derive from code, repeated emphasis words, vague rules, broken `@paths`, duplicated rules across nested files, contradictions. Writes a suggested trimmed copy of each file and a `SKILL.md` draft for each procedure into a scratch directory. It changes nothing until you pick what to apply. |
| `/guardrails:guardrails-status` | Re-runs the session report on demand. |
| `/guardrails:guardrails-test ["cmd" ...] [--path FILE]` | Dry-runs the blocklist against a built-in sample set, or the commands and paths you pass, and prints `ALLOW` / `ASK` / `DENY` with the reason. Nothing is executed. |

All three are user-invoked only (`disable-model-invocation: true`), so they cost no context until you call them.

## Test

```bash
tests/run_tests.sh
```

The script copies a fixture project to a temporary directory, feeds sample hook JSON to the real hook scripts and checks the decisions: destructive `rm` blocked, safe `rm` allowed, `.env` edits blocked, force push to `main` blocked, normal `git push` allowed, `Dockerfile` edits ask, config overrides, fail-safe on garbage input, the session report's warnings, the linter's findings, and the manifests. It exits 0 when everything passes.

To check the manifests with the CLI: `claude plugin validate .` (marketplace) and `claude plugin validate .claude-plugin/plugin.json --strict`.

## Limits, honestly

- **This is enforcement for Claude Code only.** The hooks run when Claude Code calls a tool. They do nothing for commands you type yourself, for other agents, or for scripts Claude writes and you run later.
- **It is not a sandbox.** A hook sees a command string, not what the command does. A build script that deletes files, a `make clean` target, `docker compose down -v`, or a program invoked through a name the parser does not recognise all pass. Use Claude Code's sandbox and permission modes for isolation; use this plugin for guardrails on the common mistakes.
- **It does not replace backups or branch protection.** Force pushes to `main` are blocked here, but the remote should refuse them too. Commit often.
- **Heuristics have edges.** The tokenizer handles the common shell constructs, not every one. Variables are not expanded (a command built from `$VAR` is judged on its text, and `rm -rf $VAR/` is denied for that reason). Branch detection for a bare `git push -f` runs `git symbolic-ref` in the project; if that fails the hook asks instead of guessing. The lint and contradiction checks are pattern-based and will produce some false positives; the skill tells Claude to verify each one before reporting it.
- **`python3` must be on `PATH`** for the hook processes. If it is missing, Claude Code reports a hook error and the tool call proceeds; the session report will not appear, which is your signal.
- **Windows**: paths are handled with forward slashes and the scripts avoid POSIX-only calls, but the shell rules target bash-style commands. PowerShell commands are not parsed.

## Layout

```text
guardrails-plugin/
├── .claude-plugin/
│   ├── plugin.json           plugin manifest
│   └── marketplace.json      marketplace "alstaty" listing this plugin at "."
├── hooks/hooks.json          PreToolUse (Bash; Edit|Write|MultiEdit|NotebookEdit) and SessionStart
├── scripts/
│   ├── guardrails_common.py  config loading, path classification, hook output
│   ├── shell_guard.py        tokenizer and command rules
│   ├── pretooluse.py         PreToolUse entry point
│   ├── instruction_scan.py   instruction-file discovery and heuristics
│   ├── session_report.py     SessionStart entry point (also --text)
│   ├── lint_claude_md.py     linter used by guardrails-lint
│   └── dry_run.py            used by guardrails-test
├── skills/
│   ├── guardrails-lint/      SKILL.md + reference.md
│   ├── guardrails-status/SKILL.md
│   └── guardrails-test/SKILL.md
└── tests/run_tests.sh        hook tests with a fixture project
```

## License

MIT. Copyright (c) 2026 Ahmad Alstaty.
