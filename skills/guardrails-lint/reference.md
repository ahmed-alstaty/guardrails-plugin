# CLAUDE.md rules the linter checks

Source: https://code.claude.com/docs/en/memory (sections "Write effective instructions", "Import additional files", "Organize rules with .claude/rules/", "My CLAUDE.md is too large").

| Check | What the docs say | Linter rule |
| --- | --- | --- |
| Length | Target under 200 lines per file. Longer files consume more context and reduce adherence. Claude Code skips a file over 4 MiB. You see a startup warning when one file, or the combined set, is over the limit. | `length`, `size` |
| Procedures | Keep CLAUDE.md to facts Claude should hold in every session. If an entry is a multi-step procedure or only matters for one part of the codebase, move it to a skill or a path-scoped rule. | `procedure` |
| Derivable content | `/doctor` proposes trims that cut content Claude can derive from the codebase: directory layouts, dependency lists, architecture overviews. It keeps pitfalls, rationale and conventions that differ from tool defaults. | `directory-description` |
| Specificity | Write instructions concrete enough to verify: "Use 2-space indentation" not "Format code properly". | `specificity` |
| Consistency | If two rules contradict each other, Claude may pick one arbitrarily. Review root, nested and `.claude/rules/` files periodically to remove outdated or conflicting instructions. | contradictions, duplicates |
| Imports | `@path/to/import` loads at launch. Relative paths resolve against the file containing the import. Import parsing skips code spans and fenced code blocks. Max depth four hops. | `import` |
| Emphasis | Not a documented rule; a heuristic. Repeated IMPORTANT / ALWAYS / NEVER dilute each other. Prefer one specific rule over shouting. | `emphasis` |
| Rules files | `.claude/rules/*.md` without `paths:` load at launch like `.claude/CLAUDE.md`; with `paths:` they load when Claude reads a matching file. `paths` is the only frontmatter key read. | reported by `/guardrails:guardrails-status` |
| AGENTS.md | Read only when no `CLAUDE.md`, `.claude/CLAUDE.md` or `CLAUDE.local.md` exists at cwd or above (default setting `claude-md-or-agents-md`, Claude Code v2.1.277+). To load both, import it with `@AGENTS.md` or set Project instructions to `claude-md-and-agents-md`. | reported by `/guardrails:guardrails-status` |

## How the suggestion is built

- Procedures are cut and written as `suggested-skills/<slug>/SKILL.md` with a `name` and `description`; the trimmed file gets a one-line pointer to the skill.
- Directory descriptions and trees are removed.
- A rule duplicated across files is kept in the root-most file and removed from the others.
- Headings that end up empty are removed and blank runs collapsed.
- Nothing else changes. Emphasis words and vague rules are reported, not rewritten, because rewording is a judgement call for the user.
