#!/usr/bin/env python3
"""UserPromptSubmit hook - stamp "a turn is running on this desk".

The watchdog must not start a headless `--continue` run on a desk where a
person's own terminal is mid-turn: `--continue` attaches to the most recent
conversation in that folder, so two concurrent turns would be two writers on
ONE conversation - the two-brains failure of 2026-08-01, arrived at from a
different door.

This stamp (`state\\turns\\<sid>.busy`) is that guard's only input. The Stop
hook removes it; a crashed terminal leaves it behind, which is why the
watchdog validates it against the claim pid and ages unverifiable ones out
(watchdog.interactive_busy). Queued Discord mail is not lost while it stands -
the watchdog follows up in the same conversation the moment the turn ends.

The stamp also RECORDS Claude Code's own session id, so the Stop hook can find
it by identity instead of by name. Without that, both hooks answer "who am I"
from their own cwd, and a session that cd's mid-turn (constitution par.7
forbids `git -C`, so a desk touching its repo root walks there) gets two
different answers one turn apart - the 2026-08-12 deadlock, twice in one day.
See desk_identity.py.

Same rules as turn_end_hook: print NOTHING on stdout, never block, always
exit 0 - a broken hook must not be able to wedge a session.

Registered as a "UserPromptSubmit" hook in .claude/settings.json (root and
per-project copies - settings do not inherit across folders).
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TURNS = ROOT / "state" / "turns"

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:                                    # a broken helper must never wedge a turn
    from desk_identity import desk_for_event
except Exception:                       # noqa: BLE001
    desk_for_event = None


def session_id_for(cwd):
    """cwd -> session id, the same mapping CLAUDE.md par.1 uses. None = not a desk."""
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


def main():
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        event = {}
    if not isinstance(event, dict):
        event = {}
    cwd = event.get("cwd") or os.getcwd()
    # desk_for_event falls back to the conversation file's own folder, which is
    # the cwd the session STARTED in - so even a turn that begins from the
    # wrong folder is stamped, and the two-brains guard keeps working.
    session = (desk_for_event(event) if desk_for_event else None) or session_id_for(cwd)
    if not session:
        return 0
    try:
        TURNS.mkdir(parents=True, exist_ok=True)
        (TURNS / f"{session}.busy").write_text(json.dumps({
            "session": session,
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            # The turn's own name for itself: what lets the Stop hook find this
            # file after a cd. Absent in stamps written before 2026-08-12 -
            # readers must treat a missing value as "no match", never as a
            # wildcard.
            "claudeSession": event.get("session_id"),
        }), encoding="utf-8")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
