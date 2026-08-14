#!/usr/bin/env python3
"""Stop hook - the mechanical clears that make a turn's end MEAN something.

A turn that reached its end is proof of two things, and this hook turns both
into state nothing has to remember to update:

1. The desk is not mid-turn -> remove state\\turns\\<sid>.busy (written by the
   UserPromptSubmit hook). The watchdog holds headless runs off a desk while
   that stamp stands - two concurrent writers on one conversation is the
   two-brains failure - so a stamp that outlived its turn would leave the desk
   permanently unreachable from Discord.

2. No dialog is pending -> remove <sid>.stalled. A session blocked on a
   permission prompt never reaches Stop, so arriving here means the dialog is
   gone. Before this clear existed the banner cried STALLED for hours over a
   prompt answered long before (2026-08-01).

Plus one announcement: a desk that was ASKED something (turn_started stamp)
and posted nothing gets a one-line note to the owner, so finished work cannot
die silently on disk again.

(The dead-watcher marker that used to live here went with the watchers
themselves - the run model has nothing that must stay armed, so there is
nothing whose death needs recording.)

Rules this file obeys:
- Print NOTHING on stdout. Hook output can be interpreted; this one only moves
  files.
- Never block. A Stop hook has a timeout and a slow one stalls every turn.
- Never fail loudly. A broken hook must not be able to wedge a session, so
  every path returns 0.

Design: docs/ARCHITECTURE.md par. 3.4. Registered as a "Stop" hook in
.claude/settings.json.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PERMS = ROOT / "state" / "permissions"
TURNS = ROOT / "state" / "turns"
TURN_STARTED = ROOT / "state" / "watchdog" / "turn_started"
OUTBOX = ROOT / "state" / "outbox"
MAX_NAMED_FILES = 6            # a phone screen, not a changelog
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:                                    # a broken helper must never wedge a turn
    from desk_identity import desk_for_event
except Exception:                       # noqa: BLE001
    desk_for_event = None


def session_id_for(cwd):
    """cwd -> session id, the same mapping CLAUDE.md par.1 uses. None = not a desk.

    THE CURRENT cwd, which is why this is no longer asked first: a session that
    cd'd out of its desk folder mid-turn answers differently here than it did
    at UserPromptSubmit, and on 2026-08-12 that left a project desk's stamp
    standing over a finished turn - the desk unreachable from Discord until a
    human noticed. desk_identity.desk_for_event() asks the turn's own stamp
    first and keeps this as the fallback.

    Duplicated from permission_relay rather than imported: importing that module
    pulls in api.py and reads .env, which is real work to do on every single
    turn end for a mapping that is nine lines of string handling.
    """
    try:
        rel = Path(cwd).resolve().relative_to(ROOT)
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if not parts or parts == (".",):
        return "orchestrator"
    if parts[0] == "projects" and len(parts) >= 3:
        return f"{parts[1]}.{parts[2]}"
    if parts[0] == "tools" and len(parts) >= 2:
        return f"tool.{parts[1]}"
    if parts[0] == "daybook":
        return "daybook"
    return None


def changed_files(cwd, since=None):
    """Names of files THIS TURN touched, per git. [] if git cannot say.

    git is the right oracle because it honours .gitignore: node_modules and
    dist churn on every build and would drown the one file the owner actually
    wants to hear about.

    `since` filters by mtime, and it matters: `git status` reports everything
    dirty in the repo, including edits somebody else left uncommitted. On
    2026-08-03 a desk announced "files it changed: settings.json, settings.json,
    settings.json" - all three were mine, edited minutes earlier from another
    session. Claiming another author's work is worse than saying nothing.
    """
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=cwd,
                             capture_output=True, text=True, timeout=8,
                             creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    names = []
    for line in out.splitlines():
        rel = line[3:].strip().strip('"')
        if not rel:
            continue
        if since:
            try:
                if Path(cwd, rel).stat().st_mtime < since:
                    continue                      # dirty before this turn began
            except OSError:
                continue                          # deleted or unreadable: not ours to claim
        names.append(rel.rsplit("/", 1)[-1])
    return names


def announce_if_silent(session, cwd):
    """A desk that was asked to do something must not finish in silence.

    2026-08-01: a session was asked for a PDF, built it correctly, and stopped.
    The file sat on disk and the owner - who could only see Discord - spent the
    afternoon asking whether anything was happening at all. The work was fine;
    the silence was the bug.

    Only speaks when there is something to answer for: a turn that a delivered
    envelope started, which posted nothing back. A desk that already replied is
    left alone, and so is one nobody asked anything.
    """
    stamp_file = TURN_STARTED / f"{session}.json"
    try:
        stamp = json.loads(stamp_file.read_text(encoding="utf-8"))
        started = float(stamp.get("ts") or 0)
    except (OSError, ValueError, TypeError):
        return
    # One announcement per delivered turn, whatever happens below.
    try:
        stamp_file.unlink(missing_ok=True)
    except OSError:
        pass
    if not started:
        return

    box = OUTBOX / session
    try:
        spoke = any(f.stat().st_mtime >= started for f in box.glob("*.json"))
        if not spoke:
            # A posted reply is DELETED by the watchdog within ~3s, so a run
            # that answered promptly has an empty outbox by the time its turn
            # ends. The watchdog stamps .last-posted at every post precisely so
            # this check does not cry wolf (first live run, 2026-08-01).
            lp = box / ".last-posted"
            spoke = lp.is_file() and lp.stat().st_mtime >= started
    except OSError:
        spoke = False
    if spoke:
        return                            # it reported for itself - nothing to add

    names = changed_files(cwd, since=started)
    if not names:
        # Asked, answered nothing, changed nothing. Almost always a desk that
        # replied in its own window, or judged there was nothing to say. Not
        # worth a phone notification (owner, 2026-08-03: "just issues or error
        # message, or important stuff"). The silence that mattered was WORK
        # going unreported - and that case is the branch below.
        return
    shown = ", ".join(f"`{n}`" for n in names[:MAX_NAMED_FILES])
    more = f" (+{len(names) - MAX_NAMED_FILES} more)" if len(names) > MAX_NAMED_FILES else ""
    text = (f"⚠️ **`{session}` changed files but reported nothing.**\n"
            f"{shown}{more}")
    try:
        box.mkdir(parents=True, exist_ok=True)
        (box / f"{int(datetime.now(timezone.utc).timestamp() * 1000)}-turnend.json").write_text(
            json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def clear_stall(session):
    """A turn that ENDED is proof this desk is not sitting at a dialog.

    2026-08-01, found by looking at the owner's screen instead of at the tests:
    the status banner had been showing "[!!] STALLED orchestrator - waiting at a
    local dialog" for hours, for a Bash prompt answered long before. Nothing was
    waiting for anyone. The alarm was simply never taken down.

    permission_relay writes <session>.stalled when a relayed request times out
    and falls back. Every clear that existed - an answer arriving, kill_session,
    spawn_session - is an EVENT SOMEBODY HAS TO CAUSE. The ordinary path, where
    the desk is answered at its own keyboard and just carries on, cleared
    nothing, so the marker outlived the dialog by design and both surfaces a
    human trusts (this banner, !status) kept crying wolf.

    The Stop hook is the one signal that cannot lie about this: a session
    blocked on a permission dialog has NOT finished its turn, so it never gets
    here. Reaching this line means the dialog is gone. Clearing it here is
    mechanical - like the turn_ended marker above, nothing has to remember.
    """
    try:
        (PERMS / f"{session}.stalled").unlink(missing_ok=True)
    except OSError:
        pass


def main():
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        event = {}
    if not isinstance(event, dict):
        event = {}
    cwd = event.get("cwd") or os.getcwd()

    session = (desk_for_event(event) if desk_for_event else None) or session_id_for(cwd)
    if not session:
        return 0                                    # not a desk: nothing to record

    # Both clears run UNCONDITIONALLY, before anything that could bail out.
    # They are alarms/guards the owner and the watchdog trust; taking them down
    # must never be gated on other state (a claim, a parse) being intact.
    clear_stall(session)
    try:
        (TURNS / f"{session}.busy").unlink(missing_ok=True)
    except OSError:
        pass

    announce_if_silent(session, cwd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
