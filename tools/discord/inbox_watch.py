#!/usr/bin/env python3
"""Desk check-in: write the claim, print waiting envelopes, EXIT (stdlib only).

    python tools\\discord\\inbox_watch.py <session-id> [--once]

- writes the session claim state\\sessions\\<id>.json (real pid, real times)
- prints every waiting envelope in state\\inbox\\<id>\\ as JSON, oldest first
- exits 0 immediately - there is no watch mode

There USED to be a watch mode, and its absence is the 2026-08-01 lesson in one
line: a Claude session is turn-based, so a session-side watcher process kept
being stopped at turn boundaries - three times in one evening - and every death
either left the desk deaf or invited the watchdog to spawn a duplicate brain
onto an occupied desk. Reachability is the watchdog's job now: it delivers
envelopes AND starts a headless run (`claude -p "/omnius"`) whose process it
owns. Nothing session-side has to stay alive for the desk to be reachable, so
nothing session-side can die and take reachability with it.

The claim carries NO heartbeat and NO watcherPid: pid liveness is the whole
signal. A lastSeenAt stamped fresh by a sidecar while the session itself hung
is exactly the lie that cost the evening of 2026-08-01.

--once and --force are accepted for compatibility; check-in is simply the only
behaviour there is.

Design: docs/ARCHITECTURE.md par. 3.4 / 3.5.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SESSIONS = ROOT / "state" / "sessions"
INBOX = ROOT / "state" / "inbox"
TURN_STARTED = ROOT / "state" / "watchdog" / "turn_started"
RUNS = ROOT / "state" / "watchdog" / "runs"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api  # noqa: E402  - single source of truth for .env and MACHINE


def _schema_channel(session):
    """-> the channel name schema.json gives this session, or None.

    Read rather than guessed, because the mapping is not mechanical:
    tool.fleet lives in #fleet-status. Never raises - a missing or broken
    schema must not stop a desk from claiming.
    """
    try:
        schema = json.loads((Path(__file__).resolve().parent / "schema.json")
                            .read_text(encoding="utf-8"))
        for cat in schema.get("initial", {}).get("categories", []):
            for ch in cat.get("channels", []):
                if ch.get("session") == session and ch.get("name"):
                    return ch["name"]
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return None


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def active_run(sid, my_session_pid):
    """-> the lease of a FOREIGN headless run that owns this desk, else None.

    The guard runs BOTH ways or it does not run. The watchdog already refuses
    to start a run on a desk whose terminal is mid-turn (state\\turns\\<id>.busy);
    this is the mirror. Caught live 2026-08-01: a ping arrived while this
    terminal was idle, the watchdog correctly started a run for it, and a
    person then typed /omnius in the terminal - whose check-in cheerfully
    printed the SAME envelope. Two brains, one message, one conversation.

    Relying on the agent to look first is the "somebody has to remember"
    pattern that failed all day, so the check-in itself refuses.

    my_session_pid is the claude/node ANCESTOR of this python process - the
    same pid the lease holds. Comparing os.getpid() instead would make every
    headless run refuse itself, since inbox_watch is that run's child.
    """
    try:
        lease = json.loads((RUNS / f"{sid}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if my_session_pid and lease.get("pid") == my_session_pid:
        return None                      # our own run, called via -p "/omnius"
    return lease if pid_alive(lease.get("pid")) else None


def resolve_session_pid(override=None):
    """The claude process this check-in belongs to.

    `override` is for a LAUNCHER that already knows: the desk bridge owns the
    claude process in its pty and can state the pid outright, where the
    ancestor walk below would find nothing (the bridge's own chain is
    cmd -> python, with claude a CHILD rather than a parent). Without it a
    freshly opened desk stays unclaimed until its first message - and an
    unclaimed desk has the permission relay switched off, so it prompts
    locally instead of in #alerts (2026-08-02).
    """
    if override:
        return override
    # Otherwise walk the parent chain; the claude/node ancestor is the session.
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=45,
            # No console window: a window flashing on the desktop at every
            # check-in is noise the owner has to learn to ignore. Same reason
            # as watchdog.NO_WINDOW.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        procs = {p["ProcessId"]: p for p in json.loads(out)}
        pid, chain = os.getpid(), []
        while pid in procs and len(chain) < 15:
            chain.append(procs[pid])
            pid = procs[pid]["ParentProcessId"]
        for p in chain:
            name = (p.get("Name") or "").lower()
            if "claude" in name or "node" in name:
                return p["ProcessId"]
    except Exception:
        pass
    return None


def ack(sid, ids):
    """Delete handled envelopes. -> exit code.

    Exists to keep a routine act off the permission system. Sessions were
    deleting envelopes with `Remove-Item <path>`, which no sane allow-list
    covers, so EVERY Discord message raised a permission prompt - the owner
    answered four in #alerts and gave up on the fifth (2026-08-02). Going
    through this tool means the command is `python ...`, already allowed, and
    it can only ever delete inside this desk's own inbox.
    """
    box = INBOX / sid
    gone, missing, refused = [], [], []
    for i in ids:
        name = Path(i).name                    # never let a path escape the box
        if not name.endswith(".json"):
            name += ".json"
        target = box / name
        if target.parent.resolve() != box.resolve():
            refused.append(i)
            continue
        if target.is_file():
            try:
                target.unlink()
                gone.append(name)
            except OSError as e:
                refused.append(f"{name}: {e}")
        else:
            missing.append(name)               # already handled: not an error
    print(json.dumps({"deleted": gone, "notFound": missing, "refused": refused},
                     ensure_ascii=False))
    return 1 if refused else 0


def main():
    args = sys.argv[1:]
    if "--ack" in args:
        i = args.index("--ack")
        rest = [a for a in args[:i] if not a.startswith("-")]
        if not rest:
            print("usage: inbox_watch.py <session-id> --ack <envelope-id> [...]",
                  file=sys.stderr)
            return 2
        return ack(rest[0], args[i + 1:])

    pid_override = None
    if "--pid" in args:
        i = args.index("--pid")
        try:
            pid_override = int(args[i + 1])
            args = args[:i] + args[i + 2:]
        except (IndexError, ValueError):
            print("usage: --pid <session-process-id>", file=sys.stderr)
            return 2

    positional = [a for a in args if not a.startswith("-")]
    if len(positional) != 1:
        print("usage: inbox_watch.py <session-id> [--once]   (e.g. orchestrator, recipe-app.app)\n"
              "       inbox_watch.py <session-id> --ack <envelope-id> [...]  (delete handled mail)\n"
              "  writes the claim (real pid), prints any waiting envelopes, exits",
              file=sys.stderr)
        return 2
    sid = positional[0]
    # Every id shape CLAUDE.md par.6 defines must work here: that file tells the
    # daybook and tool sessions to check in with exactly these, and they used to
    # get exit 2 and no claim - invisible to the fleet.
    if sid == "orchestrator":
        role, project, component, cwd, channel = "orchestrator", None, None, ROOT, "orchestrator"
    elif sid == "daybook":
        role, project, component, cwd, channel = "daybook", None, None, ROOT / "daybook", "daybook"
    elif sid.startswith("tool."):
        tool = sid.partition(".")[2]
        if not tool:
            print(f"unknown session id shape: {sid}", file=sys.stderr)
            return 2
        # channel was hardcoded None here, which quietly disabled Discord
        # permission escalation for EVERY tool desk: permission_relay's
        # bus_connected() treats a claim without discordChannel as "nobody is
        # driving this remotely" and returns before asking. Found 2026-08-06 when
        # tool.transcribe stopped on a local dialog in a bridge window and the
        # owner said "Discord never asked for permission" - true of tool.fleet
        # and tool.discord since the day they were created.
        #
        # schema.json already maps channel -> session, so use it rather than
        # guessing: tool.fleet is #fleet-status, NOT #fleet. Falling back to the
        # tool name covers a desk added before its schema entry; the value is
        # only a gate here (the outbox resolves the real channel, and a desk
        # with none falls back to #alerts) so a near-miss still beats None.
        role, project, component, cwd = "tool", None, tool, ROOT / "tools" / tool
        channel = _schema_channel(sid) or tool
    elif "." in sid:
        project, _, component = sid.partition(".")
        role, cwd, channel = "project", ROOT / "projects" / project / component, component
    else:
        print(f"unknown session id shape: {sid}", file=sys.stderr)
        return 2

    SESSIONS.mkdir(parents=True, exist_ok=True)
    box = INBOX / sid
    box.mkdir(parents=True, exist_ok=True)
    claim_path = SESSIONS / f"{sid}.json"

    started = now_iso()
    if claim_path.exists():
        try:
            started = json.loads(claim_path.read_text(encoding="utf-8")).get("startedAt", started)
        except (json.JSONDecodeError, OSError):
            pass

    lease = None
    try:
        lease = json.loads((RUNS / f"{sid}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    # Identity by TOKEN first: start_run puts the same OMNIUS_RUN_ID into the
    # run's environment and its lease, so an active run can prove it is the
    # active run without a WMI ancestor walk - deterministic, instant, and
    # immune to npm shims where the lease pid is a cmd wrapper rather than
    # claude itself (the next PC may resolve `claude` to a .cmd).
    env_run_id = os.environ.get("OMNIUS_RUN_ID")
    i_am_the_run = bool(lease and env_run_id and lease.get("runId") == env_run_id)
    if i_am_the_run:
        session_pid = lease.get("pid")
    else:
        session_pid = resolve_session_pid(pid_override)
        # Refuse BEFORE claiming or draining: a desk with a live run already has
        # a brain on it, and printing the envelopes here is what puts a second
        # one on the same message.
        other = active_run(sid, session_pid)
        if other:
            print(f"refusing: a headless run (pid {other['pid']}, started "
                  f"{other.get('startedAt', '?')}) already owns desk '{sid}'.\n"
                  f"  It is handling this desk's mail right now. Do not drain the inbox\n"
                  f"  or reply - you would answer the same message twice in one\n"
                  f"  conversation. Wait for it to finish, or `!restart` that desk.",
                  file=sys.stderr)
            return 4
        i_am_the_run = bool(lease and session_pid and lease.get("pid") == session_pid)

    # The mirror of the refusal: tell the ACTIVE RUN that it IS the active run.
    # Runs share a conversation lineage with terminal sessions, and on
    # 2026-08-01 a run inherited the terminal's "a run is handling it, I stand
    # down" reasoning and STOOD DOWN FROM ITSELF - the ping went unanswered
    # while its own worker waited for itself. Identity is not something the
    # model may infer from the transcript; the check-in states it outright.
    if i_am_the_run:
        print(f"YOU ARE THE ACTIVE HEADLESS RUN (pid {session_pid}) on desk '{sid}'.\n"
              f"The envelopes below are YOURS to answer, in this run, now. Never wait\n"
              f"for or defer to 'the run handling this desk' - that run is you.")

    claim = {"role": role, "project": project, "component": component,
             # api.MACHINE reads root .env first, then COMPUTERNAME. Resolving it
             # separately here made one PC claim two names, and CLAUDE.md par.6
             # treats "different machine" as stale - so claims looked prunable.
             "cwd": str(cwd), "machine": api.MACHINE,
             "pid": session_pid,
             "startedAt": started, "lastSeenAt": now_iso(), "discordChannel": channel}
    try:
        claim_path.write_text(json.dumps(claim, indent=2), encoding="utf-8")
    except OSError:
        pass                                # a failed claim must not block the drain

    envelopes = sorted(box.glob("*.json"))
    out = []
    for f in envelopes:
        try:
            out.append({"path": str(f), "envelope": json.loads(f.read_text(encoding="utf-8"))})
        except (json.JSONDecodeError, OSError):
            out.append({"path": str(f), "envelope": None})
    if out:
        # Mark the start of the turn this drain belongs to. The Stop hook reads
        # it to notice a desk that did work and then said nothing - 2026-08-01,
        # a session built the owner's PDF and the result died on disk because
        # nobody thought to post it.
        try:
            TURN_STARTED.mkdir(parents=True, exist_ok=True)
            (TURN_STARTED / f"{sid}.json").write_text(
                json.dumps({"session": sid, "at": now_iso(), "ts": time.time()}),
                encoding="utf-8")
        except OSError:
            pass                            # never let bookkeeping block a delivery
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
