#!/usr/bin/env python3
"""Shared helpers for the guardrails plugin hooks.

Standard library only. Everything here must be safe to import from a hook
that has to answer in well under a second.
"""

import fnmatch
import json
import os
import sys

HOOK_TIMEOUT_NOTE = (
    "To allow once, run the command yourself in your terminal. "
    "To allow always, add it to .claude/guardrails.json allow_commands."
)
PATH_NOTE = (
    "To allow once, make the change yourself. "
    "To allow always, add the path to .claude/guardrails.json allow_paths."
)

DEFAULT_CONFIG = {
    # Commands. Matched with shell-style globs (fnmatch) against the full
    # command string and against each simple command inside it.
    "allow_commands": [],
    "block_commands": [],
    # Paths that may never be edited, written, or removed by Claude.
    "protected_paths": [".git/**"],
    # Paths that need the user's approval (PreToolUse "ask").
    "ask_paths": [
        "Dockerfile",
        "Dockerfile.*",
        "*.dockerfile",
        "docker-compose*.yml",
        "docker-compose*.yaml",
        "compose*.yml",
        "compose*.yaml",
        ".github/workflows/**",
        ".gitlab-ci.yml",
        "terraform/**",
        "*.tf",
        "*.tfvars",
        "k8s/**",
        "kubernetes/**",
        "helm/**",
        "migrations/**",
        "**/migrations/**",
    ],
    # Files that hold credentials. Blocked for edit, write and shell writes.
    "secrets_globs": [
        ".env",
        ".env.*",
        "*.pem",
        "*.key",
        "*.p12",
        "*.pfx",
        "*.jks",
        "*.keystore",
        "id_rsa*",
        "id_dsa*",
        "id_ecdsa*",
        "id_ed25519*",
        "credentials.json",
        "credentials",
        "service-account*.json",
        "secrets/**",
        ".secrets/**",
        "*.secret",
        ".netrc",
        ".htpasswd",
        ".aws/credentials",
        ".npmrc",
        ".pypirc",
    ],
    # Exemptions from the lists above, checked first.
    "allow_paths": [
        ".env.example",
        ".env.sample",
        ".env.template",
        ".env.dist",
        "*.example",
        "*.sample",
        "*.template",
        "*.pub",
    ],
}

CONFIG_KEYS = list(DEFAULT_CONFIG.keys())


def log(msg):
    """Write to stderr. Never raises."""
    try:
        sys.stderr.write(msg.rstrip("\n") + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def read_hook_input():
    """Read the JSON document Claude Code pipes to a hook on stdin."""
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def home_dir():
    return os.path.expanduser("~")


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, None
    except Exception as exc:  # invalid JSON, permissions, ...
        return None, "%s: %s" % (path, exc)


def load_config(cwd):
    """Merge defaults, ~/.claude/guardrails.json and <cwd>/.claude/guardrails.json.

    Lists are concatenated (user config extends the defaults). Returns
    (config, list_of_warnings, list_of_loaded_files).
    """
    config = {k: list(v) for k, v in DEFAULT_CONFIG.items()}
    warnings = []
    loaded = []
    candidates = [
        os.path.join(home_dir(), ".claude", "guardrails.json"),
        os.path.join(cwd, ".claude", "guardrails.json"),
    ]
    for path in candidates:
        data, err = _read_json(path)
        if err:
            warnings.append("guardrails config not loaded, " + err)
            continue
        if data is None:
            continue
        if not isinstance(data, dict):
            warnings.append("%s: top level must be a JSON object" % path)
            continue
        loaded.append(path)
        for key, value in data.items():
            if key not in CONFIG_KEYS:
                warnings.append("%s: unknown key %r ignored" % (path, key))
                continue
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                warnings.append("%s: %r must be a list of strings" % (path, key))
                continue
            config[key].extend(value)
    return config, warnings, loaded


# --------------------------------------------------------------------------
# Path helpers
# --------------------------------------------------------------------------

def normalize_path(path, cwd):
    """Expand ~ and $HOME, make absolute against cwd, normalize. No symlink
    resolution (the file may not exist yet)."""
    if path is None:
        return None
    p = path.strip()
    if p in ("~",) or p.startswith("~/"):
        p = home_dir() + p[1:]
    for var in ("${HOME}", "$HOME"):
        if p.startswith(var):
            p = home_dir() + p[len(var):]
    for var in ("${PWD}", "$PWD"):
        if p.startswith(var):
            p = cwd + p[len(var):]
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return os.path.normpath(p)


def is_within(path, root):
    """True if path is root or inside root (both normalized absolute paths)."""
    path = os.path.normpath(path)
    root = os.path.normpath(root)
    if path == root:
        return True
    return path.startswith(root.rstrip(os.sep) + os.sep)


def relative_to(path, root):
    """Relative path if inside root, else None."""
    if not is_within(path, root):
        return None
    rel = os.path.relpath(path, root)
    return "" if rel == "." else rel.replace(os.sep, "/")


def glob_match(pattern, abs_path, cwd):
    """Match one config glob against a path.

    A pattern without a slash matches the file's basename anywhere. A pattern
    with a slash matches the path relative to the project root (or the
    absolute path, for absolute patterns). fnmatch's ``*`` crosses ``/`` so
    ``secrets/**`` and ``secrets/*`` both match ``secrets/a/b``.
    """
    pattern = pattern.strip()
    if not pattern:
        return False
    base = os.path.basename(abs_path.rstrip("/"))
    if pattern.startswith("~"):
        pattern = home_dir() + pattern[1:]
    if os.path.isabs(pattern):
        return fnmatch.fnmatch(abs_path, pattern) or fnmatch.fnmatch(abs_path, pattern.rstrip("/") + "/*")
    if "/" not in pattern:
        return fnmatch.fnmatch(base, pattern)
    rel = relative_to(abs_path, cwd)
    if rel is None:
        return False
    if fnmatch.fnmatch(rel, pattern):
        return True
    # "dir/**" should also match "dir" itself and "dir/x"
    if pattern.endswith("/**"):
        stem = pattern[:-3]
        if fnmatch.fnmatch(rel, stem) or fnmatch.fnmatch(rel, stem + "/*"):
            return True
    if pattern.startswith("**/"):
        return fnmatch.fnmatch(rel, pattern[3:]) or fnmatch.fnmatch(rel, pattern)
    return False


def match_any(patterns, abs_path, cwd):
    for pat in patterns:
        if glob_match(pat, abs_path, cwd):
            return pat
    return None


def classify_path(abs_path, cwd, config, extra_allowed_roots=()):
    """Classify a filesystem path for write/edit/delete purposes.

    Returns (verdict, detail) where verdict is one of
    'allow', 'secret', 'protected', 'ask', 'outside'.
    """
    allowed = match_any(config["allow_paths"], abs_path, cwd)
    if allowed:
        return "allow", "matches allow_paths %r" % allowed
    secret = match_any(config["secrets_globs"], abs_path, cwd)
    if secret:
        return "secret", "matches secrets_globs %r" % secret
    protected = match_any(config["protected_paths"], abs_path, cwd)
    if protected:
        return "protected", "matches protected_paths %r" % protected
    if not is_within(abs_path, cwd):
        for root in extra_allowed_roots:
            if root and is_within(abs_path, root):
                return "allow", "inside %s" % root
        return "outside", "outside the project directory %s" % cwd
    asked = match_any(config["ask_paths"], abs_path, cwd)
    if asked:
        return "ask", "matches ask_paths %r" % asked
    return "allow", ""


# --------------------------------------------------------------------------
# Hook output
# --------------------------------------------------------------------------

def emit_pretooluse(decision, reason, system_message=None):
    """Print the documented PreToolUse JSON and exit 0.

    decision: 'allow', 'deny' or 'ask'. A deny is never silent: the reason
    is also shown to the user through systemMessage and echoed to stderr.
    """
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }
    if decision in ("deny", "ask"):
        out["systemMessage"] = system_message or reason
        log(reason)
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()
    sys.exit(0)
