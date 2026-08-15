#!/usr/bin/env python3
"""Omnius watchdog - the only always-on piece (stdlib only, Python 3.10+).

Listens to all mapped Discord channels (gateway push + REST sweep), enforces
the owner allowlist, downloads media, feeds session inboxes, and handles each
desk's mail by starting a HEADLESS ONE-SHOT RUN (`claude -p "/omnius"`) whose
process it owns - one run per desk at a time, queue while busy, --continue for
continuity. Posts outbox replies back, executes control commands
(!kill !restart !status !killall). Non-interactive by design: missing config
-> clear message + exit. No terminals, no session-side watchers - see the
"headless runs" section for why.

Design: docs/ARCHITECTURE.md par. 3.4 / 3.5, docs/DISCORD.md.
Run:    python tools\\discord\\watchdog.py   (or via start-omnius.bat)
"""
import argparse
import atexit
import ctypes
import json
import os
import queue
import re
import signal
import shutil
import socket
import subprocess
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api  # noqa: E402
import omnius_config as ocfg  # noqa: E402 - config\*.ini, one reader (config\README.md)
import gateway as gw_mod  # noqa: E402  - Gateway websocket (speed); REST stays the authority
import schedule  # noqa: E402  - scheduled envelopes (pure logic; no network)
import sync_permissions as perms_sync  # noqa: E402  - the shared desk allow-list

ROOT = api.ROOT
STATE = ROOT / "state"
INBOX = STATE / "inbox"
OUTBOX = STATE / "outbox"
MEDIA = ROOT / "media"  # durable asset archive: git-ignored, travels in the zip
LOGS = STATE / "logs"
SESSIONS = STATE / "sessions"
WD_STATE = STATE / "watchdog"
RUNS = WD_STATE / "runs"          # one lease per desk while a headless run is active
PERMS = STATE / "permissions"     # permission escalation: <tool_use_id>.json / .answer
TURNS = STATE / "turns"           # hook stamps: <sid>.busy while a terminal turn runs
BRIDGES = STATE / "bridges"       # <sid>.json: a live desk bridge owns this desk

# The watchdog runs under pythonw (no console), so EVERY console child it starts
# gets a window of its own. child_counts() runs powershell every 20s for the
# board, which put a console flashing on the owner's desktop twice a minute
# (2026-08-01: "what is the terminal window popping up every 30 sec?").
# Since the run model (2026-08-01) this applies to EVERY child, including the
# claude runs themselves - the watchdog opens no windows at all.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # 0 off-Windows
# The exact opposite, and used in exactly one place: open_tab, where a visible
# window IS the feature. It replaces `cmd /c start`, whose title argument needs
# shell quoting no argv list will give it (see open_tab).
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
TRANSCRIPTS = STATE / "transcripts"  # append-only bus history: <session>/<YYYY-MM>.jsonl

POLL_SECONDS = 3.0
# How often the REST sweep runs while the Gateway websocket is carrying messages.
# The gateway gives push latency; this sweep gives CERTAINTY - it re-asks every
# mapped channel with after=<lastId>, so anything the socket dropped still
# arrives, just late. That is the whole reason a hand-rolled websocket client is
# an acceptable risk here: its worst failure mode is slowness, not silence.
RECONCILE_SECONDS = 60.0
OMNIUS_CFG = ocfg.load("omnius")   # never raises; {} when there is no config\
GATEWAY_ENABLED = ocfg.get_bool(OMNIUS_CFG, "omnius", "gateway", "DISCORD_GATEWAY",
                                True, env=api.ENV)
MAP_REFRESH_SECONDS = 60
# Discord shows "typing..." for ~10s per call. Refresh inside that so the
# indicator never blinks out mid-run, but not so often that a desk thinking for
# ten minutes costs a call every tick. Must stay under 10.
TYPING_REFRESH_SECONDS = 8.0
_typing_sent = {}               # channel id -> when we last triggered typing
STILL_ACTIVE = 259
RUN_BACKOFF_SECONDS = 300       # pause after a failed run before retrying that desk
RUN_FAILURES_BEFORE_ALERT = 3   # consecutive failures before the owner is told
BUSY_ORPHAN_SECONDS = 2 * 3600  # unvalidatable .busy stamps older than this are litter
# How still a conversation must be before an API error at its end is taken as
# the turn's death rather than a retry in progress. Claude Code retries these,
# and a retry writes - which makes the error no longer the last thing there.
API_ERROR_QUIET_SECONDS = 60
# A restored PC boots, joins wifi and starts services - frequently in that
# order. Waiting through that is not the same as failing to start.
STARTUP_NET_ATTEMPTS = 10
STARTUP_NET_BACKOFF = 5         # seconds, x attempt, capped at 30
# ...and this one is for the stamp that CAN be validated and is still wrong. A
# live claude pid proves a live PROCESS, never a live TURN: a desk's process
# outlives every turn it runs, so any turn whose Stop hook did not fire (Esc
# mid-turn, a hook timeout, a settings.json without the hook, the 2026-08-12
# identity drift) leaves a stamp that nothing on the pid path can ever
# invalidate - and the desk is deaf from that moment until a human notices.
# That is the failure the owner called a showstopper: "it has to run stable for
# weeks, it cannot fall over like this."
#
# The turn's OWN conversation file is the honest witness. A running turn appends
# to it on every message and every tool result; a turn that ended writes nothing
# again, ever. Silence this long with mail waiting means the stamp outlived its
# turn - release it and let the run start.
BUSY_SILENT_SECONDS = 15 * 60
# A turn that has run this long with mail waiting is not working, it is stuck -
# almost always frozen on a local dialog nobody can see. Found live 2026-08-02:
# two desks sat mid-turn for 54 minutes holding the owner's "ping XD", because
# the busy stamp tells the watchdog "do not start a run" and tells the bridge
# "do not nudge" - each correct alone, deadlock together, and nothing broke the
# tie.
#
# First set to 45 min to avoid interrupting long honest work (the <project>
# build ran ~30). Wrong trade: the very next freeze was a PING stuck at 9
# minutes, and the alarm would have stayed silent for another 36. Nothing is
# interrupted by this - it only SENDS A MESSAGE - so the cost of being early is
# one notice, and the cost of being late is the owner discovering it himself.
STUCK_TURN_SECONDS = 10 * 60
STUCK_QUIET_SECONDS = 3 * 60    # a session writing more recently than this is WORKING
_stuck_notified = {}            # session -> when we last said it was stuck
CONTROL_COMMANDS = ("!kill", "!restart", "!status", "!killall", "!reload",
                    "!screen", "!desktop", "!config", "!stop", "!cron", "!model")
# An envelope's `from` is either a PERSON or the fleet talking to itself. These
# three are the fleet; "owner" and every configured guest label are people.
# Stated as an exclusion list on purpose: guests are configured, not compiled,
# so a list of people would silently omit every guest added after this line.
SYSTEM_SENDERS = ("omnius", "heartbeat", "schedule")
GUESTS = {}   # label -> guest, from config\guests.ini; reloaded with the map
DROPPED = STATE / "dropped"   # cancelled mail, kept rather than deleted.
# NOT under state\inbox\: ensure_runners() treats every folder there as a
# SESSION, so parking envelopes inside it would invent a desk named after the
# folder - and the deadman would then page him about mail he just cancelled.

# Every desk runs Opus 5 at xhigh effort (user decision 2026-07-31: "make all
# sessions by default with opus 5 and effort xhigh"). The code default is what
# travels - .env never does - so a fresh machine still gets the right
# behaviour. .env overrides per instance; start_run(model=, effort=) overrides
# per desk for the rare cheap one.
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
VALID_PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
DEFAULT_MODEL = (api.ENV.get("OMNIUS_MODEL") or "opus").strip()
DEFAULT_EFFORT = (api.ENV.get("OMNIUS_EFFORT") or "xhigh").strip().lower()
def _fleet_cfg():
    """config\\fleet.json, falling back to the pre-2026-08-05 root path.

    The fallback is not politeness: a workspace copied from an older machine,
    or one caught half-way through the move, must still spawn desks. Losing
    fleet.json only costs the shipped defaults, but silently losing it while a
    file sits right there would be the confusing kind of broken."""
    moved = ROOT / "config" / "fleet.json"
    return moved if moved.is_file() else ROOT / "fleet.json"


FLEET_CFG = _fleet_cfg()


def history_dir_for(cwd):
    """-> the ~/.claude/projects/<encoded> folder Claude Code uses for this cwd.

    The encoding is the absolute path with ':' and both slashes replaced by '-',
    e.g. <OMNIUS_ROOT> -> C--Users-<user>-omnius.
    """
    enc = str(Path(cwd).resolve()).replace(":", "-").replace("\\", "-").replace("/", "-")
    return Path.home() / ".claude" / "projects" / enc


def has_history(cwd):
    """True when `claude --continue` has something of ITS OWN to resume here.

    False means --continue would silently attach to the most recent conversation
    from another folder - see the comment in start_run(). Treat an unreadable
    home directory as "no history": starting cold is always safe, resuming the
    wrong conversation is not.
    """
    try:
        d = history_dir_for(cwd)
        return d.is_dir() and any(d.iterdir())
    except OSError:
        return False


def role_of(session):
    """Session id -> role key in fleet.json. Must agree with cwd_for()."""
    if session == "orchestrator":
        return "orchestrator"
    if session == "daybook":
        return "daybook"
    if session.startswith("tool."):
        return "tool"
    return "project"


def desk_config(session):
    """Resolve model / effort / permissionMode for a desk.

    desks[<id>] -> roles[<role>] -> defaults -> the constants above. A missing or
    broken fleet.json must never stop a spawn: the fallbacks are the shipped
    behaviour, and a config file that can wedge the fleet is worse than none.
    """
    resolved = {"model": DEFAULT_MODEL, "effort": DEFAULT_EFFORT, "permissionMode": None,
                "resume": "transcript", "window": "headless"}
    try:
        cfg = json.loads(FLEET_CFG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return resolved
    except (OSError, ValueError) as e:
        log(f"fleet.json unreadable ({e}) - using built-in defaults")
        return resolved
    for layer in (cfg.get("defaults"), (cfg.get("roles") or {}).get(role_of(session)),
                  (cfg.get("desks") or {}).get(session)):
        if isinstance(layer, dict):
            for k in ("model", "effort", "permissionMode", "resume", "window"):
                if k in layer:
                    resolved[k] = layer[k]
    return resolved


def desk_config_source(session):
    """-> {key: layer} naming WHERE each resolved value came from.

    `!model` with no argument has to answer "and why", or the next question is
    always "why is this desk on opus when I set sonnet?". Same idea as the web
    Settings page, which shows the origin of every value rather than the value
    alone.
    """
    src = {k: "built-in" for k in ("model", "effort", "permissionMode", "resume", "window")}
    try:
        cfg = json.loads(FLEET_CFG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return src
    for name, layer in (("defaults", cfg.get("defaults")),
                        (f"role:{role_of(session)}", (cfg.get("roles") or {}).get(role_of(session))),
                        ("this desk", (cfg.get("desks") or {}).get(session))):
        if isinstance(layer, dict):
            for k in src:
                if k in layer:
                    src[k] = name
    return src


def fleet_set_desk(session, model=None, effort=None, clear=False):
    """Write a per-desk model/effort override into config\\fleet.json.

    Read-modify-write of the WHOLE file, atomically, because fleet.json is
    hand-edited and full of `_comment`/`_why` keys that explain hard-won
    decisions - json round-trips them as ordinary keys, so they survive, but
    only if we never rewrite the file from a template.

    Returns the resulting desk override dict. Raises OSError/ValueError, which
    the caller reports rather than swallowing: a setting the owner believes
    took effect but did not is the failure worth avoiding here.
    """
    cfg = json.loads(FLEET_CFG.read_text(encoding="utf-8"))
    desks = cfg.setdefault("desks", {})
    if clear:
        desks.pop(session, None)
        entry = {}
    else:
        entry = desks.get(session)
        if not isinstance(entry, dict):
            entry = {}
        if model is not None:
            entry["model"] = model
        if effort is not None:
            entry["effort"] = effort
        desks[session] = entry
    FLEET_CFG.parent.mkdir(parents=True, exist_ok=True)
    tmp = FLEET_CFG.with_suffix(".tmp")
    # Trailing newline on purpose: fleet.json is TRACKED, and json.dumps does
    # not add one, so every !model would otherwise leave a spurious
    # "\ No newline at end of file" in the diff.
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(FLEET_CFG)
    return entry


# Aliases Claude Code accepts today. Deliberately NOT a closed allow-list: model
# names change, and a control command that refuses tomorrow's model because this
# tuple is stale would be the same rot that made the seed say "five, not four".
# Anything else is accepted WITH a warning - never silently, because a typo
# would otherwise surface as a failed run half an hour later.
KNOWN_MODEL_ALIASES = ("opus", "sonnet", "haiku", "fable")


def running_model(session):
    """-> (model, effort) the LIVE run was launched on, or None if nothing runs.

    Not the same question as desk_config(), and the difference is the point:
    config is what the next run will use, this is what is burning tokens now.
    They diverge the moment !model is used on a busy desk.

    Returns None for a lease written before this was stamped (an upgrade in
    flight) rather than guessing - "unknown" is honest, the config is not.
    """
    if not run_active(session):
        return None
    lease = read_lease(session) or {}
    m, e = lease.get("model"), lease.get("effort")
    return (m, e) if m or e else None


def resolved_model(session, tail_bytes=200_000):
    """-> the model id Claude ACTUALLY used at this desk, e.g. `claude-opus-5`.

    `opus` is an alias; the lease records the alias we passed, not what it
    resolved to. Claude writes the real id on every message in its own
    transcript, so that is the only place the truth exists.

    Reads the TAIL of the newest transcript, not the file: these reach tens of
    MB, and a control command must stay instant. None when it cannot be known -
    never a guess, which for a question literally about which model ran would
    be the worst possible answer.
    """
    try:
        d = history_dir_for(cwd_for(session))
        files = [f for f in d.glob("*.jsonl") if f.is_file()]
        if not files:
            return None
        newest = max(files, key=lambda f: f.stat().st_mtime)
        size = newest.stat().st_size
        with newest.open("rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()                    # drop the partial first line
            chunk = fh.read().decode("utf-8", "replace")
        for line in reversed(chunk.splitlines()):
            try:
                m = (json.loads(line).get("message") or {}).get("model")
            except ValueError:
                continue
            if m and m != "<synthetic>":
                return m
    except (OSError, ValueError):
        pass
    return None


def fmt_model(session):
    """Compact `opus/xhigh` tag for !status, marked when it is only the config."""
    live = running_model(session)
    if live:
        return f"{live[0] or '?'}/{live[1] or '?'}"
    d = desk_config(session)
    return f"({d['model']}/{d['effort']})"      # parenthesised: not running, this is the config


def parse_model_effort(args):
    """`sonnet` | `sonnet low` | `effort low` -> (model, effort, error).

    Shared by !model and !restart so the two can never drift into accepting
    different words for the same thing.
    """
    if not args:
        return None, None, None
    if args[0].lower() == "effort":
        if len(args) < 2:
            return None, None, "`effort` needs a level: " + ", ".join(f"`{e}`" for e in VALID_EFFORTS)
        model, effort = None, args[1].lower()
    else:
        model = args[0]
        effort = args[1].lower() if len(args) > 1 else None
    if effort is not None and effort not in VALID_EFFORTS:
        return None, None, (f"`{effort}` is not an effort. Pick one of: "
                            + ", ".join(f"`{e}`" for e in VALID_EFFORTS))
    return model, effort, None


def _model_when(session):
    """The honest half of !model: WHEN the new setting actually applies.

    A model is fixed for the life of a Claude process - nothing can change it
    mid-run. What a desk has instead is a series of runs, so the setting lands
    on the NEXT one. Saying "done" while an Opus run is still going would be a
    lie the owner only catches from the bill.
    """
    try:
        if run_active(session):
            return ("\n-# A run is in flight on the old setting — this applies to the next one. "
                    "`!restart` to cut over now (the conversation is kept).")
        if session_alive(session):
            # NOT "/effort in that window": launching with --effort sets a
            # launch-effort PIN, and /effort refuses to move it (verified
            # 2026-08-07). Restarting is the only honest advice for effort.
            return ("\n-# A live session is open on the old setting — this applies to the next run. "
                    "`!restart` cuts over now and keeps the conversation.")
    except Exception:                                            # noqa: BLE001
        pass
    return "\n-# Applies to this desk's next run."


def model_looks_known(name):
    n = str(name or "").strip().lower()
    return n in KNOWN_MODEL_ALIASES or bool(re.fullmatch(r"claude-[a-z0-9.\-]+", n))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Windows console (cp1252) chokes on emoji in category names (📁/🎛/🗄).
        # A logging call must never crash the always-on service.
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(enc, "replace").decode(enc), flush=True)
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / "watchdog.log", "a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {msg}\n")
    except OSError:
        pass


def process_image(pid):
    """-> the exe filename behind a pid (lowercased), or None.

    A pid is NOT an identity. Windows reuses numbers freely, and a saved pid
    outlives the process that owned it - across a reboot it very likely names
    something else entirely.
    """
    if not pid:
        return None
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return None
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        ctypes.windll.kernel32.CloseHandle(h)
        return Path(buf.value).name.lower() if ok else None
    except Exception:
        return None


def pid_alive(pid, expect=None):
    """Is this pid alive AND still the kind of process we recorded?

    `expect` is a substring of the exe name ("python", "claude"). Found the
    hard way 2026-08-02: the watchdog's lock survived a reboot holding pid
    5568, Windows had handed that number to AsusOptimizationStartupTask.exe,
    pid_alive said True, and the watchdog refused to start with "another
    watchdog is already running" - on every retry, forever. Discord was dead
    until the owner noticed.

    Boot time cannot save us either: Fast Startup leaves LastBootUpTime
    reporting the previous cold boot, so "was this written before the last
    boot?" is not answerable. Identity is.
    """
    if not pid:
        return False
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        if not (bool(ok) and code.value == STILL_ACTIVE):
            return False
    except Exception:
        return False
    if not expect:
        return True
    img = process_image(pid)
    # Unreadable image (a protected process) -> trust the liveness check rather
    # than declare a live desk dead. Wrong-but-readable is the case that bites.
    return img is None or expect in img


def acquire_lock():
    WD_STATE.mkdir(parents=True, exist_ok=True)
    lock = WD_STATE / "lock.json"
    if lock.exists():
        try:
            old = json.loads(lock.read_text(encoding="utf-8"))
            # expect="python": a reused pid belonging to some other exe is a
            # STALE lock, not a running watchdog (2026-08-02, cost a reboot).
            if pid_alive(old.get("pid"), expect="python"):
                log(f"another watchdog is already running (pid {old['pid']}) - exiting")
                sys.exit(3)
        except (json.JSONDecodeError, OSError):
            pass
    import os
    lock.write_text(json.dumps({"pid": os.getpid(), "startedAt": now_iso(),
                                "machine": api.MACHINE}), encoding="utf-8")
    return lock


def write_json_atomic(path, data):
    """Write via temp + replace. A plain write_text truncates first, so a crash
    or power cut inside that window leaves a corrupt file - and for last_ids.json
    a corrupt file means every message sent while we were down is discarded."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


DEAF_PASSES_BEFORE_EXIT = 20   # ~1 min at POLL_SECONDS=3 before we call it deafness


_claude_exe = None          # resolved once, then cached; None = never looked
_claude_warned = False


def claude_exe(recheck=False):
    """-> full path to the Claude CLI, or None. Never raises.

    2026-08-14, the move to the permanent PC. `shutil.which("claude")` returned
    None inside the service process for a solid hour while claude.exe sat in
    `%USERPROFILE%\\.local\\bin` and that folder WAS in the persisted user PATH.
    The service had started at 13:03, the installer wrote the PATH entry after
    that, and a Windows process never re-reads its environment. Measured: the
    running watchdog had 15 PATH entries and none was that one; a restarted
    watchdog had 16 and resolved instantly.

    Nothing recovered, because nothing else looks. So resolution is now four
    steps, cheapest first, and the ones past `which` exist precisely for the
    stale-environment case:

      1. config\\omnius.ini [fleet] claude_path - the explicit escape hatch
      2. shutil.which - the normal answer
      3. the PERSISTED user PATH out of the registry, which is what our own
         environment is a stale copy OF
      4. the known install locations

    Reaching step 3 or 4 means this process is running on an out-of-date
    environment, which is worth saying once: it still works, and it wants
    restarting.
    """
    global _claude_exe, _claude_warned
    if _claude_exe and not recheck:
        return _claude_exe

    configured = ""
    try:
        configured = str(ocfg.get(OMNIUS_CFG, "fleet", "claude_path",
                                  "OMNIUS_CLAUDE_PATH", "", api.ENV)).strip()
    except Exception:                                            # noqa: BLE001
        configured = ""
    if configured and Path(configured).is_file():
        _claude_exe = configured
        return _claude_exe

    found = shutil.which("claude")
    if found:
        _claude_exe = found
        return _claude_exe

    stale = []
    try:                                    # the persisted PATH, not ours
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            for entry in str(winreg.QueryValueEx(k, "Path")[0]).split(";"):
                entry = os.path.expandvars(entry.strip())
                if entry:
                    stale.append(Path(entry) / "claude.exe")
    except Exception:                                            # noqa: BLE001
        pass
    home = Path(os.path.expanduser("~"))
    stale += [home / ".local" / "bin" / "claude.exe",
              home / "AppData" / "Local" / "Programs" / "claude" / "claude.exe",
              home / "AppData" / "Roaming" / "npm" / "claude.cmd"]
    for cand in stale:
        try:
            if cand.is_file():
                _claude_exe = str(cand)
                if not _claude_warned:
                    _claude_warned = True
                    log(f"claude found at {cand} but NOT on this process's PATH - "
                        f"my environment predates the install. Working, but restart "
                        f"the Omnius Watchdog task to pick up the real PATH.")
                return _claude_exe
        except OSError:
            continue
    return None


_no_cli_alerted = False


def alert_no_cli():
    """Tell him ONCE that nothing can be spawned at all. -> True if posted.

    The whole fleet being unable to start a single desk is the loudest thing
    this process can know, and on 2026-08-14 it was written only to a log file
    he had no reason to open: two of his messages and the daily briefing went
    into a backoff nobody could see, for an hour. Post-once-then-shut-up, the
    same discipline as the missed-routine alert - a repeat every 3s would be
    its own outage.
    """
    global _no_cli_alerted
    if _no_cli_alerted:
        return False
    _no_cli_alerted = True
    try:
        cid = broadcast_channel_id()
        if not cid:
            return False
        api.send_message(cid, "🛑 **The whole fleet is down.** I cannot find the "
                              "`claude` CLI, so no desk can be started - mail will "
                              "queue, not be answered.\n"
                              "Fix on that PC: install Claude Code, then restart the "
                              "`Omnius Watchdog` task (a running service keeps the "
                              "PATH it was born with). Or set `[fleet] claude_path` "
                              "in `config\\omnius.ini`.")
        return True
    except Exception as e:                                       # noqa: BLE001
        log(f"could not post the no-CLI alert: {type(e).__name__}: {e}")
        return False


def write_beacon(channels, gateway=None):
    """Stamp proof that we are still LISTENING.

    lock.json says a process exists; the beacon says it is still listening.
    Only the second one is worth trusting.

    Two things earn a stamp, and either is enough: a REST pass that reached
    every mapped channel, or a Gateway socket that is connected and having its
    heartbeats acknowledged. Keeping the gateway case is what lets the beacon
    stay ~3s fresh after the websocket swap - status_banner.py (90s) and
    autostart.ps1 (120s) both read this file and neither should have to know
    which transport is currently carrying us."""
    try:
        # The process is the only authority on its OWN environment - the same
        # reason this file, not a live pid, is the authority on "listening".
        # A watchdog that cannot resolve the CLI is a DEAD FLEET while every
        # other signal reads green, which is exactly what happened for an hour
        # on 2026-08-14. null here is what autostart -Action status fails on.
        data = {"at": now_iso(), "channels": channels, "machine": api.MACHINE,
                "claude": claude_exe()}
        if gateway is not None:
            data["gateway"] = bool(gateway)
        write_json_atomic(WD_STATE / "beacon.json", data)
    except OSError:
        pass


class SeenIds:
    """The last N message ids we have already handled.

    Two transports now feed the same handler, so the same message can genuinely
    arrive twice: the gateway pushes it onto the queue while a REST sweep is
    mid-flight, the sweep fetches it too (its cursor has not moved yet), and the
    queued copy is drained afterwards. Both paths run on the main thread, so
    this is not a race - it is an ordering overlap, and a small memory of what
    has been handled is the cheapest correct fix. A duplicate here would mean a
    duplicate INBOX ENVELOPE, i.e. a session answering the user twice."""

    def __init__(self, size=1024):
        self._order = deque(maxlen=size)
        self._set = set()

    def add(self, mid):
        """-> False if this id was already handled (caller should skip it)."""
        if mid in self._set:
            return False
        if len(self._order) == self._order.maxlen and self._order:
            self._set.discard(self._order[0])
        self._order.append(mid)
        self._set.add(mid)
        return True

    def __contains__(self, mid):
        return mid in self._set

    def __len__(self):
        return len(self._set)


def deliver(m, cid, target, me, mapping, last_ids, persist, seen):
    """Hand one message to handle_message, once, with the cursor durable first.

    Shared by the REST sweep and the gateway drain so the durability rule cannot
    drift between them: persist the cursor BEFORE acting. A control command can
    end this process mid-handling (!reload re-execs immediately), and a cursor
    written afterwards would never be written at all - the next process re-read
    the SAME !reload and re-execed. Observed 2026-07-31: an endless restart loop,
    ~3s per cycle, that no amount of waiting would clear."""
    mid = str(m.get("id"))
    if not seen.add(mid):
        return "duplicate"
    last_ids[cid] = mid
    persist()
    try:
        return handle_message(m, cid, target, me, mapping)
    except Exception as e:
        # One malformed message must not wedge the bus: log and move on so the
        # cursor keeps advancing.
        log(f"handling message {mid} in {getattr(target, 'channel', cid)} "
            f"failed: {type(e).__name__}: {e}")
        return "error"


def drain_gateway(gw, until, mapping, me, last_ids, persist, seen):
    """Handle pushed messages until `until` (monotonic-ish wall clock).

    This IS the latency win: the call blocks on the queue, so a message becomes
    an inbox envelope the moment the socket delivers it instead of on the next
    poll tick. Returns how many were handled."""
    handled = 0
    while True:
        remaining = until - time.time()
        if remaining <= 0:
            return handled
        try:
            m = gw.events.get(timeout=remaining)
        except queue.Empty:
            return handled
        cid = str(m.get("channel_id") or "")
        target = mapping.get(cid)
        if not target:
            continue          # a guild channel that is not part of the fleet
        deliver(m, cid, target, me, mapping, last_ids, persist, seen)
        handled += 1


def rest_sweep(mapping, me, last_ids, persist, seen):
    """One full REST pass over every mapped channel. Returns (deaf, delivered).

    Unchanged in behaviour from the pre-gateway loop - it is still the thing
    that proves we can reach Discord, and still the only path that can catch up
    after any outage."""
    deaf = delivered = 0
    for cid, target in list(mapping.items()):
        # Per-channel isolation: one deleted/forbidden channel must not abort
        # the pass, which would starve every channel after it AND skip the
        # housekeeping below - deaf and mute from one bad channel.
        try:
            if cid not in last_ids:  # first sight: skip history, start at newest
                last_ids[cid] = api.latest_message_id(cid)
                continue
            for m in sorted(api.messages_after(cid, last_ids[cid]),
                            key=lambda x: int(x["id"])):
                if deliver(m, cid, target, me, mapping, last_ids, persist,
                           seen) != "duplicate":
                    delivered += 1
        except api.ApiError as e:
            log(f"channel {cid} unreachable ({e}) - skipping this pass")
            deaf += 1
    return deaf, delivered


def start_gateway():
    """-> a started Gateway, or None if it is switched off or cannot start.

    Never fatal: a watchdog that refuses to run because the websocket failed
    would be strictly worse than the polling one it replaced."""
    if not GATEWAY_ENABLED:
        log("gateway disabled (DISCORD_GATEWAY=0) - REST polling only")
        return None
    try:
        gw = gw_mod.Gateway(api.TOKEN, log=log).start()
        log(f"gateway: connecting (REST sweep every {RECONCILE_SECONDS:.0f}s as backstop)")
        return gw
    except Exception as e:
        log(f"gateway: could not start ({type(e).__name__}: {e}) - REST polling only")
        return None


def rotate_log(max_bytes=2_000_000):
    """Keep watchdog.log bounded. It had no rotation, and status_banner reads the
    whole file just to show its last 200 lines - on an always-on service that
    grows without limit."""
    p = LOGS / "watchdog.log"
    try:
        if p.exists() and p.stat().st_size > max_bytes:
            p.replace(LOGS / "watchdog.log.1")   # one generation is plenty
    except OSError:
        pass


def release_lock():
    """Drop our lock. Every exit path should call this - a lock left behind by a
    crash makes the next start look like 'another watchdog is already running'."""
    try:
        (WD_STATE / "lock.json").unlink(missing_ok=True)
    except OSError:
        pass


# --- channel map --------------------------------------------------------------

class Target:
    def __init__(self, session, channel_name, category_name):
        self.session = session          # "orchestrator" | "<project>.<component>" | None
        self.channel_name = channel_name
        self.category_name = category_name


def build_map(schema):
    """channel_id -> Target, derived from live guild structure + schema prefixes."""
    orch_cat = schema["initial"]["categories"][0]["name"]
    proj_prefix = schema["prefixes"]["project"]
    arch_prefix = schema["prefixes"]["archived"]
    # A category may claim EVERY channel inside it for one desk. Added 2026-08-06
    # for 📧 EMAIL: one channel per account, all answered by tool.email. Doing it
    # at the category level means a new account needs no code change at all - the
    # envelope carries channelId, so the reply goes back to the right account's
    # channel by itself.
    cat_sessions = {c["name"]: c["session"]
                    for c in schema["initial"]["categories"] if c.get("session")}
    chans = api.guild_channels()
    cats = {c["id"]: c["name"] for c in chans if c["type"] == api.CHANNEL_CATEGORY}
    mapping = {}
    for c in chans:
        if c["type"] != api.CHANNEL_TEXT:
            continue
        cat = cats.get(c.get("parent_id"), "")
        name = c["name"]
        session = None
        if cat == orch_cat:
            if name in ("omnius", "orchestrator"):
                # Renamed to #omnius 2026-07-31 (it is the persona, inside the
                # 🎛 ORCHESTRATOR category). BOTH names are accepted on purpose:
                # a running watchdog maps by channel name, so renaming while it
                # holds old code would unmap the channel and cut the owner off
                # entirely. Accepting both makes the rename a non-event.
                session = "orchestrator"
            elif name == "daybook":
                # Its own desk (user decision 2026-07-31): capturing notes should
                # never occupy the orchestrator, whose job is fleet coordination.
                session = "daybook"
            elif name == "fleet-status":
                # Same reasoning, one step further (user decision 2026-07-31):
                # asking "how is the fleet?" in natural language should not queue
                # behind whatever the orchestrator is doing. tool.fleet fits the
                # existing tool.<name> id shape, so §6 needs no amendment. The
                # watchdog still posts its own hello and board here.
                session = "tool.fleet"
            elif name == "transcribe":
                # The recordings desk (user decision 2026-08-06). Same reasoning
                # a third time, and here it is not merely tidy: transcribing two
                # hours takes ~25 minutes and reading the transcript back costs
                # ~40k tokens. Either one inside the orchestrator would take him
                # off the air - "i cannot ask or use omnius anymore until that
                # job is finished". The desk starts a DETACHED job and returns
                # at once; the job's completion envelope lands in this desk's
                # inbox and a fresh run does the reading.
                session = "tool.transcribe"
        elif cat in cat_sessions:
            session = cat_sessions[cat]
        elif cat.startswith(proj_prefix):
            project = cat[len(proj_prefix):].strip()
            if name == "general":
                # Single-component projects: #general goes straight to the desk.
                # Owner, 2026-08-01, watching his "create a demo PDF" get relayed:
                # "when I write in the project, it should start the project
                # session, not you again." The orchestrator relays #general only
                # when there is a genuine WHICH-component question to answer.
                try:
                    comps = api.project_components(project)
                except Exception:
                    comps = []
                session = f"{project}.{comps[0]}" if len(comps) == 1 else "orchestrator"
            else:
                session = f"{project}.{name}"
                if not (ROOT / "projects" / project / name).is_dir():
                    log(f"warn: #{name} in {cat} has no folder projects\\{project}\\{name} - unmapped")
                    session = None
        elif cat.startswith(arch_prefix):
            session = None  # archived: ignore
        if session or (cat == orch_cat):
            mapping[c["id"]] = Target(session, name, cat)
    return mapping


def reload_guests():
    """Re-read config\\guests.ini into GUESTS. -> the dict.

    Called wherever the channel map is rebuilt, so adding or removing a guest
    takes effect within MAP_REFRESH_SECONDS and needs neither a restart nor
    `!reload` (which re-execs the whole process to answer a config edit)."""
    global GUESTS
    try:
        GUESTS = ocfg.guests()
    except Exception as e:                                   # noqa: BLE001
        # Config may never take the fleet down (omnius_config rule 3). An
        # unreadable guest list means "no guests", never "everyone".
        log(f"guests.ini unreadable ({type(e).__name__}: {e}) - no guests this round")
        GUESTS = {}
    return GUESTS


def guest_for(author_id):
    """-> (label, guest) for a non-owner who is allowed on the bus, else (None, None)."""
    aid = str(author_id or "").strip()
    if not aid:
        return None, None
    for label, g in GUESTS.items():
        if str(g.get("id") or "") == aid:
            return label, g
    return None, None


def guest_may_write(guest, cid, channel_name):
    """Is this channel one the guest's entry names? By id (exact) or by name.

    Names are accepted because they are what a human types, but ids are what
    guests.example.ini recommends: channel names collide across projects - every
    project may have a #web - and this is the boundary that keeps a guest out of
    the rest of the fleet."""
    allowed = (guest or {}).get("channels") or []
    return str(cid) in allowed or str(channel_name or "") in allowed


def is_human_sender(who):
    """True when a PERSON wrote this envelope: him, or one of the guests.

    Used by the guards that decide whether someone is WAITING - a window on his
    desktop, the deaf-desk alarm. A heartbeat going unanswered is a log line; a
    person going unanswered is the failure this whole layer exists to prevent,
    and that is just as true when the person is a guest."""
    return bool(who) and str(who).strip().lower() not in SYSTEM_SENDERS


def cwd_for(session):
    # Must agree with inbox_watch.py's own id->cwd table. It already handled
    # daybook and tool.<name>; this side did not, so cwd_for("daybook") returned
    # projects\daybook\ (missing) and a daybook desk could never be spawned.
    if session == "orchestrator":
        return ROOT
    if session == "daybook":
        return ROOT / "daybook"
    if session.startswith("tool."):
        return ROOT / "tools" / session.split(".", 1)[1]
    project, _, component = session.partition(".")
    return ROOT / "projects" / project / component


def primary_channel_id(mapping, session):
    # Dot-less ids exist (orchestrator, daybook); split(".", 1)[1] raises IndexError
    # on them, which would have crashed the watchdog the first time a daybook
    # session needed its primary channel.
    if session == "orchestrator":
        # #omnius first, #orchestrator while the rename is pending. This must be
        # explicit: EVERY project's #general also maps to the orchestrator, so the
        # any-channel fallback below could otherwise answer the owner in some
        # project's channel instead of his own.
        wants = ["omnius", "orchestrator"]
    elif "." not in session:
        wants = [session]
    else:
        wants = [session.split(".", 1)[1]]
    for want in wants:
        for cid, t in mapping.items():
            if t.session == session and t.channel_name == want:
                return cid
    for cid, t in mapping.items():  # fallback: any channel of that session
        if t.session == session:
            return cid
    return None


# --- claims / sessions --------------------------------------------------------

def read_claim(session):
    p = SESSIONS / f"{session}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def session_alive(session):
    """A live presence on this desk: an interactive terminal (claim pid) or an
    active headless run (lease pid). Claims are written once at check-in - there
    is no heartbeat process any more, and that is deliberate: a lastSeenAt
    stamped by a sidecar while the session itself was gone is exactly the lie
    that made a dead desk read as '1 live' all evening (2026-08-01)."""
    c = read_claim(session)
    if c and str(c.get("machine") or "") == api.MACHINE             and pid_alive(c.get("pid"), expect="claude"):
        return True
    return run_active(session)


CLAUDE_CFG = Path.home() / ".claude.json"


def ensure_trusted(folder):
    """Pre-accept Claude Code's folder trust dialog (machine-local user config)
    so an unattended spawn never stalls on the 'Security guide' prompt. Only
    ever called for folders inside this workspace - the watchdog is the
    machine's agent and the workspace is its own."""
    try:
        cfg = json.loads(CLAUDE_CFG.read_text(encoding="utf-8")) if CLAUDE_CFG.exists() else {}
    except (OSError, json.JSONDecodeError):
        return False
    key = str(folder).replace("\\", "/")
    proj = cfg.setdefault("projects", {}).setdefault(key, {})
    if proj.get("hasTrustDialogAccepted"):
        return True
    proj["hasTrustDialogAccepted"] = True
    try:
        tmp = CLAUDE_CFG.with_suffix(".json.omnius-tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        tmp.replace(CLAUDE_CFG)
        log(f"pre-trusted folder for spawn: {key}")
        return True
    except OSError as e:
        log(f"could not pre-trust {key}: {e}")
        return False


# --- headless runs --------------------------------------------------------------
#
# THE simplification of 2026-08-01, bought with a night of duplicate
# orchestrators: a desk is no longer a long-lived terminal that must keep itself
# reachable - it is a SERIES OF RUNS. A message arrives, the watchdog starts
# `claude -p "/omnius"` headless in that folder, the run drains the inbox, does
# the work, replies via outbox, and exits. Continuity lives in the conversation
# transcript (--continue), not in a process that must survive.
#
# What this deletes, deliberately: session-side inbox watchers (turn-based
# sessions cannot host daemons - the harness stopped the watcher three times in
# one evening, and every death either left the desk deaf or invited a duplicate
# brain), claim heartbeats stamped by sidecars (they made dead desks look
# alive), and the whole heal-deaf-desks/notify-deaf apparatus (nothing can go
# deaf: nothing has to stay armed). The watchdog OWNS the process it spawned -
# busy/done is proc.poll() on a handle it holds, not a seance over claim files.
#
# One desk, one run at a time. While a run is active new envelopes queue in the
# inbox; when it exits, ensure_runners() starts the next run if mail is waiting.

RUNNING = {}          # session -> subprocess.Popen (this watchdog's own children)
_run_failures = {}    # session -> consecutive failed runs
_run_backoff = {}     # session -> unix ts before which we will not retry
_run_oldest = {}      # session -> oldest envelope name the run was started for
_run_alerted = set()  # sessions whose crash-loop the owner has already heard about


TAB_GRACE_SECONDS = 150   # terminal spawn -> claude boot -> /omnius check-in, generously


def read_lease(session):
    try:
        return json.loads((RUNS / f"{session}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def tab_title(session, ascii_only=False):
    """Windows Terminal tab title for a visible desk. Mirrors the Discord
    category emoji so a tab and its channel read as one desk at a glance.
    ascii_only strips emoji for the cmd.exe fallback (cp1252 title mangling)."""
    sep = " - " if ascii_only else " · "
    if session == "orchestrator":
        title = "🎛 Omnius"
    elif session == "daybook":
        title = "📓 Daybook"
    elif session.startswith("tool."):
        title = f"🔧 {session.split('.', 1)[1]}"
    elif "." in session:
        project, component = session.split(".", 1)
        title = f"📁 {project}{sep}{component}"
    else:
        title = session
    if ascii_only:
        title = "".join(c for c in title if ord(c) < 128).strip()
    return title


def human_mail_waiting(session):
    """True if any queued envelope came from a PERSON, rather than from the system.

    Heartbeats and scheduled jobs are the fleet talking to itself. They are
    worth a headless run and never worth a window on his desktop. Mail from a
    guest (config\\guests.ini) counts as a person: he is the developer at this
    machine, and someone writing into one of his projects is exactly what he
    would want the window for."""
    try:
        for f in (INBOX / session).glob("*.json"):
            try:
                if is_human_sender(json.loads(f.read_text(encoding="utf-8")).get("from")):
                    return True
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return False


def oldest_envelope(session):
    """Name of the oldest queued envelope, or None. Names sort chronologically."""
    try:
        names = sorted(f.name for f in (INBOX / session).glob("*.json"))
    except OSError:
        return None
    return names[0] if names else None


def run_active(session):
    """True while a headless run - or a terminal still booting - owns this desk.

    Our own child first (authoritative: we hold the handle). Then the lease
    file, which is what survives a watchdog restart: the new watchdog cannot
    hold the old handle, but the lease pid is enough to ADOPT the run instead
    of putting a second brain onto a busy desk.

    A `mode:"terminal"` lease has no pid to validate (wt detaches): it covers
    the boot window between opening the tab and the session's check-in writing
    a claim - without it, the 3s poll loop would open a tab per pass. The
    claim, once it exists, takes over as the desk's presence."""
    p = RUNNING.get(session)
    if p is not None and p.poll() is None:
        return True
    lease = read_lease(session)
    if not lease:
        return False
    if lease.get("mode") == "terminal":
        c = read_claim(session)
        if c and str(c.get("machine") or "") == api.MACHINE and pid_alive(c.get("pid")):
            (RUNS / f"{session}.json").unlink(missing_ok=True)   # booted: claim governs now
            return False
        try:
            age = time.time() - datetime.strptime(
                lease.get("startedAt", ""), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            age = TAB_GRACE_SECONDS + 1
        if age < TAB_GRACE_SECONDS:
            return True
        # The tab never claimed. That is a FAILED start, not a free retry:
        # without this the desk got a fresh window every TAB_GRACE_SECONDS,
        # forever - 4 windows before the owner caught it on 2026-08-02, and it
        # would have been hundreds by lunchtime. Same ledger as failed runs, so
        # it backs off and the owner hears about it after RUN_FAILURES_BEFORE_ALERT.
        (RUNS / f"{session}.json").unlink(missing_ok=True)
        fails = _run_failures.get(session, 0) + 1
        _run_failures[session] = fails
        _run_backoff[session] = time.time() + RUN_BACKOFF_SECONDS
        log(f"terminal for {session} never claimed within {TAB_GRACE_SECONDS}s "
            f"(failure #{fails}) - backing off {RUN_BACKOFF_SECONDS}s")
        if fails >= RUN_FAILURES_BEFORE_ALERT and session not in _run_alerted:
            _run_alerted.add(session)
            try:
                cid = primary_channel_id(build_map(api.load_schema()), session)
                if cid:
                    api.send_message(cid, f"🛑 `{session}`: its desk window opens but never "
                                          f"connects ({fails} tries). Something in that desk's "
                                          f"setup is refusing the prompt — check the window, and "
                                          f"`state\\logs\\bridge-{session}.log` if it is a bridge.")
            except Exception as e:
                log(f"tab-loop alert failed for {session}: {e}")
        return False
    if pid_alive(lease.get("pid"), expect="claude"):
        return True
    if session not in RUNNING:
        # a dead lease from a previous watchdog: nothing owns it, clean it up
        (RUNS / f"{session}.json").unlink(missing_ok=True)
    return False


def _desktop_hosted(p):
    """True when this claude process is a conversation inside the Claude
    desktop APP, not a terminal at a desk.

    Found live 2026-08-03 22:01, at the end of the deaf-desk chain: the
    takeover asked to close "his window" on the orchestrator desk, and the pid
    it aimed at was a background conversation hosted by the desktop app (its
    parent chain ends in AnthropicClaude\\app-*\\claude.exe). Saying "ok"
    would have killed the very chat he was typing into. App conversations are
    chats, not desks - they never hold a desk's terminal, so they are never
    the thing a takeover needs cleared.

    Fail open (False): if ancestry cannot be read, keep the old behaviour -
    the worst that does is ASK, which is safe by construction."""
    try:
        for anc in p.parents():
            try:
                if "anthropicclaude" in (anc.exe() or "").lower():
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _bridge_owned(p):
    """True when this claude was started BY a desk bridge - ours, not his.

    Ancestry, not a file. The presence file names ONE bridge, so the moment a
    second terminal opens for the same desk it stops describing the first, and
    the first bridge's claude becomes indistinguishable from a window he opened
    by hand. On 2026-08-05 that ate its own tail: the desk asked to take the
    desk from "his" session, took it, opened a fresh terminal, and the new
    bridge made the previous one look native again - seven windows, and
    !kill could not win the race because it was killing one process out of a
    loop that immediately rebuilt it.

    A parent chain cannot be overwritten by the next spawn."""
    try:
        for anc in p.parents():
            try:
                if "desk_bridge.py" in " ".join(anc.cmdline() or []):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def native_sessions(session):
    """-> pids of claude processes the OWNER started in this desk's folder.

    A native session (he just ran `claude` there) has no claim unless he also
    ran /omnius, so the bus cannot see it - and it holds the desk's
    conversation. When mail arrives for a desk he is not sitting at, that
    window has to go before a bridge can own the desk (his rule, 2026-08-03:
    "if for some reason I forgot to close a native CLI window, you close them
    and then start the bridge session").

    cwd is the only reliable discriminator, and WMI cannot report it - psutil
    reads the PEB. Bridge-owned claude processes are excluded by pid, so this
    never returns the very session the bridge is driving.
    """
    try:
        import psutil
    except ImportError:
        log("psutil missing - cannot see native sessions (pip install psutil)")
        return []
    want = str(cwd_for(session)).lower()
    mine = set()
    lease = read_lease(session)
    if lease and lease.get("pid"):
        mine.add(int(lease["pid"]))
    try:
        bd = json.loads((BRIDGES / f"{session}.json").read_text(encoding="utf-8"))
        # the bridge process itself, and the claude it owns (recorded on claim)
        mine.add(int(bd.get("pid") or 0))
        for child in psutil.Process(int(bd["pid"])).children(recursive=True):
            mine.add(child.pid)
    except Exception:
        pass
    out = []
    for p in psutil.process_iter(["pid", "name"]):
        if "claude" not in (p.info["name"] or "").lower() or p.info["pid"] in mine:
            continue
        try:
            if str(p.cwd()).lower() == want and not _desktop_hosted(p) \
                    and not _bridge_owned(p):
                out.append(p.info["pid"])
        except Exception:
            continue          # protected or gone: not something we can act on
    return out


NATIVE_IN_USE_SECONDS = 15 * 60   # a session that wrote this recently has a human in it


def native_in_use(session):
    """-> seconds since a native session on this desk last did anything, or None.

    "Close the window I forgot about" and "kill the session I am working in"
    are the same action seen from two sides, and only RECENCY tells them apart.
    The owner's rule says forgotten; his live work is not forgotten.

    Evidence is Claude Code's own conversation file for that cwd: it is written
    on every exchange, so its mtime is when that session last did something.
    The busy stamp only covers a turn actually in flight, and he is idle
    between turns while very much present.
    """
    try:
        d = history_dir_for(cwd_for(session))
        newest = max((f.stat().st_mtime for f in d.glob("*.jsonl")), default=None)
    except (OSError, ValueError):
        return None
    return (time.time() - newest) if newest else None


def close_native_sessions(session, force=False):
    """Close the owner's windows on this desk. ONLY when he said so. -> pids.

    `force` is not an optimisation, it is the entire contract: this runs from
    answer_takeover() and nowhere else.

    It used to auto-close once a window had been quiet for 15 minutes. That is
    how it killed a session he was actively working in on 2026-08-03: it asked
    at 12 minutes, he had not answered yet, the window crossed 15, and the
    guard released and took it. **Asking and then acting anyway is worse than
    either policy alone** - it teaches him the question is decorative.

    And the measurement was wrong too: "quiet" came from the conversation
    file's mtime, which only moves when he SUBMITS a prompt. Reading output or
    thinking for twelve minutes looks identical to an abandoned window.
    """
    pids = native_sessions(session)
    if not pids:
        return []
    if not force:
        log(f"{session}: native session(s) {pids} present - only he can close them")
        return []
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=NO_WINDOW)
    # `idle` died with the auto-close path this used to describe; referencing
    # it here raised NameError inside handle_message on the FIRST real "ok"
    # (2026-08-03). The takeover still happened - the exception landed after
    # the kill - so it only cost the confirmation message, which is exactly
    # the kind of half-failure that makes a working system look broken.
    log(f"{session}: closed native session(s) {pids} - he approved the takeover")
    return pids


BRIDGE_DELIVER_SECONDS = 90     # a live bridge gets this long to get mail into a turn


def turn_stalled(session):
    """True when this desk's turn is provably FROZEN on a local dialog.

    Written by permission_relay.py when an ask times out and it falls back to
    the local prompt. That fallback is fine at a keyboard and useless when he
    is away: the dialog is on a machine nobody is looking at.

    This exists because the .busy stamp cannot tell working from wedged - both
    are "a turn is running". 2026-08-04, his first message from home: the desk
    asked to run a PowerShell pipeline, the ask timed out at 15:29, the session
    sat on the local dialog, and the stamp kept every guard quiet for 2.5 h
    while two of his messages rotted in the inbox. The evidence was on disk the
    whole time; nothing read it.
    """
    return (PERMS / f"{session}.stalled").is_file()


def turn_died(session):
    """True when this desk's turn ENDED IN AN API ERROR and nothing followed.

    2026-08-14, `tool.transcribe`: his message started a turn at 09:34, the API
    answered **529 Overloaded** at 09:37, and the turn stopped there. Claude
    Code does not run its Stop hook on that path, so the `.busy` stamp survived
    a turn that no longer existed - and because the session itself was still
    alive and idle, `turn_busy()` kept saying yes. Every recovery is gated on
    that one bit: the bridge would not nudge, `bridge_not_delivering` returned
    False, `recover_bridge` never ran, and two of his messages sat unread with
    nothing on any surface able to move them. Only the deaf-desk alarm fired,
    which is the whole reason that alarm consults an outcome instead of a
    classification.

    Why THIS signal and not "the transcript went quiet": quiet is a guess, and
    `lessons.md` rejects acting on it - a slow turn and a dead one look
    identical, and the honest 25-minute turn measured on 2026-08-12 had gaps of
    120s. An API error as the LAST thing in the conversation is not a guess. It
    is the turn reporting its own death, in writing.

    Two guards keep it honest:
      - the error must POSTDATE the stamp, or it belongs to an earlier turn;
      - the file must have been still for API_ERROR_QUIET_SECONDS, because
        Claude Code retries these, and a retry writes and makes the error no
        longer last.
    """
    stamp = TURNS / f"{session}.busy"
    try:
        if not stamp.is_file():
            return False
        d = history_dir_for(cwd_for(session))
        files = [p for p in d.glob("*.jsonl")] if d.is_dir() else []
        if not files:
            return False
        f = max(files, key=lambda p: p.stat().st_mtime)
        when = f.stat().st_mtime
        if when < stamp.stat().st_mtime:
            return False                      # error predates this turn
        if time.time() - when < API_ERROR_QUIET_SECONDS:
            return False                      # may still be retrying
        # Tail only: these files reach tens of MB and this runs every poll.
        with open(f, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            tail = fh.read().decode("utf-8", "replace")
        for line in reversed(tail.splitlines()):
            try:
                e = json.loads(line)
            except ValueError:
                continue                      # a partial first line, or noise
            if e.get("type") != "assistant":
                continue
            content = (e.get("message") or {}).get("content")
            blocks = content if isinstance(content, list) else []
            text = " ".join(str(b.get("text") or "") for b in blocks
                            if isinstance(b, dict))
            return text.strip().startswith("API Error")
        return False
    except (OSError, ValueError):
        return False                          # cannot tell -> leave the stamp alone


def turn_busy(session):
    """True when a turn really is in progress on this desk.

    NOT just "the .busy stamp exists". The stamp is written by the
    UserPromptSubmit hook and cleared by the Stop hook - so closing a terminal
    mid-turn, or killing its window, ORPHANS it. Nothing then clears it, and
    the watchdog stands aside forever for a session that no longer exists:
    mail is delivered, never picked up, and every surface says "a turn is
    running, it is just slow".

    Cost of not checking, measured on a fresh install 2026-08-11: he ran
    `claude` once to authenticate - exactly as the guide tells him to - closed
    the window, and his first Discord message was never answered. The alarm
    even said "mid-turn for 10 minutes ... it has written nothing for 10m".
    It was right that nothing was working and wrong about why.

    So: a stamp is only believed while something alive could have written it -
    a claimed terminal, or a run we own. Otherwise it is debris.
    """
    if not (TURNS / f"{session}.busy").is_file():
        return False
    # A turn that reported its own death is over, however alive the session
    # that ran it still looks (turn_died, 2026-08-14). Clear the debris here
    # rather than at each caller: this is the one bit every recovery path
    # reads, so fixing it in one place is what actually unwedges the desk.
    if turn_died(session):
        log(f"{session}: its turn ended in an API error - releasing the stale busy stamp")
        (TURNS / f"{session}.busy").unlink(missing_ok=True)
        return False
    try:
        return bool(session_alive(session) or run_active(session))
    except Exception:                                            # noqa: BLE001
        return True          # cannot tell -> assume busy, never double-drive a desk


def bridge_not_delivering(session):
    """True when a LIVE bridge has failed to get waiting mail into a turn.

    The last hole in the transport, and the one that cannot be closed by
    checking whether the bridge process is alive: a bridge can be perfectly
    healthy and still not deliver - it nudged while the session was wedged, or
    its claude stopped reading, or the nudge landed somewhere that ate it.
    Every surface then says "handed to the live bridge (warm session)" and the
    owner waits forever (seen 2026-08-02, 11:23 -> nothing, until he opened a
    fourth terminal himself).

    The honest test is not liveness but PROGRESS: mail waiting, and no turn
    started. A bridge that is working has a busy stamp within seconds - it
    types the nudge in under a second and retries every four.

    The clock runs from whichever came LAST: the mail arriving, or this bridge
    starting. Both are required. Measuring the envelope's age alone spawned 87
    windows in four minutes on 2026-08-03: three messages had waited 4.6 hours
    because his own window held the desk, so every replacement bridge was born
    hours past a 90-second deadline and was shot three seconds later, its own
    log still reading "session still booting". A bridge that started ten
    seconds ago has not failed at anything yet.
    """
    n, oldest = inbox_backlog(session)
    if not n:
        return False
    if turn_busy(session) and not turn_stalled(session):
        return False                     # a turn IS running: delivery worked, it is just slow
    # Second brake, independent of the clock: replacing a desk's window is a
    # remedy, and a remedy that has not worked three times is not a remedy.
    # Stop churning his desktop and let the alert stand.
    if _run_failures.get(session, 0) >= RUN_FAILURES_BEFORE_ALERT:
        return False
    held = min(oldest, time.time() - bridge_started_at(session))
    return held >= BRIDGE_DELIVER_SECONDS


def bridge_started_at(session):
    """-> epoch seconds this desk's bridge started, or now if unknown.

    Unknown means NOT YET OVERDUE, never overdue: a bridge we cannot date is
    one we must not shoot."""
    try:
        d = json.loads((BRIDGES / f"{session}.json").read_text(encoding="utf-8"))
        return datetime.strptime(d["startedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError):
        return time.time()


def recover_bridge(session):
    """Replace a bridge that is not delivering. -> status token.

    Safe to do unasked because the bridge is OURS: the watchdog started it, it
    holds no work of the owner's, and its conversation survives on disk. That
    is exactly why native windows are asked about and this one is not.
    """
    pids = []
    try:
        d = json.loads((BRIDGES / f"{session}.json").read_text(encoding="utf-8"))
        if pid_alive(d.get("pid"), expect="python"):
            pids.append(d["pid"])
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=NO_WINDOW)
    (BRIDGES / f"{session}.json").unlink(missing_ok=True)
    (TURNS / f"{session}.busy").unlink(missing_ok=True)
    n, oldest = inbox_backlog(session)
    fails = _run_failures.get(session, 0) + 1
    _run_failures[session] = fails
    log(f"{session}: bridge alive but not delivering ({n} unread for {int(oldest)}s) "
        f"- replaced it (failure #{fails})")
    started = start_run(session)
    # Repeated replacement means the fault is not the bridge. Say so rather
    # than churning windows on his desktop for the rest of the evening.
    if fails >= RUN_FAILURES_BEFORE_ALERT and session not in _run_alerted:
        _run_alerted.add(session)
        try:
            cid = primary_channel_id(build_map(api.load_schema()), session)
            if cid:
                api.send_message(cid, f"🛑 `{session}`: mail keeps not being picked up, even "
                                      f"after replacing its window {fails} times. Something on "
                                      f"that desk is wedged — `!restart` it, or look at "
                                      f"`state\\logs\\bridge-{session}.log`.")
        except Exception as e:
            log(f"bridge-recovery alert failed for {session}: {e}")
    return "bridge-replaced" if started else "start-failed"


def bridge_active(session):
    """True while a desk bridge (tools\\bridge\\desk_bridge.py) owns this desk.

    A bridge is a warm terminal the owner can also sit at; it types waiting
    mail into that live session itself. Starting a headless run alongside it
    would be the two-brains failure with extra steps - and would waste the
    whole point, which is that the session is already warm.

    Pid liveness only, no heartbeat: if the bridge dies, its file goes with it
    (it withdraws on exit) or the pid check fails, and the desk falls straight
    back to headless runs. Slow, never deaf."""
    try:
        d = json.loads((BRIDGES / f"{session}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if pid_alive(d.get("pid"), expect="python"):
        return True
    (BRIDGES / f"{session}.json").unlink(missing_ok=True)   # stale: it crashed
    return False


def permission_pending(session):
    """True while this desk is waiting on a permission decision.

    Two shapes, both written by permission_relay: an unanswered request
    (`<tool_use_id>.json`, which names its session) while the relay is still
    waiting, and `<sid>.stalled` once it gave up and fell back to the local
    dialog. Either one means a REAL turn is parked, not a dead one - which is
    the single case where a silent conversation file must not be read as "the
    turn is over".
    """
    if (PERMS / f"{session}.stalled").is_file():
        return True
    try:
        for req in PERMS.glob("*.json"):
            try:
                data = json.loads(req.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("session") == session:
                return True
    except OSError:
        pass
    return False


def turn_silent_for(stamp):
    """-> seconds since THIS turn's conversation was last written, or None.

    Not `native_in_use`, and the difference matters: that one asks "has anything
    happened in this desk's FOLDER", which answers about the desk, over a folder
    the turn may have walked out of. This asks about the turn itself. The stamp
    records Claude Code's own session id (turn_start_hook, 2026-08-12) and
    conversation files are named after it, so the exact file is one glob away
    regardless of which folder the session started in or wandered into.

    None means "cannot tell" - an older stamp with no session id, an unreadable
    home directory - and every caller must treat that as busy. Guessing "over"
    from missing evidence is how a live turn gets a second brain.
    """
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
        claude_session = data.get("claudeSession") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
    if not claude_session:
        return None
    try:
        history = Path.home() / ".claude" / "projects"
        newest = max((f.stat().st_mtime
                      for f in history.glob(f"*/{claude_session}.jsonl")), default=None)
    except (OSError, ValueError):
        return None
    return (time.time() - newest) if newest else None


def interactive_busy(session):
    """True while a person's own terminal is mid-turn on this desk.

    The .busy stamp is written by the UserPromptSubmit hook and removed by the
    Stop hook. While it stands, starting a headless --continue would put two
    concurrent writers on ONE conversation - the two-brains failure. Queued
    mail waits; the moment the turn ends, ensure_runners() follows up in the
    same conversation.

    Validation: a live claim pid proves the stamp (a dead terminal cannot be
    mid-turn - the Stop hook only misses when the process died). With no claim
    to check it against, trust the stamp for BUSY_ORPHAN_SECONDS, then treat it
    as litter from a crashed turn.

    LIVENESS IS CHECKED FIRST, and that ordering is the whole function. It used
    to classify first: any stamp older than STUCK_TURN_SECONDS with mail waiting
    returned True as "a stuck turn" without ever asking whether the process
    still existed. On 2026-08-03 his "Hola" reached a warm session, the session
    answered two permission asks and then died at a third, and the desk called
    itself mid-turn for TWO HOURS over a corpse - the deaf-desk failure the run
    model was supposed to have deleted, rebuilt out of a stamp nobody could
    invalidate. A dead writer's stamp is litter within one poll, always."""
    stamp = TURNS / f"{session}.busy"
    try:
        age = time.time() - stamp.stat().st_mtime
    except OSError:
        return False
    c = read_claim(session)
    if c and str(c.get("machine") or "") == api.MACHINE:
        # expect="claude": a recycled pid must not keep a dead desk "busy".
        if not pid_alive(c.get("pid"), expect="claude"):
            stamp.unlink(missing_ok=True)     # terminal is gone; the stamp is litter
            return False
    elif age >= BUSY_ORPHAN_SECONDS:
        stamp.unlink(missing_ok=True)         # nothing to validate against, and ancient
        return False
    # A live process is not a live turn. Everything above only proved that the
    # SESSION exists, and a desk's session outlives every turn it runs - so a
    # Stop hook that did not fire leaves a stamp no pid check can ever kill.
    # Ask the turn's own conversation file instead: still being written = really
    # working, silent for a quarter of an hour = the turn ended and nothing said
    # so. Never released while a permission decision is pending, because a turn
    # parked at a dialog is genuinely mid-turn and writes nothing either.
    if age > BUSY_SILENT_SECONDS and not permission_pending(session):
        silent = turn_silent_for(stamp)
        if silent is None:
            # A stamp from before the session id was recorded in it. Coarser
            # evidence, same question: has ANY conversation in this desk's own
            # folder moved? Still None (no history at all) -> stays busy.
            silent = native_in_use(session)
        if silent is not None and silent > BUSY_SILENT_SECONDS:
            stamp.unlink(missing_ok=True)
            log(f"{session}: busy stamp released - its conversation has been silent "
                f"for {int(silent // 60)}m, so the turn is over and the Stop hook missed it")
            return False
    # Past here the session is PROVABLY ALIVE, so a long turn is a real turn.
    # A stuck turn is REPORTED, never auto-released. Releasing the stamp would
    # let the bridge nudge, and a nudge is keystrokes: "/omnius" + Enter typed
    # into a desk frozen on a permission dialog would ANSWER that dialog. An
    # auto-clicker for permission prompts is a far worse bug than late mail, so
    # the deadlock is broken by telling the owner, not by guessing for him.
    # A LONG turn and a FROZEN turn look identical on the clock. They do not
    # look identical on disk: a working session writes its conversation file
    # continuously, and one stopped at a dialog writes nothing at all.
    #
    # 2026-08-03, the false alarm this exists to prevent: he was 11 minutes
    # into real work, his session had written 36 seconds earlier, no permission
    # was pending - and the alarm told him it was "most likely frozen on a
    # local permission dialog". A warning that fires during honest work is
    # worse than none, because the next real one gets ignored too.
    if age > STUCK_TURN_SECONDS and inbox_backlog(session)[0]:
        quiet = native_in_use(session)
        if quiet is None or quiet > STUCK_QUIET_SECONDS:
            report_stuck_turn(session, age, quiet)
    return True


_native_notified = {}
TAKEOVER = STATE / "takeover"          # <session>.json = "asked, waiting for ok/no"


def takeover_pending(session):
    try:
        return json.loads((TAKEOVER / f"{session}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def report_native_in_use(session):
    """ASK whether to take the desk. Do not narrate a state and leave him guessing.

    First version reported the situation ("I have not touched it") and offered
    two things to do at the keyboard - useless when the whole point is that he
    is NOT at the keyboard. His correction, 2026-08-03: ask, and let "ok" mean
    take it. So this is a question with a verb, answerable from a phone.

    While the desk is genuinely mid-turn it is not even a question yet: the
    honest answer is "it is busy, wait", and asking would invite him to kill
    work in flight.
    """
    # Ask once per MESSAGE HE SENDS, never on a timer. His rule, 2026-08-03:
    # "you ask in discord, if i ignore it, do nothing. Whenever i write again
    # in discord you ask again." Ignoring is a valid answer that costs him
    # nothing, and the next thing he writes is the natural moment to re-offer.
    # A clock-based throttle got this wrong in both directions: it nagged while
    # he was mid-thought, and stayed silent when he wrote again.
    newest = None
    try:
        names = sorted(f.name for f in (INBOX / session).glob("*.json"))
        newest = names[-1] if names else None
    except OSError:
        pass
    if newest and _native_notified.get(session) == newest:
        return                                   # already asked about this one
    _native_notified[session] = newest
    n, _ = inbox_backlog(session)
    idle = native_in_use(session)
    busy = turn_busy(session)
    mins = int((idle or 0) // 60)

    if busy:
        # Working right now. State it and promise the follow-up; no question.
        log(f"{session}: owner's session is mid-turn - {n} queued, not asking yet")
        text = (f"⏳ Your session on `{session}` is **working right now**, so I have not "
                f"interrupted it — {n} message(s) waiting.\n"
                f"I will ask again once it finishes. If it is stuck, `!restart` takes the "
                f"desk immediately.")
    else:
        try:
            TAKEOVER.mkdir(parents=True, exist_ok=True)
            (TAKEOVER / f"{session}.json").write_text(json.dumps(
                {"session": session, "askedAt": time.time(), "at": now_iso()}),
                encoding="utf-8")
        except OSError:
            pass
        log(f"{session}: asking whether to take the desk from the owner's own session")
        text = (f"🧑‍💻 **`{session}` is open at your desk** (last used {mins}m ago) and "
                f"{n} message(s) are waiting here.\n\n"
                f"**`ok`** — close that window and answer from here\n"
                f"**`no`** — leave it alone; I will stay quiet and the messages keep waiting\n\n"
                f"*Or just answer at the desk with `/omnius` — that clears the queue too.*")
    try:
        cid = primary_channel_id(build_map(api.load_schema()), session)
        if cid:
            api.send_message(cid, text)
    except Exception as e:
        log(f"native-in-use notice failed for {session}: {e}")


def answer_takeover(text, session):
    """Interpret ok/no as the answer to a pending takeover. -> reply, or None.

    Only ever consulted when this desk actually asked, so ordinary chat is
    never swallowed."""
    if not session or not takeover_pending(session):
        return None
    head = (text or "").lower().strip().strip(".,!`").split(" ")[0]
    if head in ALLOW_WORDS:
        (TAKEOVER / f"{session}.json").unlink(missing_ok=True)
        pids = close_native_sessions(session, force=True)
        _native_notified.pop(session, None)
        log(f"{session}: owner approved the takeover - closed {pids or 'nothing'}")
        # Built outside the f-string on purpose. Nesting the same quote inside
        # an f-string expression is PEP 701 - valid from 3.12, a hard
        # SyntaxError before it - and we promise 3.10+. On a fresh install with
        # Python 3.11 this file would not even parse, so the watchdog
        # crash-looped every 60 seconds (2026-08-11). tools\check_py_compat.py
        # now fails the build on it.
        pid_note = " (pid {})".format(", ".join(str(p) for p in pids)) if pids else ""
        return (f"✅ Took `{session}` — closed your window{pid_note}. "
                f"Answering here now.")
    if head in DENY_WORDS:
        # "no" answers THIS message and nothing more - no timer, no mute
        # window. His correction, 2026-08-03: "is it not just better off until
        # i write in discord again?" It is, and a 30-minute silence
        # contradicted his own rule, that the next thing he writes asks again.
        # A clock has now been the wrong shape here twice.
        (TAKEOVER / f"{session}.json").unlink(missing_ok=True)
        newest = None
        try:
            names = sorted(f.name for f in (INBOX / session).glob("*.json"))
            newest = names[-1] if names else None
        except OSError:
            pass
        _native_notified[session] = newest        # answered for this one; next asks
        log(f"{session}: owner declined the takeover - staying quiet")
        return (f"👍 Leaving `{session}` alone. Your messages stay queued — answer at the "
                f"desk with `/omnius`, or say `ok` here whenever you want me to take it.")
    return None


def report_stuck_turn(session, age, quiet=None):
    """Say it out loud, once per hour per desk, and name the EVIDENCE.

    Silence here is what cost the owner 54 minutes: the desk looked busy, the
    mail looked queued, and every surface agreed nothing was wrong.

    But do not invent a cause. The first version asserted "most likely frozen
    on a local permission dialog" whether or not one existed, and said it once
    while he was simply doing long work. Report what is known - how long since
    that session wrote anything - and name a dialog only when there really is
    one pending."""
    if time.time() - _stuck_notified.get(session, 0) < 3600:
        return
    _stuck_notified[session] = time.time()
    n, _ = inbox_backlog(session)
    note = stall_note(session).strip(" ·")
    why = note or (f"it has written nothing for {int(quiet // 60)}m, so it is not working"
                   if quiet else "it has not written anything since")
    log(f"{session}: turn stuck for {int(age / 60)}m with {n} unread - notifying the owner")
    try:
        cid = primary_channel_id(build_map(api.load_schema()), session)
        if cid:
            api.send_message(cid, f"⚠️ `{session}` has been mid-turn for {int(age / 60)} minutes "
                                  f"with {n} message(s) waiting — {why}.\n"
                                  f"Nothing is lost, but nothing will move until that turn ends. "
                                  f"Answer it in that window, or reply **`!restart`** here and I "
                                  f"will restart the desk and deliver the queue to a fresh one.")
    except Exception as e:
        log(f"stuck-turn notice failed for {session}: {e}")


def project_settings(session, cwd):
    """-> the PROJECT settings file to pass with --settings, or None.

    Only a project component has one: settings do not inherit across folders,
    so `projects\\<p>\\<c>` needs `projects\\<p>\\.claude\\settings.json`
    passed explicitly or it runs with no pre-approvals.

    The test is "is this cwd literally `<root>\\projects\\<p>\\<c>`" - stated
    positively, because both looser versions have now been wrong:

    - `session != "orchestrator"` sent `daybook` and `tool.<x>` (one level
      down) the ROOT settings file. Its hook commands are relative to
      ${CLAUDE_PROJECT_DIR}, which for that session is the DESK folder, so
      every hook path pointed at a file that does not exist. A failing
      UserPromptSubmit hook BLOCKS the prompt: the daybook desk could not
      accept `/omnius`, never claimed, and the watchdog opened it a fresh
      window every 150s all morning (2026-08-02, found on reboot).
    - `cwd.parent != ROOT` then sent the ORCHESTRATOR the owner's personal
      `~\\.claude\\settings.json`, because the root's parent is his home
      directory. Caught by verification before it shipped, one commit later.
    """
    if cwd.parent.parent != ROOT / "projects":
        return None
    p = cwd.parent / ".claude" / "settings.json"
    return p if p.is_file() else None


def open_tab(session, cwd, model, effort):
    """Open a VISIBLE desk window: the BRIDGE, not a bare claude tab.

    The one place the watchdog is allowed to make a window, and only for
    window:"terminal" desks with no live claim yet. No creationflags: the
    window is the point.

    It launches tools\\bridge\\desk_bridge.py rather than claude directly, and
    that is not cosmetic. A bare tab is warm but UNREACHABLE - the bus cannot
    type into it - so mail arriving afterwards would start a headless
    `--continue` run against the very conversation sitting idle in that
    window: two writers, one transcript. The bridge makes the window itself
    the recipient, which is the entire point of the 2026-08-02 rebuild. It
    also names the tab after the desk, since claude renames every tab "Claude
    Code" and six of those are indistinguishable.

    The lease has mode:"terminal" and no pid (wt hands off and exits);
    run_active() honours it for TAB_GRACE_SECONDS, then the desk's own claim
    takes over - and an expired one counts as a failure, so a desk that cannot
    boot never papers the desktop with windows.
    """
    # cmd /c, not /k: a killed or closed desk must take its WINDOW with it.
    # With /k the shell outlived the bridge, so every kill/restart left a dead
    # tab behind and they piled up (owner, 2026-08-02: "daybook opened 2 tabs").
    # `|| pause` keeps the window only on a non-zero exit, so a genuine startup
    # failure is still readable - and state\logs\bridge-<id>.log has it too.
    bridge = ROOT / "tools" / "bridge" / "desk_bridge.py"
    if not bridge.is_file():
        log(f"cannot open a desk window for {session}: {bridge} is missing")
        return False
    inner = f'python "{bridge}" {session} --model {model} --effort {effort}'
    wt = shutil.which("wt")
    try:
        if wt:
            subprocess.Popen([wt, "-w", "0", "new-tab", "--title", session,
                              "-d", str(ROOT), "cmd", "/c", inner + " || pause"])
        else:
            # NO `start`. Windows Terminal does not ship with Windows 10, so
            # this branch is the normal one there - and it was broken: `start`
            # treats its first UNQUOTED token as the program to run, not as a
            # window title, and an argv list gives cmd no reason to quote a
            # bare word. So `start orchestrator /D ...` asked Windows to run a
            # program called "orchestrator" and popped an error dialog saying
            # exactly that (2026-08-15, first boot of a stock Win10 VM).
            #
            # CREATE_NEW_CONSOLE gives the visible window directly, with no
            # `start` in between. The title is not lost: the bridge sets it
            # itself with an OSC sequence once it starts.
            #
            # A COMMAND STRING here, and this is the one place that is right.
            # The argv-list rule exists because Python quotes list elements for
            # us - but it quotes them the way a C program expects (\" inside
            # quotes), and cmd.exe does not read \" that way. Passing the whole
            # `python "<path>" ... || pause` line as one list element therefore
            # arrived as a literal quoted path glued onto the cwd:
            #   python: can't open file 'C:\...\omnius-agent\"C:\...\desk_bridge.py"'
            # (2026-08-15, first desk window on a machine without Windows
            # Terminal). `||` is cmd syntax, so cmd has to parse this line;
            # our own quoting is the only kind it will read correctly.
            subprocess.Popen(f'cmd /c {inner} || pause', cwd=str(ROOT),
                             creationflags=NEW_CONSOLE)
    except OSError as e:
        log(f"desk window failed for {session}: {e}")
        return False
    try:
        RUNS.mkdir(parents=True, exist_ok=True)
        write_json_atomic(RUNS / f"{session}.json",
                          {"session": session, "mode": "terminal", "startedAt": now_iso(),
                           "model": model, "effort": effort})
    except OSError:
        pass
    log(f"terminal opened for {session} in {cwd}")
    return True


def _unrunnable(session, why):
    """A desk that CANNOT be started at all. Ledger it like any failed run. -> False.

    Both callers used to `return False` bare, which is not a no-op: ensure_runner
    is reached every POLL_SECONDS, so a desk that can never start was retried
    every ~3s forever, silently, with no backoff and nobody told. Found
    2026-08-12 on `wl-integration` - an inbox folder for a desk that is not
    ours, created by envelopes addressed to it on 2026-08-11 - by which time
    99.2% of watchdog.log was this one line, the rotation was hours from
    deleting the only copy of that morning's real history, and
    status_banner.py's bot-identity check (last 200 lines) saw nothing but
    flood. Every other failure path in this file already backs off and pages
    him after RUN_FAILURES_BEFORE_ALERT; these two were simply missed.

    It pages rather than only backing off because _run_backoff SILENCES
    deaf_desk_alarm ("a retry is scheduled; its ledger reports itself"). A bare
    ledger write would have muted the deadman for any desk with a missing
    folder and real human mail waiting - trading a loud bug for a quiet one.
    """
    log(f"cannot run {session}: {why}")
    fails = _run_failures.get(session, 0) + 1
    _run_failures[session] = fails
    _run_backoff[session] = time.time() + RUN_BACKOFF_SECONDS
    if fails >= RUN_FAILURES_BEFORE_ALERT and session not in _run_alerted:
        _run_alerted.add(session)
        try:
            cid = primary_channel_id(build_map(api.load_schema()), session)
            if cid:
                api.send_message(cid, f"🛑 `{session}` cannot be started — {why}. "
                                      f"Its mail is waiting and nothing can handle it. "
                                      f"Retrying every {RUN_BACKOFF_SECONDS}s.")
        except Exception as e:                                          # noqa: BLE001
            log(f"unrunnable alert failed for {session}: {e}")
    return False


def start_run(session, model=None, effort=None):
    """Start ONE headless run on a desk: `claude -p "/omnius"` in its folder.

    No window, no tab, no watcher. The run drains the inbox, does the work,
    replies via the outbox, and exits; stdout/stderr land in
    state\\logs\\runs\\<sid>.log, so "the terminal closed too fast to read why"
    is not a failure mode a run can have.

    Permission posture is unchanged from the terminal era ON PURPOSE: no
    --permission-mode flag unless fleet.json says so (see the A/B receipts in
    fleet.json - bypassPermissions is what HANGS an interactive spawn, and the
    rails are wanted anyway). The allow-list in .claude\\settings.json covers
    routine work; anything past it fires the PermissionRequest relay and
    becomes an ok/no question in #alerts - which IS the Discord confirmation
    gate the destructive verbs need.
    """
    cwd = cwd_for(session)
    if not cwd.is_dir():
        return _unrunnable(session, f"folder missing ({cwd})")
    exe = claude_exe()
    if not exe:
        alert_no_cli()
        return _unrunnable(session, "claude CLI not found (PATH, registry, or "
                                    "the known install locations)")
    ensure_trusted(ROOT if session == "orchestrator" else cwd.parent)
    # A fresh run on this desk means any earlier stall is over. Mechanical
    # clear - see stall_note(); nothing is left for an agent to remember.
    (PERMS / f"{session}.stalled").unlink(missing_ok=True)
    desk = desk_config(session)
    chosen_model = str(model or desk["model"] or DEFAULT_MODEL).strip()
    chosen_effort = str(effort or desk["effort"] or DEFAULT_EFFORT).strip().lower()
    if chosen_effort not in VALID_EFFORTS:
        log(f"effort {chosen_effort!r} is not one of {'/'.join(VALID_EFFORTS)} - using xhigh")
        chosen_effort = "xhigh"

    # window:"terminal" (fleet.json): the owner works AT the desk too. "I want
    # them normal terminal windows, so when I am at the desk I can work
    # directly in the claude CLI and not Discord" (2026-08-01). The FIRST
    # session on such a desk opens as a visible tab; once its claim exists,
    # later mail is handled by headless --continue runs in the SAME
    # conversation (an idle terminal cannot be woken from outside - that
    # impossibility is what killed the watcher era), and the busy stamp keeps
    # runs off while a person is actually typing there.
    c = read_claim(session)
    has_live_claim = bool(c and str(c.get("machine") or "") == api.MACHINE
                          and pid_alive(c.get("pid")))
    # A window is for HIM. Only mail he actually sent earns one; heartbeats and
    # scheduled jobs run headless however this desk is configured.
    #
    # 2026-08-03: he was working in another project when an orchestrator window
    # opened by itself. Three heartbeat envelopes had queued about a stale
    # claim nobody pruned, and the moment no session of his was detected at the
    # root they opened a desk. A window appearing unbidden is exactly what
    # "boot opens nothing" was meant to prevent - it just arrived by a
    # different door.
    if (str(desk.get("window") or "").lower() == "terminal"
            and not has_live_claim and human_mail_waiting(session)):
        return open_tab(session, cwd, chosen_model, chosen_effort)

    # CreateProcess does not reliably execute the npm .cmd shim directly, and
    # string-quoting for cmd.exe is where the 2026-08-01 backtick incident came
    # from - so: argv list, no shell string, cmd /c only as a launcher.
    argv = [exe] if exe.lower().endswith(".exe") else ["cmd", "/c", exe]
    # --add-dir: a component session's sandbox is its own folder; the bus, the
    # root skill and shared memory live at the workspace root (parity doctrine).
    # --settings: settings do NOT inherit from ancestor folders - a session in
    # projects/<p>/<c> never loads projects/<p>/.claude/settings.json on its
    # own, so the run would have zero pre-approvals and every tool would relay.
    argv += ["--add-dir", str(ROOT)]
    ps = project_settings(session, cwd)
    if ps:
        argv += ["--settings", str(ps)]
    argv += ["--model", chosen_model, "--effort", chosen_effort]
    mode = desk.get("permissionMode")
    if mode:
        if mode in VALID_PERMISSION_MODES:
            argv += ["--permission-mode", mode]
        else:
            log(f"permissionMode {mode!r} unknown (fleet.json) - ignoring, desk keeps its profile")
    # --continue ONLY when this cwd actually has a conversation to resume, AND
    # the desk is not configured to boot fresh.
    #
    # 2026-08-01, caught live on a brand-new desk: `claude --continue` in a folder
    # with no history does not fail - it attaches to the most recent conversation
    # from SOMEWHERE ELSE. Spawning <project>.slides resumed the ORCHESTRATOR's
    # conversation inside the slides folder, with the orchestrator's whole
    # context. Deciding it here, from evidence on disk, is the only safe form.
    #
    # resume:"fresh" (fleet.json) is the latency fix measured the same evening:
    # the orchestrator's dev transcript was 11.2 MB while the actual Discord
    # conversation (state\transcripts\) was 52 KB - every run was paying a
    # 200x context tax to answer a chat message, and the owner felt every
    # second of it. Fresh runs read memory + the bus transcript instead
    # (the support-desk pattern: small purpose-built context, never a saga).
    resume_mode = str(desk.get("resume") or "transcript").strip().lower()
    # A HUMAN sitting at this desk outranks the resume policy. He starts desks
    # himself now (2026-08-03), so a live claim means a person's terminal owns
    # that conversation - and `--continue` would put this run into it as a
    # second writer the moment he is between turns. A fresh session cannot
    # collide; its context comes from memory and the bus transcript, exactly
    # like the orchestrator's does.
    if has_live_claim:
        log(f"{session}: a terminal holds this desk - running FRESH, not --continue")
    elif resume_mode != "fresh" and has_history(cwd):
        argv += ["--continue"]
    argv += ["-p", "/omnius"]

    # Identity by token, not inference. The run's environment and its lease
    # carry the same OMNIUS_RUN_ID, so the check-in can PROVE "I am the active
    # run" without a WMI ancestor walk - and without the 2026-08-01 misbind
    # where a run resumed the shared conversation and stood down from itself.
    run_id = f"{session}-{int(time.time() * 1000)}"
    env = os.environ.copy()
    env["OMNIUS_SESSION"] = session
    env["OMNIUS_RUN_ID"] = run_id

    logdir = LOGS / "runs"
    runlog = logdir / f"{session}.log"
    try:
        logdir.mkdir(parents=True, exist_ok=True)
        if runlog.exists() and runlog.stat().st_size > 2_000_000:
            runlog.replace(runlog.with_suffix(".log.1"))   # one generation, like rotate_log
    except OSError:
        pass
    try:
        with open(runlog, "ab") as fh:
            fh.write(f"\n===== {now_iso()} run starting "
                     f"({'resume' if '--continue' in argv else 'fresh'})\n".encode())
            proc = subprocess.Popen(argv, cwd=str(cwd), stdout=fh,
                                    stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env,
                                    creationflags=NO_WINDOW)
    except OSError as e:
        log(f"run failed to start for {session}: {e}")
        return False
    RUNNING[session] = proc
    _run_oldest[session] = oldest_envelope(session)
    try:
        RUNS.mkdir(parents=True, exist_ok=True)
        # The lease is what survives a watchdog restart: the next watchdog reads
        # the pid and adopts the run instead of spawning a second brain.
        # model/effort are stamped here because they are pinned at LAUNCH and
        # the config can be changed underneath a running desk. Without this the
        # only answer to "what is it running on?" is the config - i.e. what the
        # NEXT run will use, which is the wrong answer while a run is in flight.
        write_json_atomic(RUNS / f"{session}.json",
                          {"session": session, "pid": proc.pid, "runId": run_id,
                           "startedAt": now_iso(),
                           "model": chosen_model, "effort": chosen_effort})
    except OSError:
        pass
    log(f"run started for {session} (pid {proc.pid})")
    return True


def _reap(session):
    """Account for a finished child of ours, if any. Never blocks.

    A failed run must not become a spawn loop: mail still queued + instant
    respawn + same crash = a new session every few seconds, forever. Failure
    means backoff, and repeated failure means the OWNER hears about it once -
    a crash loop the fleet handles 'silently' is still a desk that does not
    answer."""
    p = RUNNING.get(session)
    if p is None or p.poll() is None:
        return
    rc = p.returncode
    del RUNNING[session]
    (RUNS / f"{session}.json").unlink(missing_ok=True)
    # An rc-0 run that left its oldest envelope untouched did NOT do its job -
    # count it as a failure or it respawns against the same envelope forever.
    undrained = (_run_oldest.get(session) is not None
                 and oldest_envelope(session) == _run_oldest.get(session))
    _run_oldest.pop(session, None)
    if rc == 0 and not undrained:
        _run_failures.pop(session, None)
        _run_alerted.discard(session)
        log(f"run finished for {session}")
        return
    fails = _run_failures.get(session, 0) + 1
    _run_failures[session] = fails
    _run_backoff[session] = time.time() + RUN_BACKOFF_SECONDS
    why = f"rc {rc}" if rc != 0 else "exited clean but left its oldest envelope unhandled"
    log(f"run FAILED for {session} ({why}, failure #{fails}) - "
        f"backing off {RUN_BACKOFF_SECONDS}s")
    if fails >= RUN_FAILURES_BEFORE_ALERT and session not in _run_alerted:
        _run_alerted.add(session)
        try:
            cid = primary_channel_id(build_map(api.load_schema()), session)
            if cid:
                api.send_message(cid, f"🛑 `{session}` keeps failing to handle its mail "
                                      f"({fails} runs, last: {why}). "
                                      f"Log: `state\\logs\\runs\\{session}.log` · `!restart` retries now.")
        except Exception as e:
            log(f"crash-loop alert failed for {session}: {e}")


def reap_runs():
    for session in list(RUNNING):
        _reap(session)


def ensure_runner(session):
    """Make sure queued mail on this desk will be handled. -> status token.

    THE single choke point for starting runs - handle_message, the schedule
    firer, the heartbeat and the poll loop all funnel through here, so the
    one-run-per-desk rule cannot drift between callers (the 2026-07-31
    duplicate-desk incident was exactly a second caller with its own rules)."""
    _reap(session)
    if not oldest_envelope(session):
        return "empty"
    if run_active(session):
        return "run-in-progress"
    # A live bridge is the FASTEST path: it types the mail into a session that
    # is already warm, so the reply costs thinking time and nothing else. But
    # trust it only while it is DELIVERING - alive is not the same as working,
    # and that difference is what left mail unanswered for hours.
    if bridge_active(session):
        if bridge_not_delivering(session):
            return recover_bridge(session)
        return "bridge-owns-desk"
    # A turn genuinely in flight is left alone - he may have started a long
    # build before leaving, and killing that to answer a chat message is the
    # one trade that is never worth it. The stuck-turn notice covers the case
    # where that turn is actually frozen.
    if interactive_busy(session):
        return "terminal-busy"
    if time.time() < _run_backoff.get(session, 0):
        return "backoff"
    # A window of his on this desk means the desk is HIS until he says
    # otherwise. ASK; never decide. The takeover happens in answer_takeover(),
    # driven by his "ok" and nothing else.
    #
    # The earlier version auto-closed a window quiet for 15 minutes, and on
    # 2026-08-03 it killed a session he was working in: asked at 12 minutes,
    # took it at 15 before he had answered. Asking and then acting anyway is
    # worse than either policy alone - it makes the question decorative.
    if native_sessions(session):
        report_native_in_use(session)           # asks once per message, or not at all
        return "owner-at-the-desk"
    return "started" if start_run(session) else "start-failed"


# --- the deadman ---------------------------------------------------------------

DEAF_DESK_SECONDS = 10 * 60   # owner mail unhandled this long with nobody alive -> page him
_deaf_alerted = {}            # session -> envelope name already paged (event key, not a clock)


def oldest_human_envelope(session):
    """-> (name, age_seconds) of the oldest waiting envelope from a PERSON, or (None, 0).

    System mail (heartbeats, schedules) never pages: the fleet talking to
    itself going unanswered is a log line, not a reason to interrupt him. A
    guest's mail DOES page - rotting for ten minutes with nobody alive is the
    same failure whether the sender was him or someone he invited, and she has
    no other way to tell him it happened."""
    box = INBOX / session
    try:
        names = sorted(f.name for f in box.glob("*.json"))
    except OSError:
        return None, 0.0
    for name in names:
        try:
            if is_human_sender(json.loads((box / name).read_text(encoding="utf-8")).get("from")):
                return name, time.time() - (box / name).stat().st_mtime
        except (OSError, ValueError):
            continue
    return None, 0.0


def deaf_desk_alarm(session):
    """Page the owner when his mail is rotting and NOTHING alive explains it.

    The guard of last resort. 2026-08-03 produced two silent failures in one
    day, and every specific guard missed both - the storm because a deadline
    measured the wrong clock, the two-hour corpse because classification ran
    before liveness. From his phone the two were identical: message sent,
    nothing back, no error anywhere. Specific guards watch for the failures we
    predicted; this one exists for the ones we did not.

    What makes it different in kind from the guards that failed:
    - it consults OUTCOMES (the queue head not moving) and raw pid liveness -
      never the classification functions, whose confident verdicts were
      exactly what lied both times
    - it fires once per stuck envelope (event key), not per interval - the
      clock lesson, again
    - it REPORTS and never acts. If its view of the desk disagrees with
      ensure_runner's, one of the two is wrong - and a reporter that is wrong
      costs one Discord line, while an actor that is wrong types into a
      session that was alive after all. He breaks the tie with !restart.

    Quiet whenever the ball is provably in flight or in HIS court: an active
    run, a scheduled retry, a crash-loop already paged, a takeover question
    standing, a decline he has already given, a provably live turn, a bridge
    younger than its delivery deadline."""
    name, age = oldest_human_envelope(session)
    if not name or age < DEAF_DESK_SECONDS:
        _deaf_alerted.pop(session, None)      # queue moved: that incident is over
        return False
    if _deaf_alerted.get(session) == name:
        return False
    if run_active(session):
        return False
    if time.time() < _run_backoff.get(session, 0):
        return False                          # a retry is scheduled; its ledger reports itself
    if session in _run_alerted:
        return False                          # the crash-loop alert already paged this desk
    if takeover_pending(session):
        return False                          # we asked; silence is an answer he is allowed
    try:
        names = sorted(f.name for f in (INBOX / session).glob("*.json"))
        if names and _native_notified.get(session) == names[-1]:
            return False                      # asked or declined for this queue state already
    except OSError:
        pass
    c = read_claim(session)
    if turn_busy(session) and not turn_stalled(session) and c \
            and str(c.get("machine") or "") == api.MACHINE \
            and pid_alive(c.get("pid"), expect="claude"):
        # ...but only while that turn is PROVABLY MOVING. turn_busy() is a
        # classification function, and the docstring above says this alarm must
        # never trust those - it did anyway, and 2026-08-12 collected the bill:
        # a Stop hook resolved the desk id from the session's CURRENT cwd, which
        # had legitimately cd'd out of the desk folder mid-turn, so it cleared a
        # stamp under another id, and that desk's .busy outlived its own turn.
        # One stale 61-byte file then silenced every guard at once, this one
        # included - a bridge desk's claimed claude pid stays alive BETWEEN
        # turns, so all three conditions above held while the desk sat idle on
        # his mail for 35 minutes and no surface anywhere said so.
        #
        # Recency is an OUTCOME, which is what this alarm is allowed to consult:
        # a working session writes its conversation file continuously (measured
        # max gap on the honest 25-minute turn that same morning: 120.6s against
        # this 180s threshold), a wedged one writes nothing at all. None means
        # cannot-tell, and cannot-tell keeps the old silence - this only ADDS a
        # reason to page, never removes one.
        quiet = native_in_use(session)
        if quiet is None or quiet <= STUCK_QUIET_SECONDS:
            return False                      # a live turn that is MOVING is slow, not deaf
    if bridge_active(session) \
            and time.time() - bridge_started_at(session) < BRIDGE_DELIVER_SECONDS:
        return False                          # a fresh bridge has not had its chance yet
    n, _ = inbox_backlog(session)
    snippet = ""
    try:
        env = json.loads((INBOX / session / name).read_text(encoding="utf-8"))
        text = " ".join(str(env.get("text") or "").split())
        snippet = api.redact(text)[:48] + ("…" if len(text) > 48 else "")
    except (OSError, ValueError):
        pass
    _deaf_alerted[session] = name
    log(f"{session}: DEAF DESK - oldest owner mail {int(age)}s old, {n} queued, and "
        f"nothing alive is handling it (alarm of last resort)")
    try:
        cid = primary_channel_id(build_map(api.load_schema()), session)
        if cid:
            quote = f' ("{snippet}")' if snippet else ""
            # Name what is actually true. The old text said "no live session"
            # unconditionally, and on 2026-08-14 that was false: the session was
            # alive and idle, its TURN had died on an API 529. Telling him the
            # wrong reason sent him looking for a crash that had not happened.
            alive = session_alive(session) or run_active(session)
            why = ("its turn stopped without finishing — the session is still "
                   "there but has written nothing since"
                   if alive else
                   "no run, no live session, nothing in flight")
            api.send_message(cid, f"🔇 `{session}`: your message from {int(age // 60)} min "
                                  f"ago{quote} has **nobody working on it** — {why}. "
                                  f"`!restart` clears the desk and retries.")
    except Exception as e:
        log(f"deaf-desk alarm for {session} failed to send: {e}")
    return True


def ensure_runners():
    if not INBOX.is_dir():
        return
    for box in sorted(INBOX.iterdir()):
        if box.is_dir():
            ensure_runner(box.name)
            deaf_desk_alarm(box.name)


def stop_session(session):
    """CANCEL a desk: drop its queued mail, clear its state, kill its processes.

    The escape hatch that did not exist. His case, 2026-08-05: he was typing a
    message to a desk in Discord, hit RETURN by accident before he had
    finished, and could not take it back. Closing the terminal spawned another
    because the ENVELOPE was still queued; !kill reported success and the work
    carried on because killing a worker does not withdraw the work.

    So the order matters and is the whole point: state FIRST, then processes.
    Removing the lease, presence and claim before killing means the poll that
    lands mid-kill sees a desk nothing is expected to be running on, instead of
    a desk whose worker just vanished and must be replaced.

    Mail is MOVED, never deleted - he cancelled it, he did not disown it, and a
    half-typed instruction is still the only record of what he meant.
    """
    # FIND THE PROCESSES FIRST. The state files are how a desk's pids are
    # known, so clearing them before looking leaves nothing to kill - the first
    # version of this reported "nothing was running" while the bridge was still
    # alive, which is the same lie that made !kill useless.
    pids = desk_processes(session)

    moved = []
    box = INBOX / session
    try:
        for f in sorted(box.glob("*.json")):
            DROPPED.mkdir(parents=True, exist_ok=True)
            target = DROPPED / f"{session}-{f.name}"
            f.replace(target)
            moved.append(target.name)
    except OSError as e:
        log(f"{session}: could not set aside queued mail: {e}")

    for path in (RUNS / f"{session}.json", BRIDGES / f"{session}.json",
                 SESSIONS / f"{session}.json", TAKEOVER / f"{session}.json",
                 TURNS / f"{session}.busy", PERMS / f"{session}.stalled"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    _run_failures.pop(session, None)
    _run_alerted.discard(session)
    _run_backoff.pop(session, None)
    _native_notified.pop(session, None)
    _deaf_alerted.pop(session, None)

    killed = list(pids)
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=NO_WINDOW)
    RUNNING.pop(session, None)
    time.sleep(1.0)
    # Verify. Anything still breathing is REPORTED, never assumed dead.
    survivors = [p for p in pids if pid_alive(p)] + [
        p for p in desk_processes(session) if p not in pids]
    log(f"{session}: STOPPED on request - dropped {len(moved)} envelope(s), "
        f"killed {len(killed)} process(es), {len(survivors)} left")
    parts = [f"🛑 **`{session}` stopped.**"]
    parts.append(f"· {len(moved)} queued message(s) set aside" if moved
                 else "· nothing was queued")
    parts.append(f"· killed {len(killed)} process(es)" if killed
                 else "· nothing was running")
    if survivors:
        # Never claim a kill that did not happen - that is exactly what made
        # !kill useless: it said "killed" while the work carried on.
        parts.append(f"⚠️ **{len(survivors)} process(es) survived** "
                     f"({', '.join(str(p) for p in survivors)}). Close that "
                     f"window by hand.")
    else:
        parts.append("· nothing left running — it will stay down until you "
                     "write to this channel again")
    if moved:
        parts.append("*Nothing was deleted — cancelled mail is kept in* "
                     "`state\\dropped\\`")
    return "\n".join(parts)


def desk_processes(session):
    """-> pids belonging to this desk: bridge, its children, run child, claim."""
    found = set()
    p = RUNNING.get(session)
    if p is not None and p.poll() is None:
        found.add(p.pid)
    for path, expect in ((RUNS / f"{session}.json", "claude"),
                         (BRIDGES / f"{session}.json", "python"),
                         (SESSIONS / f"{session}.json", "claude")):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        for key in ("pid", "watcherPid"):
            if d.get(key) and pid_alive(d[key], expect=expect):
                found.add(int(d[key]))
    try:
        import psutil
        want = str(cwd_for(session)).lower()
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cl = " ".join(proc.info["cmdline"] or [])
                if f"desk_bridge.py {session}" in cl or f"desk_bridge.py\" {session}" in cl:
                    found.add(proc.info["pid"])
                    for ch in proc.children(recursive=True):
                        found.add(ch.pid)
                elif "claude" in (proc.info["name"] or "").lower() \
                        and "AnthropicClaude" not in cl \
                        and str(proc.cwd()).lower() == want:
                    found.add(proc.info["pid"])
            except Exception:
                continue
    except ImportError:
        pass
    return sorted(found)


def kill_desk_processes(session):
    """Kill everything desk_processes() can see. -> pids we tried."""
    pids = desk_processes(session)
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=NO_WINDOW)
    RUNNING.pop(session, None)
    return pids


def kill_session(session):
    # Whatever it was frozen or crash-looping on, it is not any more.
    (PERMS / f"{session}.stalled").unlink(missing_ok=True)
    (TURNS / f"{session}.busy").unlink(missing_ok=True)
    _run_backoff.pop(session, None)
    _run_failures.pop(session, None)
    _run_alerted.discard(session)
    pids = []
    p = RUNNING.pop(session, None)
    if p is not None and p.poll() is None:
        pids.append(p.pid)
    # The bridge too, or !restart leaves the window holding the desk: its
    # presence file keeps bridge_active() true, so the watchdog would refuse to
    # start a run and the desk would be deadlocked by its own rescue.
    try:
        bd = json.loads((BRIDGES / f"{session}.json").read_text(encoding="utf-8"))
        if pid_alive(bd.get("pid")):
            pids.append(bd["pid"])
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    (BRIDGES / f"{session}.json").unlink(missing_ok=True)
    lease = read_lease(session)
    if lease and pid_alive(lease.get("pid")) and lease.get("pid") not in pids:
        pids.append(lease["pid"])          # a run adopted from a previous watchdog
    c = read_claim(session)
    for key in ("pid", "watcherPid"):      # watcherPid: legacy claims only
        pid = (c or {}).get(key)
        if pid and pid_alive(pid) and pid not in pids:
            pids.append(pid)
    if not c and not lease and not pids:
        return f"{session}: nothing running"
    # Classify BEFORE killing. !restart is explicit consent, so it does not ask
    # - but closing a window he was working in must never be a surprise,
    # least of all from his sofa. He asked outright whether !restart can kill
    # the native CLI (2026-08-03); it can, and now it says so.
    own = set(native_sessions(session))
    mine = [p for p in pids if p not in own]
    his = [p for p in pids if p in own]
    idle = native_in_use(session) if his else None
    killed = []
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=NO_WINDOW)
        killed.append(str(pid))
    if his:
        detail = f"{len(his)} of YOUR OWN window(s)"
        if idle is not None:
            detail += f", last active {int(idle // 60)}m ago"
        parts = [detail] + ([f"{len(mine)} of mine"] if mine else [])
        try:
            (SESSIONS / f"{session}.json").unlink(missing_ok=True)
            (RUNS / f"{session}.json").unlink(missing_ok=True)
        except OSError:
            pass
        return f"{session}: killed {' and '.join(parts)} ({', '.join(killed)})"
    try:
        (SESSIONS / f"{session}.json").unlink(missing_ok=True)
        (RUNS / f"{session}.json").unlink(missing_ok=True)
    except OSError:
        pass
    return f"{session}: killed ({', '.join(killed) or 'no live pid, claim removed'})"


# --- inbound ------------------------------------------------------------------

def save_attachments(msg):
    files = []
    for att in msg.get("attachments", []):
        name = "".join(ch for ch in att.get("filename", "file") if ch.isalnum() or ch in "._-") or "file"
        dest = MEDIA / "inbox" / datetime.now().strftime("%Y-%m") / f"{msg['id']}-{name}"
        try:
            api.download(att["url"], dest)
            files.append({"path": str(dest), "name": name,
                          "type": att.get("content_type", "application/octet-stream")})
        except Exception as e:
            log(f"attachment download failed ({name}): {e}")
    return files


SCHEDULE_CHECK_SECONDS = 20
_last_schedule_check = 0.0
_last_jobs_written = None    # last payload we wrote, so an idle tick writes nothing


MISSED_ALARM_AT = 3          # consecutive skips before saying so
_missed_alerted = set()      # job ids already paged (the incident, not the count)


def broadcast_channel_id(name="alerts"):
    """Channel id for a session-less fleet channel, from the local schema map.

    The same lookup resolve_outbox_target() uses for a permission ask's
    fallback. NOT api.resolve_channel(), which is a REST round trip and raises
    ApiError when the channel is missing - neither belongs on a path called
    from the fire loop.
    """
    try:
        for cid, t in build_map(api.load_schema()).items():
            if t.channel_name == name:
                return cid
    except Exception as e:                                       # noqa: BLE001
        log(f"broadcast channel {name} lookup failed: {type(e).__name__}: {e}")
    return None


def warn_on_missed(jobs):
    """Say something when a routine keeps getting skipped.

    `missed` has been counted since schedule.py was written and surfaced
    NOWHERE - so a routine that never fires because the laptop is always asleep
    at that hour has looked exactly like one that works. That is the silent
    class of failure this system keeps relearning.

    Report-only, and once per incident rather than per pass (the same event-key
    rule as _deaf_alerted): a counter that keeps climbing is one alert, not one
    per tick. Resetting when the count drops means a rescheduled job that
    starts working again re-arms the warning.
    """
    for j in jobs:
        jid, missed = j.get("id"), int(j.get("missed", 0) or 0)
        if not jid:
            continue
        if missed < MISSED_ALARM_AT:
            _missed_alerted.discard(jid)        # fired again: that incident is over
            continue
        if jid in _missed_alerted:
            continue        # once per incident, NOT once per further miss - the
                            # counter climbs every hour and re-paging each step
                            # is how an alert channel becomes wallpaper
        _missed_alerted.add(jid)
        log(f"schedule {jid}: skipped {missed}x (too stale to fire on wake)")
        try:
            cid = broadcast_channel_id("alerts")
            if cid:
                api.send_message(cid,
                    f"⏱ routine `{jid}` has been **skipped {missed} times** — its "
                    f"slot keeps passing while the PC is off or asleep. It is not "
                    f"broken, just never awake in time. `!cron` to see it; pick a "
                    f"time the machine is on, or `!cron rm {jid}`.")
            else:
                log(f"missed-routine alert for {jid}: no #alerts channel to post to")
        except Exception as e:                                  # noqa: BLE001
            log(f"missed-routine alert failed for {jid}: {type(e).__name__}: {e}")


def fire_due_schedules():
    """Turn due scheduled jobs into ordinary inbox envelopes.

    Deliberately reuses the normal envelope path rather than inventing a second
    delivery mechanism: a scheduled message wakes or spawns its target session
    exactly the way a Discord message does, so there is only one code path to
    keep correct. schedule.py handles the catch-up policy (missed runs are
    rescheduled, not replayed)."""
    global _last_schedule_check
    if time.time() - _last_schedule_check < SCHEDULE_CHECK_SECONDS:
        return
    _last_schedule_check = time.time()
    try:
        fire, kept = schedule.due_jobs()
    except Exception as e:                       # a bad jobs.json must not stop the bus
        log(f"schedule check failed: {type(e).__name__}: {e}")
        return
    warn_on_missed(kept)   # before any early-out: a routine that ONLY ever gets
                           # skipped never appears in `fire`, and it is exactly
                           # the one worth complaining about
    # NO early return on an empty `fire`. due_jobs() also rewrites state for
    # jobs it DECLINED to fire - the incremented `missed` counter and the
    # fast-forwarded nextRun - and returning here threw both away (found
    # 2026-08-07). A routine skipped as too-stale then re-evaluated from the
    # same stale timestamp on every pass: permanently stuck, never firing, its
    # miss counter pinned at 1 on disk. Exactly the silent failure warn_on_missed
    # exists to surface, hidden by the thing meant to record it.
    delivered = []
    for job in fire:
        session = job.get("to") or "orchestrator"
        box = INBOX / session
        try:
            box.mkdir(parents=True, exist_ok=True)
            mid = f"sched-{job['id']}-{int(time.time())}"
            (box / f"{mid}.json").write_text(json.dumps({
                "id": mid, "from": "schedule", "channel": None, "channelId": None,
                "category": None, "ts": now_iso(), "text": job.get("text", ""),
                "files": []}, ensure_ascii=False, indent=2), encoding="utf-8")
            delivered.append(job.get("id"))   # the write succeeded: this one really landed
            transcribe(session, "in", f"[scheduled] {job.get('text', '')}")
            log(f"schedule {job['id']} -> inbox {session}")
            ensure_runner(session)
        except OSError as e:
            log(f"schedule {job.get('id')} delivery failed: {e}")
    # Save whenever the pass CHANGED something - not only when something fired.
    # stamp_success is pure/in-memory, so it is safe to run with an empty
    # `delivered`; the payload compare is what keeps a 20s tick from rewriting
    # the file 4300 times a day for no reason.
    global _last_jobs_written
    schedule.stamp_success(kept, delivered)
    payload = json.dumps(kept, sort_keys=True)
    if payload != _last_jobs_written:
        try:
            schedule.save_jobs(kept)
            _last_jobs_written = payload
        except OSError as e:
            log(f"schedule save failed: {e}")


def _heartbeat_minutes():
    """Minutes between heartbeat checks. Default 30, 0 = off.

    `config\\omnius.ini [omnius] heartbeat_minutes`, still overridden by the
    `.env`/environment key it used to live in - so nobody has to edit anything
    for the move (config\\README.md: env > file > default).

    A wrong-but-non-empty value has killed this service before, so garbage
    falls back to the default instead of raising."""
    return max(0, ocfg.get_int(OMNIUS_CFG, "omnius", "heartbeat_minutes",
                               "HEARTBEAT_MINUTES", 30, env=api.ENV))


def stale_claims():
    """Claims that are certainly not a running session on this machine.

    Same rule fleet_ops uses: a dead pid here, or any claim from another
    machine (normal after the workspace moves to a new PC)."""
    out = []
    for f in sorted(SESSIONS.glob("*.json")):
        c = read_claim(f.stem)
        if not c:
            continue
        if str(c.get("machine") or "") != api.MACHINE                 or not pid_alive(c.get("pid"), expect="claude"):
            out.append(f.stem)
    return out


def heartbeat_reasons(state, now=None):
    """Cheap, MECHANICAL reasons to wake Omnius. Empty list = stay quiet.

    ARCHITECTURE par. 3.10 says "nothing needing attention -> no message
    anywhere". Taken literally that is a rule about MESSAGES, and it would still
    spawn an Opus session every 30 minutes, 24/7, just to decide there is
    nothing to say - roughly 48 sessions a day against goal 6, "nothing runs and
    nothing spends unless there is work".

    So the quiet rule is enforced HERE, in the transport, where the checks cost
    nothing: a few file stats and a clock read. Judgement still belongs to
    Omnius - this only decides whether there is a candidate worth the wake.
    In practice that is a couple of wakes a day instead of forty-eight."""
    now = now or datetime.now()
    reasons = []
    # NEVER count the orchestrator's own stale claim. Caught live on the first
    # heartbeat ever fired (2026-08-01): the envelope said "1 stale claim to
    # prune: orchestrator", the watchdog then SPAWNED the orchestrator to handle
    # it, and by the time Omnius read the list it was itself that orchestrator -
    # alive, holding a fresh claim. Acting on the snapshot would have deleted its
    # own claim, freeing the desk and inviting a SECOND orchestrator onto it,
    # which is the exact duplicate-desk failure the one-session-per-desk rule
    # exists to prevent (RELIABILITY R3, 2026-07-31).
    #
    # It is also a pointless wake: a stale orchestrator claim with no orchestrator
    # running is harmless litter, and spawning Omnius replaces it automatically.
    stale = [s for s in stale_claims() if s != "orchestrator"]
    if stale:
        reasons.append(f"{len(stale)} stale claim(s) to prune: {', '.join(stale[:5])}")
    today = now.strftime("%Y-%m-%d")
    # "First heartbeat after 07:00" - never a backlog. A machine switched on at
    # 18:00 gets today's briefing once, not eleven hourly ones (same catch-up
    # policy as schedule.py: missed is rescheduled, not replayed).
    if now.hour >= 7 and state.get("lastDaily") != today:
        reasons.append("daily briefing is due (first heartbeat after 07:00)")
    if now.weekday() == 0 and now.hour >= 7 and state.get("lastWeekly") != today:
        reasons.append("weekly memory gardening is due (Monday)")
    # No backup destination = this instance's notes, projects and media exist on
    # one disk. It nags until fixed, on purpose (his instruction 2026-08-10) -
    # but ONCE A DAY, not every 30 minutes. The lesson two days earlier was that
    # a heartbeat line repeated every morning forever becomes landfill; a setup
    # gap that is one edit away earns a daily reminder and no more. It stops the
    # moment the folder is set, which is the difference from the staleness check.
    try:
        if ocfg.backup_folder()[0] is None and state.get("lastBackupNag") != today:
            reasons.append("NO BACKUP FOLDER SET — `[backup] folder` in "
                           "config\\omnius.ini is empty, so nothing is being backed up")
    except Exception:                                            # noqa: BLE001
        pass
    return reasons


def read_heartbeat_state():
    try:
        return json.loads((WD_STATE / "heartbeat.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def fire_heartbeat():
    """Wake Omnius when - and only when - there is something mechanical to do."""
    minutes = _heartbeat_minutes()
    if minutes <= 0:
        return
    state = read_heartbeat_state()
    now = datetime.now()
    last = state.get("lastCheck") or ""
    try:
        if last and (now - datetime.strptime(last, "%Y-%m-%dT%H:%M:%S")).total_seconds() \
                < minutes * 60:
            return
    except ValueError:
        pass                                  # unreadable stamp: treat as due

    # Prune before asking. A claim whose pid is dead ON THIS MACHINE is litter,
    # not a judgement call, and waking an Opus session to delete a file is
    # absurd - especially since it kept failing to: the desk was busy, the
    # claim stayed, and the same heartbeat fired every 30 minutes forever
    # (three queued by 2026-08-03, which is what eventually opened a window).
    # This IS the re-check the checklist demands, done at the moment of acting.
    for f in sorted(SESSIONS.glob("*.json")):
        c = read_claim(f.stem)
        if not c or str(c.get("machine") or "") != api.MACHINE:
            continue                       # another PC's claim: not ours to judge
        if not pid_alive(c.get("pid"), expect="claude") and not run_active(f.stem) \
                and not bridge_active(f.stem):
            f.unlink(missing_ok=True)
            log(f"pruned stale claim {f.stem} (pid {c.get('pid')} is gone)")

    reasons = heartbeat_reasons(state, now)
    # Stamp the CHECK before doing anything else. If the envelope write below
    # fails, the retry is one heartbeat away rather than every poll pass - the
    # !reload lesson (2026-07-31) was a side effect that outlived its cursor.
    state["lastCheck"] = now.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        write_json_atomic(WD_STATE / "heartbeat.json", state)
    except OSError:
        pass
    if not reasons:
        return                                # the quiet rule, costing nothing

    body = ("Heartbeat. Work through `memory\\orchestrator\\HEARTBEAT.md`.\n\n"
            "What the watchdog noticed **when this was composed** - it is a "
            "snapshot, not a work order. RE-CHECK EACH ITEM BEFORE ACTING; a desk "
            "that restarted in between looks dead until its next run checks in, "
            "and pruning a live desk frees it for a second session.\n"
            + "\n".join(f"- {r}" for r in reasons)
            + "\n\nIf nothing actually needs attention once you look, END THE TURN "
              "SILENTLY - no message anywhere. This is not a message from the user "
              "and nobody is waiting for a reply.")
    box = INBOX / "orchestrator"
    mid = f"heartbeat-{int(time.time())}"
    try:
        box.mkdir(parents=True, exist_ok=True)
        (box / f"{mid}.json").write_text(json.dumps({
            "id": mid, "from": "heartbeat", "channel": None, "channelId": None,
            "category": None, "ts": now_iso(), "text": body, "files": []},
            ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        log(f"heartbeat delivery failed: {e}")
        return
    today = now.strftime("%Y-%m-%d")
    if any(r.startswith("daily") for r in reasons):
        state["lastDaily"] = today
    if any(r.startswith("weekly") for r in reasons):
        state["lastWeekly"] = today
    if any(r.startswith("NO BACKUP") for r in reasons):
        state["lastBackupNag"] = today          # once a day, not once a heartbeat
    try:
        write_json_atomic(WD_STATE / "heartbeat.json", state)
    except OSError:
        pass
    transcribe("orchestrator", "in", f"[heartbeat] {'; '.join(reasons)}")
    log(f"heartbeat -> orchestrator ({'; '.join(reasons)})")
    ensure_runner("orchestrator")


def transcribe(session, direction, text, channel=None, channel_id=None, files=None,
               who=None):
    """Append one line to the session's monthly bus transcript.

    Both halves of a remote conversation were previously write-only: an inbound
    envelope is deleted once handled and an outbox file once posted, so the only
    copy lived in Discord. That survives neither !kill, nor a fresh --continue,
    nor leaving the channel - and it is why "search what I told Omnius last
    week" could not be built: there was nothing local to search. Retention is
    the prerequisite question, not the storage engine.

    JSONL, one file per session per month, under state\\ - machine-local and
    excluded from the zip, so it is a log, not luggage."""
    try:
        d = TRANSCRIPTS / session
        d.mkdir(parents=True, exist_ok=True)
        # `from` is written for inbound lines so a later run can tell WHOSE words
        # these were. Without it a shared channel reads as one voice, and the
        # tail of this file is exactly what a fresh run consults to understand
        # "what we decided earlier".
        line = {"ts": now_iso(), "dir": direction, "channel": channel,
                "channelId": channel_id, "text": api.redact(text or ""),
                "files": [Path(p).name for p in (files or [])]}
        if who:
            line["from"] = who
        with open(d / f"{datetime.now().strftime('%Y-%m')}.jsonl", "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError) as e:
        log(f"transcript write failed for {session}: {e}")   # never fatal


def write_envelope(session, channel_name, msg, files, channel_id=None, category=None,
                   sender="owner"):
    box = INBOX / session
    box.mkdir(parents=True, exist_ok=True)
    # channelId is what a reply should echo back: names collide across projects,
    # ids do not. category tells the orchestrator WHICH project's #general asked -
    # every project's #general maps to the same session, so without it the
    # orchestrator has to guess which project an instruction is about.
    #
    # `sender` is WHO WROTE IT, and it used to be the string "owner" no matter
    # what. The watchdog has always held the Discord author (the allowlist is
    # built on it) and threw it away one step before the desk could see it - so
    # a desk could not tell the owner from anyone else, and the whole idea of
    # letting a second person write was unimplementable. Values: "owner", a
    # guest label from config\guests.ini, or one of SYSTEM_SENDERS.
    envelope = {"id": msg["id"], "from": sender, "channel": channel_name,
                "channelId": channel_id, "category": category,
                "ts": msg.get("timestamp", now_iso()), "text": msg.get("content", ""),
                "files": files}
    (box / f"{msg['id']}.json").write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    transcribe(session, "in", msg.get("content", ""), channel=channel_name,
               channel_id=channel_id, files=[f.get("path") for f in (files or [])],
               who=sender)


NOTES_STALE_DAYS = 3


# (The deaf-desk healing/notify apparatus lived here until 2026-08-01. It was
# the immune system for a disease the run model removes: desks went deaf only
# because a session-side watcher had to stay alive between turns. No watchers,
# no deafness - a desk with queued mail and no active run is simply the next
# thing ensure_runners() starts.)


BOARD_REFRESH_SECONDS = 20      # how often the live fleet board is rewritten
_last_board = 0.0
_board_file = None              # set lazily: state\watchdog\board.json


def child_counts():
    """-> {pid: number of child processes}, or {} if it cannot be determined.

    A Claude session with children is EXECUTING tools right now; one with none is
    thinking, waiting, or asleep. It is the only "what is it doing" signal
    available without the desk cooperating, and desks cannot cooperate while
    stalled - which is exactly when you need to know. One WMI call for the whole
    machine, not one per desk.
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId "
             "| ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=25, creationflags=NO_WINDOW).stdout
        rows = json.loads(out or "[]")
        counts = {}
        for r in rows if isinstance(rows, list) else [rows]:
            counts[r["ParentProcessId"]] = counts.get(r["ParentProcessId"], 0) + 1
        return counts
    except Exception:
        return {}


def show_working(mapping):
    """Keep "Omnius is typing..." alive in every channel whose desk is working.

    His ask, 2026-08-13: from a phone there was no way to tell a desk that is
    thinking from one that never woke up - the 👀 on his message says *received*
    and then nothing moves until the answer lands, which on a long turn is
    minutes of looking broken.

    Typing was chosen over the alternatives because it is the only one that
    CANNOT go stale. Discord expires it after ~10s, so a watchdog that dies
    mid-run leaves no false "working" behind - no marker to clean up, no state
    to reconcile, and nothing to get wrong on restart. A channel rename would
    have been visible in the sidebar, but Discord allows two renames per ten
    minutes per channel, so short runs would strand the wrong emoji on it; a
    posted message would have to be edited or deleted afterwards, and pushes
    the real answer up out of view.

    Both kinds of work count: a headless run the watchdog owns, and a terminal
    mid-turn. From his phone the question is the same one - "is this desk busy
    right now?" - and the answer should not depend on where the work started.

    Caveat, deliberate: a session that owns SEVERAL channels (📧 EMAIL, one per
    account) types in all of them. The signal is per-DESK and the desk really is
    busy; splitting it per channel would mean stamping the triggering channel
    into the run lease, and a slightly over-broad truth beats new state to keep
    in step. Revisit only if he says it misleads him.
    """
    by_session = {}
    for cid, target in mapping.items():
        if target.session:
            by_session.setdefault(target.session, []).append(cid)
    now = time.time()
    for session, cids in by_session.items():
        try:
            if not (run_active(session) or turn_busy(session)):
                continue
        except Exception:                                        # noqa: BLE001
            continue          # a busy marker must never break the tick
        for cid in cids:
            if now - _typing_sent.get(cid, 0.0) < TYPING_REFRESH_SECONDS:
                continue
            try:
                api.trigger_typing(cid)
                _typing_sent[cid] = now
            except Exception as e:                               # noqa: BLE001
                # Cosmetic. Back off this channel for a full cycle rather than
                # retrying every tick, and say so once - a typing endpoint that
                # is failing usually means the token lost the channel, which
                # rest_sweep reports properly.
                _typing_sent[cid] = now
                log(f"typing indicator failed for {mapping[cid].channel_name}: "
                    f"{type(e).__name__}: {e}")


def fleet_board(mapping):
    """Maintain ONE live board in #fleet-status, edited in place.

    Built 2026-08-01 on the owner's complaint, which was exactly right: "no
    puedo estar saltando para arriba a Omnius y luego al proyecto, no sé si está
    encendida la sesión, no sé nada". Health was scattered across channels and
    only appeared when he asked. This is one place, always current, that says
    per desk: alive, actually executing, how long silent, what is queued.

    Edited rather than reposted (api.send_embed(message_id=...)) so the channel
    does not fill with snapshots - the same reason the pinned board exists.
    """
    global _last_board, _board_file
    if time.time() - _last_board < BOARD_REFRESH_SECONDS:
        return
    _last_board = time.time()
    if _board_file is None:
        _board_file = WD_STATE / "board.json"

    cid = next((c for c, t in mapping.items() if t.channel_name == "fleet-status"), None)
    if not cid:
        return
    counts, rows = child_counts(), []
    ids = {f.stem for f in SESSIONS.glob("*.json")}
    ids |= {f.stem for f in RUNS.glob("*.json")} if RUNS.is_dir() else set()
    for sid in sorted(ids):
        c = read_claim(sid) or {}
        pid = c.get("pid")
        if run_active(sid):
            doing = "🟢 working (run in progress)"
        elif str(c.get("machine") or "") == api.MACHINE and pid_alive(pid):
            kids = counts.get(pid, 0) if pid else 0
            doing = (f"🟢 working ({kids} tool{'s' if kids != 1 else ''})" if kids
                     else "🟡 idle terminal — wakes when you type or message")
        else:
            doing = "⚫ off — next message starts a run"
        n, oldest = inbox_backlog(sid)
        queue = f" · 📥 {n} queued ({int(oldest // 60)}m)" if n else ""
        rows.append(f"**{sid}**\n{doing}{queue}{stall_note(sid)}")
    if not rows:
        rows = ["*no desks claimed*"]

    prev = {}
    try:
        prev = json.loads(_board_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        res = api.send_embed(
            cid, title="fleet — live", description="\n\n".join(rows)[:4000],
            footer=f"auto-refresh every {BOARD_REFRESH_SECONDS}s · {api.MACHINE} · "
                   f"{datetime.now().strftime('%H:%M:%S')}",
            message_id=prev.get("id"))
        if res and res.get("id") != prev.get("id"):
            write_json_atomic(_board_file, {"id": res["id"]})
    except api.ApiError as e:
        # A board that cannot be drawn must never stop the bus.
        log(f"fleet board refresh failed: {e}")


BACKLOG_NOTICE_SECONDS = 90     # how long a desk may sit on a message in silence
# ...and how long a desk that IS working may stay silent about it. Deliberately
# far above the first: a run in progress is not a fault, and "still working"
# every 90 seconds is the info-message noise he banned on 2026-08-03. But past
# the quarter hour the honest reading from a phone is "it died", and on
# 2026-08-12 he acted on exactly that reading and restarted a healthy desk.
LONG_WORK_NOTICE_SECONDS = 15 * 60


def heard_from(session, age):
    """True when this desk has POSTED anything since the message arrived.

    An acknowledgement is the entire difference between silence and a slow
    answer: once a desk has said "on it", the wait is explained and a watchdog
    notice on top would be the info-message he banned. The marker is stamped by
    the poster on every successful post, so this asks about REPLIES, not about
    processes being alive - which is the only thing he can see from a phone.
    """
    try:
        posted = (OUTBOX / session / ".last-posted").stat().st_mtime
    except OSError:
        return False
    return posted >= time.time() - age
_backlog_notified = set()       # "<session>/<envelope id>" already announced


def inbox_backlog(session):
    """-> (count, oldest_age_seconds) of envelopes this desk has not handled."""
    box = INBOX / session
    if not box.is_dir():
        return 0, 0.0
    now, oldest, n = time.time(), 0.0, 0
    for f in box.glob("*.json"):
        try:
            oldest = max(oldest, now - f.stat().st_mtime)
            n += 1
        except OSError:
            continue
    return n, oldest


def check_backlogs():
    """Tell the owner when a desk has not picked a message up.

    The acknowledge-first rule in /omnius only helps when a session is AWAKE and
    merely slow. On 2026-08-01 the owner waited 13 minutes and sent "Holaaa???":
    the envelope had been delivered instantly and the desk simply was not
    running yet, so there was nobody to ack. From a phone, "asleep", "hung",
    "stuck on a dialog" and "busy" are indistinguishable - and every one of them
    is a case where the session itself cannot report.

    So the notice comes from the watchdog, the only always-on piece. It is
    cause-agnostic on purpose: an undrained envelope means not-being-handled,
    whatever the reason.
    """
    now = time.time()
    live = set()
    for box in INBOX.glob("*"):
        if not box.is_dir():
            continue
        session = box.name
        for env_file in box.glob("*.json"):
            key = f"{session}/{env_file.stem}"
            live.add(key)
            if key in _backlog_notified:
                continue
            try:
                age = now - env_file.stat().st_mtime
                if age < BACKLOG_NOTICE_SECONDS:
                    continue
                env = json.loads(env_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            cid = env.get("channelId")
            if not cid:
                continue
            mins = int(age // 60)
            waited = f"{mins}m" if mins else f"{int(age)}s"
            note = stall_note(session).strip(" ·")
            why = f" — {note}" if note else ""
            # "Hasn't picked this up / will answer when it wakes" next to an
            # actively working run reads as a broken system (seen on the owner's
            # screen 2026-08-01). A run in progress is the opposite of asleep -
            # say which one is true.
            # NOTICES ARE FOR PROBLEMS AND QUESTIONS ONLY (owner, 2026-08-03:
            # "i do not need any info messages at all, just issues or error
            # message, or important stuff"). Everything below that is merely
            # TRUE is not worth a notification on his phone.
            if native_sessions(session):
                # His own window has it, and the takeover question already
                # asked him about exactly this. Saying it twice is noise.
                _backlog_notified.add(key)
                continue
            if run_active(session) or bridge_active(session):
                # Something is working on it. "Still working" is the definition
                # of an info message - the reply itself is the notification.
                #
                # Except when the reply is a long time coming. 2026-08-12, after
                # 20 silent minutes of real work: "you got stuck, I had to
                # restart it - a user must never be left hanging." From a phone,
                # a desk deep in a long task and a desk that died look exactly
                # the same, and he pays for the difference by killing healthy
                # work. So ONE line, once per message, only after the wait has
                # become unreasonable - not a heartbeat, not a progress bar.
                if age < LONG_WORK_NOTICE_SECONDS or heard_from(session, age):
                    _backlog_notified.add(key)
                    continue
                try:
                    api.send_message(cid, f"⏳ `{session}` is still working on this ({waited}) "
                                          f"— not stuck, no dialog waiting. "
                                          f"`!status` for detail, `!stop {session}` to cancel it.")
                    _backlog_notified.add(key)
                except api.ApiError:
                    pass
                continue
            # Nothing holds this desk and nothing is working: that is a real
            # problem, and the only case still worth interrupting him for.
            try:
                api.send_message(cid, f"⚠️ `{session}` has not picked this up ({waited}){why} "
                                      f"and nothing is running on that desk.")
                _backlog_notified.add(key)
            except api.ApiError:
                pass
    # Forget envelopes that have been handled, so the set cannot grow forever
    # on an always-on service.
    _backlog_notified.intersection_update(live)


def stall_note(session):
    """-> a suffix telling *alive* from *listening*, else ''.

    The worst observability hole found (2026-07-31): a session frozen on a local
    permission dialog is indistinguishable from a healthy one. Every signal lies
    in the same direction - session pid alive, watcher pid alive, lastSeenAt 3s
    old, !status "on" - because the heartbeat is written by inbox_watch, a
    SEPARATE process that keeps stamping while the session does nothing at all.
    Liveness of the watcher was never evidence of liveness of the session.

    Two honest signals, both from the permission relay:
      <tool_use_id>.json  a request is open RIGHT NOW - the desk is waiting on
                          Discord and will fall back to a local dialog on timeout
      <session>.stalled   it already timed out and fell back - nobody can see
                          that dialog but the person at the keyboard
    """
    try:
        if (PERMS / f"{session}.stalled").is_file():
            d = json.loads((PERMS / f"{session}.stalled").read_text(encoding="utf-8"))
            return f" · ⛔ STALLED at a local dialog since {d.get('since', '?')} ({d.get('tool', '?')})"
    except (OSError, ValueError):
        return " · ⛔ STALLED at a local dialog"
    for req in PERMS.glob("*.json"):
        try:
            d = json.loads(req.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("session") == session and d.get("code"):
            return f" · \U0001f510 waiting on permission `{d['code']}` — answer ok/no in #alerts"
    return ""


def notes_age(session):
    """-> ' · notes Nd old' for a project session whose component notes are going
    stale, else ''.

    CLAUDE.md par.2 makes memory\\sessions\\<component>.md the way siblings
    coordinate and says "keep yours current" - but nothing ever checked, so
    "current" was purely aspirational. Compaction silently truncates the
    transcript, and whatever was never written down is simply gone. Claude Code
    offers no hook that can make an agent flush memory (PreCompact cannot inject
    model context; only Stop can, and injecting there continues the conversation
    - a runaway risk on an always-on fleet). So this does not enforce: it makes
    staleness VISIBLE, which is the honest half and costs nothing."""
    if "." not in session:
        return ""
    project, _, component = session.partition(".")
    notes = ROOT / "projects" / project / "memory" / "sessions" / f"{component}.md"
    try:
        age_days = (time.time() - notes.stat().st_mtime) / 86400
    except OSError:
        return " · ⚠ no session notes"
    return f" · ⚠ notes {int(age_days)}d old" if age_days >= NOTES_STALE_DAYS else ""


def run_desktop_verb(verb_name, rest, caller_channel):
    """-> (ok, message, screenshot_path_or_None) for the !screen / !desktop commands.

    tools\\desktop is imported LAZILY and its absence is survivable. The watchdog
    is the only thing listening to Discord; it must not fail to start, or fail a
    poll pass, because an optional desktop feature is missing a dependency."""
    sys.path.insert(0, str(ROOT / "tools" / "desktop"))
    import desktop as dt

    if verb_name not in dt.REMOTE_VERBS:
        allowed = ", ".join(dt.REMOTE_VERBS)
        # Say WHY the input verbs are missing rather than pretending they do not
        # exist - a user who reaches for `type-into` deserves the real reason.
        extra = ("\n`key` / `type-into` exist but are local-CLI only: they cannot "
                 "confirm the app acted on the input, and an unverifiable action "
                 "is not something to drive blind from a phone.")
        return False, f"unknown or non-remote verb {verb_name!r} - allowed: {allowed}{extra}", None

    args = argparse.Namespace(target=rest or None, text=None,
                              window=rest or None if verb_name == "screenshot" else None,
                              out=None, json=False, caller=f"discord:#{caller_channel}")
    ok, msg = dt.run(verb_name, args)
    shot = None
    if ok and verb_name == "screenshot" and "|" in msg:
        shot, msg = msg.split("|", 1)
    return ok, msg, shot


def handle_control(text, cid, target, mapping):
    cmd = text.split()[0].lower()
    if cmd == "!status":
        lines = []
        for claim_file in sorted(SESSIONS.glob("*.json")):
            s = claim_file.stem
            # stall_note first: "[on] but frozen" is the case that misled a
            # human once already, so it must be the thing the eye lands on.
            n, oldest = inbox_backlog(s)
            queued = f" · 📥 {n} queued (oldest {int(oldest // 60)}m)" if n and oldest >= 60 else (
                f" · 📥 {n} queued" if n else "")
            lines.append(f"{'[on] ' if session_alive(s) else '[off]'} {s} · {fmt_model(s)}"
                         f"{stall_note(s)}{queued}{notes_age(s)}")
        api.send_message(cid, "**fleet @ " + api.MACHINE + "**\n" +
                         ("\n".join(lines) if lines else "no sessions yet") +
                         "\n-# model/effort: bare = what that run launched on, "
                         "(parens) = config for its next run")
    elif cmd == "!kill":
        if target.session:
            api.send_message(cid, kill_session(target.session))
        else:
            api.send_message(cid, "this channel has no session")
    elif cmd == "!restart":
        if target.session:
            # `!restart sonnet low` = change the setting AND cut over, in one
            # command. Model and effort are pinned at launch, so "change a
            # running desk's model" is always really "restart it on the new
            # one" - making that a single verb removes the step where the
            # setting is written and then forgotten.
            model, effort, err = parse_model_effort(text.split()[1:])
            if err:
                api.send_message(cid, err)
                return
            changed = ""
            if model or effort:
                # PERSIST rather than pass a one-shot override to start_run:
                # a desk that silently reverted on its next run would be the
                # more confusing of the two behaviours.
                try:
                    fleet_set_desk(target.session, model=model, effort=effort)
                except (OSError, ValueError) as e:
                    api.send_message(cid, f"could not write fleet.json: {type(e).__name__}: {e}")
                    return
                d = desk_config(target.session)
                changed = f" on **{d['model']}** / **{d['effort']}**"
                if model and not model_looks_known(model):
                    changed += f" ⚠ (unrecognised model `{model}` — `!model reset` undoes it)"
            result = kill_session(target.session)
            # Report what actually happened - claiming "restarted" regardless
            # would hide exactly the failure worth seeing. A fresh run starts
            # even with an empty inbox: it checks in, finds nothing, exits 0 -
            # which is itself the proof the desk works.
            ok = start_run(target.session)
            api.send_message(cid, result + ((" -> fresh run started" + changed) if ok else
                                            " -> RUN COULD NOT START (see watchdog log)"))
        else:
            api.send_message(cid, "this channel has no session")
    elif cmd == "!stop":
        if target.session:
            api.send_message(cid, stop_session(target.session))
        else:
            api.send_message(cid, "this channel has no session")
    elif cmd == "!cron":
        # Handled HERE rather than by waking a desk: listing routines needs
        # speed, not judgment, so it costs zero tokens and spawns nothing -
        # same reasoning as !status and !config. Creating a routine is the
        # opposite (which desk? what prompt?) and stays natural language.
        parts = text.split()
        verb = parts[1].lower() if len(parts) > 1 else "list"
        arg = parts[2] if len(parts) > 2 else None
        try:
            if verb in ("pause", "resume") and arg:
                job = schedule.set_paused(arg, verb == "pause")
                api.send_message(cid, f"{verb}d `{arg}`" if job
                                 else f"no routine with id `{arg}`")
            elif verb in ("rm", "remove", "delete") and arg:
                jobs = schedule.load_jobs()
                kept = [j for j in jobs if j.get("id") != arg]
                if len(kept) == len(jobs):
                    api.send_message(cid, f"no routine with id `{arg}`")
                else:
                    schedule.save_jobs(kept)
                    api.send_message(cid, f"removed `{arg}`")
            elif verb == "adopt" and arg:
                claimed = schedule.adopt(arg)
                api.send_message(cid, f"adopted {len(claimed)} routine(s) onto "
                                 f"{api.MACHINE}" if claimed else
                                 "nothing to adopt - all routines already belong here")
            else:
                jobs = schedule.load_jobs()
                if not jobs:
                    api.send_message(cid, "no routines yet. Ask in #omnius, e.g. "
                                     "*\"check my gmail every hour on weekdays "
                                     "during work hours\"*.")
                else:
                    me = api.MACHINE
                    body = "\n".join(schedule.describe(j, me) for j in
                                     sorted(jobs, key=lambda x: x.get("nextRun") or ""))
                    # A fenced block, never a markdown table - Discord renders
                    # no tables, and this is columnar (/omnius §4, 2026-08-06).
                    api.send_message(cid, f"⏱ **{len(jobs)} routine(s)**\n```\n{body}\n```")
        except Exception as e:                                  # noqa: BLE001
            # A broken routines file must not take the control surface down -
            # !cron is how he'd diagnose it.
            api.send_message(cid, f"could not read routines: {type(e).__name__}: {e}")
    elif cmd == "!config":
        # READ ONLY, deliberately. His call, 2026-08-05: settings are readable
        # from Discord and edited at the desk, because a value fat-fingered
        # from a phone with nobody at the keyboard is the failure that cannot
        # be undone remotely. There is no !config set, and secrets are never
        # printed - only whether they are present.
        try:
            api.send_message(cid, ocfg.describe())
        except Exception as e:
            api.send_message(cid, f"could not read config: {type(e).__name__}: {e}")
    elif cmd == "!reload":
        # Restart the watchdog in place so it picks up code changes. Until this
        # existed, every edit to watchdog.py needed physical access to the machine:
        # Python imports at startup, so a running watchdog keeps the old code
        # forever. Requested 2026-07-31 ("que el sistema se pueda reiniciar solo").
        here = Path(__file__).resolve()
        problems = []
        for f in (here, here.parent / "api.py", here.parent / "schedule.py"):
            try:
                compile(f.read_text(encoding="utf-8"), str(f), "exec")
            except SyntaxError as e:
                problems.append(f"{f.name}:{e.lineno}: {e.msg}")
            except OSError as e:
                problems.append(f"{f.name}: {e}")
        if problems:
            # THE important guard. The watchdog is the only thing listening, so
            # re-exec'ing into code that cannot start would kill the bus with no
            # remote way to bring it back - the machine would have to be reached
            # physically. Refuse loudly and keep running the code that works.
            api.send_message(cid, "♻ reload **REFUSED** - this code would not start:\n```\n"
                             + "\n".join(problems) + "\n```\nStill running the previous version.")
            log(f"reload refused, syntax errors: {problems}")
            return
        api.send_message(cid, f"♻ reloading watchdog @ {api.MACHINE} - back in a moment")
        log("reload requested - re-exec")
        release_lock()   # the replacement must not find our lock and exit(3)
        # Re-exec THROUGH service_runner when it started us. It uses runpy, so
        # we are the same process it is - execv'ing a bare `python watchdog.py`
        # dropped the supervisor and the redirected log with it, and left the
        # scheduled task reading Ready while a watchdog it no longer owned kept
        # running (2026-08-12, seen on the second install and confirmed here).
        runner = os.environ.get("OMNIUS_SERVICE_RUNNER") or ""
        argv = [sys.executable]
        if runner and Path(runner).is_file():
            argv.append(runner)
        argv += [str(here)] + sys.argv[1:]
        try:
            os.execv(sys.executable, argv)
        except OSError as e:
            acquire_lock()          # exec failed: we are still alive, take the lock back
            log(f"re-exec failed: {e}")
            api.send_message(cid, f"reload FAILED - still on the old code: {e}")
            return
    elif cmd in ("!screen", "!desktop"):
        # "Show me the screen" from the phone. The watchdog runs it in-process
        # rather than spawning a session: it must work when every desk is dead,
        # which is exactly when you most want to look at the screen.
        #
        # Only tools\desktop\desktop.py REMOTE_VERBS are reachable here, and the
        # owner allowlist upstream is the real chokepoint (docs\PERMISSIONS.md).
        # Note what the read side cannot do: A SCREENSHOT CANNOT BE REDACTED. If
        # .env, a password manager or a private message is on screen, this posts
        # it to Discord. That is a known, accepted limitation of the feature, not
        # an oversight - see tools\desktop\README.md.
        rest = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
        if cmd == "!screen":
            verb_name, rest = "screenshot", rest
        else:
            parts = rest.split(None, 1)
            verb_name = parts[0].lower() if parts else ""
            rest = parts[1].strip() if len(parts) > 1 else ""
        try:
            ok, msg, shot = run_desktop_verb(verb_name, rest, target.channel_name)
        except Exception as e:
            api.send_message(cid, f"🖥 desktop unavailable: {type(e).__name__}: {e}")
            log(f"desktop verb failed to run: {e}")
            return
        api.send_message(cid, ("🖥 " + msg) if ok else ("🖥 " + msg),
                         files=[shot] if shot else None)
    elif cmd == "!model":
        # Acts on THIS channel's desk, exactly like !kill and !restart. Naming a
        # desk as an argument is the footgun that trap note already warns about.
        session = target.session
        if not session:
            api.send_message(cid, "this channel has no desk — run `!model` in a desk's own channel")
            return
        args = text.split()[1:]
        cur, src = desk_config(session), desk_config_source(session)

        if not args:                                   # show, with provenance
            live = running_model(session)
            # The alias is what we asked for; this is what answered. Shown
            # whenever it adds information - "opus" does not tell you which
            # Opus, and that is the question he actually asked.
            real = resolved_model(session)
            lines = [f"**`{session}`**"]
            if live:
                exact = f"  → `{real}`" if real and real != live[0] else ""
                lines.append(f"▶ **running now** — `{live[0] or '?'}` / `{live[1] or '?'}`{exact}")
                if (live[0], live[1]) != (cur["model"], cur["effort"]):
                    # The whole reason this command reads two sources: a change
                    # made mid-run is real but not yet in effect, and showing
                    # only the config would claim otherwise.
                    lines.append(f"⚠ config differs — the **next** run gets "
                                 f"`{cur['model']}` / `{cur['effort']}`. `!restart` to cut over.")
            elif run_active(session):
                lines.append("▶ running, but it started before the watchdog "
                             "recorded model/effort — `!restart` to know for sure")
            else:
                lines.append("○ nothing running — this is what the next run will use")
            alias_note = f" → `{real}`" if real and real != cur["model"] else ""
            lines.append(f"• model — `{cur['model']}`{alias_note}  ({src['model']})")
            lines.append(f"• effort — `{cur['effort']}`  ({src['effort']})")
            lines.append("-# `!model sonnet` · `!model sonnet low` · `!model effort low` "
                         "· `!model reset` · `!restart sonnet low`")
            api.send_message(cid, "\n".join(lines))
            return

        if args[0].lower() == "reset":
            try:
                fleet_set_desk(session, clear=True)
            except (OSError, ValueError) as e:
                api.send_message(cid, f"could not write fleet.json: {type(e).__name__}: {e}")
                return
            new = desk_config(session)
            api.send_message(cid, f"↩ `{session}` back to the inherited default — "
                                  f"**{new['model']}** / **{new['effort']}**.{_model_when(session)}")
            log(f"!model reset {session}")
            return

        model, effort, err = parse_model_effort(args)
        if err:
            api.send_message(cid, err)
            return

        warn = ""
        if model is not None and not model_looks_known(model):
            # Accepted anyway - see KNOWN_MODEL_ALIASES. Better a warning now
            # than a run that dies in half an hour with nobody watching.
            warn = (f"\n⚠ I don't recognise **{model}** — using it as given. If it is "
                    f"wrong the next run will fail; `!model reset` undoes this.")

        try:
            fleet_set_desk(session, model=model, effort=effort)
        except (OSError, ValueError) as e:
            api.send_message(cid, f"could not write fleet.json: {type(e).__name__}: {e}")
            return
        new = desk_config(session)
        log(f"!model {session} -> {new['model']} / {new['effort']}")
        api.send_message(cid, f"✓ `{session}` → **{new['model']}** / **{new['effort']}**"
                              f"{_model_when(session)}{warn}")

    elif cmd == "!killall":
        if target.channel_name not in ("omnius", "orchestrator"):
            api.send_message(cid, "!killall only works in #omnius")
            return
        results = [kill_session(f.stem) for f in sorted(SESSIONS.glob("*.json"))]
        api.send_message(cid, "**fleet stop**\n" + ("\n".join(results) or "nothing was running"))
    log(f"control: {cmd} in #{target.channel_name}")


# --- outbound -----------------------------------------------------------------

REFUSED = "__refused__"
# Channels that carry no session: any desk may raise a flag there on purpose.
BROADCAST_CHANNELS = ("alerts", "fleet-status")


def resolve_outbox_target(mapping, session, data):
    """-> channel id, None (no target), or REFUSED (target owned by someone else).

    Names are ambiguous across projects: two projects each own an "#app", so
    matching t.channel_name over the WHOLE mapping posted a reply into whichever
    project happened to come first in dict order. Prefer the channel id the
    envelope came in with, and never resolve into a channel this session does
    not own - CLAUDE.md par.2 promises scoped writes, and the transport has to
    honour that too."""
    def allowed(t):
        return t.session == session or t.channel_name in BROADCAST_CHANNELS

    cid = data.get("channelId")
    if cid and cid in mapping:               # unknown id: fall through to name/primary
        return cid if allowed(mapping[cid]) else REFUSED

    name = data.get("channel")
    if name:
        matches = [k for k, t in mapping.items() if t.channel_name == name]
        mine = [k for k in matches if allowed(mapping[k])]
        if mine:
            return mine[0]
        if matches:
            return REFUSED                   # it exists, but not for this session
    cid = primary_channel_id(mapping, session)
    if cid:
        return cid
    # A desk with no channel of its own would otherwise keep the envelope
    # forever. Permission asks carry fallback:"alerts" so a question is never
    # the thing that goes unseen - it is the one message class where silence
    # costs him a blocked desk.
    name = data.get("fallback")
    if name:
        for k, t in mapping.items():
            if t.channel_name == name:
                return k
    return None


def flush_outboxes(mapping):
    if not OUTBOX.is_dir():
        return
    for box in sorted(OUTBOX.iterdir()):
        if not box.is_dir():
            continue
        session = box.name
        for f in sorted(box.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                log(f"outbox {session}/{f.name}: unreadable ({e}) - renamed .bad")
                f.rename(f.with_suffix(".bad"))
                continue
            cid = resolve_outbox_target(mapping, session, data)
            if cid == REFUSED:
                log(f"outbox {session}: refused - {data.get('channelId') or data.get('channel')} "
                    f"belongs to another session; renamed .refused")
                f.rename(f.with_suffix(".refused"))
                continue
            if not cid:
                log(f"outbox {session}: no channel mapped - kept")
                continue
            try:
                api.send_message(cid, data.get("text", ""), files=data.get("files"))
                for p in (data.get("files") or []):  # sent copies -> durable archive
                    src = Path(p)
                    if src.exists() and MEDIA not in src.parents:
                        dest = MEDIA / "sent" / datetime.now().strftime("%Y-%m") / src.name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        if not dest.exists():
                            shutil.copy2(src, dest)
                transcribe(session, "out", data.get("text", ""),
                           channel=mapping[cid].channel_name if cid in mapping else None,
                           channel_id=cid, files=data.get("files"))
                f.unlink()
                # Proof-of-reply for the Stop hook. Posting DELETES the outbox
                # file, and the flush runs every ~3s - faster than a run ends -
                # so by turn end a prompt reply has vanished and the silence
                # announcer cried wolf on the very first live run (2026-08-01:
                # "posted nothing" straight after it posted). The stamp's mtime
                # is what announce_if_silent checks alongside surviving files.
                try:
                    (box / ".last-posted").write_text(now_iso(), encoding="utf-8")
                except OSError:
                    pass
                log(f"posted outbox {session}/{f.name}")
            except api.ApiError as e:
                log(f"outbox post failed ({session}/{f.name}): {e}")


# --- inbound dispatch ---------------------------------------------------------

ALLOW_WORDS = ("ok", "okay", "yes", "y", "allow", "approve", "go", "sure", "do it")
DENY_WORDS = ("no", "n", "deny", "nope", "stop", "reject", "cancel")


def pending_permissions():
    """-> list of pending request dicts, oldest first."""
    out = []
    if not PERMS.is_dir():
        return out
    for p in sorted(PERMS.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("id"):
                out.append(d)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return out


def answer_permission(text):
    """Interpret a reply as a verdict on a pending permission request.

    Returns a confirmation string to post, or None if this message was not an
    answer - so ordinary chat is never swallowed. Only ever consulted when
    something is actually pending."""
    pend = pending_permissions()
    if not pend:
        return None
    words = text.lower().replace("`", "").split()
    if not words:
        return None
    head = words[0].strip(".,!")
    two = " ".join(words[:2]).strip(".,!")
    if head in ALLOW_WORDS or two in ALLOW_WORDS:
        behavior = "allow"
    elif head in DENY_WORDS or two in DENY_WORDS:
        behavior = "deny"
    else:
        return None

    codes = {d.get("code"): d for d in pend if d.get("code")}
    target_req = None
    for w in words[1:]:
        w = w.strip("`.,!")
        if w in codes:
            target_req = codes[w]
            break

    if target_req is None and len(pend) > 1:
        # One "ok" answers ALL of a single desk's pending asks.
        #
        # 2026-08-02, the owner's words: "I could not write 20 times ok in
        # discord". He asked one desk to read a website; it fired SIX WebFetch
        # asks in six seconds, and this function then refused every bare "ok"
        # as ambiguous and demanded a code. Technically correct, useless on a
        # phone: he answered twenty times, nothing was decided, and all six
        # timed out. The asks were one intention of his, so one answer settles
        # them. A code still targets a single request when he wants that, and
        # asks spanning DIFFERENT desks still require one - those really are
        # separate decisions.
        sessions = {d.get("session") for d in pend}
        if len(sessions) == 1:
            for d in pend:
                (PERMS / f"{d['id']}.answer").write_text(
                    json.dumps({"behavior": behavior, "at": now_iso()}), encoding="utf-8")
                (PERMS / f"{d['id']}.json").unlink(missing_ok=True)
            log(f"permission {behavior} for {pend[0].get('session')} "
                f"- ALL {len(pend)} pending ({', '.join(d.get('code', '?') for d in pend)})")
            mark = "✅ allowed" if behavior == "allow" else "⛔ denied"
            tools = ", ".join(sorted({str(d.get("tool")) for d in pend}))
            note = remember_allowed(behavior, [d.get("tool") for d in pend])
            return (f"{mark} — all {len(pend)} waiting requests for "
                    f"`{pend[0].get('session')}` ({tools}){note}")
        listing = ", ".join(f"`{d.get('code')}` ({d.get('session')})" for d in pend)
        return (f"⚠️ {len(pend)} requests are waiting, on different desks - "
                f"answer with the code: {listing}")

    if target_req is None:
        target_req = pend[0]

    (PERMS / f"{target_req['id']}.answer").write_text(
        json.dumps({"behavior": behavior, "at": now_iso()}), encoding="utf-8")
    (PERMS / f"{target_req['id']}.json").unlink(missing_ok=True)
    log(f"permission {behavior} for {target_req.get('session')} "
        f"({target_req.get('tool')}, code {target_req.get('code')})")
    mark = "✅ allowed" if behavior == "allow" else "⛔ denied"
    note = remember_allowed(behavior, [target_req.get("tool")])
    return f"{mark} — `{target_req.get('session')}` · {target_req.get('tool')}{note}"


def remember_allowed(behavior, tools):
    """An "ok" also teaches the fleet, so a tool asks ONCE. -> a note, or "".

    His decision 2026-08-13, after asking the right question: why is there a
    list to forget in the first place? Because it cannot be complete - a new
    MCP server is named the day he connects it, and `Artifact` did not exist in
    older Claude Code. The list always drifts FORWARD, so a longer list is not
    the fix; letting an approval stick is.

    Scope of what an "ok" now buys: that TOOL, on every desk, from its next run.
    Not the arguments it was asked about - a tool name carries no path and no
    command, so this cannot widen into "allow that particular rm -rf". The deny
    list is never touched: Read(./.env) survives every approval, and a "no"
    teaches nothing at all.
    """
    if behavior != "allow":
        return ""
    fresh = []
    try:
        for t in tools:
            if perms_sync.learn(t):
                fresh.append(str(t))
        if fresh:
            perms_sync.main()          # stamp every desk now, not at next boot
    except Exception as e:             # noqa: BLE001
        # Never let bookkeeping swallow the answer he is waiting on: the
        # permission itself is already written and the desk is already moving.
        log(f"could not remember allowed tool(s) {tools}: {type(e).__name__}: {e}")
        return ""
    if not fresh:
        return ""
    log(f"allow-list learned: {', '.join(fresh)} (every desk, from its next run)")
    return f"\n➕ added **{', '.join(fresh)}** to the allow-list — won't ask again."


def handle_message(m, cid, target, me, mapping):
    """Dispatch one inbound Discord message. Returns a status token (also handy
    for tests): skip-bot | skip-nonowner | skip-guest-channel | control |
    unmapped | delivered | spawned."""
    author = m.get("author", {})
    if author.get("bot") or str(author.get("id")) == str(me["id"]):
        return "skip-bot"
    sender = "owner"
    if str(author.get("id")) != str(api.OWNER):
        label, guest = guest_for(author.get("id"))
        if not guest:
            log(f"ignored non-owner message in #{target.channel_name} (author {author.get('id')})")
            return "skip-nonowner"
        # A guest is confined to the channels their entry names, and is refused
        # SILENTLY: answering "you may not write here" in a channel they were
        # never meant to reach would draw them a map of the fleet.
        if not guest_may_write(guest, cid, target.channel_name):
            log(f"ignored guest {label} in #{target.channel_name} "
                f"- not in their channel list")
            return "skip-guest-channel"
        if not target.session:
            log(f"ignored guest {label} in unmapped #{target.channel_name}")
            return "skip-guest-channel"
        sender = label
    text = (m.get("content") or "").strip()
    # Control verbs, permission answers and takeover answers are HIS ALONE.
    # A guest typing !screen must not get a screenshot of his desktop, and a
    # guest's "ok" must not answer a question the watchdog asked HIM - the
    # takeover prompt closes his own terminal window. Guests only ever send
    # mail; everything below this block is reachable by the owner only.
    if sender == "owner":
        if text.lower().startswith(CONTROL_COMMANDS):
            handle_control(text, cid, target, mapping)
            return "control"
        verdict = answer_permission(text)
        if verdict:
            try:
                api.send_message(cid, verdict)
            except api.ApiError:
                pass
            return "permission"
        # "ok" answering a takeover question is an ANSWER, not mail. Checked
        # before delivery so it never lands in the desk's inbox as a message to
        # reply to.
        takeover = answer_takeover(text, target.session)
        if takeover:
            try:
                api.send_message(cid, takeover)
            except api.ApiError:
                pass
            return "takeover"
    if not target.session:
        # An ok/no in #alerts that arrives after the request timed out matched
        # nothing, so the owner got "nobody listens here" for answering exactly
        # where he was told to - which is how a working rail comes to look
        # broken (2026-08-02). Say what actually happened instead.
        if (target.channel_name == "alerts"
                and text.lower().strip(".,!` ").split(" ")[0] in ALLOW_WORDS + DENY_WORDS):
            try:
                api.send_message(cid, "⌛ Nothing is waiting for an answer right now — that "
                                      "request already timed out and fell back to the desk's "
                                      "own screen. Answers only count while the 🔐 ask is open.")
            except api.ApiError:
                pass
            return "unmapped"
        # Don't leave the owner talking to a wall: point at the live channels.
        talk = sorted({f"#{t.channel_name}" for t in mapping.values() if t.session})
        try:
            api.send_message(cid, f"\U0001f507 #{target.channel_name} is a read-only status channel - "
                                  f"nobody listens here. Talk to me in: {', '.join(talk)}")
        except api.ApiError:
            pass
        log(f"note: message in unmapped #{target.channel_name} - redirected owner")
        return "unmapped"
    try:
        api.add_reaction(cid, m["id"])
    except api.ApiError:
        pass
    files = save_attachments(m)
    write_envelope(target.session, target.channel_name, m, files,
                   channel_id=cid, category=target.category_name, sender=sender)
    preview = text[:60].replace("\n", " ")
    # Log what actually happened, not what was intended - "spawned" when nothing
    # spawned is the comfortable lie that hid a stalled desk for three hours.
    state = ensure_runner(target.session)
    notes = {"empty": "already handled by the active run",
             "run-in-progress": "queued behind the active run",
             "bridge-owns-desk": "handed to the live bridge (warm session)",
             "bridge-replaced": "the bridge was not delivering - replaced it",
             "owner-at-the-desk": "his own window has this desk - asked, not taken",
             "terminal-busy": "queued - a terminal is mid-turn there, follows up when it ends",
             "backoff": "queued - desk is in failure backoff, see state\\logs\\runs",
             "started": "run started",
             "start-failed": "RUN COULD NOT START - see watchdog log"}
    who = "" if sender == "owner" else f" from {sender}"
    log(f'#{target.channel_name}{who} -> inbox {target.session} '
        f'({notes.get(state, state)}): "{preview}"')
    return {"started": "spawned", "empty": "delivered"}.get(state, "queued")


def hello_post(mapping):
    """Startup is LOGGED, not announced.

    It used to post "watchdog online" to #fleet-status on every start. On
    2026-08-03 that was fifteen notifications in a day, none of which told him
    anything he wanted at the moment it arrived - the watchdog coming up is the
    normal case, and the fleet board already shows the truth continuously.

    Kept as a function because a FAILURE to start is worth saying; that path
    lives in main() and still speaks."""
    log(f"watchdog online @ {api.MACHINE} (not announced - startup is not news)")
    return False


# --- main loop ----------------------------------------------------------------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("=" * 60)
    print(" OMNIUS WATCHDOG - transport layer (Ctrl+C to stop)")
    print("=" * 60)
    problems = api.config_problems()
    if problems:
        for p in problems:
            log(f"Discord not configured: {p}")
        log("fix .env (guided: run install.bat). Exiting.")
        sys.exit(2)
    acquire_lock()
    # Best effort only, and worth being precise about: on Windows a hard
    # TerminateProcess (taskkill /F, a killed console, most supervisors) runs
    # NEITHER atexit handlers NOR Python signal handlers, and SIGTERM is largely
    # fictional there. Observed 2026-07-25: the process was killed and the lock
    # survived. So this covers only the graceful exits.
    #
    # The lock is therefore NOT the safety mechanism - acquire_lock's pid
    # liveness check is, and it is what actually lets the next start take over a
    # stranded lock (verified the same day). Do not add logic that assumes the
    # lock file disappears on exit.
    atexit.register(release_lock)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: sys.exit(0))   # exit -> atexit runs
        except (ValueError, OSError, AttributeError):
            pass                                        # not on the main thread
    for d in (INBOX, OUTBOX, MEDIA, LOGS, SESSIONS, WD_STATE, RUNS, TURNS, BRIDGES):
        d.mkdir(parents=True, exist_ok=True)
    # Runs started by a previous watchdog: adopt them via their lease pid rather
    # than spawning a second brain onto a busy desk. Dead leases clean up lazily
    # in run_active().
    for f in sorted(RUNS.glob("*.json")):
        lease = read_lease(f.stem)
        if lease and pid_alive(lease.get("pid")):
            log(f"adopting live run for {f.stem} (pid {lease['pid']}) from a previous watchdog")

    # Everything up to the poll loop runs OUTSIDE its try/except. An exception
    # here used to kill the service with the traceback going to a console the
    # user then closed - leaving a log that stops dead after "token ok" and no
    # way to tell what happened. Never lose a startup failure again.
    # A restored PC boots, joins wifi, and starts its services - often in that
    # order. On 2026-08-14 the very first call died with `getaddrinfo failed`
    # because the network was not up yet, dumped a 40-line traceback and exited;
    # only the task's 1-minute self-heal trigger brought it back, three minutes
    # later. That is luck, not design. A name that will not resolve YET is not a
    # startup failure, it is a wait - so wait, out loud, in one line each.
    me = schema = mapping = None
    for attempt in range(STARTUP_NET_ATTEMPTS):
        try:
            me = api.api("GET", "/users/@me")
            log(f"token ok - bot: {me['username']} @ {api.MACHINE}")
            api.ensure_structure(log=log)
            schema = api.load_schema()
            mapping = build_map(schema)
            reload_guests()
            break
        except Exception as e:                                   # noqa: BLE001
            # Only the network is worth waiting for. A bad token or a revoked
            # bot fails identically every time, and retrying it for two minutes
            # just delays the traceback that explains it.
            transient = isinstance(e, (socket.gaierror, TimeoutError, ConnectionError)) \
                or "getaddrinfo" in str(e) or "urlopen error" in str(e) \
                or isinstance(getattr(e, "reason", None), (socket.gaierror, OSError))
            if transient and attempt < STARTUP_NET_ATTEMPTS - 1:
                wait = min(STARTUP_NET_BACKOFF * (attempt + 1), 30)
                log(f"network not up yet ({type(e).__name__}) - retrying in {wait}s "
                    f"({attempt + 1}/{STARTUP_NET_ATTEMPTS})")
                time.sleep(wait)
                continue
            log(f"STARTUP FAILED: {type(e).__name__}: {e}")
            for ln in traceback.format_exc().rstrip().splitlines():
                log(f"  {ln}")
            release_lock()
            sys.exit(2)
    log(f"mapped {len(mapping)} channels, owner allowlist: {api.OWNER}")
    for _label, _g in sorted(GUESTS.items()):
        log(f"guest '{_label}' ({_g['id']}) may write in: {', '.join(_g['channels'])}")
    hello_post(mapping)

    last_ids_file = WD_STATE / "last_ids.json"
    try:
        last_ids = json.loads(last_ids_file.read_text(encoding="utf-8"))
        if not isinstance(last_ids, dict):
            raise ValueError("last_ids.json is not an object")
    except FileNotFoundError:
        last_ids = {}  # first run - normal, start from newest
    except (OSError, json.JSONDecodeError, ValueError) as e:
        # Absent is normal; corrupt is not. Resetting silently would re-deliver
        # or skip history, so say it loudly and keep the evidence.
        log(f"last_ids.json is unreadable ({e}) - starting from newest; "
            f"kept a copy as last_ids.bad")
        try:
            last_ids_file.replace(last_ids_file.with_suffix(".bad"))
        except OSError:
            pass
        last_ids = {}
    last_ids_written = dict(last_ids)
    last_refresh = time.time()
    consecutive_deaf = 0
    seen = SeenIds()

    def persist():
        # Only write when a cursor actually moved. Writing every poll was
        # ~10.5M rewrites/year of a file that changes only when a message
        # arrives - pointless disk churn on an always-on service.
        nonlocal last_ids_written
        if last_ids != last_ids_written:
            write_json_atomic(last_ids_file, last_ids)
            last_ids_written = dict(last_ids)

    gw = start_gateway()
    write_beacon(len(mapping), gateway=bool(gw and gw.connected))

    # The housekeeping cadence stays exactly what it was (POLL_SECONDS). What
    # changes is how the time in between is spent: blocked on the gateway queue
    # so a message is handled the instant it lands, instead of slept through.
    next_tick = time.time() + POLL_SECONDS
    last_reconcile = 0.0
    # For the "gateway missed" diagnostic: it is only evidence of a dropped push
    # if the socket was up for the WHOLE window the sweep covers. The first sweep
    # after a start or a reconnect legitimately finds everything that arrived
    # while we were away, and reporting that as a miss cries wolf about the one
    # signal that would tell us gateway.py has a real bug.
    prev_sweep_live, prev_reconnects = False, 0
    while True:
        try:
            if gw is not None:
                drain_gateway(gw, next_tick, mapping, me, last_ids, persist, seen)
            else:
                time.sleep(max(0.0, next_tick - time.time()))
            next_tick = time.time() + POLL_SECONDS

            if time.time() - last_refresh > MAP_REFRESH_SECONDS:
                # Re-stamp the structure, not just re-read it. ensure_structure
                # is find-or-create, so this is free while nothing is missing -
                # and when something IS missing it comes back on its own within
                # a minute. Before this, the structure was built ONCE at
                # startup: delete a channel on a running instance and it stayed
                # gone until someone restarted the watchdog or ran a CLI verb
                # (2026-08-11, he deleted the lot to start clean and was left
                # with a server the bot no longer recognised). A schema is a
                # description of how the server SHOULD look; nothing that only
                # applies at boot can honour that.
                try:
                    api.ensure_structure(log=log)
                except Exception as e:                           # noqa: BLE001
                    log(f"structure re-check failed: {type(e).__name__}: {e}")
                mapping = build_map(schema)
                # Same cadence as the map: a guest added to config\guests.ini is
                # live within the minute, with no restart and no !reload.
                before = set(GUESTS)
                if set(reload_guests()) != before:
                    log(f"guest list changed -> {', '.join(sorted(GUESTS)) or 'none'}")
                last_refresh = time.time()
                rotate_log()

            # While the socket is healthy the REST sweep is a backstop, not the
            # transport, so it runs every RECONCILE_SECONDS. The moment the
            # gateway is down or was never available, this is once per tick -
            # bit-for-bit the old 3-second polling behaviour.
            live = bool(gw is not None and gw.connected and not gw.fatal)
            if time.time() - last_reconcile >= (RECONCILE_SECONDS if live else 0):
                last_reconcile = time.time()
                deaf_channels, delivered = rest_sweep(mapping, me, last_ids, persist, seen)
                unbroken = live and prev_sweep_live and gw.reconnects == prev_reconnects
                if unbroken and delivered:
                    # Worth saying out loud: the socket was up the whole window,
                    # so it dropped something the sweep had to rescue. Rare is
                    # fine, frequent is a bug in gateway.py.
                    log(f"gateway missed {delivered} message(s) - recovered by the REST sweep")
                prev_sweep_live = live
                prev_reconnects = gw.reconnects if gw else 0
                # A sweep that reached every channel proves we can still talk to
                # Discord. A revoked token or a permissions change leaves the
                # process alive, holding its lock and logging happily, while
                # delivering nothing - which no process-liveness check can detect.
                if deaf_channels == 0:
                    consecutive_deaf = 0
                else:
                    consecutive_deaf += 1
                    if consecutive_deaf >= DEAF_PASSES_BEFORE_EXIT:
                        log(f"DEAF: {consecutive_deaf} consecutive sweeps could not reach "
                            f"{deaf_channels} channel(s). Exiting so a supervisor can restart "
                            f"us - staying up while delivering nothing is worse than being down.")
                        release_lock()
                        sys.exit(4)

            flush_outboxes(mapping)
            check_backlogs()
            reap_runs()
            ensure_runners()
            show_working(mapping)
            fleet_board(mapping)
            fire_due_schedules()
            fire_heartbeat()
            # Either transport being demonstrably healthy earns the stamp, so the
            # beacon stays ~3s fresh however messages are currently arriving.
            if consecutive_deaf == 0 or live:
                write_beacon(len(mapping), gateway=live)
            persist()
        except KeyboardInterrupt:
            log("stopped by user - bye")
            release_lock()
            return
        except api.ApiError as e:
            log(f"api error: {e} - retrying in 10s")
            time.sleep(10)
        except Exception as e:
            log(f"unexpected: {type(e).__name__}: {e} - continuing in 10s")
            time.sleep(10)


if __name__ == "__main__":
    main()
