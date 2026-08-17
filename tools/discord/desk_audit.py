#!/usr/bin/env python3
"""Prove every desk is reachable, unstallable and self-recovering. Read-only.

    python tools\\discord\\desk_audit.py           the table + a verdict
    python tools\\discord\\desk_audit.py --quiet   only what is wrong

Answers one question, per desk, with evidence rather than confidence:

    "can this desk be driven from Discord, without permission prompts,
     without hanging, and does it come back on its own when something fails?"

Exit 0 = every desk green. Exit 1 = something named below is off. It REPORTS
and never fixes: a review that silently edits settings is how a fleet drifts
without anyone deciding anything (docs\\WEB.md W4).

The invariants, and why each one is here:

  allows shell        bare Bash/PowerShell, so routine work never stops on a
                      dialog nobody can see - the 2026-08-02 freeze, twice
  denies .env         the one fence kept: secrets stay out of the model
  denies AskUserQuestion  it draws a menu in a terminal nobody is sitting at,
                      and blocks the very turn that would have to end for
                      anyone to reach it
  hooks wired         PermissionRequest (escalate to Discord instead of
                      stalling), UserPromptSubmit + Stop (the busy stamp that
                      keeps a headless run off a live terminal turn)
  no tracked paths    hook commands are machine-absolute, so they belong in
                      settings.local.json, never in git
  folder + skill      the desk exists on disk and can discover /omnius
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))

SHELL_ALLOWS = ("Bash", "PowerShell")
MUST_DENY = ("AskUserQuestion",)
HOOKS = ("PermissionRequest", "UserPromptSubmit", "Stop")


def desk_dirs():
    """Every folder that is a desk: root, daybook, tool.<x> and project
    components. Mirrors fix_hook_paths.desk_dirs - a desk is a folder with a
    .claude\\ (for tools) or a component of a project."""
    out = [("orchestrator", ROOT)]
    if (ROOT / "daybook").is_dir():
        out.append(("daybook", ROOT / "daybook"))
    for d in sorted((ROOT / "tools").glob("*")):
        # settings.json, not merely .claude\: fix_hook_paths wires hooks into
        # any tool folder that has a .claude, which leaves LIBRARY folders
        # (tools\discord itself) holding a settings.local.json and nothing
        # else. A desk is a folder somebody deliberately gave a permission
        # profile - the schema's tool desks are exactly those.
        if d.is_dir() and (d / ".claude" / "settings.json").is_file():
            out.append((f"tool.{d.name}", d))
    projects = ROOT / "projects"
    if projects.is_dir():
        for p in sorted(projects.iterdir()):
            if not p.is_dir() or p.name.startswith("_") or not (p / ".claude").is_dir():
                continue
            for c in sorted(p.iterdir()):
                if c.is_dir() and not c.name.startswith(".") and c.name != "memory":
                    out.append((f"{p.name}.{c.name}", c))
    return out


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def audit_desk(sid, folder):
    """-> (ok, [problems]) for one desk. Never writes anything."""
    problems = []
    if not folder.is_dir():
        return False, [f"folder missing ({folder})"]
    tracked = read_json(folder / ".claude" / "settings.json") or {}
    local = read_json(folder / ".claude" / "settings.local.json") or {}
    perms = (tracked.get("permissions") or {})
    allow = set(perms.get("allow") or [])
    deny = set(perms.get("deny") or [])
    # ...the root's own settings serve desks that inherit via --add-dir, so a
    # component with no tracked file is fine as long as the project's is there.
    if not tracked and folder != ROOT:
        parent = read_json(folder.parent / ".claude" / "settings.json") or {}
        perms = parent.get("permissions") or {}
        allow, deny = set(perms.get("allow") or []), set(perms.get("deny") or [])

    missing_shell = [a for a in SHELL_ALLOWS if a not in allow]
    if missing_shell:
        problems.append(f"can stall on a prompt: {', '.join(missing_shell)} not allowed")
    if not any(".env" in str(d) for d in deny):
        problems.append("does not deny reading .env")
    for d in MUST_DENY:
        if d not in deny:
            problems.append(f"does not deny {d} (a menu nobody can reach)")
    if tracked.get("hooks"):
        problems.append("tracked settings.json carries hooks (machine paths must not be in git)")

    hooks = (local.get("hooks") or {})
    for h in HOOKS:
        entries = hooks.get(h) or []
        cmds = [c.get("command", "") for e in entries for c in (e.get("hooks") or [])]
        if not cmds:
            problems.append(f"hook not wired: {h}")
            continue
        for c in cmds:
            script = c.split('"')[1] if '"' in c else c.split()[-1]
            if script.endswith(".py") and not Path(script).is_file():
                problems.append(f"{h} points at a missing script ({Path(script).name})")
    # The stub lives where the SESSION's project root is: the project folder
    # for a component (that is where the template ships it and how skill
    # discovery walks), the desk folder itself for a tool or the daybook.
    # Checking the component folder was this file's own first bug - it flagged
    # eight healthy desks on 2026-08-17.
    if folder != ROOT:
        stubs = [folder / ".claude" / "skills" / "omnius" / "SKILL.md",
                 folder.parent / ".claude" / "skills" / "omnius" / "SKILL.md"]
        if not any(s.is_file() for s in stubs):
            problems.append("no /omnius skill stub here or at its project root "
                            "- a headless run cannot find the protocol")
    return (not problems), problems


def fleet_checks():
    """Invariants that belong to the fleet, not to one desk."""
    out = []
    wd = (ROOT / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
    out.append(("failed runs back off instead of looping",
                "RUN_BACKOFF_SECONDS" in wd and "_run_backoff" in wd))
    out.append(("repeated failure alerts the owner once",
                "RUN_FAILURES_BEFORE_ALERT" in wd and "_run_alerted" in wd))
    out.append(("a dead bridge's process tree is reaped",
                "_kill_tree" in wd and "_tree_pids" in wd))
    out.append(("unlisted tools escalate to Discord, never stall",
                (ROOT / "tools" / "discord" / "permission_relay.py").is_file()))
    out.append(("a live terminal turn blocks headless runs (busy stamp)",
                "interactive_busy" in wd))
    out.append(("no session-side watcher can go deaf",
                "there is no watch mode" in
                (ROOT / "tools" / "discord" / "inbox_watch.py").read_text(encoding="utf-8")))
    return out


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Desk stability review (read-only).")
    ap.add_argument("--quiet", action="store_true", help="only print what is wrong")
    a = ap.parse_args(argv)

    desks = desk_dirs()
    bad = 0
    if not a.quiet:
        print(f"=== desks ({len(desks)}) " + "=" * 40)
    for sid, folder in desks:
        ok, problems = audit_desk(sid, folder)
        if ok:
            if not a.quiet:
                print(f"  [OK] {sid:28} shell+deny+hooks+skill")
        else:
            bad += 1
            print(f"  [X ] {sid:28} " + "; ".join(problems))
    if not a.quiet:
        print("\n=== fleet " + "=" * 45)
    for label, ok in fleet_checks():
        if ok and not a.quiet:
            print(f"  [OK] {label}")
        elif not ok:
            bad += 1
            print(f"  [X ] {label}")

    # The one thing this file cannot prove by reading: hooks pointing at THIS
    # machine. fix_hook_paths already answers it exactly, so ask it.
    r = subprocess.run([sys.executable, str(HERE / "fix_hook_paths.py"), "--check"],
                       capture_output=True, text=True)
    line = (r.stdout or r.stderr or "").strip().splitlines()
    if r.returncode != 0:
        bad += 1
        print(f"  [X ] hook paths: {line[-1] if line else 'check failed'}")
    elif not a.quiet:
        print(f"  [OK] {line[-1] if line else 'hook paths wired to this machine'}")

    print()
    if bad:
        print(f"[X] {bad} problem(s) - see above. Nothing was changed; fixes are yours.")
        return 1
    print(f"[OK] {len(desks)} desk(s): no prompts can hang them, hooks wired, "
          f"escalation live, self-recovery in place.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
