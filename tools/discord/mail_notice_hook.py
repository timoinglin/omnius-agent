#!/usr/bin/env python3
"""PostToolUse hook - tell a RUNNING turn that mail is waiting.

The gap this closes. A desk mid-turn cannot be handed mail: the watchdog will
not start a `--continue` run against a live turn (two writers, one
conversation), and the bridge's nudge is keystrokes that only land once the CLI
is reading again. So an envelope arriving one minute into a twenty-minute turn
waited for the whole turn - and `/omnius` already says "re-drain between major
steps", which is advice a session has no way to act on because nothing tells it
there is anything to re-drain.

A PostToolUse hook can: whatever it prints as `additionalContext` reaches the
model between tool calls, in the turn that is already running. Owner's item 4,
2026-09-03.

Why PostToolUse and not PreToolUse: a PreToolUse hook can *block* the call it
precedes, and nothing here is worth that risk. Running after the tool has
already done its work means the worst case of a bug in this file is a lost
notice, never a wedged turn.

Rules, same as every hook here: print nothing when there is nothing to say,
never block, always exit 0. A broken hook must not be able to wedge a session.

RATE LIMITED, because a long turn makes hundreds of tool calls and the same
line injected into every one of them would crowd out the work it is trying to
interrupt. One notice per NOTICE_SECONDS, and any change in the count is worth
saying immediately.
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INBOX = ROOT / "state" / "inbox"
MARKS = ROOT / "state" / "turns"

NOTICE_SECONDS = 60

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:                                    # a broken helper must never wedge a turn
    from desk_identity import desk_for_event, session_id_for
except Exception:                       # noqa: BLE001
    desk_for_event = session_id_for = None


def waiting(session):
    """-> how many envelopes sit in this desk's inbox. ONE listing, no reads."""
    try:
        return sum(1 for f in (INBOX / session).iterdir()
                   if f.suffix == ".json")
    except OSError:
        return 0


def already_said(session, n):
    """True when this exact count was announced less than NOTICE_SECONDS ago.

    The mark is one tiny file per desk, holding the count and the time. It is
    advisory only: if it cannot be read or written the notice still goes out,
    because a missed notice is the failure this hook exists to prevent.
    """
    mark = MARKS / f"{session}.mailnotice"
    try:
        prev = json.loads(mark.read_text(encoding="utf-8"))
        if int(prev.get("n") or 0) == n \
                and time.time() - float(prev.get("at") or 0) < NOTICE_SECONDS:
            return True
    except (OSError, ValueError, TypeError):
        pass
    try:
        MARKS.mkdir(parents=True, exist_ok=True)
        mark.write_text(json.dumps({"n": n, "at": time.time()}), encoding="utf-8")
    except OSError:
        pass
    return False


def main():
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return 0
    session = None
    if desk_for_event:
        session = desk_for_event(event)
    if not session and session_id_for:
        session = session_id_for(event.get("cwd") or os.getcwd())
    if not session:
        return 0                        # not a desk: nothing to re-drain
    n = waiting(session)
    if not n or already_said(session, n):
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": (
            f"[bus] {n} envelope(s) are waiting in state\\inbox\\{session}\\ — "
            f"they arrived while this turn was running. Re-drain before you "
            f"finish: python tools\\discord\\inbox_watch.py {session} --once. "
            f"A queued 'stop' beats finishing the wrong thing."),
    }}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                   # noqa: BLE001 - never wedge a turn
        sys.exit(0)
