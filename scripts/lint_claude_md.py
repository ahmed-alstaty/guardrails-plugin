#!/usr/bin/env python3
"""Lint CLAUDE.md files against the documented guidance and propose a trim.

Usage:
  lint_claude_md.py [--cwd DIR] [--suggest-dir DIR] [--json] [FILE ...]

With no FILE arguments, every instruction file that loads for DIR is linted
(root CLAUDE.md, .claude/CLAUDE.md, CLAUDE.local.md, ancestors, nested
CLAUDE.md files, .claude/rules/*.md, ~/.claude/CLAUDE.md).

Nothing is modified. Suggested trimmed versions are written to --suggest-dir
(default: a fresh temporary directory) and their paths are printed.
"""

import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import instruction_scan as scan  # noqa: E402

RULES_DOC = "https://code.claude.com/docs/en/memory"


def lint_file(path, cwd):
    text = scan.read_text(path)
    if text is None:
        return {"path": path, "error": "unreadable"}
    lines = text.splitlines()
    findings = []
    n = len(lines)
    size = scan.file_size(path)

    if size > scan.MAX_BYTES:
        findings.append({"rule": "size", "severity": "WARN", "msg": "file is over 4 MiB; Claude Code skips it entirely"})
    if n > scan.MAX_LINES:
        findings.append({"rule": "length", "severity": "WARN", "msg": "%d lines, target is under %d (longer files reduce adherence)" % (n, scan.MAX_LINES)})
    else:
        findings.append({"rule": "length", "severity": "OK", "msg": "%d lines (target under %d)" % (n, scan.MAX_LINES)})

    procedures = scan.find_procedures(text)
    for start, end, heading, steps in procedures:
        findings.append({
            "rule": "procedure", "severity": "WARN", "lines": [start, end],
            "msg": "lines %d-%d: %d-step procedure%s; multi-step procedures belong in a skill (.claude/skills/<name>/SKILL.md), not in CLAUDE.md" % (
                start, end, steps, " under '%s'" % heading if heading else ""),
        })

    for start, end, kind in scan.find_directory_descriptions(text):
        findings.append({
            "rule": "directory-description", "severity": "WARN", "lines": [start, end],
            "msg": "lines %d-%d: %s; Claude can derive the layout from the code, keep only what it cannot infer (conventions, pitfalls)" % (start, end, kind),
        })

    emph = scan.count_emphasis(text)
    heavy = {k: v for k, v in emph.items() if v > 1}
    if heavy:
        findings.append({
            "rule": "emphasis", "severity": "WARN",
            "msg": "emphasis words used more than once: %s; when everything is IMPORTANT nothing is, prefer specific verifiable rules" % ", ".join("%s x%d" % kv for kv in sorted(heavy.items())),
        })

    for raw, resolved, exists in scan.find_imports(text, os.path.dirname(path)):
        if not exists:
            findings.append({"rule": "import", "severity": "WARN", "msg": "@%s points at a missing file (%s)" % (raw, resolved)})
        else:
            findings.append({"rule": "import", "severity": "OK", "msg": "@%s resolves (%s)" % (raw, scan.display(resolved, cwd))})

    vague = []
    for i, l in enumerate(lines, 1):
        if re.match(r"^\s*[-*+]\s+(write|keep|make|ensure|be)\b.*\b(clean|good|nice|proper(ly)?|best practices|high quality|well[- ]organi[sz]ed|readable)\b", l, re.I):
            vague.append(i)
    if vague:
        findings.append({"rule": "specificity", "severity": "WARN", "msg": "lines %s: vague rules; write something concrete enough to verify ('Use 2-space indentation', 'Run npm test before committing')" % ", ".join(str(v) for v in vague[:8])})

    return {"path": path, "lines": n, "bytes": size, "findings": findings, "procedures": procedures, "text": text}


def suggest_trim(result, duplicates_for_file, cwd):
    """Build a trimmed text plus skill drafts. Removes nothing from disk."""
    text = result["text"]
    lines = text.splitlines()
    drop = set()
    notes = []
    skills = []
    for start, end, heading, steps in result["procedures"]:
        slug = re.sub(r"[^a-z0-9]+", "-", (heading or "procedure").lower()).strip("-")[:40] or "procedure"
        body = "\n".join(lines[start - 1:end])
        skills.append((slug, heading or slug, body))
        for k in range(start - 1, end):
            drop.add(k)
        notes.append("moved %d-step procedure '%s' (lines %d-%d) to suggested skill %s" % (steps, heading or slug, start, end, slug))
    for start, end, kind in scan.find_directory_descriptions(text):
        for k in range(start - 1, end):
            drop.add(k)
        notes.append("removed %s (lines %d-%d): derivable from the code" % (kind, start, end))
    for norm, lineno in duplicates_for_file:
        drop.add(lineno - 1)
        notes.append("removed line %d, duplicated in another instruction file: %r" % (lineno, norm[:60]))

    out = []
    for i, l in enumerate(lines):
        if i in drop:
            continue
        # soften repeated emphasis after the first occurrence
        out.append(l)
    # drop headings whose section became empty
    kept = []
    for idx, l in enumerate(out):
        if l.startswith("#"):
            nxt = idx + 1
            while nxt < len(out) and out[nxt].strip() == "":
                nxt += 1
            if nxt >= len(out) or out[nxt].startswith("#"):
                notes.append("removed now-empty heading %r" % l.strip())
                continue
        kept.append(l)
    # collapse runs of blank lines
    trimmed = []
    for l in kept:
        if l.strip() == "" and trimmed and trimmed[-1].strip() == "":
            continue
        trimmed.append(l)
    if skills:
        trimmed.append("")
        trimmed.append("<!-- guardrails-lint: procedures moved to skills; invoke them by name -->")
        for slug, heading, _ in skills:
            trimmed.append("- %s: use the `%s` skill" % (heading, slug))
    return "\n".join(trimmed).rstrip() + "\n", skills, notes


def main(argv):
    cwd = os.getcwd()
    suggest_dir = None
    as_json = False
    files = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--cwd":
            cwd = argv[i + 1]
            i += 2
            continue
        if a == "--suggest-dir":
            suggest_dir = argv[i + 1]
            i += 2
            continue
        if a == "--json":
            as_json = True
            i += 1
            continue
        files.append(a)
        i += 1
    cwd = os.path.normpath(os.path.abspath(cwd))

    if not files:
        d = scan.discover(cwd)
        files = [r["path"] for r in d["launch"]] + [r["path"] for r in d["rules"]] + [r["path"] for r in d["nested"] if r["kind"] != "AGENTS.md"]
        if not d["project_claude_present"]:
            files += [r["path"] for r in d["agents_md"]]
    files = [os.path.abspath(f) for f in files if os.path.isfile(f)]
    if not files:
        print("guardrails-lint: no instruction files found for %s" % cwd)
        return 1

    results = [lint_file(f, cwd) for f in files]
    texts = {r["path"]: r["text"] for r in results if "text" in r}
    duplicates = scan.find_duplicates(texts)
    contradictions = scan.find_contradictions(texts)

    if suggest_dir is None:
        suggest_dir = tempfile.mkdtemp(prefix="guardrails-lint-")
    os.makedirs(suggest_dir, exist_ok=True)

    report = {"cwd": cwd, "files": [], "duplicates": [], "contradictions": [], "suggest_dir": suggest_dir}
    total_warn = 0
    for r in results:
        if "error" in r:
            report["files"].append({"path": r["path"], "error": r["error"]})
            continue
        dups_here = [(norm, ln) for norm, locs in duplicates for p, ln in locs if p == r["path"] and p != _keeper(locs)]
        trimmed, skills, notes = suggest_trim(r, dups_here, cwd)
        rel = scan.display(r["path"], cwd).replace("~", "home").replace("/", "__")
        out_path = os.path.join(suggest_dir, rel + ".trimmed.md")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(trimmed)
        skill_paths = []
        for slug, heading, body in skills:
            sd = os.path.join(suggest_dir, "suggested-skills", slug)
            os.makedirs(sd, exist_ok=True)
            sp = os.path.join(sd, "SKILL.md")
            with open(sp, "w", encoding="utf-8") as fh:
                fh.write("---\nname: %s\ndescription: %s (moved from %s by guardrails-lint; edit before use)\n---\n\n# %s\n\n%s\n" % (
                    slug, heading, scan.display(r["path"], cwd), heading, body))
            skill_paths.append(sp)
        warn = [f for f in r["findings"] if f["severity"] == "WARN"]
        total_warn += len(warn)
        report["files"].append({
            "path": r["path"], "lines": r["lines"], "findings": r["findings"],
            "trimmed_lines": len(trimmed.splitlines()), "suggestion": out_path,
            "suggested_skills": skill_paths, "trim_notes": notes,
        })
    for norm, locs in duplicates:
        report["duplicates"].append({"rule": norm, "locations": ["%s:%d" % (scan.display(p, cwd), ln) for p, ln in locs]})
        total_warn += 1
    for phrase, entries in contradictions:
        report["contradictions"].append({"subject": phrase, "locations": ["%s:%d %s" % (scan.display(p, cwd), ln, l) for p, l, ln in entries]})
        total_warn += 1

    if as_json:
        print(json.dumps(report, indent=2))
        return 0

    print("GUARDRAILS lint  (cwd: %s)" % cwd)
    print("Rules: %s (target under 200 lines; procedures go in skills; no contradictions; nothing Claude can derive from code)" % RULES_DOC)
    for f in report["files"]:
        print("")
        print("%s" % scan.display(f["path"], cwd))
        if "error" in f:
            print("  WARN    %s" % f["error"])
            continue
        for fd in f["findings"]:
            print("  %-7s %s" % (fd["severity"], fd["msg"]))
        if f["trim_notes"]:
            print("  suggested trim: %d -> %d lines, written to %s" % (f["lines"], f["trimmed_lines"], f["suggestion"]))
            for n in f["trim_notes"]:
                print("    - %s" % n)
            for sp in f["suggested_skills"]:
                print("    - skill draft: %s" % sp)
        else:
            print("  suggested trim: nothing to remove (copy at %s)" % f["suggestion"])
    if report["duplicates"]:
        print("")
        print("Duplicated rules across files (keep one, ideally in the most specific file)")
        for dref in report["duplicates"]:
            print("  WARN    %r in %s" % (dref["rule"][:70], ", ".join(dref["locations"])))
    if report["contradictions"]:
        print("")
        print("Possible contradictions (Claude may pick one arbitrarily)")
        for c in report["contradictions"]:
            print("  WARN    '%s': %s" % (c["subject"], " | ".join(c["locations"])))
    print("")
    print("Summary: %d file(s), %d warning(s). Suggestions in %s. Nothing was modified." % (len(report["files"]), total_warn, suggest_dir))
    return 0


def _keeper(locs):
    """For a duplicated rule, keep the copy in the shortest path (root-most file)."""
    return sorted(locs, key=lambda x: (x[0].count(os.sep), x[0]))[0][0]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
