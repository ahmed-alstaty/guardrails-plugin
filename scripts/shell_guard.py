#!/usr/bin/env python3
"""Destructive shell command detection for the guardrails plugin.

The command is tokenized (quotes, escapes, operators, redirections,
subshells) and split into simple commands, so ``rm -rf /`` hidden behind
``true && rm -rf /`` or inside ``$( )`` is still seen, while
``rm build/old.log`` is left alone.

Public entry point: ``evaluate_command(command, cwd, config)`` which returns
``(decision, reason)`` with decision in ``allow``, ``ask`` or ``deny``.
"""

import fnmatch
import os
import re
import subprocess

from guardrails_common import (
    HOOK_TIMEOUT_NOTE,
    classify_path,
    home_dir,
    is_within,
    normalize_path,
)

# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

WORD, OP, REDIR = "word", "op", "redir"

CONTROL_OPS = ("&&", "||", ";;", "|&", ";", "|", "&", "\n", "(", ")", "{", "}", "`")
REDIR_OPS = ("&>>", "&>", ">>", ">|", ">&", "<<<", "<<", "<&", "<>", ">", "<")


class Token(object):
    __slots__ = ("kind", "text", "quoted")

    def __init__(self, kind, text, quoted=False):
        self.kind = kind
        self.text = text
        self.quoted = quoted

    def __repr__(self):
        return "%s(%r)" % (self.kind, self.text)


def tokenize(command):
    """Split a POSIX-ish shell command into words, operators and redirects."""
    tokens = []
    buf = []
    quoted = False
    in_word = False
    i = 0
    n = len(command)

    def flush():
        nonlocal buf, quoted, in_word
        if in_word:
            tokens.append(Token(WORD, "".join(buf), quoted))
        buf = []
        quoted = False
        in_word = False

    while i < n:
        ch = command[i]
        if ch == "\\":
            if i + 1 < n:
                nxt = command[i + 1]
                if nxt == "\n":
                    i += 2
                    continue
                buf.append(nxt)
                in_word = True
                i += 2
                continue
            i += 1
            continue
        if ch == "'":
            j = command.find("'", i + 1)
            if j == -1:
                j = n
            buf.append(command[i + 1:j])
            quoted = True
            in_word = True
            i = j + 1
            continue
        if ch == '"':
            i += 1
            while i < n and command[i] != '"':
                if command[i] == "\\" and i + 1 < n and command[i + 1] in '"\\$`\n':
                    buf.append(command[i + 1])
                    i += 2
                    continue
                buf.append(command[i])
                i += 1
            i += 1
            quoted = True
            in_word = True
            continue
        if ch == "#" and not in_word:
            # comment to end of line
            j = command.find("\n", i)
            i = n if j == -1 else j
            continue
        if ch in " \t\r":
            flush()
            i += 1
            continue
        if ch == "$" and i + 1 < n and command[i + 1] == "(":
            flush()
            tokens.append(Token(OP, "("))
            i += 2
            continue
        if ch == "<" and i + 1 < n and command[i + 1] == "(":
            # process substitution <(cmd): treat like a subshell
            flush()
            tokens.append(Token(OP, "<("))
            i += 2
            continue
        if ch == ">" and i + 1 < n and command[i + 1] == "(":
            flush()
            tokens.append(Token(OP, "("))
            i += 2
            continue
        matched = None
        for op in REDIR_OPS:
            if command.startswith(op, i):
                matched = op
                break
        if matched:
            # a numeric fd directly before the operator belongs to it (2>&1)
            if in_word and "".join(buf).isdigit() and not quoted:
                buf = []
                in_word = False
            else:
                flush()
            tokens.append(Token(REDIR, matched))
            i += len(matched)
            continue
        matched = None
        for op in CONTROL_OPS:
            if command.startswith(op, i):
                matched = op
                break
        if matched and not (matched == "{" and in_word) and not (matched == "}" and in_word):
            flush()
            tokens.append(Token(OP, matched))
            i += len(matched)
            continue
        buf.append(ch)
        in_word = True
        i += 1
    flush()
    return tokens


class SimpleCommand(object):
    """One simple command: words plus redirection targets."""

    def __init__(self):
        self.words = []          # list of Token
        self.redirects = []      # list of (op, target Token or None)
        self.piped_from = None   # previous SimpleCommand in the same pipeline

    def argv(self):
        return [t.text for t in self.words]

    def text(self):
        return " ".join(self.argv())

    def __repr__(self):
        return "SimpleCommand(%r)" % self.argv()


def split_commands(tokens):
    """Group tokens into SimpleCommand objects, following pipelines."""
    commands = []
    current = SimpleCommand()
    pending_pipe = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.kind == REDIR:
            target = None
            if i + 1 < len(tokens) and tokens[i + 1].kind == WORD:
                target = tokens[i + 1]
                i += 1
            current.redirects.append((tok.text, target))
        elif tok.kind == OP:
            if current.words or current.redirects:
                if pending_pipe and commands:
                    current.piped_from = commands[-1]
                commands.append(current)
            pending_pipe = tok.text in ("|", "|&")
            current = SimpleCommand()
        else:
            current.words.append(tok)
        i += 1
    if current.words or current.redirects:
        if pending_pipe and commands:
            current.piped_from = commands[-1]
        commands.append(current)
    return commands


WRAPPERS = {
    "sudo": {"flags_with_arg": ("-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T")},
    "doas": {"flags_with_arg": ("-u", "-C")},
    "env": {"flags_with_arg": ("-u", "-C", "-S")},
    "nohup": {"flags_with_arg": ()},
    "time": {"flags_with_arg": ("-f", "-o")},
    "nice": {"flags_with_arg": ("-n",)},
    "ionice": {"flags_with_arg": ("-c", "-n", "-p")},
    "command": {"flags_with_arg": ()},
    "builtin": {"flags_with_arg": ()},
    "exec": {"flags_with_arg": ("-a",)},
    "timeout": {"flags_with_arg": ("-s", "-k", "--signal", "--kill-after")},
    "xargs": {"flags_with_arg": ("-n", "-I", "-L", "-P", "-d", "-s", "-E", "-a", "--max-args", "--max-procs", "--replace", "--delimiter", "--arg-file")},
    "stdbuf": {"flags_with_arg": ("-i", "-o", "-e")},
    "caffeinate": {"flags_with_arg": ("-t",)},
}


def unwrap(argv):
    """Strip VAR=x assignments and wrappers like sudo/env/nohup/xargs.

    Returns (argv, via_xargs, timeout_first).
    """
    via_xargs = False
    changed = True
    while changed and argv:
        changed = False
        # leading assignments
        while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
            argv = argv[1:]
            changed = True
        if not argv:
            break
        name = os.path.basename(argv[0])
        if name in WRAPPERS:
            spec = WRAPPERS[name]
            if name == "xargs":
                via_xargs = True
            rest = argv[1:]
            while rest:
                a = rest[0]
                if a == "--":
                    rest = rest[1:]
                    break
                if a.startswith("-"):
                    if a in spec["flags_with_arg"] and len(rest) > 1:
                        rest = rest[2:]
                    else:
                        rest = rest[1:]
                    continue
                if name == "env" and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", a):
                    rest = rest[1:]
                    continue
                if name == "timeout" and re.match(r"^\d+[smhd]?$", a):
                    rest = rest[1:]
                    continue
                break
            argv = rest
            changed = True
    return argv, via_xargs


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

ROOT_LIKE = {"/", "/*", "/.", "/..", "//"}
HOME_TOKENS = {"~", "~/", "$HOME", "${HOME}", "$HOME/", "${HOME}/", "~/*", "$HOME/*", "${HOME}/*"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "ash"}
INTERPRETERS = SHELLS | {"python", "python2", "python3", "perl", "ruby", "node", "php", "pwsh", "powershell"}
FETCHERS = {"curl", "wget", "fetch", "http", "https", "aria2c"}


def _short_flags(argv, start=1):
    """Return (set_of_single_letter_flags, list_of_long_flags, positional args)."""
    letters = set()
    longs = []
    args = []
    rest = argv[start:]
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--":
            args.extend(rest[i + 1:])
            break
        if a.startswith("--"):
            longs.append(a)
        elif a.startswith("-") and len(a) > 1:
            letters.update(a[1:])
        else:
            args.append(a)
        i += 1
    return letters, longs, args


def _strip_trailing_glob(p):
    """'./build/*' -> './build', '*' -> '.', 'x/**' -> 'x'."""
    while True:
        base = os.path.basename(p.rstrip("/"))
        if base and set(base) <= set("*?") and p not in ("*", "**"):
            p = os.path.dirname(p.rstrip("/")) or "."
            continue
        break
    if p in ("*", "**"):
        return "."
    return p


def _extra_roots(config):
    return [r for r in config.get("_extra_roots", []) if r]


def _danger_target(target, cwd, config=None):
    """Return a reason string if removing target recursively is catastrophic."""
    t = target.strip()
    if t in ROOT_LIKE or t in HOME_TOKENS:
        return "%s is the filesystem root or the home directory" % t
    if t.startswith("$") and not (t.startswith("$HOME") or t.startswith("${HOME}") or t.startswith("$PWD") or t.startswith("${PWD}")):
        return "%s starts with a variable; if it is empty this becomes a root path" % t
    stripped = _strip_trailing_glob(t)
    resolved = normalize_path(stripped, cwd)
    if resolved in ("/", home_dir()):
        return "%s resolves to %s" % (t, resolved)
    if resolved == os.path.normpath(cwd):
        return "%s is the project directory itself" % t
    if not is_within(resolved, cwd):
        for root in _extra_roots(config or {}):
            if is_within(resolved, root) and resolved != os.path.normpath(root):
                return None
        return "%s resolves outside the project directory (%s)" % (t, resolved)
    return None


def check_rm(argv, via_xargs, cwd, config):
    name = os.path.basename(argv[0])
    letters, longs, args = _short_flags(argv)
    recursive = bool(letters & {"r", "R"}) or "--recursive" in longs
    if name == "shred":
        recursive = True
    if "--no-preserve-root" in longs:
        return "deny", "rm --no-preserve-root can erase the whole filesystem."
    if via_xargs and not args:
        if recursive:
            return "ask", "xargs %s -r removes whatever the pipe feeds it; the targets are not visible to the hook." % name
        return "allow", ""
    for a in args:
        if recursive:
            why = _danger_target(a, cwd, config)
            if why:
                return "deny", "recursive %s of %s: %s." % (name, a, why)
        resolved = normalize_path(_strip_trailing_glob(a), cwd)
        verdict, detail = classify_path(resolved, cwd, config, _extra_roots(config))
        if verdict == "protected":
            return "deny", "%s targets a protected path (%s)." % (name, detail)
        if verdict == "secret":
            return "deny", "%s targets a secrets file (%s)." % (name, detail)
        if verdict == "outside":
            return ("deny" if recursive else "ask"), "%s of %s which is %s." % (name, a, detail)
        if verdict == "ask":
            return "ask", "%s of %s (%s)." % (name, a, detail)
    return "allow", ""


def check_find(argv, cwd, config):
    """find <root> ... -delete / -exec rm ... : judge by the search root."""
    roots = []
    for a in argv[1:]:
        if a.startswith("-") or a in ("(", ")", "!", ","):
            break
        roots.append(a)
    if not roots:
        roots = ["."]
    destructive = "-delete" in argv or ("-exec" in argv and any(os.path.basename(x) in ("rm", "shred") for x in argv))
    if not destructive:
        return "allow", ""
    for r in roots:
        why = _danger_target(r, cwd, config)
        if why and "project directory itself" not in why:
            return "deny", "find with -delete/-exec rm starting at %s: %s." % (r, why)
        resolved = normalize_path(r, cwd)
        verdict, detail = classify_path(resolved, cwd, config, _extra_roots(config))
        if verdict in ("protected", "secret"):
            return "deny", "find deleting under %s (%s)." % (r, detail)
    filters = {"-name", "-iname", "-path", "-ipath", "-regex", "-iregex", "-type", "-mtime", "-mmin", "-newer", "-size", "-empty", "-user", "-perm", "-atime", "-ctime", "-maxdepth", "-links", "-samefile"}
    if not (set(argv) & filters):
        return "ask", "find starting at %s deletes everything it matches and has no filter." % ", ".join(roots)
    return "allow", ""


GIT_GLOBAL_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env"}


def _git_subcommand(argv):
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in GIT_GLOBAL_WITH_ARG:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, argv[i + 1:]
    return None, []


def _current_branch(cwd):
    for argv in (["git", "symbolic-ref", "--short", "-q", "HEAD"], ["git", "rev-parse", "--abbrev-ref", "HEAD"]):
        try:
            out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=3)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except Exception:
            pass
    return None


PROTECTED_BRANCHES = ("main", "master")


def check_git(argv, cwd, config):
    sub, rest = _git_subcommand(argv)
    if sub is None:
        return "allow", ""
    letters, longs, args = _short_flags([sub] + rest)

    if sub == "push":
        forced = "f" in letters or "--force" in longs or any(a.startswith("+") for a in args)
        lease = any(l.startswith("--force-with-lease") for l in longs)
        mirror = "--mirror" in longs
        delete = "d" in letters or "--delete" in longs
        if mirror:
            return "deny", "git push --mirror overwrites every ref on the remote."
        refspecs = args[1:] if args else []
        targets = []
        for spec in refspecs:
            spec = spec.lstrip("+")
            dst = spec.split(":", 1)[1] if ":" in spec else spec
            dst = dst.replace("refs/heads/", "")
            targets.append(dst)
            if spec.startswith(":"):
                delete = True
        if not targets:
            branch = _current_branch(cwd)
            targets = [branch] if branch else []
            unknown = branch is None
        else:
            unknown = False
        hits_protected = any(t in PROTECTED_BRANCHES for t in targets)
        if delete and hits_protected:
            return "deny", "git push deleting %s on the remote." % "/".join(t for t in targets if t in PROTECTED_BRANCHES)
        if forced:
            if hits_protected:
                return "deny", "git push --force to %s rewrites shared history." % "/".join(t for t in targets if t in PROTECTED_BRANCHES)
            if unknown:
                return "ask", "git push --force with an unknown current branch; confirm it is not main or master."
            return "allow", ""
        if lease and hits_protected:
            return "ask", "git push --force-with-lease to a protected branch (%s)." % "/".join(targets)
        return "allow", ""

    if sub == "reset":
        if "--hard" in longs or "--merge" in longs:
            return "deny", "git reset --hard discards uncommitted work and moves the branch."
        return "allow", ""

    if sub == "clean":
        dry = "n" in letters or "--dry-run" in longs
        force = "f" in letters or "--force" in longs
        if dry:
            return "allow", ""
        if force and (letters & {"d", "x", "X"}):
            return "deny", "git clean -f with -d/-x removes untracked files and directories permanently."
        if force:
            return "ask", "git clean -f removes untracked files permanently."
        return "allow", ""

    if sub == "branch":
        if "D" in letters or ("--delete" in longs and ("--force" in longs or "f" in letters)):
            return "deny", "git branch -D force-deletes a branch even if it is unmerged."
        if "M" in letters and any(a in PROTECTED_BRANCHES for a in args):
            return "ask", "git branch -M renames over a protected branch."
        return "allow", ""

    if sub in ("checkout", "restore", "switch"):
        # discarding working-tree changes
        if sub == "switch":
            return "allow", ""
        pathspecs = [a for a in args]
        if sub == "restore" and ("--staged" in longs or "S" in letters) and not ("--worktree" in longs or "W" in letters):
            return "allow", ""  # unstaging only
        if sub == "checkout" and "--" not in rest:
            # `git checkout <branch>` or `git checkout -b x`; but `git checkout .` is a pathspec
            if not any(p in (".", "./", "*", ":/", ":/.") for p in pathspecs):
                return "allow", ""
        if any(p in (".", "./", "*", ":/", ":/.", "**") for p in pathspecs):
            return "deny", "git %s . discards every uncommitted change in the working tree." % sub
        if pathspecs:
            return "ask", "git %s discards uncommitted changes in %s." % (sub, " ".join(pathspecs))
        return "allow", ""

    if sub in ("filter-branch", "filter-repo"):
        return "deny", "git %s rewrites the entire history." % sub

    if sub == "stash" and args and args[0] in ("drop", "clear"):
        return "ask", "git stash %s deletes stashed work." % args[0]

    if sub == "reflog" and args and args[0] == "expire":
        return "ask", "git reflog expire removes the safety net for recovering lost commits."

    if sub == "gc" and any(l.startswith("--prune") for l in longs):
        return "ask", "git gc --prune permanently deletes unreachable objects."

    if sub == "update-ref" and ("d" in letters or "--delete" in longs):
        return "ask", "git update-ref -d deletes a ref."

    return "allow", ""


SQL_DENY = [
    (re.compile(r"\bdrop\s+database\b", re.I), "DROP DATABASE deletes a whole database."),
    (re.compile(r"\bdrop\s+schema\b", re.I), "DROP SCHEMA deletes a whole schema."),
    (re.compile(r"\bdropDatabase\s*\(", re.I), "dropDatabase() deletes a whole MongoDB database."),
    (re.compile(r"\bflush(all|db)\b", re.I), "redis FLUSHALL/FLUSHDB wipes the keyspace."),
    (re.compile(r"\bdb:drop\b"), "db:drop deletes the database."),
    (re.compile(r"\bmigrate\s+reset\b"), "migrate reset drops and recreates the database."),
]
SQL_ASK = [
    (re.compile(r"\bdrop\s+table\b", re.I), "DROP TABLE deletes a table and its data."),
    (re.compile(r"\btruncate\s+(table\s+)?\w", re.I), "TRUNCATE empties a table."),
    (re.compile(r"\bdelete\s+from\s+[\w.\"`]+\s*(;|$|\"|')", re.I), "DELETE FROM without a WHERE clause empties a table."),
]


def check_sql_text(raw):
    for rx, why in SQL_DENY:
        if rx.search(raw):
            return "deny", why
    for rx, why in SQL_ASK:
        if rx.search(raw):
            return "ask", why
    return "allow", ""


DB_CLIENTS = {
    "psql", "mysql", "mariadb", "sqlite3", "sqlite", "mongo", "mongosh", "redis-cli",
    "clickhouse-client", "cqlsh", "prisma", "rails", "rake", "knex", "sequelize",
    "alembic", "typeorm", "flyway", "liquibase", "npx", "pnpm", "yarn", "npm",
    "bundle", "php", "artisan", "dbmate", "goose", "migrate", "drizzle-kit", "mix",
}

FORK_BOMB = re.compile(r"(\S+)\s*\(\s*\)\s*\{[^}]*\1\s*\|\s*\1\s*&[^}]*\}\s*;?\s*\1")


def check_raw_patterns(raw):
    if ":(){" in raw.replace(" ", "") or FORK_BOMB.search(raw):
        return "deny", "Fork bomb pattern."
    if re.search(r"\bsudo\s+su\b|\bsudo\s+-i\b|\bsudo\s+-s\b", raw):
        return "ask", "Opening an interactive root shell."
    if re.search(r"\b(bash|sh|zsh)\s+<\(\s*(curl|wget)\b", raw):
        return "deny", "Executing a script straight from the network (process substitution)."
    if re.search(r"\b(bash|sh|zsh)\s+-c\s+[\"']?\$\(\s*(curl|wget)\b", raw) or re.search(r"\beval\s+[\"']?\$\(\s*(curl|wget)\b", raw):
        return "deny", "Executing a script straight from the network (command substitution)."
    return "allow", ""


def check_simple(cmd, cwd, config):
    """Evaluate one SimpleCommand. Returns (decision, reason)."""
    argv, via_xargs = unwrap(cmd.argv())

    # redirections into secrets / protected paths / devices
    for op, target in cmd.redirects:
        if target is None or op in ("<", "<<", "<<<", "<&"):
            continue
        t = target.text
        if t.startswith("/dev/sd") or t.startswith("/dev/nvme") or t.startswith("/dev/hd") or t.startswith("/dev/disk") or t.startswith("/dev/mmcblk") or t.startswith("/dev/vd") or t.startswith("/dev/xvd"):
            return "deny", "Writing directly to a block device (%s)." % t
        if t in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty") or t.isdigit():
            continue
        resolved = normalize_path(t, cwd)
        verdict, detail = classify_path(resolved, cwd, config, _extra_roots(config))
        if verdict == "secret":
            return "deny", "Shell redirection into a secrets file (%s: %s)." % (t, detail)
        if verdict == "protected":
            return "deny", "Shell redirection into a protected path (%s: %s)." % (t, detail)
        if verdict == "outside":
            return "ask", "Shell redirection writes %s, which is %s." % (t, detail)
        if verdict == "ask":
            return "ask", "Shell redirection writes %s (%s)." % (t, detail)

    if not argv:
        return "allow", ""
    name = os.path.basename(argv[0])

    if name in ("rm", "shred", "unlink"):
        if name == "unlink":
            argv = ["rm"] + argv[1:]
        return check_rm(argv, via_xargs, cwd, config)

    if name == "find":
        return check_find(argv, cwd, config)

    if name == "git":
        return check_git(argv, cwd, config)

    if name in ("mkfs", "mke2fs", "mkswap", "wipefs", "fdisk", "sfdisk", "parted") or name.startswith("mkfs."):
        return "deny", "%s formats or repartitions a disk." % name

    if name == "dd":
        for a in argv[1:]:
            if a.startswith("of=") and a[3:].startswith("/dev/") and a[3:] not in ("/dev/null",):
                return "deny", "dd writing to a device (%s)." % a[3:]
        return "allow", ""

    if name in ("chmod", "chown", "chgrp"):
        letters, longs, args = _short_flags(argv)
        recursive = "R" in letters or "--recursive" in longs
        mode = args[0] if args else ""
        targets = args[1:]
        if name == "chmod" and recursive and (mode in ("777", "0777", "a+rwx", "ugo+rwx", "-R") or mode.endswith("777")):
            return "deny", "chmod -R 777 makes every file world-writable."
        if recursive:
            for t in targets:
                why = _danger_target(t, cwd, config)
                if why:
                    return "deny", "%s -R on %s: %s." % (name, t, why)
        return "allow", ""

    if name in ("shutdown", "reboot", "halt", "poweroff"):
        return "deny", "%s would take the machine down." % name
    if name == "init" and argv[1:2] in (["0"], ["6"]):
        return "deny", "init %s would take the machine down." % argv[1]
    if name in ("kill", "pkill", "killall"):
        if name == "kill" and "-1" in argv[1:]:
            return "deny", "kill -1 signals every process you own."
        if name == "killall" and any(a in ("-9", "-KILL") for a in argv) and len(argv) <= 2:
            return "ask", "killall without a process name."
        return "allow", ""
    if name == "crontab" and "-r" in argv[1:]:
        return "deny", "crontab -r deletes every scheduled job."

    if name == "dropdb":
        return "deny", "dropdb deletes a whole database."

    if name in ("mv", "cp", "tee", "install", "ln", "truncate", "sed", "perl", "rsync"):
        # destination writes into secrets / protected paths
        candidates = []
        if name in ("mv", "cp", "install", "ln", "rsync"):
            positional = [a for a in argv[1:] if not a.startswith("-")]
            if positional:
                candidates = [positional[-1]]
            if name == "mv" and positional and positional[-1] == "/dev/null":
                return "deny", "mv to /dev/null destroys the source."
            if name == "rsync" and ("--delete" in argv):
                for p in positional[-1:]:
                    why = _danger_target(p, cwd, config)
                    if why:
                        return "deny", "rsync --delete into %s: %s." % (p, why)
        elif name == "tee":
            candidates = [a for a in argv[1:] if not a.startswith("-")]
        elif name == "truncate":
            candidates = [a for a in argv[1:] if not a.startswith("-") and not re.match(r"^\d", a)]
        elif name in ("sed", "perl"):
            inplace = any(a == "-i" or a.startswith("-i") or a == "--in-place" for a in argv[1:])
            if inplace:
                candidates = [a for a in argv[2:] if not a.startswith("-")][1:] if name == "sed" else [a for a in argv[1:] if not a.startswith("-")][1:]
        for c in candidates:
            if c in ("-", "/dev/null"):
                continue
            resolved = normalize_path(c, cwd)
            verdict, detail = classify_path(resolved, cwd, config)
            if verdict == "secret":
                return "deny", "%s writes a secrets file (%s: %s)." % (name, c, detail)
            if verdict == "protected":
                return "deny", "%s writes a protected path (%s: %s)." % (name, c, detail)
            if verdict == "ask":
                return "ask", "%s writes %s (%s)." % (name, c, detail)
        if name == "mv":
            positional = [a for a in argv[1:] if not a.startswith("-")]
            for src in positional[:-1]:
                resolved = normalize_path(src, cwd)
                verdict, detail = classify_path(resolved, cwd, config)
                if verdict in ("protected", "secret"):
                    return "deny", "mv moves %s away (%s)." % (src, detail)
        return "allow", ""

    # curl | sh style pipelines, judged at the interpreter end
    if name in INTERPRETERS or name in ("sudo",):
        prev = cmd.piped_from
        while prev is not None:
            pargv, _ = unwrap(prev.argv())
            if pargv and os.path.basename(pargv[0]) in FETCHERS:
                return "deny", "Piping %s output into %s executes remote code without review." % (os.path.basename(pargv[0]), name)
            prev = prev.piped_from
        # bash -c "..." : recurse into the inline script
        if name in SHELLS and "-c" in argv[1:]:
            idx = argv.index("-c")
            if idx + 1 < len(argv):
                return evaluate_command(argv[idx + 1], cwd, config, _nested=True)
        return "allow", ""

    if name == "eval" and len(argv) > 1:
        return evaluate_command(" ".join(argv[1:]), cwd, config, _nested=True)

    return "allow", ""


def _matches_list(patterns, raw, simple_texts):
    for pat in patterns:
        pat = pat.strip()
        if not pat:
            continue
        if fnmatch.fnmatchcase(raw.strip(), pat):
            return pat
        for t in simple_texts:
            if fnmatch.fnmatchcase(t, pat):
                return pat
    return None


def evaluate_command(command, cwd, config, _nested=False):
    """Return (decision, reason) for a Bash tool command string."""
    raw = command or ""
    cwd = os.path.normpath(cwd or os.getcwd())
    tokens = tokenize(raw)
    commands = split_commands(tokens)
    simple_texts = [c.text() for c in commands]

    if not _nested:
        hit = _matches_list(config.get("block_commands", []), raw, simple_texts)
        if hit:
            return "deny", "Guardrails blocked: command matches block_commands pattern %r in .claude/guardrails.json. %s" % (hit, HOOK_TIMEOUT_NOTE)
        hit = _matches_list(config.get("allow_commands", []), raw, simple_texts)
        if hit:
            return "allow", "matches allow_commands %r" % hit

    d, why = check_raw_patterns(raw)
    if d != "allow":
        return d, _format(d, why)

    # SQL passed through a database client (inline, -c/-e, or a heredoc)
    names = set()
    for c in commands:
        a, _ = unwrap(c.argv())
        if a:
            names.add(os.path.basename(a[0]))
    if names & DB_CLIENTS:
        d, why = check_sql_text(raw)
        if d != "allow":
            return d, _format(d, why)

    worst = ("allow", "")
    for cmd in commands:
        d, why = check_simple(cmd, cwd, config)
        if d == "deny":
            return "deny", _format("deny", why)
        if d == "ask" and worst[0] == "allow":
            worst = ("ask", _format("ask", why))
    if worst[0] == "allow" and not commands:
        return "allow", ""
    return worst


def _format(decision, why):
    if why.startswith("Guardrails"):
        return why
    if decision == "deny":
        return "Guardrails blocked: %s %s" % (why, HOOK_TIMEOUT_NOTE)
    return "Guardrails asks for approval: %s" % why
