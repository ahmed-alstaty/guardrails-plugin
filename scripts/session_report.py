#!/usr/bin/env python3
"""SessionStart hook: a short, loud checklist of what Claude will load.

Usage:
  session_report.py            read SessionStart JSON on stdin, print hook JSON
  session_report.py --text     print the plain report (for /guardrails:guardrails-status)
  session_report.py --cwd DIR  override the project directory
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import instruction_scan as scan  # noqa: E402
from guardrails_common import load_config, log  # noqa: E402

OK, WARN, SKIP = "OK     ", "WARN   ", "SKIPPED"


class Report(object):
    def __init__(self):
        self.lines = []
        self.counts = {"OK": 0, "WARN": 0, "SKIPPED": 0}

    def section(self, title):
        self.lines.append("")
        self.lines.append(title)

    def add(self, marker, text):
        key = marker.strip()
        self.counts[key] += 1
        self.lines.append("  %s %s" % (marker, text))

    def render(self):
        return "\n".join(self.lines).strip("\n")


def _file_checks(rep, path, cwd, label_prefix, loads_note, files_text):
    text = scan.read_text(path)
    size = scan.file_size(path)
    shown = scan.display(path, cwd)
    if text is None:
        rep.add(WARN, "%s%s: unreadable" % (label_prefix, shown))
        return
    files_text[path] = text
    n_lines = len(text.splitlines())
    problems = []
    if size > scan.MAX_BYTES:
        problems.append("over 4 MiB, Claude Code skips it")
    if n_lines > scan.MAX_LINES:
        problems.append("over %d lines (%d)" % (scan.MAX_LINES, n_lines))
    missing = [raw for raw, _, exists in scan.find_imports(text, os.path.dirname(path)) if not exists]
    if missing:
        problems.append("@import missing: " + ", ".join(missing[:4]) + (" ..." if len(missing) > 4 else ""))
    emph = scan.count_emphasis(text)
    heavy = {k: v for k, v in emph.items() if v > 1}
    if heavy:
        problems.append("emphasis words repeated: " + ", ".join("%s x%d" % kv for kv in sorted(heavy.items())))
    desc = ("%s%s (%d lines) %s" % (label_prefix, shown, n_lines, loads_note)).rstrip()
    if problems:
        rep.add(WARN, desc + " - " + "; ".join(problems))
    else:
        rep.add(OK, desc)


def build_report(cwd):
    cwd = os.path.normpath(cwd)
    home = os.path.expanduser("~")
    rep = Report()
    rep.lines.append("GUARDRAILS session report  (cwd: %s)" % cwd)
    d = scan.discover(cwd)
    files_text = {}

    # ---- Instruction files
    rep.section("Instruction files (load at launch)")
    if not d["launch"]:
        rep.add(SKIP, "no CLAUDE.md, .claude/CLAUDE.md or CLAUDE.local.md at %s or above" % cwd)
    for rec in d["launch"]:
        _file_checks(rep, rec["path"], cwd, "[%s] " % rec["scope"], "", files_text)
        if rec["kind"] == "CLAUDE.local.md" and rec["scope"] == "project":
            gi = scan.read_text(os.path.join(cwd, ".gitignore")) or ""
            if "CLAUDE.local.md" not in gi:
                rep.add(WARN, "CLAUDE.local.md is not listed in .gitignore")

    # AGENTS.md
    if d["agents_md"]:
        for rec in d["agents_md"]:
            shown = scan.display(rec["path"], cwd)
            if d["project_claude_present"]:
                rep.add(SKIP, "%s present but NOT read: a CLAUDE.md/CLAUDE.local.md exists at cwd or above (default 'claude-md-or-agents-md'). Import it with @AGENTS.md or set Project instructions to claude-md-and-agents-md." % shown)
            else:
                _file_checks(rep, rec["path"], cwd, "[agents] ", "read as project instructions (no CLAUDE.md found; needs Claude Code v2.1.277+)", files_text)
                rep.add(WARN, "AGENTS.md present but CLAUDE.md missing: older Claude Code versions and some sessions will not read it; add a CLAUDE.md containing '@AGENTS.md' to be safe")

    # rules
    rep.section("Rules (.claude/rules)")
    if not d["rules"]:
        rep.add(SKIP, "no .claude/rules/*.md in project or ~/.claude/rules")
    for rec in d["rules"]:
        note = "paths: %s (on demand)" % ", ".join(rec["paths"]) if rec["paths"] else "no paths: loads at launch"
        _file_checks(rep, rec["path"], cwd, "[%s] " % rec["scope"], note, files_text)

    # nested
    rep.section("Nested instruction files (load when Claude reads files there)")
    if not d["nested"]:
        rep.add(SKIP, "none found under %s" % cwd)
    for rec in d["nested"]:
        _file_checks(rep, rec["path"], cwd, "", "on demand", files_text)

    # contradictions across everything that loads
    contradictions = scan.find_contradictions(files_text)
    for phrase, entries in contradictions[:8]:
        where = "; ".join("%s:%d %r" % (scan.display(p, cwd), n, l[:60]) for p, l, n in entries[:3])
        rep.add(WARN, "possible contradiction on '%s': %s" % (phrase, where))

    # ---- Skills
    rep.section("Skills")
    if not d["skills"]:
        rep.add(SKIP, "no .claude/skills/*/SKILL.md in project or ~/.claude/skills")
    for rec in d["skills"]:
        meta, warnings = scan.check_skill(rec["path"])
        name = meta.get("name") or rec["dir"]
        shown = scan.display(rec["path"], cwd)
        if warnings:
            rep.add(WARN, "[%s] /%s (%s): %s" % (rec["scope"], name, shown, "; ".join(warnings)))
        else:
            rep.add(OK, "[%s] /%s (%s)" % (rec["scope"], name, shown))

    # ---- Hooks
    rep.section("Hooks")
    for rec in d["hooks"]:
        shown = scan.display(rec["path"], cwd)
        if not rec["present"]:
            rep.add(SKIP, "%s: not present" % shown)
        elif rec["error"]:
            rep.add(WARN, "%s: invalid JSON (%s)" % (shown, rec["error"]))
        elif rec["events"]:
            rep.add(OK, "%s: %s" % (shown, ", ".join("%s(%d)" % kv for kv in sorted(rec["events"].items()))))
        else:
            rep.add(OK, "%s: no hooks" % shown)
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        evs = scan.plugin_hook_events(plugin_root)
        rep.add(OK, "plugin guardrails: %s (this plugin)" % ", ".join(evs) if evs else "plugin guardrails: hooks.json not found")
    for p in d["plugins"]:
        evs = scan.plugin_hook_events(p.get("installPath"))
        if evs:
            rep.add(OK, "plugin %s: %s" % (p["id"], ", ".join(evs)))

    # ---- Plugins
    rep.section("Plugins")
    enabled = {}
    for rec in d["hooks"]:
        for k, v in (rec.get("enabledPlugins") or {}).items():
            enabled[k] = (v, rec["scope"])
    if not d["plugins"] and not enabled:
        rep.add(SKIP, "no plugins installed under ~/.claude/plugins")
    for p in d["plugins"]:
        state = enabled.get(p["id"])
        flag = ""
        if state is not None:
            flag = " (%s in %s settings)" % ("enabled" if state[0] else "disabled", state[1])
        rep.add(OK, "%s%s%s" % (p["id"], " v" + str(p["version"]) if p.get("version") else "", flag))
    for pid, (v, sc) in sorted(enabled.items()):
        if not any(p["id"] == pid for p in d["plugins"]):
            rep.add(WARN if v else SKIP, "%s %s in %s settings but not installed on disk" % (pid, "enabled" if v else "disabled", sc))

    # ---- Guardrails config
    rep.section("Guardrails")
    config, warnings, loaded = load_config(cwd)
    for w in warnings:
        rep.add(WARN, w)
    if loaded:
        rep.add(OK, "config: %s (allow_commands %d, block_commands %d, protected_paths %d, ask_paths %d, secrets_globs %d, allow_paths %d)" % (
            ", ".join(scan.display(p, cwd) for p in loaded), len(config["allow_commands"]), len(config["block_commands"]),
            len(config["protected_paths"]), len(config["ask_paths"]), len(config["secrets_globs"]), len(config["allow_paths"])))
    else:
        rep.add(OK, "defaults in use (no .claude/guardrails.json); enforcement hooks active for Bash, Edit, Write, MultiEdit, NotebookEdit")
    for n in d["notes"]:
        rep.add(WARN, n)

    rep.lines.append("")
    rep.lines.append("Summary: %d OK, %d WARN, %d SKIPPED. Run /guardrails:guardrails-lint to fix instruction files." % (
        rep.counts["OK"], rep.counts["WARN"], rep.counts["SKIPPED"]))
    return rep.render(), rep.counts


def main(argv):
    text_mode = "--text" in argv
    cwd = None
    if "--cwd" in argv:
        cwd = argv[argv.index("--cwd") + 1]
    data = {}
    if not text_mode:
        try:
            raw = sys.stdin.read()
            data = json.loads(raw) if raw.strip() else {}
        except Exception:
            data = {}
    cwd = cwd or data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        report, counts = build_report(cwd)
    except Exception as exc:
        report = "GUARDRAILS session report failed: %s: %s" % (type(exc).__name__, exc)
        counts = {"OK": 0, "WARN": 1, "SKIPPED": 0}
    if text_mode:
        print(report)
        return 0
    # loud: stderr (debug log), systemMessage (user), additionalContext (Claude)
    log(report)
    headline = "Guardrails: %d OK, %d WARN, %d SKIPPED. " % (counts["OK"], counts["WARN"], counts["SKIPPED"])
    warn_lines = [l.strip() for l in report.splitlines() if l.strip().startswith("WARN")]
    if warn_lines:
        headline += "Warnings: " + " | ".join(w[len("WARN"):].strip() for w in warn_lines[:6])
        if len(warn_lines) > 6:
            headline += " | ... and %d more (run /guardrails:guardrails-status)" % (len(warn_lines) - 6)
    else:
        headline += "Destructive commands and secrets edits are blocked by PreToolUse hooks."
    out = {
        "systemMessage": headline,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "The guardrails plugin is active. It blocks destructive shell commands and edits to "
                "secrets/protected paths with PreToolUse hooks; when it blocks something, tell the user "
                "the reason it gave and do not try to work around it.\n\n" + report
            ),
        },
    }
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
