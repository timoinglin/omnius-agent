#!/usr/bin/env python3
"""Give every desk the same allow-list, from ONE definition.

    python tools\\discord\\sync_permissions.py            # apply
    python tools\\discord\\sync_permissions.py --check    # exit 1 if any desk is short

Why this exists (2026-08-13). A desk that hits a tool nobody allow-listed stops
and asks. Over Discord that is a question on a screen he may not be looking at,
which is the failure this whole posture exists to prevent - so the allow-list is
not a convenience, it is the thing keeping desks moving.

Kept by hand, it drifted immediately and invisibly:

- The widening of 2026-08-12 edited the seven settings files that are TRACKED.
  `projects\\*` is gitignored, so every project desk on this machine kept the
  original eleven entries and none of the ten added. Nobody could see it,
  because the files that were fixed and the files that were not look identical
  from the repo.
- `Artifact` was in NO list anywhere. It surfaced 2026-08-13 when a project desk
  publishing a report stopped mid-answer to ask - exactly the interruption the
  posture is meant to remove.

So the list lives here once, and this script is the only writer. Anything the
list does not cover still escalates through the PermissionRequest relay and
becomes an ok/no in Discord - the fence is the deny list, not omission.

What is deliberately NOT here: `--dangerously-skip-permissions`. It would end
the whole class of problem, and it also switches OFF the deny list, including
the `Read(./.env)` fence he explicitly kept. That is his call to make, not a
script's - see `memory\\orchestrator\\topics\\permissions.md`.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# One definition. Order is meaningful only to a human reading the file.
ALLOW = [
    # --- files and search ---
    "Read", "Edit", "Write", "Glob", "Grep", "LS", "NotebookEdit",
    # --- shell ---
    "Bash", "PowerShell", "BashOutput", "KillShell",
    # --- web ---
    "WebFetch", "WebSearch",
    # --- delegation and skills ---
    "Task", "Agent", "Skill", "SlashCommand", "Workflow", "TodoWrite",
    "ToolSearch", "ListSkills", "SearchSkills",
    # --- talking to him: output surfaces a desk uses to ANSWER. A desk that
    # must ask permission to publish its own reply is the case that started
    # this file (Artifact, 2026-08-13).
    "Artifact", "SendUserFile", "ReportFindings",
    "SendMessage", "PushNotification",
    # --- background work and scheduling ---
    "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate",
    "CronCreate", "CronDelete", "CronList", "Monitor",
    # --- planning ---
    "EnterPlanMode", "ExitPlanMode",
    # --- MCP servers that ship with Claude Code itself, allowed whole.
    # Servers YOU connect are instance-specific and belong in LEARNED below,
    # not here: their names say what this install is wired to, and this file
    # travels into every fresh release.
    "mcp__claude-in-chrome", "mcp__computer-use", "mcp__Claude_Browser",
    "mcp__visualize", "mcp__ccd_session", "mcp__ccd_session_mgmt",
    "mcp__mcp-registry", "mcp__scheduled-tasks",
]

# Learned entries: written when he answers "ok" to a permission request, so a
# tool asks ONCE and never again, on any desk (his decision 2026-08-13). Lives
# in config\ because it is per-install - a new MCP server or a tool a newer
# Claude Code added. config\* is gitignored, so it travels in his backup zip
# and never reaches a release. See config\README.md.
# DENIED outright, and it is not about danger - it is about ANSWERABILITY.
# AskUserQuestion draws a menu in the desk's terminal and waits for a keypress.
# No desk terminal has a human in front of it, and the bus can only type into a
# pty while no turn is running - so the widget blocks the very turn that would
# have to end for anyone to reach it. 2026-08-13: a project desk asked which
# of two browsers to drive, he answered "ok" in Discord (which only allowed it
# to ASK), and the desk sat there. Merely leaving it off the allow-list is not
# enough - that prompts, he says ok, and it hangs exactly the same way. A deny
# makes the tool fail, and the desk then asks in plain text, which he CAN answer.
DENY = ["AskUserQuestion"]

LEARNED = ROOT / "config" / "allow-learned.json"


def learned():
    """-> [tool names] he has approved before. Never raises: an unreadable or
    hand-mangled file means "nothing learned yet", never "allow everything"."""
    try:
        data = json.loads(LEARNED.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [t for t in data if isinstance(t, str) and t.strip()]


def learn(tool):
    """Remember that he allowed `tool`. -> True if this is new.

    Only ever ADDS, and only a plain tool name: a permission request carries
    the tool, and a tool name is not a path or an argument, so there is nothing
    here that could widen into "allow this specific dangerous command". The
    deny list is untouched by design - Read(./.env) survives every approval.
    """
    tool = str(tool or "").strip()
    if not tool or tool in ALLOW or tool in learned():
        return False
    # Defensive: a permission request is machine-written, but this file feeds an
    # allow-list, so refuse anything that is not a bare identifier-ish name.
    if not re.fullmatch(r"[A-Za-z][\w.-]{0,63}", tool):
        return False
    current = learned() + [tool]
    LEARNED.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEARNED.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(set(current)), indent=2) + "\n", encoding="utf-8")
    tmp.replace(LEARNED)
    return True


def effective_allow():
    """The shared list plus everything he has approved since."""
    out = list(ALLOW)
    out += [t for t in learned() if t not in out]
    return out


def settings_files():
    """Every desk's settings.json. Missing trees are skipped, not an error."""
    out = [ROOT / ".claude" / "settings.json",
           ROOT / "templates" / "project" / ".claude" / "settings.json",
           ROOT / "daybook" / ".claude" / "settings.json"]
    for base in (ROOT / "projects", ROOT / "tools"):
        if base.is_dir():
            # A project may put a desk in a component subfolder, so look one
            # level deeper too - that is where the 2026-08-12 widening never
            # reached: project desks live in gitignored folders, so a widening
            # committed to the repo did not touch a single one of them.
            out += sorted(base.glob("*/.claude/settings.json"))
            out += sorted(base.glob("*/*/.claude/settings.json"))
    return [p for p in out if p.is_file()]


def _merge_local(p, learned_entries):
    """Put LEARNED entries in settings.local.json beside settings.json.

    -> True if the local file changed. Claude Code merges both files, so the
    desk sees the union - but only settings.json is tracked. Learned entries
    used to be written straight into it, which put the names of his MCP
    servers into files that ship (found 2026-08-14: an integration's name had
    been stamped into every tracked settings.json in the repo). settings.local
    .json is already gitignored fleet-wide, which is the entire point.
    """
    lp = p.with_name("settings.local.json")
    try:
        local = json.loads(lp.read_text(encoding="utf-8")) if lp.is_file() else {}
    except (OSError, json.JSONDecodeError):
        local = {}
    lperms = local.setdefault("permissions", {})
    lhave = lperms.get("allow") or []
    add = [t for t in learned_entries if t not in lhave]
    if not add:
        return False
    lperms["allow"] = lhave + add
    lp.write_text(json.dumps(local, indent=2, ensure_ascii=False) + "\n",
                  encoding="utf-8")
    return True


def main():
    check_only = "--check" in sys.argv
    extra = learned()
    short, changed = [], 0
    for p in settings_files():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  SKIP  {p.relative_to(ROOT)}: {type(e).__name__}: {e}")
            continue
        perms = data.setdefault("permissions", {})
        have = perms.get("allow") or []
        held = perms.get("deny") or []
        missing = [t for t in ALLOW if t not in have]
        # A tool cannot be both: an entry we now deny must leave the allow list,
        # or the file says two things and which one wins is the reader's guess.
        stale = [t for t in have if t in DENY]
        # Learned entries MIGRATE out of the tracked file into settings.local
        # .json - they name his integrations, and this file ships.
        misplaced = [t for t in have if t in extra]
        undenied = [t for t in DENY if t not in held]
        lp = p.with_name("settings.local.json")
        try:
            lhave = (json.loads(lp.read_text(encoding="utf-8"))
                     .get("permissions", {}).get("allow") or []) if lp.is_file() else []
        except (OSError, json.JSONDecodeError):
            lhave = []
        local_missing = [t for t in extra if t not in lhave]
        if not (missing or stale or undenied or misplaced or local_missing):
            continue
        short.append((p, missing + local_missing))
        if check_only:
            continue
        # Additive on purpose: a desk may have earned an entry of its own
        # (a project-specific MCP server), and this script must not eat it.
        perms["allow"] = [t for t in have if t not in DENY and t not in extra] + missing
        if undenied:
            perms["deny"] = held + undenied
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8")
        _merge_local(p, extra)
        changed += 1
        print(f"  +{len(missing):<3} {p.relative_to(ROOT)}"
              + (f" (+{len(local_missing)} learned -> settings.local.json)"
                 if local_missing or misplaced else ""))

    if check_only:
        for p, missing in short:
            print(f"  SHORT {p.relative_to(ROOT)}: missing {len(missing)} "
                  f"({', '.join(missing[:4])}{'...' if len(missing) > 4 else ''})")
        print(f"permissions: {len(short)} desk(s) short of the shared allow-list"
              if short else
              f"permissions: all desks carry the full {len(ALLOW)}-entry allow-list"
              + (f" (+{len(extra)} learned, in settings.local.json)" if extra else ""))
        return 1 if short else 0
    print(f"permissions: {changed} settings file(s) updated"
          if changed else
          f"permissions: already in sync ({len(ALLOW)} entries everywhere"
          + (f", +{len(extra)} learned locally)" if extra else ")"))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
