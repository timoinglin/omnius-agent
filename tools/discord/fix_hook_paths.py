#!/usr/bin/env python3
"""Write THIS machine's hooks into every desk's settings.local.json.

    python tools\\discord\\fix_hook_paths.py [--check]

Two rules. The first one is old; the second one is why this file was rewritten
on 2026-08-14, the day the workspace became a public repo.

**1. A hook path must be ABSOLUTE.** Hook commands used to be written as
`${CLAUDE_PROJECT_DIR}/../../tools/discord/x.py`, with the number of `..`
chosen for that settings file's depth. That cannot be made correct, because ONE
settings file is loaded by sessions at DIFFERENT depths: `--add-dir <root>`
pulls the root's settings into a desk session whose CLAUDE_PROJECT_DIR is its
own folder, so the root's paths resolve against `daybook\\`,
`projects\\<p>\\<c>\\`, and so on. The failure is silent and total - python
exits 2, a failing UserPromptSubmit hook BLOCKS the prompt, and the desk cannot
even run `/omnius`. It cost the owner a morning of window loops (2026-08-02).

**2. An absolute path must NEVER BE COMMITTED.** Rule 1 was solved by writing
the real path into the tracked `.claude\\settings.json` files - which published
one machine's home directory in six of them the moment the workspace was pushed
to GitHub. Anyone cloning it inherited a path that does not exist on their PC,
and every prompt they typed died with `UserPromptSubmit operation blocked by
hook`. A machine-specific value in a tracked file is not a leak that can be
tidied up once: `sync_permissions` and this script both write those files, so
the next commit puts it straight back.

So hooks live in **`.claude\\settings.local.json`** - generated here, ignored by
git (`.gitignore`: `**/.claude/settings.local.json`) and excluded from release
zips (`pack.ps1`) - and the tracked `settings.json` carries permissions only.
That is the split `sync_permissions.py` already made on 2026-08-14 for learned
allow-entries, for the same reason: the tracked file is the PRODUCT, the local
file is THIS INSTALL.

Measured before choosing it, with real `claude -p` runs rather than by reading
the docs (2026-08-14):

- a `.claude\\settings.local.json` with **no settings.json beside it** does
  register its hooks, and they fire;
- they still fire when the desk is launched with `--settings <other file>`,
  which is exactly how the watchdog starts a project component;
- a hook pointing at a file that is not there **blocks the prompt outright**.
  So shipping a `<OMNIUS_ROOT>` placeholder in a tracked hooks block would have
  been the same bug wearing a different name - it only fails later, on someone
  else's machine.

Idempotent, and every entry point runs it: `install.ps1`, `autostart.ps1
-Action install|repair`, and `fleet_ops.py` after stamping a project. A
workspace that moves is repaired before anything starts.

`--check` exits 1 if any desk is missing a hook, points at a script that is not
there, or still carries a `hooks` block in its tracked `settings.json`.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DISCORD = ROOT / "tools" / "discord"

# The one definition of what a desk's hooks are. Same shape in every desk: a
# hook that fires on one desk and not another is the kind of asymmetry nobody
# notices until a desk goes quiet.
#
#   (event, script, timeout seconds, statusMessage, matcher)
#
# PermissionRequest gets 620s because the owner has to see the ask in Discord
# and answer it from wherever he is; the others only move files or read one
# string and must never delay a turn.
#
# PreToolUse/secret_guard.py is the .env fence (2026-09-02). The deny lists in
# settings.json only understand FILE rules, and Bash/PowerShell are allow-listed
# fleet-wide, so `type .env` walked straight through the fence those files
# advertise. The matcher names the tools that can carry a path or a command;
# everything else is not even asked.
HOOK_SPEC = (
    ("PermissionRequest", DISCORD / "permission_relay.py", 620,
     "asking the owner in Discord...", None),
    ("Stop", DISCORD / "turn_end_hook.py", 10, None, None),
    ("UserPromptSubmit", DISCORD / "turn_start_hook.py", 10, None, None),
    ("PreToolUse", DISCORD / "secret_guard.py", 10, None,
     "Bash|PowerShell|Read|Grep|Glob|Edit|Write|MultiEdit|NotebookEdit"),
    # PostToolUse/mail_notice_hook.py lets a RUNNING turn learn that mail
    # arrived (2026-09-03). Nothing else could tell it: the watchdog will not
    # start a --continue run against a live turn, and the bridge's nudge is
    # keystrokes that only land when the CLI reads again - so an envelope
    # arriving one minute into a twenty-minute turn waited for all of it.
    # PostToolUse, not Pre: a Pre hook can block the call it precedes, and a
    # lost notice is a far better worst case than a wedged turn.
    ("PostToolUse", DISCORD / "mail_notice_hook.py", 10, None, None),
)


def hooks_block():
    """-> the hooks mapping for THIS machine, absolute paths and all."""
    out = {}
    for event, script, timeout, status, matcher in HOOK_SPEC:
        hook = {"type": "command", "command": f'python "{script}"', "timeout": timeout}
        if status:
            hook["statusMessage"] = status
        entry = {"hooks": [hook]}
        if matcher:
            entry["matcher"] = matcher
        out.setdefault(event, []).append(entry)
    return out


def missing_scripts():
    """-> hook scripts this checkout does not actually have. Should be empty;
    a non-empty answer means a broken checkout, not a broken machine."""
    return [s for _e, s, _t, _m, _x in HOOK_SPEC if not s.is_file()]


def desk_dirs():
    """Every folder a desk RUNS in, and therefore needs hooks in.

    A desk is a cwd, not a settings file - which is the distinction that
    matters for project components: the watchdog starts them in
    `projects\\<p>\\<c>` and passes the PROJECT's settings.json with
    `--settings`, so the hooks have to come from the component's own folder.
    Verified live 2026-08-14: a cwd-local settings.local.json fires its hooks
    even with `--settings` pointing somewhere else entirely.

    `tools\\<x>` counts only when it already has a `.claude` folder - that is
    what makes a tool a desk rather than a library, and it keeps
    `tools\\playwright` and friends free of files they would never read.
    `templates\\` is never a desk: it is a skeleton that gets copied, and
    fleet_ops re-runs this script on the copy.
    """
    out = [ROOT, ROOT / "daybook"]
    tools = ROOT / "tools"
    if tools.is_dir():
        out += [d for d in sorted(tools.iterdir())
                if d.is_dir() and (d / ".claude").is_dir()]
    projects = ROOT / "projects"
    if projects.is_dir():
        for p in sorted(projects.iterdir()):
            if not p.is_dir() or p.name.startswith((".", "_")):
                continue
            if (p / ".claude").is_dir():
                out.append(p)          # a terminal opened at the project root
            out += [c for c in sorted(p.iterdir())
                    if c.is_dir() and not c.name.startswith((".", "_", "memory"))]
    return [d for d in out if d.is_dir()]


def tracked_settings():
    """Every settings.json that git tracks (or a stamped copy of one).

    These must never carry hooks: they travel between machines, so an absolute
    path in one of them is a path that is wrong everywhere except here.
    """
    out = [ROOT / ".claude" / "settings.json",
           ROOT / "templates" / "project" / ".claude" / "settings.json",
           ROOT / "daybook" / ".claude" / "settings.json"]
    for base in (ROOT / "tools", ROOT / "projects"):
        if base.is_dir():
            out += sorted(base.glob("*/.claude/settings.json"))
            out += sorted(base.glob("*/*/.claude/settings.json"))
    return [p for p in out if p.is_file()]


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def local_for(desk_dir):
    return desk_dir / ".claude" / "settings.local.json"


def ensure_hooks(desk_dir):
    """Give this desk the current hooks. -> True if the file changed.

    Merges: the local file also holds learned allow-entries
    (sync_permissions.py), and eating those would re-open every permission
    question the owner has already answered.
    """
    path = local_for(desk_dir)
    data = read_json(path)
    want = hooks_block()
    if data.get("hooks") == want:
        return False
    data["hooks"] = want
    write_json(path, data)
    return True


def strip_hooks(settings_json):
    """Take hooks out of a tracked settings.json. -> True if it changed.

    Migration AND guard: an instance installed before 2026-08-14 has them, and
    so does anyone who pulls a settings.json that was committed with them.
    """
    data = read_json(settings_json)
    if "hooks" not in data:
        return False
    data.pop("hooks")
    write_json(settings_json, data)
    return True


def problems():
    """-> list of human-readable reasons this instance is not correctly wired."""
    out = [f"hook script missing from this checkout: {s}" for s in missing_scripts()]
    want = hooks_block()
    for d in desk_dirs():
        have = read_json(local_for(d)).get("hooks")
        if not have:
            out.append(f"{_rel(d)}: no hooks in .claude\\settings.local.json")
            continue
        for event, blocks in want.items():
            if have.get(event) != blocks:
                out.append(f"{_rel(d)}: {event} hook missing or not this machine's path")
    for p in tracked_settings():
        if "hooks" in read_json(p):
            out.append(f"{_rel(p)}: tracked settings.json still carries hooks "
                       f"(they belong in settings.local.json - never commit a path)")
    return out


def _rel(path):
    try:
        return str(path.relative_to(ROOT)) or "."
    except ValueError:
        return str(path)


def main(argv):
    if "--check" in argv:
        bad = problems()
        for line in bad:
            print(f"  PROBLEM {line}", file=sys.stderr)
        print(f"{len(bad)} problem(s)" if bad else
              f"hooks: all {len(desk_dirs())} desk(s) wired to this machine, "
              f"no tracked settings.json carries a path")
        return 1 if bad else 0

    gone = [_rel(p) for p in tracked_settings() if strip_hooks(p)]
    wired = [_rel(d) for d in desk_dirs() if ensure_hooks(d)]
    for line in gone:
        print(f"  moved out  {line} (hooks -> settings.local.json)")
    for line in wired:
        print(f"  wired      {line}")
    missing = missing_scripts()
    for s in missing:
        print(f"  PROBLEM hook script missing from this checkout: {s}", file=sys.stderr)
    print(f"hooks: {len(wired)} desk(s) wired to this machine"
          + (f", {len(gone)} settings.json cleaned" if gone else ""))
    return 1 if missing else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
