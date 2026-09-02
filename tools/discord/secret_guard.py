#!/usr/bin/env python3
"""PreToolUse hook: make the .env fence real instead of decorative.

Claude Code's deny list only understands FILE rules - `Read(./.env)` and
friends. Bash and PowerShell are allow-listed fleet-wide (they have to be: a
desk that stops to ask for a shell is a desk stopped on a screen nobody is
watching), and a shell reads any file on the machine. So every one of these
walked straight through the fence the settings files advertise:

    type .env
    cat ../.env
    python -c "open('.env')"
    git show HEAD:.env
    Grep --glob .env

This hook closes that. It runs BEFORE the tool, reads the tool-call JSON on
stdin, and exits 2 - which Claude Code treats as "blocked, tell the model why" -
when the call names one of the three things this workspace protects:

    .env (any path shape, any `.env.<something>` variant)  - tokens, mail
        passwords, API keys. `.env.example` is the template and is allowed.
    state\\web\\                                            - saved browser
        sessions: real logged-in cookies for his accounts.
    audit-sentinels                                        - the canary strings
        pack.ps1 hunts for; reading them teaches a desk how to dodge the audit.

WHAT IT DOES NOT DO, on purpose. It reads the COMMAND and the PATHS, never file
CONTENT or a Grep pattern. Writing a document that mentions `.env` is ordinary
work and must not be blocked - the rule this whole posture is built on is that
routine work never prompts. A guard with false positives gets switched off, and
then there is no guard at all.

This is a fence, not containment: anything with a shell can obfuscate a path
past a regex. The containment is that only the owner and people he invites can
talk to a desk. The fence is here so a desk does not reach for the token file by
habit, and so an outsider talking to a desk cannot ask for it in plain words.

Stamped onto every desk by tools\\discord\\fix_hook_paths.py, like the other
hooks - absolute path, in the gitignored settings.local.json.
"""
import json
import re
import sys

# `.env`, `.env.local`, `.envrc`, but NOT `.environment` (the trailing lookahead
# rejects it) and NOT `.env.example` (filtered by name below - it is the shipped
# template and every install is told to copy it).
ENV_RE = re.compile(r"\.env(?:rc)?(?:\.[A-Za-z0-9_-]+)?(?![A-Za-z0-9_-])")
WEB_RE = re.compile(r"state[\\/]+web(?![A-Za-z0-9_-])", re.I)
SENTINEL_RE = re.compile(r"audit-sentinels", re.I)

# Only these carry a path or a command. `content`, `new_string` and a Grep
# `pattern` are deliberately absent - see the module docstring.
FIELDS = ("command", "file_path", "path", "notebook_path", "glob", "file_paths")

# A commit message is prose about the work, and the work is often about the
# fence itself ("the .env deny was decorative"). The first commit after this
# guard shipped was blocked by its own message. For `git commit` only, the
# message bodies are stripped before scanning: PowerShell here-strings, bash
# heredocs and -m/--message arguments. Everything else in the command still
# counts, so `git commit -- .env` is not what this exempts.
_COMMIT_BODIES = (
    re.compile(r"@'.*?'@", re.S),
    re.compile(r'@".*?"@', re.S),
    re.compile(r"<<-?\s*['\"]?(\w+)['\"]?\n.*?\n\1(?=\s|$)", re.S),
    re.compile(r"(?:-m|--message)(?:\s+|=)(\"(?:[^\"\\]|\\.)*\"|'[^']*')", re.S),
)


def scannable(text):
    """-> the part of a command the fence scans; a commit message is prose."""
    if not re.match(r"\s*git\s+commit\b", text):
        return text
    for rx in _COMMIT_BODIES:
        text = rx.sub(" ", text)
    return text


def reason(text):
    """-> one-line reason this string may not be used, or None."""
    if not text:
        return None
    text = scannable(text)
    for m in ENV_RE.finditer(text):
        if m.group(0).lower().endswith(".example"):
            continue
        return ("blocked: this references .env, which holds the bot token, mail "
                "passwords and API keys. Do not read it. If you genuinely need "
                "one value, say which key and why, in words.")
    if WEB_RE.search(text):
        return ("blocked: state\\web\\ holds saved browser sessions - live "
                "logged-in cookies for his accounts. Not readable from a desk.")
    if SENTINEL_RE.search(text):
        return ("blocked: audit-sentinels are the canary strings the release "
                "audit hunts for. A desk must not read or edit them.")
    return None


def strings(tool_input):
    """-> the command/path strings of a tool call, flattened."""
    out = []
    if isinstance(tool_input, dict):
        for k in FIELDS:
            v = tool_input.get(k)
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, list):
                out += [x for x in v if isinstance(x, str)]
    elif isinstance(tool_input, str):
        out.append(tool_input)
    return out


def verdict(payload):
    """-> reason string, or None to let the call through."""
    if not isinstance(payload, dict):
        return None
    for s in strings(payload.get("tool_input")):
        why = reason(s)
        if why:
            return why
    return None


def main():
    # Never raise. A hook that crashes on malformed input BLOCKS the tool call
    # (exit != 0), and a guard that breaks ordinary work is worse than no guard.
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return 0
    try:
        why = verdict(payload)
    except Exception:
        return 0
    if why:
        print(why, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
