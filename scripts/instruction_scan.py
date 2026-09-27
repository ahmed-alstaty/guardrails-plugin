#!/usr/bin/env python3
"""Discovery and analysis of Claude Code instruction files.

Shared by the SessionStart report and the guardrails-lint skill. Standard
library only. All heuristics are conservative: they surface candidates for
a human to review, they never edit anything.
"""

import json
import os
import re
import sys

MAX_LINES = 200
MAX_BYTES = 4 * 1024 * 1024  # Claude Code skips files over 4 MiB
SKIP_DIRS = {
    ".git", "node_modules", "vendor", "dist", "build", "out", "target", ".venv",
    "venv", "env", "__pycache__", ".next", ".nuxt", ".cache", ".tox", ".mypy_cache",
    ".pytest_cache", "coverage", ".idea", ".vscode", "Pods", "DerivedData", ".terraform",
}
MAX_WALK_DEPTH = 6
MAX_WALK_DIRS = 4000

EMPHASIS_WORDS = ("IMPORTANT", "ALWAYS", "NEVER", "MUST", "CRITICAL", "DO NOT")


# --------------------------------------------------------------------------
# Small file helpers
# --------------------------------------------------------------------------

def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return None


def file_size(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def display(path, cwd):
    """Short display form: relative to cwd, or ~ for the home dir."""
    home = os.path.expanduser("~")
    try:
        rel = os.path.relpath(path, cwd)
        if not rel.startswith(".."):
            return rel
    except Exception:
        pass
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def parse_frontmatter(text):
    """Return (dict, body). Minimal YAML: key: value, key: [a, b], key:\n  - a."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return {}, text
    meta = {}
    key = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m and not line.startswith((" ", "\t")):
            key, val = m.group(1), m.group(2).strip()
            if val.startswith("[") and val.endswith("]"):
                meta[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            elif val == "":
                meta[key] = []
            else:
                meta[key] = val.strip("'\"")
        elif key is not None and line.lstrip().startswith("- "):
            if not isinstance(meta.get(key), list):
                meta[key] = []
            meta[key].append(line.lstrip()[2:].strip().strip("'\""))
    body = "\n".join(lines[end + 1:])
    return meta, body


# --------------------------------------------------------------------------
# @imports
# --------------------------------------------------------------------------

IMPORT_RE = re.compile(r"(?<![\w@/.`-])@((?:~|\.{1,2})?/?[\w./-]+(?:/[\w./-]+)*)")


def strip_code(text):
    """Remove fenced blocks and inline code spans (imports are skipped there)."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"~~~.*?~~~", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    return text


def find_imports(text, base_dir):
    """Return list of (raw, resolved_path, exists)."""
    out = []
    plain = strip_code(text)
    for m in IMPORT_RE.finditer(plain):
        raw = m.group(1)
        # skip emails/handles like user@example.com and decorators
        start = m.start()
        if start > 0 and plain[start - 1].isalnum():
            continue
        if "/" not in raw and "." not in raw:
            continue  # @handle, not a path
        if raw.startswith("~"):
            resolved = os.path.expanduser(raw)
        elif os.path.isabs(raw):
            resolved = raw
        else:
            resolved = os.path.normpath(os.path.join(base_dir, raw))
        out.append((raw, resolved, os.path.exists(resolved)))
    return out


# --------------------------------------------------------------------------
# Directives, contradictions, duplicates, procedures
# --------------------------------------------------------------------------

DIRECTIVE_RE = re.compile(
    r"^\s*(?:[-*+]|\d+[.)])?\s*(?:\*\*)?\s*(?:(?:important|note|critical|rule|warning)\s*[:\-]\s*)?(?:\*\*)?\s*"
    r"(?:you\s+)?(always|never|do not|don't|must not|must|should not|should)\b\s*[:\-]?\s*(.+?)\s*(?:\*\*)?\s*$",
    re.I,
)
NEGATIVE = {"never", "do not", "don't", "must not", "should not"}
POSITIVE = {"always", "must", "should"}
STOPWORDS = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "with", "any", "all", "your", "our", "this", "that", "please", "be", "use", "using"}


def normalize_phrase(phrase):
    phrase = phrase.lower()
    phrase = re.sub(r"`", "", phrase)
    phrase = re.sub(r"[^\w\s/.-]", " ", phrase)
    words = [w for w in phrase.split() if w not in STOPWORDS]
    words = [re.sub(r"(ing|ed|s)$", "", w) if len(w) > 4 else w for w in words]
    return " ".join(words[:6])


def extract_directives(text):
    """Return list of (polarity, normalized_phrase, original_line, lineno)."""
    out = []
    for i, line in enumerate(strip_code(text).splitlines(), 1):
        m = DIRECTIVE_RE.match(line)
        if not m:
            continue
        word = m.group(1).lower()
        phrase = m.group(2)
        pol = "neg" if word in NEGATIVE else "pos"
        norm = normalize_phrase(phrase)
        if norm:
            out.append((pol, norm, line.strip(), i))
    return out


def find_contradictions(files_text):
    """files_text: dict path -> text. Returns list of (phrase, [(path, line, lineno)])."""
    items = []
    for path, text in files_text.items():
        for pol, norm, line, ln in extract_directives(text):
            items.append((pol, norm.split(), path, line, ln))
    out = []
    reported = set()
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if a[0] == b[0]:
                continue
            wa, wb = a[1], b[1]
            short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
            # same subject: one phrase is a word-prefix of the other
            if not short or long_[:len(short)] != short:
                continue
            key = " ".join(short)
            pair = (key, a[2], a[4], b[2], b[4])
            if pair in reported:
                continue
            reported.add(pair)
            out.append((key, [(a[2], a[3], a[4]), (b[2], b[3], b[4])]))
    return out


def normalize_rule_line(line):
    line = line.strip()
    line = re.sub(r"^([-*+]|\d+[.)])\s+", "", line)
    line = re.sub(r"\s+", " ", line).strip().rstrip(".").lower()
    return line


def find_duplicates(files_text):
    """Identical rule lines (bullets) appearing in more than one file."""
    where = {}
    for path, text in files_text.items():
        for i, raw in enumerate(strip_code(text).splitlines(), 1):
            if not re.match(r"^\s*([-*+]|\d+[.)])\s+\S", raw):
                continue
            norm = normalize_rule_line(raw)
            if len(norm) < 12:
                continue
            where.setdefault(norm, set()).add((path, i))
    out = []
    for norm, locs in where.items():
        paths = {p for p, _ in locs}
        if len(paths) > 1:
            out.append((norm, sorted(locs)))
    return out


def find_procedures(text):
    """Numbered lists of 3+ steps, or sections whose heading smells procedural.

    Returns list of (start_line, end_line, heading, step_count).
    """
    lines = text.splitlines()
    out = []
    heading = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
        if re.match(r"^\s*1[.)]\s+\S", line):
            j = i
            count = 0
            expected = 1
            while j < len(lines):
                m = re.match(r"^\s*(\d+)[.)]\s+\S", lines[j])
                if m and int(m.group(1)) == expected:
                    count += 1
                    expected += 1
                    j += 1
                    continue
                if lines[j].strip() == "" or lines[j].startswith((" ", "\t")):
                    j += 1
                    continue
                break
            if count >= 3:
                out.append((i + 1, j, heading, count))
                i = j
                continue
        i += 1
    for i, line in enumerate(lines, 1):
        if re.match(r"^#{1,6}\s+.*\b(how to|steps?|procedure|workflow|runbook|playbook|checklist|release process|deploy(ment)? process)\b", line, re.I):
            # section = heading up to the next heading
            j = i
            while j < len(lines) and not lines[j].startswith("#"):
                j += 1
            if any(i <= s <= j for s, _, _, _ in out):
                continue  # a numbered procedure in this section is already reported
            # only the first contiguous list block right under the heading
            k = i
            while k < j and lines[k].strip() == "":
                k += 1
            first = k + 1
            bullets = 0
            while k < j and (re.match(r"^\s*([-*+]|\d+[.)])\s+\S", lines[k]) or lines[k].startswith((" ", "\t"))):
                if re.match(r"^\s*([-*+]|\d+[.)])\s+\S", lines[k]):
                    bullets += 1
                k += 1
            if bullets >= 3:
                out.append((first, k, line.lstrip("#").strip(), bullets))
    return sorted(set(out))


DIR_DESC_RE = re.compile(r"^\s*[-*+]\s+`?([\w.@-]+/[\w./@-]*|[\w-]+\.[a-z]{1,5})`?\s*[-:–—]\s+\S")
TREE_RE = re.compile(r"^\s*[│|]?\s*(├──|└──|\|--|`--)\s")


def find_directory_descriptions(text):
    """Runs of 3+ bullets shaped 'path/ - what it holds', or ASCII trees.

    Returns list of (start_line, end_line, kind).
    """
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        if DIR_DESC_RE.match(lines[i]):
            j = i
            while j < len(lines) and (DIR_DESC_RE.match(lines[j]) or lines[j].strip() == ""):
                j += 1
            run = [k for k in range(i, j) if DIR_DESC_RE.match(lines[k])]
            if len(run) >= 3:
                out.append((i + 1, run[-1] + 1, "path-by-path bullets"))
            i = max(j, i + 1)
            continue
        if TREE_RE.match(lines[i]):
            j = i
            while j < len(lines) and (TREE_RE.match(lines[j]) or lines[j].strip() in ("", "```")):
                j += 1
            run = [k for k in range(i, j) if TREE_RE.match(lines[k])]
            if len(run) >= 3:
                out.append((i + 1, run[-1] + 1, "directory tree"))
            i = max(j, i + 1)
            continue
        i += 1
    return out


def count_emphasis(text):
    counts = {}
    plain = strip_code(text)
    for w in EMPHASIS_WORDS:
        n = len(re.findall(r"\b" + re.escape(w) + r"\b", plain))
        if n:
            counts[w] = n
    return counts


# --------------------------------------------------------------------------
# Skill checks
# --------------------------------------------------------------------------

PROCEDURE_HINT_RE = re.compile(
    r"^\s*(\d+[.)]|[-*+])\s+\S|^#{1,6}\s+.*(step|procedure|workflow|instruction|how to|usage|process)|"
    r"\b(run|execute|open|read|check|create|write|use)\b\s+[`\"']|\$\{CLAUDE_(SKILL_DIR|PLUGIN_ROOT)\}|\.(md|py|sh|js|ts)\b",
    re.I,
)


def skill_has_procedure(body):
    plain = body.strip()
    if not plain:
        return False
    steps = len([l for l in plain.splitlines() if re.match(r"^\s*(\d+[.)]|[-*+])\s+\S", l)])
    pointers = re.findall(r"\[[^\]]+\]\(([^)]+\.md|[^)]+/[^)]+)\)|\$\{CLAUDE_(?:SKILL_DIR|PLUGIN_ROOT)\}[^\s`)]*|\b[\w./-]+\.(?:md|py|sh|js|ts|mjs)\b", plain)
    imperative = len(re.findall(r"^\s*(?:\d+[.)]\s+)?(Run|Read|Open|Check|Create|Write|Use|List|Report|Ask|Verify|Parse|Print|Compare|Search|Look|Find)\b", plain, re.M))
    return steps >= 2 or len(pointers) >= 1 or imperative >= 2


def check_skill(skill_md):
    """Return (meta, warnings) for a SKILL.md."""
    text = read_text(skill_md) or ""
    meta, body = parse_frontmatter(text)
    warnings = []
    dirname = os.path.basename(os.path.dirname(skill_md))
    if not meta:
        warnings.append("no YAML frontmatter")
    if "name" not in meta:
        warnings.append("no frontmatter name (defaults to directory name %r)" % dirname)
    elif isinstance(meta["name"], str) and not re.match(r"^[a-z0-9][a-z0-9-]*$", meta["name"]):
        warnings.append("name %r is not lowercase-with-hyphens" % meta["name"])
    if not meta.get("description"):
        warnings.append("no description, Claude cannot decide when to use it")
    elif isinstance(meta.get("description"), str) and len(meta["description"]) > 1024:
        warnings.append("description is very long (%d chars, cap is 1,536 with when_to_use)" % len(meta["description"]))
    if not skill_has_procedure(body):
        warnings.append("SKILL.md reads as a summary: no steps and no pointer to a file that has them")
    n_lines = len(text.splitlines())
    if n_lines > 500:
        warnings.append("SKILL.md is %d lines; keep it under 500 and move detail to reference files" % n_lines)
    for raw, resolved, exists in find_imports(body, os.path.dirname(skill_md)):
        if not exists:
            warnings.append("@import %s points at a missing file" % raw)
    return meta, warnings


# --------------------------------------------------------------------------
# Instruction file discovery
# --------------------------------------------------------------------------

def _walk_project(cwd):
    """Yield directories under cwd with pruning. Returns (dirpath, dirnames, filenames)."""
    count = 0
    base_depth = cwd.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(cwd):
        depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".") or d == ".claude")
        if depth >= MAX_WALK_DEPTH:
            dirnames[:] = []
        count += 1
        if count > MAX_WALK_DIRS:
            return
        yield dirpath, dirnames, filenames


def managed_claude_md():
    if sys.platform == "darwin":
        return "/Library/Application Support/ClaudeCode/CLAUDE.md"
    if sys.platform.startswith("win"):
        return r"C:\Program Files\ClaudeCode\CLAUDE.md"
    return "/etc/claude-code/CLAUDE.md"


def discover(cwd):
    """Collect every instruction-related file for cwd.

    Returns a dict with lists of records: {"path","kind","loads","scope",...}
    """
    home = os.path.expanduser("~")
    cwd = os.path.normpath(cwd)
    res = {
        "cwd": cwd,
        "launch": [],       # loaded at launch
        "nested": [],       # on demand (subdirectory CLAUDE.md / AGENTS.md)
        "rules": [],        # .claude/rules/*.md (project + user)
        "agents_md": [],    # AGENTS.md files found
        "skills": [],       # SKILL.md files
        "hooks": [],        # settings files with hooks
        "plugins": [],      # installed/enabled plugins
        "notes": [],
    }

    # Managed and user files
    for path, scope in ((managed_claude_md(), "managed"), (os.path.join(home, ".claude", "CLAUDE.md"), "user")):
        if os.path.isfile(path):
            res["launch"].append({"path": path, "scope": scope, "kind": "CLAUDE.md"})

    # cwd and every directory above it (root first)
    chain = []
    d = cwd
    while True:
        chain.append(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    chain.reverse()
    project_claude_present = False
    for d in chain:
        for name in ("CLAUDE.md", os.path.join(".claude", "CLAUDE.md"), "CLAUDE.local.md"):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                res["launch"].append({"path": p, "scope": "project" if d == cwd else "ancestor", "kind": name})
                project_claude_present = True
        for name in ("AGENTS.md", os.path.join(".claude", "AGENTS.md")):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                res["agents_md"].append({"path": p, "scope": "project" if d == cwd else "ancestor"})
    res["project_claude_present"] = project_claude_present

    # rules
    for rules_dir, scope in ((os.path.join(cwd, ".claude", "rules"), "project"), (os.path.join(home, ".claude", "rules"), "user")):
        if os.path.isdir(rules_dir):
            for dp, dn, fn in os.walk(rules_dir):
                for f in sorted(fn):
                    if f.endswith(".md"):
                        p = os.path.join(dp, f)
                        meta, _ = parse_frontmatter(read_text(p) or "")
                        paths = meta.get("paths")
                        if isinstance(paths, str):
                            paths = [x.strip() for x in paths.split(",") if x.strip()]
                        res["rules"].append({"path": p, "scope": scope, "paths": paths or []})

    # nested files and project skills
    for dp, dn, fn in _walk_project(cwd):
        if dp == cwd:
            continue
        for name in ("CLAUDE.md", "CLAUDE.local.md"):
            if name in fn:
                res["nested"].append({"path": os.path.join(dp, name), "kind": name})
        if "AGENTS.md" in fn:
            res["nested"].append({"path": os.path.join(dp, "AGENTS.md"), "kind": "AGENTS.md"})
    for skills_dir, scope in ((os.path.join(cwd, ".claude", "skills"), "project"), (os.path.join(home, ".claude", "skills"), "user")):
        if os.path.isdir(skills_dir):
            try:
                entries = sorted(os.listdir(skills_dir))
            except Exception:
                entries = []
            for e in entries:
                p = os.path.join(skills_dir, e, "SKILL.md")
                if os.path.isfile(p):
                    res["skills"].append({"path": p, "scope": scope, "dir": e})

    # hooks in settings files
    for p, scope in (
        (os.path.join(home, ".claude", "settings.json"), "user"),
        (os.path.join(cwd, ".claude", "settings.json"), "project"),
        (os.path.join(cwd, ".claude", "settings.local.json"), "local"),
    ):
        rec = {"path": p, "scope": scope, "present": os.path.isfile(p), "events": {}, "error": None, "enabledPlugins": {}}
        if rec["present"]:
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                for ev, matchers in (data.get("hooks") or {}).items():
                    n = 0
                    for m in matchers or []:
                        n += len(m.get("hooks") or [])
                    rec["events"][ev] = n
                rec["enabledPlugins"] = data.get("enabledPlugins") or {}
            except Exception as exc:
                rec["error"] = str(exc)
        res["hooks"].append(rec)

    # plugins on disk
    plugins_root = os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR") or os.path.join(home, ".claude", "plugins")
    installed = os.path.join(plugins_root, "installed_plugins.json")
    if os.path.isfile(installed):
        try:
            with open(installed, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            plugins = data.get("plugins", data) if isinstance(data, dict) else {}
            if isinstance(plugins, dict):
                for pid, info in plugins.items():
                    entries = info if isinstance(info, list) else [info]
                    for e in entries:
                        if isinstance(e, dict):
                            res["plugins"].append({
                                "id": pid,
                                "scope": e.get("scope"),
                                "version": e.get("version"),
                                "installPath": e.get("installPath"),
                            })
        except Exception as exc:
            res["notes"].append("could not read %s: %s" % (installed, exc))
    synced = os.path.join(plugins_root, "synced")
    if os.path.isdir(synced):
        for e in sorted(os.listdir(synced)):
            p = os.path.join(synced, e)
            if os.path.isdir(p) and not e.startswith("."):
                res["plugins"].append({"id": _plugin_name_from_dir(p) + "@synced", "scope": "synced", "version": None, "installPath": p})
    return res


def _plugin_name_from_dir(path):
    manifest = os.path.join(path, ".claude-plugin", "plugin.json")
    try:
        with open(manifest, "r", encoding="utf-8") as fh:
            return json.load(fh).get("name") or os.path.basename(path)
    except Exception:
        return os.path.basename(path)


def plugin_hook_events(install_path):
    """Events declared in a plugin's hooks/hooks.json, if any."""
    if not install_path:
        return []
    p = os.path.join(install_path, "hooks", "hooks.json")
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        hooks = data.get("hooks", data)
        return sorted(hooks.keys()) if isinstance(hooks, dict) else []
    except Exception:
        return []
