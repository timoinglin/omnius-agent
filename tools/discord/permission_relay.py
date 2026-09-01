#!/usr/bin/env python3
"""PermissionRequest hook - relay a permission prompt to Discord and wait.

Registered in .claude\\settings.json. Claude Code runs this when a permission
dialog is about to appear, passing the tool call as JSON on stdin. Whatever
decision we print on stdout is applied; printing NOTHING falls through to the
normal local dialog.

Why this exists: the parity doctrine says a remotely driven session must never
hit a permission prompt - but that is a rule you cannot enforce. One unforeseen
tool call and the session stalls on a dialog nobody can see. The old workaround
was to widen the allow-list until prompts stopped, which is how Bash(python:*)
ended up making every deny rule decorative. Escalation is the alternative: ask
the owner in Discord, so the profile can be TIGHTENED instead.

Design rules:
- Sessions never touch Discord (ARCHITECTURE par.3.4). We write to the outbox;
  the watchdog posts it and writes the answer back as a file.
- Only BUS-CONNECTED sessions escalate. A plain desktop session exits
  immediately and sees no added latency at all.
- Fail SAFE, not open: no answer in time -> print nothing -> the local dialog
  appears exactly as it does today. We never auto-allow on silence.

Design: docs/ARCHITECTURE.md par. 3.4, docs/PERMISSIONS.md.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "state"
SESSIONS = STATE / "sessions"
OUTBOX = STATE / "outbox"
PERMS = STATE / "permissions"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api  # noqa: E402  - for redact() and the shared .env reader
try:                                    # a broken helper must never wedge a turn
    from desk_identity import desk_for_event  # noqa: E402
except Exception:                       # noqa: BLE001
    desk_for_event = None

POLL_SECONDS = 1.0
DEFAULT_WAIT = 600               # seconds to wait for an answer before falling back
# The history of this number is the history of the feature failing:
#   120 - the first real firing (2026-07-31 16:39) timed out; the owner's "Ok"
#         arrived at 16:47, matching nothing.
#   170 - the whole 180s hook budget. Still too short: two asks timed out on
#         2026-08-02 while he was three feet away, because noticing a phone
#         notification, unlocking it and typing takes longer than three minutes.
#   600 - with the hook's own `timeout` raised to 620 in every settings file.
# The ceiling is always that hook timeout; raising this alone does nothing.


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def pid_alive(pid):
    """Windows-safe probe. Never os.kill(pid, 0) - on Windows it can terminate."""
    if not pid:
        return False
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259          # STILL_ACTIVE
    except Exception:
        return False


def bus_connected(session):
    """True only if this desk is claimed AND bound to a Discord channel.

    Gate for the whole feature: a session nobody is driving remotely must not
    pay a second of delay, so we answer this before doing anything else.

    Liveness is the claim PID, nothing else. Claims used to carry a heartbeat
    stamped by a sidecar watcher, and freshness-checking it here silently
    switched the relay OFF for any session between stamps - since 2026-08-01
    there is no heartbeat at all (check-in writes the claim once), so a
    freshness window would have disabled escalation for every long turn."""
    try:
        claim = json.loads((SESSIONS / f"{session}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(claim, dict) or not claim.get("discordChannel"):
        return False
    if str(claim.get("machine") or "") != api.MACHINE:
        return False
    return pid_alive(claim.get("pid"))


def describe(tool_name, tool_input):
    """A one-glance summary for a phone screen. Redacted - a command line can
    contain a token, and this is going to a chat channel."""
    if not isinstance(tool_input, dict):
        return api.redact(str(tool_input))[:400]
    for key in ("command", "file_path", "path", "url", "pattern"):
        if tool_input.get(key):
            return api.redact(str(tool_input[key]))[:400]
    return api.redact(json.dumps(tool_input, ensure_ascii=False))[:400]


EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Update"}


def own_claude_file(tool_name, tool_input):
    """Is this the workspace editing its OWN .claude\\ wiring? -> the path, or None.

    WHY THIS IS ALLOWED, and it is not a loosening. Claude Code guards writes
    under `.claude\\` above the allow-list: a skill or settings file changes what
    the agent may do, so `Edit` being allow-listed does not cover it. Sound in
    general - and on THIS fleet it stops the wrong door. Every desk already
    allows bare `Bash`/`PowerShell` (owner, 2026-08-06), so a desk that wants to
    rewrite its own skill can already do it with one unaudited
    `python -c "open(...).write(...)"` and no prompt at all. The dialog therefore
    blocks the legible, reviewable tool and waves through the illegible one. That
    is friction, not a fence.

    And it is friction he pays for personally: 9 of the 9 permission asks this
    fleet has ever raised were this exact class, ending 2026-09-01 with two `ok`s
    in three minutes for one skill edit - "And still all these annoying ask
    permission ok". His standing instruction is the other half: "Over discord
    make everything auto allow, no allow questions, no matter where ... what you
    can do is that you as LLM ask twice." The model is the brake now.

    So: narrow, and provably narrow. The path must be INSIDE this workspace
    (a `.claude` folder in someone else's tree is not ours to touch), must have
    a real `.claude` path SEGMENT (never a filename that merely starts with it),
    and `.env` is refused here as everywhere - that one fence is real, and it is
    enforced by the deny-list regardless of what this returns.
    """
    if tool_name not in EDIT_TOOLS or not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("file_path") or tool_input.get("path") or ""
    if not raw:
        return None
    try:
        target = Path(str(raw)).resolve()
        rel = target.relative_to(ROOT)            # ValueError = outside the workspace
    except (ValueError, OSError):
        return None
    if ".claude" not in rel.parts:
        return None
    if target.name == ".env" or target.name.startswith(".env."):
        return None
    return rel.as_posix()


def wait_seconds():
    # os.environ first, then root .env. Keeping both in step matters: the audit
    # found PORT/WHISPER_MODEL read from one source by the service and the other
    # by the banner, so neither could actually be changed.
    raw = os.environ.get("PERMISSION_ESCALATION_SECONDS") or \
        api.ENV.get("PERMISSION_ESCALATION_SECONDS", DEFAULT_WAIT)
    try:
        return max(2, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_WAIT


def main():
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0                                    # malformed: normal flow
    if not isinstance(event, dict):
        return 0

    # Identity from the turn's own stamp first, cwd second: a desk that cd'd out
    # of its folder (git at the repo root, constitution par.7) used to resolve
    # None here and escalate NOTHING - a dialog on an unwatched screen, which is
    # the exact failure this file exists to prevent. See desk_identity.py.
    session = (desk_for_event(event) if desk_for_event else None) \
        or session_id_for(event.get("cwd") or os.getcwd())
    if not session or not bus_connected(session):
        return 0                                    # desktop-only: no delay, no relay

    tool_name = event.get("tool_name", "?")
    tool_use_id = str(event.get("tool_use_id") or f"req-{int(time.time()*1000)}")
    code = tool_use_id[-6:]                          # short handle to answer by

    # The workspace editing its own .claude\ wiring: allow, and say so in the log
    # rather than in his channel. Reasoning in own_claude_file(). Logged, not
    # silent - an auto-allow nobody can audit is how a relay stops being trusted.
    own = own_claude_file(tool_name, event.get("tool_input"))
    if own:
        try:
            (STATE / "logs").mkdir(parents=True, exist_ok=True)
            with open(STATE / "logs" / "permission_relay.log", "a", encoding="utf-8") as fh:
                fh.write(f"{now_iso()} auto-allow {session} {tool_name} {own}\n")
        except OSError:
            pass                                     # logging must never block a turn
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }}))
        return 0

    PERMS.mkdir(parents=True, exist_ok=True)
    # A new request proves the desk is running again, so any earlier stall
    # marker is stale by definition. Clearing it here is one of the mechanical
    # clears - nothing has to remember.
    (PERMS / f"{session}.stalled").unlink(missing_ok=True)
    req = PERMS / f"{tool_use_id}.json"
    answer = PERMS / f"{tool_use_id}.answer"
    answer.unlink(missing_ok=True)
    req.write_text(json.dumps({
        "id": tool_use_id, "code": code, "session": session, "tool": tool_name,
        "detail": describe(tool_name, event.get("tool_input")),
        "cwd": event.get("cwd"), "askedAt": now_iso(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    box = OUTBOX / session
    box.mkdir(parents=True, exist_ok=True)
    # Say that one "ok" covers everything this desk is waiting on. A single
    # request from the owner can become half a dozen asks (six WebFetch calls
    # in six seconds, 2026-08-02), and without this line the natural reading of
    # six identical messages is that each needs its own answer.
    others = len([p for p in PERMS.glob("*.json")
                  if p.name != f"{tool_use_id}.json" and p.stat().st_size > 0])
    also = f"  ·  covers all {others + 1} waiting on this desk" if others else ""
    text = (f"🔐 **permission needed** — `{session}`\n"
            f"**{tool_name}**\n```\n{describe(tool_name, event.get('tool_input'))}\n```\n"
            f"reply **ok** to allow or **no** to deny{also}  ·  code `{code}`")
    # ASK WHERE HE IS TALKING, not in #alerts. His words, 2026-08-04, after
    # answering three git asks for one scaffold: "I have to jump from omnius
    # to alerts - not practical." The reply already works from any channel
    # (answer_permission runs on every owner message); it was only the QUESTION
    # that was posted somewhere else, which is what sent him there. No channel
    # key means the desk's own channel; #alerts is the fallback for a desk that
    # has none, and keeps its real job - errors.
    (box / f"{int(time.time()*1000)}-perm.json").write_text(
        json.dumps({"text": text, "fallback": "alerts"}, ensure_ascii=False),
        encoding="utf-8")

    deadline = time.time() + wait_seconds()
    while time.time() < deadline:
        try:
            verdict = json.loads(answer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            time.sleep(POLL_SECONDS)
            continue
        behavior = "allow" if verdict.get("behavior") == "allow" else "deny"
        req.unlink(missing_ok=True)
        answer.unlink(missing_ok=True)
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": behavior},
        }}))
        return 0

    # Timed out. Say so in the channel and fall through to the local dialog -
    # never auto-allow.
    #
    # Also leave a DURABLE marker. Falling back to the local dialog means this
    # desk is now frozen on a prompt nobody may ever see, and until 2026-07-31
    # that state was invisible: the claim heartbeat is written by inbox_watch,
    # a separate process that keeps stamping lastSeenAt while the session does
    # nothing at all. pid alive, watcher alive, lastSeenAt 3s old, !status "on"
    # - and stuck. The marker is what lets !status tell *alive* from
    # *listening*. It is cleared mechanically (spawn/kill/next request), never
    # by an agent remembering to.
    try:
        (PERMS / f"{session}.stalled").write_text(json.dumps({
            "session": session, "since": now_iso(), "tool": tool_name, "code": code,
            "detail": describe(tool_name, event.get("tool_input")),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    req.unlink(missing_ok=True)
    # Honest about both worlds: an interactive terminal now shows the local
    # dialog; a headless run has no dialog - the action is simply denied and
    # the run reports what it could not do. Either way the Stop hook clears
    # the stall marker mechanically when the turn ends.
    (box / f"{int(time.time()*1000)}-perm-timeout.json").write_text(
        json.dumps({"text": f"⌛ no answer for `{code}` — the action stays blocked. "
                            f"A terminal on `{session}` now shows the local prompt; "
                            f"a headless run skips the action and reports.",
                    "fallback": "alerts"},
                   ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
