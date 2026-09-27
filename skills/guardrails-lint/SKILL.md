---
name: guardrails-lint
description: Lint CLAUDE.md, CLAUDE.local.md, nested CLAUDE.md and .claude/rules files against Anthropic's documented rules (under 200 lines, procedures in skills, no contradictions, nothing Claude can derive from code, no broken @imports) and produce a suggested trimmed version without changing any file.
argument-hint: "[path/to/CLAUDE.md ...]"
disable-model-invocation: true
allowed-tools: Bash(python3 *), Read, Glob, Grep
---

# Guardrails lint

Lint the project's instruction files and propose trims. This skill never edits a file; every suggestion is written to a scratch directory and the user applies what they agree with.

## Procedure

1. Run the linter. If `$ARGUMENTS` is empty it discovers every file that loads for the current directory (root `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md`, ancestor files, nested `CLAUDE.md` files, `.claude/rules/*.md`, `~/.claude/CLAUDE.md`); otherwise it lints only the paths given.

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lint_claude_md.py" --cwd "$PWD" $ARGUMENTS
   ```

   The script prints a text report and the path of a suggestion directory that holds one `*.trimmed.md` per file and a `suggested-skills/<slug>/SKILL.md` draft for each procedure it moved out.

2. Read the report. Each file gets findings against the rules in [reference.md](reference.md):
   - `length`: line count versus the 200-line target (files over 4 MiB are skipped by Claude Code entirely).
   - `procedure`: numbered lists of 3+ steps, or sections headed "How to ...", "Steps", "Workflow", "Runbook". These belong in a skill.
   - `directory-description`: runs of `path/ - what it holds` bullets or ASCII trees. Claude derives layout from the code; keep only conventions and pitfalls.
   - `emphasis`: IMPORTANT / ALWAYS / NEVER / MUST / CRITICAL used more than once.
   - `import`: `@path` references that do not resolve.
   - `specificity`: vague rules ("write clean code") that cannot be verified.
   - Cross-file: identical rules duplicated across files, and possible contradictions (the same subject under "always" in one place and "never" or "do not" in another).

3. For every WARN, open the file at the lines named and confirm the finding is real. The heuristics are conservative but not perfect: a "procedure" might be a short checklist worth keeping, and a "contradiction" might be two rules about different things that share words. Drop false positives from your summary and say why.

4. Read the `*.trimmed.md` suggestion for each file that had trims and compare it with the original. Check that nothing essential was removed: build and test commands, conventions that differ from tool defaults, known pitfalls and their rationale all stay. Layout listings, dependency lists and architecture overviews that the code already shows can go.

5. Present the result to the user as a compact checklist, one line per file: `OK` or `WARN`, the line count before and after the suggested trim, and the findings you confirmed. Then list:
   - the suggested trimmed file paths,
   - the suggested skill drafts and where they would go (`.claude/skills/<slug>/SKILL.md`),
   - contradictions and duplicates with both locations, and which copy to keep (prefer the most specific file: a path-scoped rule over a root CLAUDE.md).

6. Ask the user which suggestions to apply. Only after they choose, copy the chosen trimmed content over the original with the Edit or Write tool and create the chosen skill directories. Do not apply anything they did not pick, and do not delete the suggestion directory; they may want to diff it later.

## Notes

- Rationale for each rule and the documentation links are in [reference.md](reference.md).
- Run `/guardrails:guardrails-status` afterwards to confirm the files now load without warnings.
