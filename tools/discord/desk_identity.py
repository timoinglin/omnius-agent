#!/usr/bin/env python3
"""Which desk is this turn on? - identity that survives a session changing folder.

Every turn hook used to answer that question the same way: map the payload's
**current** cwd to a session id, once, and believe it. That is right until a
session walks out of its own folder - and desks walk, by instruction:
constitution par.7 forbids `git -C`, so a desk that must touch its repo root
cd's there.

2026-08-12, twice, a project's `web` desk did exactly that to create its
GitHub repo. UserPromptSubmit stamped `<project>.web.busy` from
`projects\\<project>\\web`; twenty minutes later Stop ran from
`projects\\<project>`, wanted three path parts, got two, resolved None -
and cleared nothing. **Two different answers to "who am I", one turn apart.**

The stale stamp then silenced every guard at once - it tells the bridge "do
not nudge", the watchdog "do not run", and (until the recency check added the
same day) the deaf-desk alarm "a turn is running". His mail sat unread for 35
minutes with no surface anywhere saying so. The watchdog side of that was
patched to REPORT the deadlock; this module removes the cause.

Three answers, best first:

1. **The stamp this very conversation wrote.** `turn_start_hook` records
   Claude Code's own session id in it, so the file can be found by identity
   instead of by name - whatever folder the turn has wandered into since.
2. **cwd, when it maps.** Free, and right for every desk that stayed put.
3. **The conversation file's folder.** Claude Code names it after the cwd the
   session STARTED in - a desk's own folder - by replacing every character
   that is not a letter or digit with '-'. Decoding is ambiguous (a '-' in a
   folder name is indistinguishable from a separator), so candidates are
   ENCODED and compared, and an ambiguous match returns None. This is the one
   that saves a desk whose FIRST turn already ran from the wrong folder, so
   there is no stamp to find.

Rules for anything a hook imports: no heavy imports (this is on the path of
every turn - `api.py` reads `.env` and is exactly what the duplicated mapping
in the hooks was avoiding), never print, never raise.
"""
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TURNS = ROOT / "state" / "turns"
BUSY_SUFFIX = ".busy"


def session_id_for(cwd):
    """cwd -> session id, the same mapping CLAUDE.md par.1 uses. None = not a desk."""
    try:
        rel = Path(cwd).resolve().relative_to(ROOT)
    except (ValueError, OSError, TypeError):
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


def encode_project_dir(path):
    """Claude Code's conversation-folder name for a cwd.

    `W:\\omnius\\projects\\some-app\\web` -> `W--omnius-projects-some-app-web`.
    Deliberately not reversible in this direction only: callers encode their
    own candidates and compare, so a folder called `some-app` can never be
    mistaken for a `some\\app` that does not exist.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def candidate_desks():
    """[(session id, folder)] for every desk this workspace could seat.

    Cheap on purpose - three shallow listdirs, no recursion. Extra entries
    (`projects\\<p>\\memory` looks like a component from here) are harmless:
    they can only match if a session actually started in that folder, and an
    ambiguous match is refused by the caller anyway.
    """
    desks = [("orchestrator", ROOT), ("daybook", ROOT / "daybook")]
    try:
        for project in sorted((ROOT / "projects").iterdir()):
            if not project.is_dir() or project.name.startswith((".", "_")):
                continue
            for comp in sorted(project.iterdir()):
                if comp.is_dir() and not comp.name.startswith((".", "_")):
                    desks.append((f"{project.name}.{comp.name}", comp))
    except OSError:
        pass
    try:
        for tool in sorted((ROOT / "tools").iterdir()):
            if tool.is_dir() and not tool.name.startswith((".", "_")):
                desks.append((f"tool.{tool.name}", tool))
    except OSError:
        pass
    return desks


def desk_from_transcript(transcript_path):
    """The desk whose folder Claude Code encoded into this conversation's path.

    None when nothing matches, and None when more than one desk matches: a
    wrong identity clears another desk's stamp, which is worse than the stale
    one this exists to prevent.
    """
    try:
        folder = Path(transcript_path).parent.name
    except (TypeError, ValueError, OSError):
        return None
    if not folder:
        return None
    hits = {sid for sid, path in candidate_desks()
            if encode_project_dir(path) == folder}
    return hits.pop() if len(hits) == 1 else None


def desk_from_live_turn(claude_session):
    """The desk whose .busy stamp THIS conversation wrote. Exact, or None.

    Reads the stamps rather than the claim files because a stamp is written by
    the turn itself: it cannot be inherited from a dead session, and it names
    the desk as of the moment the turn began - before any cd could move it.
    """
    if not claude_session:
        return None
    try:
        stamps = sorted(TURNS.glob("*" + BUSY_SUFFIX))
    except OSError:
        return None
    for stamp in stamps:
        try:
            data = json.loads(stamp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("claudeSession") == claude_session:
            return data.get("session") or stamp.name[:-len(BUSY_SUFFIX)]
    return None


def desk_for_event(event, cwd_fallback=None):
    """Hook payload -> session id. None = this session is not a desk.

    Order matters: the stamp is asked FIRST because it is the only source that
    cannot drift mid-turn. A desk that walked into `tools\\discord` to fix a
    tool still ends the turn as itself, instead of taking down `tool.discord`'s
    stamp and leaving its own standing.
    """
    if not isinstance(event, dict):
        event = {}
    return (desk_from_live_turn(event.get("session_id"))
            or session_id_for(event.get("cwd") or cwd_fallback or os.getcwd())
            or desk_from_transcript(event.get("transcript_path")))
