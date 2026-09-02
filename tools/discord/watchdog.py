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
import hashlib
import os
import queue
import re
import signal
import shutil
import socket
import subprocess
import sys
import threading
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
import voice  # noqa: E402  - audio replies -> native Discord voice notes

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
THREADS = WD_STATE / "threads"    # desk-mail chain ledgers (docs\DELEGATION.md D1)
TWOFA = STATE / "twofa"           # 2FA code relay: <id>.json asked, <id>.code answered
GATE = STATE / "gate"             # held cross-project desk mail awaiting ok/no (D4).
# NOT under state\inbox\ - every folder there is treated as a desk (see DROPPED).
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
                    "!screen", "!desktop", "!config", "!stop", "!cron", "!model",
                    "!update", "!trace")
# An envelope's `from` is either a PERSON or the fleet talking to itself. These
# three are the fleet; "owner" and every configured guest label are people.
# Stated as an exclusion list on purpose: guests are configured, not compiled,
# so a list of people would silently omit every guest added after this line.
SYSTEM_SENDERS = ("omnius", "heartbeat", "schedule")
GUESTS = {}   # label -> guest, from config\guests.ini; reloaded with the map
SLASH_SKILLS = set()   # /<name> owner mail may fire, from config\skills.ini; same cadence
DROPPED = STATE / "dropped"   # cancelled mail, kept rather than deleted.
# NOT under state\inbox\: ensure_runners() treats every folder there as a
# SESSION, so parking envelopes inside it would invent a desk named after the
# folder - and the deadman would then page him about mail he just cancelled.

# Every desk runs Opus 5 at xhigh effort (user decision 2026-07-31: "make all
# sessions by default with opus 5 and effort xhigh"). The code default is what
# travels - .env never does - so a fresh machine still gets the right
# behaviour. .env overrides per instance; start_run(model=, effort=) overrides
# per desk for the rare cheap one.
# `ultracode` is not a sixth level - it is xhigh PLUS standing dynamic-workflow
# orchestration, which is why the CLI's own slider stops at max and puts it
# behind a dotted line. It belongs in this list anyway because `--effort
# ultracode` is what the CLI accepts: its parser maps the word and sets the
# workflow flag itself (verified 2026-09-01 against 2.1.252 - `--settings` is
# NOT needed). Before that it was this tuple, not Claude Code, that threw the
# word away and silently fell back to xhigh on line 2373.
# Opt-in per desk only (fleet.json override or `!model <desk> effort
# ultracode`): it orchestrates a workflow on every substantive turn, so the
# default stays xhigh and the spend stays predictable.
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultracode")
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
    """True when `claude --continue` PLAUSIBLY has something of ITS OWN here.

    False means --continue would silently attach to the most recent conversation
    from another folder - see the comment in start_run(). Treat an unreadable
    home directory as "no history": starting cold is always safe, resuming the
    wrong conversation is not.

    This is a cheap NEGATIVE check, not a promise: an empty folder, stray
    non-transcript files, or a .jsonl with no conversation turns mean "nothing
    to resume" for certain, but True is only a prediction - the CLI owns the
    real decision, and it can change it (2026-08-18: a transcript written by
    CLI 2.1.232, 145 valid turns, was refused by 2.1.234 with "No conversation
    found to continue", and the folder-non-empty version of this check turned
    that refusal into a 112-window boot loop on a desk). The durable guard is
    downstream: the bridge relaunches WITHOUT --continue when the CLI refuses
    it at boot (desk_bridge), and a fast-dying tab now counts as a failed
    start (run_active)."""
    try:
        d = history_dir_for(cwd)
        if not d.is_dir():
            return False
        for f in d.glob("*.jsonl"):
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    if '"message"' in fh.read(65536):
                        return True
            except OSError:
                continue
        return False
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


def agent_name():
    """The owner's name for this agent (config\\omnius.ini `[omnius] name`).

    "omnius" is the install folder and the skill; THIS is what he calls it.
    Read live rather than cached at import, so `!reload` is not needed to
    change it - and never fatal, because a config file may not take the
    fleet down (omnius_config rule 3)."""
    try:
        return ocfg.agent_name()
    except Exception:                                    # noqa: BLE001
        return "Omnius"


def agent_slug():
    """agent_name() as a channel name - what #omnius is called here."""
    try:
        return ocfg.agent_slug()
    except Exception:                                    # noqa: BLE001
        return "omnius"


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
    # Which desk a channel serves is remembered by channel ID (api.pin_channel).
    # Everything below this line derives it from the NAME instead - which was
    # the whole bug: renaming #web to #frontend in the Discord app made the
    # desk deaf, and #omnius could never be called anything else. So the pin
    # wins where there is one, names are consulted only for channels nobody
    # has pinned yet, and what they derive gets pinned on the spot. That is
    # also the migration: the first refresh after an update pins the fleet as
    # it stands today, and from then on names are cosmetic.
    pins = api.unpin_missing(chans)
    by_id = {str(v.get("id")): v.get("session") for v in pins.values() if v.get("id")}
    # Session-less schema channels (#alerts) carry no desk to key on, so they
    # are pinned as "<category>#<name>" - the key ensure_structure uses too.
    static = {cc["name"]: {ch["name"] for ch in cc.get("channels", [])}
              for cc in schema["initial"]["categories"]}
    mapping = {}
    for c in chans:
        if c["type"] != api.CHANNEL_TEXT:
            continue
        cat = cats.get(c.get("parent_id"), "")
        name = c["name"]
        pinned = by_id.get(str(c["id"]))
        if pinned:
            session = pinned
            if "." in pinned and not pinned.startswith("tool."):
                # A pin says which desk, not that the desk still exists:
                # the component folder can be renamed or removed too.
                project, _, comp = pinned.partition(".")
                if not (ROOT / "projects" / project / comp).is_dir():
                    log(f"warn: #{name} in {cat} is pinned to {pinned} but "
                        f"projects{chr(92)}{project}{chr(92)}{comp} is gone - unmapped")
                    session = None
            # Pinned means "ours" even when unmapped: say so, never drop it.
            mapping[c["id"]] = Target(session, name, cat)
            continue
        session = None
        # Is this the desk's OWN channel, or one that merely relays to it?
        # Only a home channel may claim the desk's pin - otherwise a project
        # #general (which relays to the orchestrator) could take the
        # "orchestrator" pin purely by being listed first, and he would be
        # answered in some project's channel instead of his own.
        home = True
        if cat == orch_cat:
            if name in ("omnius", "orchestrator", agent_slug()):
                # Renamed to #omnius 2026-07-31 (it is the persona, inside the
                # 🎛 ORCHESTRATOR category). BOTH names were accepted because a
                # watchdog of that era mapped by channel NAME, so renaming while
                # it held old code unmapped the channel and cut the owner off
                # entirely - accepting both made that rename a non-event. A
                # third is accepted since 2026-08-24: the name he gave this
                # agent at install (#jarvis, #maikel). None of it is load-
                # bearing any more - this branch only runs for a channel nobody
                # has pinned yet, and after that the id decides and the name is
                # his to change.
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
                # #general is a DOOR, never a home: a one-component project
                # answers here as a convenience, but its desk still lives in
                # (and replies to) its own channel.
                home = False
            else:
                session = f"{project}.{name}"
                if not (ROOT / "projects" / project / name).is_dir():
                    log(f"warn: #{name} in {cat} has no folder projects\\{project}\\{name} - unmapped")
                    session = None
        elif cat.startswith(arch_prefix):
            session = None  # archived: ignore
        # Channels in a FLEET category are mapped even when they answer to no
        # desk, so the owner gets told instead of ignored. Renaming #web to
        # #frontend unmaps it only while the channel is UNPINNED (a wiped
        # state\, a new PC - the name rules above run for nothing else), and
        # until 2026-08-20 the message was dropped in silence: `if not target:
        # continue`. A channel outside the fleet's categories stays unmapped
        # and unmentioned, which is right: it is not ours.
        # First sighting of a channel this instance derived by name: remember
        # the id, so the owner may rename it from now on. A desk's home channel
        # is pinned under the desk id (that is what primary_channel_id asks
        # for); everything else - relays, extra channels of one desk, the
        # session-less ones - under "<category>#<name>", which routes just as
        # well and cannot displace a home.
        if session:
            key = session if (home and session not in pins) else f"{cat}#{name}"
        elif name in static.get(cat, ()):
            key = f"{cat}#{name}"
        else:
            key = None
        if key and key not in pins:
            api.pin_channel(key, c["id"], session)
            pins[key] = {"id": str(c["id"]), "session": session}
        if session or cat == orch_cat or cat.startswith(proj_prefix):
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
    reload_slash_skills()
    return GUESTS


def reload_slash_skills():
    """Re-read config\\skills.ini into SLASH_SKILLS (docs\\DELEGATION.md D6).
    Rides reload_guests' cadence - both are authorisation lists, both fail
    closed: unreadable means NOTHING passes, never everything."""
    global SLASH_SKILLS
    try:
        SLASH_SKILLS = ocfg.slash_skills()
    except Exception as e:                                   # noqa: BLE001
        log(f"skills.ini unreadable ({type(e).__name__}: {e}) - no pass-through this round")
        SLASH_SKILLS = set()
    return SLASH_SKILLS


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
    and that is just as true when the person is a guest.

    Since desk mail (docs\\DELEGATION.md D2) the negative space grew: a session
    id or a tool's job tag in `from` is the fleet talking to itself, and the
    fleet waiting on the fleet is a log line too - never a window, never a
    page. is_fleet_sender (desk-mail section) is the positive predicate."""
    return bool(who) and not is_fleet_sender(who)


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
    # The pin is the answer where there is one: it was written when the
    # channel was created and survives every rename the owner makes. The
    # name rules below are the fallback for channels nobody pinned yet.
    pinned = str((api.channel_pins().get(session) or {}).get("id") or "")
    if pinned and pinned in mapping and mapping[pinned].session == session:
        return pinned
    # Dot-less ids exist (orchestrator, daybook); split(".", 1)[1] raises IndexError
    # on them, which would have crashed the watchdog the first time a daybook
    # session needed its primary channel.
    if session == "orchestrator":
        # The configured name first, then #omnius/#orchestrator. This must be
        # explicit: EVERY project's #general also maps to the orchestrator, so the
        # any-channel fallback below could otherwise answer the owner in some
        # project's channel instead of his own.
        wants = [agent_slug(), "omnius", "orchestrator"]
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


def fleet_channel_id(mapping, name, session=None):
    """Id of a fleet channel by IDENTITY first, by name second.

    `name` is what the channel was CREATED as (schema); the owner may have
    renamed it since, which is the whole reason the pin is asked first.
    Session-less channels (#alerts) are pinned as "<category>#<name>"."""
    pins = api.channel_pins()
    keys = ([session] if session else []) + sorted(
        k for k, v in pins.items()
        if k.endswith("#" + name) and (v or {}).get("session") in (None, session))
    for k in keys:
        cid = str((pins.get(k) or {}).get("id") or "")
        if cid and cid in mapping:
            return cid
    if session:
        cid = next((c for c, t in mapping.items() if t.session == session), None)
        if cid:
            return cid
    return next((c for c, t in mapping.items() if t.channel_name == name), None)


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


def _ledger_path():
    return WD_STATE / "failures.json"


def save_failure_ledger():
    """Persist strikes/backoff/alerted across restarts.

    These lived only in module dicts until 2026-08-18, which meant three
    strikes required 15 minutes of UNINTERRUPTED watchdog uptime: a desk that
    crash-looped through a restart (or through !reload, or the 60s self-heal)
    handed every desk a clean slate and the owner was never paged. The whole
    crash-loop defence quietly reset itself.

    Cheap and best-effort: a lost ledger costs a delayed alert, never a
    delivery, so this never raises."""
    try:
        WD_STATE.mkdir(parents=True, exist_ok=True)
        write_json_atomic(_ledger_path(), {
            "failures": _run_failures,
            "backoff": {k: v for k, v in _run_backoff.items() if v > time.time()},
            "alerted": sorted(_run_alerted),
            "at": now_iso()})
    except Exception:                                            # noqa: BLE001
        pass


def load_failure_ledger():
    """Restore the ledger at boot. Backoffs already expired are dropped."""
    try:
        d = json.loads(_ledger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    try:
        _run_failures.update({k: int(v) for k, v in (d.get("failures") or {}).items()})
        now = time.time()
        _run_backoff.update({k: float(v) for k, v in (d.get("backoff") or {}).items()
                             if float(v) > now})
        _run_alerted.update(d.get("alerted") or [])
        if _run_failures:
            log(f"failure ledger restored: {dict(_run_failures)}")
    except Exception:                                            # noqa: BLE001
        pass
_tab_booted = {}      # session -> when its terminal CLAIMED (fast-death watch, run_active)
TAB_FAST_DEATH_SECONDS = 30  # a claim dying this soon after boot is a FAILED start


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
        title = f"🎛 {agent_name()}"
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
        _tab_fast_death(session)
        return False
    if lease.get("mode") == "terminal":
        c = read_claim(session)
        if c and str(c.get("machine") or "") == api.MACHINE and pid_alive(c.get("pid")):
            (RUNS / f"{session}.json").unlink(missing_ok=True)   # booted: claim governs now
            _tab_booted[session] = time.time()                   # fast-death watch starts
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


def _tab_fast_death(session):
    """Count 'the tab claimed, then died within seconds' as a FAILED start.

    The 2026-08-18 boot loop: a refused `--continue` killed the desk ~4s after
    it claimed, every ~6s, 112 windows in an hour - and the tab-loop ledger
    never moved, because it only counts a tab that NEVER claims. To every
    check the loop looked like a healthy boot. So: when the claim a terminal
    just wrote dies inside TAB_FAST_DEATH_SECONDS of booting, it goes into the
    same failure ledger as never-claimed - backoff, and the owner is told at
    the same threshold. A claim that survives the window clears the watch: an
    owner closing his own window ten minutes later is not a crash."""
    booted = _tab_booted.get(session)
    if booted is None:
        return
    c = read_claim(session)
    if c and str(c.get("machine") or "") == api.MACHINE and pid_alive(c.get("pid")):
        if time.time() - booted > TAB_FAST_DEATH_SECONDS:
            _tab_booted.pop(session, None)      # survived the window: a real desk
        return
    _tab_booted.pop(session, None)
    if time.time() - booted > TAB_FAST_DEATH_SECONDS * 2:
        return   # died long after boot (or we looked late) - not a boot failure
    fails = _run_failures.get(session, 0) + 1
    _run_failures[session] = fails
    _run_backoff[session] = time.time() + RUN_BACKOFF_SECONDS
    log(f"terminal for {session} claimed and DIED within "
        f"{int(time.time() - booted)}s (failure #{fails}) - backing off "
        f"{RUN_BACKOFF_SECONDS}s")
    if fails >= RUN_FAILURES_BEFORE_ALERT and session not in _run_alerted:
        _run_alerted.add(session)
        try:
            cid = primary_channel_id(build_map(api.load_schema()), session)
            if cid:
                api.send_message(cid, f"🛑 `{session}`: its desk window connects and then "
                                      f"dies within seconds ({fails} tries). Check "
                                      f"`state\\logs\\bridge-{session}.log` - the last "
                                      f"exec line and what follows it usually name the "
                                      f"reason.")
        except Exception as e:
            log(f"fast-death alert failed for {session}: {e}")


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
BRIDGE_QUIET_SECONDS = 45       # ...unless it is still printing (bridges\<id>.out)
BRIDGE_WORK_CEILING = 900       # but no desk gets more than 15 min on that excuse


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
    if held < BRIDGE_DELIVER_SECONDS:
        return False
    # STILL PRINTING IS STILL WORKING. Resuming a long conversation compacts it
    # first: minutes of progress bar, no turn started, so the 90s rule shot the
    # window mid-compaction and opened a fresh one - which then had nothing to
    # compact and worked, hiding the cause (2026-08-19, his screenshot: killed
    # at 1m21s, 59%). A wedged session prints nothing; the bridge stamps
    # state\bridges\<id>.out every 5s while bytes flow, so the difference is
    # observable instead of guessed.
    #
    # Bounded, because a spinner is also output: past BRIDGE_WORK_CEILING the
    # original rule applies again and the desk gets its remedy.
    if held < BRIDGE_WORK_CEILING and time.time() - bridge_output_at(session) < BRIDGE_QUIET_SECONDS:
        return False
    return True


def bridge_output_at(session):
    """-> epoch of the desk's last console output, or 0 if it has never spoken.

    0 means "no evidence of work", which is the safe reading: an old bridge
    that predates this stamp keeps the original 90-second behaviour rather than
    becoming unkillable.
    """
    try:
        return float((BRIDGES / f"{session}.out").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0


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


def _tree_pids(root_pid, table=None):
    """root + every recorded descendant, walked over a ppid snapshot.

    Works when the ROOT IS ALREADY DEAD: Windows keeps a child's recorded
    ppid pointing at the vanished pid, so the subtree is still enumerable -
    which is the whole point. A ConPTY child outlives the python that spawned
    it, and `taskkill /T` on a dead root kills nothing (verified 2026-08-17:
    /T does traverse a LIVE root's whole tree - the pty parent link is intact
    - so the leak was never /T, it was the kill being skipped for dead
    roots). Pure over `table` ({pid: ppid}) so the suite can feed synthetic
    trees; None snapshots the real machine via psutil."""
    if not root_pid:
        return []
    if table is None:
        try:
            import psutil
            table = {p.info["pid"]: p.info["ppid"]
                     for p in psutil.process_iter(["pid", "ppid"])}
        except Exception:                                    # noqa: BLE001
            return [int(root_pid)]     # cannot map: at least try the root
    out, frontier = [int(root_pid)], [int(root_pid)]
    while frontier:
        kids = [pid for pid, ppid in table.items()
                if ppid in frontier and pid not in out]
        out.extend(kids)
        frontier = kids
    return out


def _kill_tree(root_pid, why=""):
    """Kill a process tree WE OWN, dead root included. -> (tried, survivors).

    Enumerates the subtree itself (see _tree_pids) and kills each pid
    directly, so a dead root no longer shields its living children - the
    orphan-claude leak a second instance measured on 2026-08-17: bridges
    killed without /T (or crashed) left their ConPTY claude running, the
    presence file was unlinked anyway, and the tree became invisible to every
    surface. Survivors are REPORTED, never assumed dead."""
    tried = _tree_pids(root_pid)
    for pid in tried:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=NO_WINDOW)
    time.sleep(0.3)
    survivors = [p for p in tried if pid_alive(p)]
    if tried and (len(tried) > 1 or survivors):
        log(f"kill_tree({root_pid}{', ' + why if why else ''}): "
            f"tried {len(tried)} pid(s), {len(survivors)} survived"
            + (f" ({survivors})" if survivors else ""))
    return tried, survivors


def recover_bridge(session):
    """Replace a bridge that is not delivering. -> status token.

    Safe to do unasked because the bridge is OURS: the watchdog started it, it
    holds no work of the owner's, and its conversation survives on disk. That
    is exactly why native windows are asked about and this one is not.

    The kill is NOT gated on the bridge python being alive (that gate was the
    orphan leak): a dead python's claude children are still ours, still
    enumerable through the recorded ppids, and still holding RAM. Only after
    the tree is dealt with does the presence file go - deleting it first is
    what made every leaked tree invisible.
    """
    try:
        d = json.loads((BRIDGES / f"{session}.json").read_text(encoding="utf-8"))
        if d.get("pid"):
            _kill_tree(d["pid"], why=f"bridge {session}")
    except (OSError, json.JSONDecodeError, ValueError):
        pass
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
                                      f"after replacing its window {fails} times.{desk_fault(session)}")
        except Exception as e:
            log(f"bridge-recovery alert failed for {session}: {e}")
    return "bridge-replaced" if started else "start-failed"


def desk_fault(session):
    """Why can this desk not take its mail? -> a sentence to append to an alert.

    "Something on that desk is wedged - look at the log" is true and useless to
    somebody holding a phone. Every cause below is a fact the watchdog already
    has, and each has a different fix: a desk whose folder was never created
    will never work no matter how many windows are replaced, and a machine that
    cannot resolve the CLI has nothing to do with this desk at all.
    """
    cwd = cwd_for(session)
    if not cwd.is_dir():
        return (f" Its folder `{cwd.name}` does not exist (`{cwd}`) - the channel "
                f"is mapped to a desk nothing ever created. Either create the "
                f"project component, or remove/rename the channel.")
    if not claude_exe():
        return (" This machine cannot find the `claude` CLI at all, so NO desk "
                "can start - install Claude Code, then restart the watchdog "
                "(a running service keeps the PATH it was born with), or set "
                "`[fleet] claude_path` in `config\\omnius.ini`.")
    tail = ""
    try:
        p = LOGS / f"bridge-{session}.log"
        lines = [ln.strip() for ln in
                 p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        if lines:
            tail = "\n```\n" + "\n".join(lines[-3:])[:400] + "\n```"
    except OSError:
        pass
    return (f" `!restart` it, or look at `state\\logs\\bridge-{session}.log`.{tail}")


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


TAKEOVER_ASK_TTL = 6 * 3600      # an unanswered ask stops muting the deadman


def takeover_pending(session):
    """The open takeover question for this desk, or None.

    EXPIRES. An ask used to live forever, and deaf_desk_alarm treats a pending
    one as "we asked, silence is his answer" - so one ignored question muted
    that desk's deadman permanently. Drilled 2026-08-18: a 30-day-old ask with
    an hour-old owner message and nothing alive still paged nobody. Silence is
    a valid answer to THAT message, not to every message forever; after the TTL
    the desk is deaf like any other and the pager may speak."""
    p = TAKEOVER / f"{session}.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    try:
        if time.time() - float(d.get("askedAt") or 0) > TAKEOVER_ASK_TTL:
            p.unlink(missing_ok=True)
            log(f"{session}: takeover ask expired unanswered - the deadman speaks again")
            return None
    except (TypeError, ValueError):
        pass
    return d


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
    # An rc-0 run that left the envelope it was STARTED FOR untouched did NOT
    # do its job - count it as a failure or it respawns against the same
    # envelope forever.
    #
    # Ask whether THAT envelope is still queued, never "is it still the
    # minimum". Inbox names come from four writers - <snowflake>, dm-*,
    # heartbeat-*, sched-* - which ASCII-order digits < dm < heartbeat < sched,
    # so ordinary mail landing DURING a run could take over the minimum and
    # score an unhandled run as a success: strikes wiped, no backoff, instant
    # relaunch against the same stuck envelope. Drilled 2026-08-18: a heartbeat
    # or one desk-mail arrival was enough to defer the crash-loop alert
    # indefinitely, and for system senders that alert is the ONLY guard.
    _started_for = _run_oldest.get(session)
    undrained = bool(_started_for) and (INBOX / session / _started_for).exists()
    _run_oldest.pop(session, None)
    if rc == 0 and not undrained:
        _run_failures.pop(session, None)
        _run_alerted.discard(session)
        save_failure_ledger()
        log(f"run finished for {session}")
        return
    fails = _run_failures.get(session, 0) + 1
    _run_failures[session] = fails
    _run_backoff[session] = time.time() + RUN_BACKOFF_SECONDS
    save_failure_ledger()
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


TELEGRAM_BRIDGE = ROOT / "tools" / "telegram" / "bridge.py"
TELEGRAM_LEASE = WD_STATE / "telegram.json"
TELEGRAM_BEACON = STATE / "telegram" / "beacon.json"
TELEGRAM_LOCK = STATE / "telegram" / "lock.json"
WATCHDOG_STARTED = time.time()   # this boot's anchor; see ensure_telegram_bridge
TELEGRAM_CHECK_SECONDS = 60      # config written -> live within a minute
# Longer than the longest thing one pass may legitimately do. A voice note gives
# whisper up to 600s (bridge.transcribe), and at 300s this killed the bridge
# mid-transcription - then the same voice note came back on restart, because the
# Telegram offset had not advanced. A poison pill built from three sane numbers.
TELEGRAM_STALE_SECONDS = 900
_telegram_checked = 0.0
_telegram_spawned = set()        # pids WE started, this watchdog process only
_telegram_quiet = set()          # foreign bridges already reported once


def _read_json(path):
    """-> parsed json, or None. A torn or missing file is a normal state here."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ensure_telegram_bridge():
    """Keep tools\\telegram\\bridge.py running while somebody is invited.

    The promise config\\guests.ini already makes - write the file, it is live
    within a minute - had no equivalent for Telegram: the bridge is a separate
    PROCESS (a blocking long poll cannot live in this loop), and a separate
    process meant a scheduled task, and a scheduled task meant running a
    PowerShell command on the machine. He runs this fleet from his phone. A
    feature you can only switch on by sitting at the PC is a feature he cannot
    use, and one more thing every other owner has to be told about.

    So the lifecycle lives here, in the piece that is already always-on and
    already restarts itself. Start-only by design: if the config disappears the
    bridge idles by itself (its own rule) and stop-omnius.bat still stops
    everything under this root - killing a process because a file was deleted is
    a surprise nobody asked for.

    Duplicates are impossible regardless of who starts it: the bridge takes its
    own pid-validated lock, and a second copy stands by (beacon `state:
    "standby"`, polling nothing) rather than exiting - an exit reads as a crash
    here and would earn a fresh doomed copy every minute, so it stays alive and
    takes over the moment the first one stops.
    """
    global _telegram_checked
    now = time.time()
    if now - _telegram_checked < TELEGRAM_CHECK_SECONDS:
        return
    _telegram_checked = now
    # SOMEBODY INVITED, not merely a file on disk. install.ps1 copies every
    # config\*.example.* into its real name, so telegram.ini exists on EVERY
    # fresh install - and keying off existence started an idle bridge, and an
    # "idle, no token" problem line in -Action status, on machines whose owner
    # had never heard of Telegram (found 2026-08-19 while asking why the
    # skills example still shipped - it no longer does, for the same reason).
    if not TELEGRAM_BRIDGE.is_file() or not ocfg.telegram_chats():
        return                                  # nobody invited: nothing to run

    # THE BRIDGE'S OWN LOCK, not just our lease. A bridge started by hand, or by
    # a leftover scheduled task from an older install, holds that lock and makes
    # any child we spawn exit immediately - so supervising by our lease alone
    # meant spawning a doomed process every 60 seconds, forever, while a
    # perfectly healthy bridge ran beside it.
    lock = _read_json(TELEGRAM_LOCK) or {}
    lease = _read_json(TELEGRAM_LEASE) or {}
    # The LIVE one wins, not the lock unconditionally: a bridge idling for want
    # of a token never refreshes the lock, so a stale dead pid there outranked a
    # perfectly good lease and got a fresh copy spawned every minute.
    pid, started = None, 0.0
    for rec in (lock, lease):
        if pid_alive(rec.get("pid"), expect="python"):
            pid = rec.get("pid")
            started = float(rec.get("startedTs") or 0)
            break
    if pid:
        try:
            beacon_at = TELEGRAM_BEACON.stat().st_mtime
        except OSError:
            beacon_at = 0
        # PROOF, not assumption: only the bridge writes that beacon, so a stamp
        # newer than the lock proves the process at this pid really is our bridge
        # and not some other python that inherited a recycled pid. Without it a
        # reboot could hand pid 8412 to the daybook service and we would taskkill
        # its whole tree.
        # Both timestamps survive a reboot, so "beacon newer than lock" is true
        # of any leftover pair from last night - and the pid it names now
        # belongs to something else. Proof has to be anchored in THIS boot, and
        # the watchdog's own start is: a live bridge re-stamps within 30s.
        proven = beacon_at > max(started, WATCHDOG_STARTED) > 0
        # Measured from the START when the beacon predates it. Reading the stale
        # mtime as this process's age killed every freshly started bridge one
        # minute in - after a cold boot the beacon file is always last night's.
        age = (now - beacon_at) if proven else (now - started)
        if age < TELEGRAM_STALE_SECONDS:
            return                              # working, or still starting up
        if not (proven or pid in _telegram_spawned):
            # Old, unproven, and not ours: leave it alone and say so once.
            if pid not in _telegram_quiet:
                _telegram_quiet.add(pid)
                log(f"a telegram bridge (pid {pid}) holds the lock but has never "
                    f"stamped a beacon - leaving it alone (not started by this "
                    f"watchdog); check state\\logs\\telegram.log")
            return
        log(f"telegram bridge (pid {pid}) has been silent for {int(age)}s - restarting it")
        _kill_tree(pid, why="telegram bridge wedged")
        _telegram_spawned.discard(pid)
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        out = LOGS / "telegram.out.log"
        if out.exists() and out.stat().st_size > 2_000_000:
            out.replace(out.with_suffix(".log.1"))
        with open(out, "ab") as fh:
            proc = subprocess.Popen([sys.executable, str(TELEGRAM_BRIDGE)],
                                    cwd=str(ROOT), stdout=fh,
                                    stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    creationflags=NO_WINDOW)
    except OSError as e:
        log(f"telegram bridge failed to start: {e}")
        return
    write_json_atomic(TELEGRAM_LEASE, {"pid": proc.pid, "startedAt": now_iso(),
                                       "startedTs": now})
    _telegram_spawned.add(proc.pid)
    log(f"telegram bridge started (pid {proc.pid})")


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
    # DEAD python included - its ConPTY claude children live on (the orphan
    # leak, 2026-08-17); _tree_pids enumerates them through recorded ppids.
    try:
        bd = json.loads((BRIDGES / f"{session}.json").read_text(encoding="utf-8"))
        if bd.get("pid"):
            for _p in _tree_pids(bd["pid"]):
                if _p not in pids:
                    pids.append(_p)
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
        return fleet_channel_id(build_map(api.load_schema()), name,
                                "tool.fleet" if name == "fleet-status" else None)
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


def fire_due_schedules(mapping=None):
    """Turn due scheduled jobs into ordinary inbox envelopes.

    Deliberately reuses the normal envelope path rather than inventing a second
    delivery mechanism: a scheduled message wakes or spawns its target session
    exactly the way a Discord message does, so there is only one code path to
    keep correct. schedule.py handles the catch-up policy (missed runs are
    rescheduled, not replayed). `mapping` is only for the loop-budget notice's
    channel fallback; None keeps every delivery working."""
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
        led = None
        if job.get("loop"):
            # The fire-time belt (docs\DELEGATION.md D5). The add-time refusal
            # in schedule.py is the primary brake - this catches a hand-edited
            # job. Over budget, closed, or orphaned: the job is DROPPED and the
            # loop's channel told once; a loop never fires past its budget.
            led = schedule.load_loop(job["loop"])
            if (led is None or led.get("closed")
                    or int(led.get("fired") or 0) >= int(led.get("max") or 0)):
                kept = [j for j in kept if j.get("id") != job.get("id")]
                if led is not None and not led.get("closed"):
                    led["closed"] = "budget"
                    try:
                        schedule.save_loop(led)
                    except OSError:
                        pass
                log(f"loop {job.get('loop')}: over budget or closed - job "
                    f"{job.get('id')} dropped, nothing fired")
                cid = job.get("channelId") or (
                    primary_channel_id(mapping, session) if mapping else None)
                if cid:
                    try:
                        api.send_message(cid, f"⏸ loop `{job.get('loop')}` on "
                                              f"`{session}` used its budget - write "
                                              f"in this channel to start a new one")
                    except api.ApiError:
                        pass
                continue
        box = INBOX / session
        try:
            box.mkdir(parents=True, exist_ok=True)
            mid = f"sched-{job['id']}-{int(time.time())}"
            # channelId rides in from the job (D5): a loop that must answer a
            # human carries WHERE explicitly - scheduled mail had no channel to
            # echo before this.
            write_json_atomic(box / f"{mid}.json", {
                "id": mid, "from": "schedule", "channel": None,
                "channelId": job.get("channelId"), "category": None,
                "ts": now_iso(), "text": job.get("text", ""), "files": []})
            delivered.append(job.get("id"))   # the write succeeded: this one really landed
            if led is not None:
                led["fired"] = int(led.get("fired") or 0) + 1
                led["lastFiredAt"] = now_iso()
                try:
                    schedule.save_loop(led)
                except OSError as e:
                    log(f"loop ledger save failed ({led.get('id')}): {e}")
            transcribe(session, "in", f"[scheduled] {job.get('text', '')}")
            log(f"schedule {job['id']} -> inbox {session}"
                + (f" (loop {led['id']} run {led['fired']}/{led.get('max')})" if led else ""))
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
                   sender="owner", slash=None):
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
    if slash:
        # The watchdog validated an owner /<skill> against config\skills.ini
        # (docs\DELEGATION.md D6). Only THIS writer may stamp it: desk mail
        # never carries slash, guests never reach the gate.
        envelope["slash"] = slash
    # Atomic since desk mail (docs\DELEGATION.md): with more writers than the
    # watchdog composing envelopes, a torn read by ensure_runners is reachable.
    write_json_atomic(box / f"{msg['id']}.json", envelope)
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

    cid = fleet_channel_id(mapping, "fleet-status", "tool.fleet")
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
            if not is_human_sender(env.get("from")):
                # Loop envelopes carry a channelId since D5, but the fleet
                # waiting on the fleet stays a log line, never a notice
                # (docs\DELEGATION.md D2). The failure ledger covers a desk
                # that cannot start.
                _backlog_notified.add(key)
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


def do_reload(cid, announce=True):
    """Restart the watchdog in place so it picks up code changes. Until this
    existed, every edit to watchdog.py needed physical access to the machine:
    Python imports at startup, so a running watchdog keeps the old code
    forever. Requested 2026-07-31 ("que el sistema se pueda reiniciar solo").
    Shared by !reload and !update, which announces in its own words."""
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
    if announce:
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


def _git(*args, timeout=60):
    """Run git in the workspace root. -> (rc, combined output). Never raises,
    never a shell string - the same argv discipline start_run follows."""
    try:
        p = subprocess.run(["git", "-C", str(ROOT)] + list(args),
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:                                   # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def _update_suite():
    """The gate suite. -> (ok, last line, frozenset of failing names, ran).

    The NAMES matter as much as the verdict: the suite mixes product checks
    with instance hygiene (memory budgets, desk wiring), so the update gate
    must compare failures BEFORE and AFTER the pull and judge only the delta
    - proven live 2026-08-15, when a second instance's own untidy memory
    blocked its very first !update go and the rollback blamed the commit.
    Its own function so tests can stub the minute of reality out.

    THE FOURTH VALUE IS "DID IT RUN AT ALL". Parsing only [FAIL] lines made a
    suite that CRASHED - a bad import in the new code, a timeout, no output -
    look identical to a suite that passed: zero failing names, verdict green,
    update reloaded onto code nothing had tested. A crash is not a pass; it is
    the loudest possible failure, and the caller must roll back on it."""
    try:
        p = subprocess.run([sys.executable,
                            str(ROOT / "tools" / "discord" / "test_watchdog.py")],
                           capture_output=True, text=True, timeout=600,
                           creationflags=NO_WINDOW)
        lines = [ln for ln in (p.stdout or "").strip().splitlines() if ln.strip()]
        fails = frozenset(ln.strip()[6:].split("  ")[0].strip()
                          for ln in lines if ln.strip().startswith("[FAIL]"))
        if not lines or (p.returncode != 0 and not fails):
            # Exit code says something went wrong and no check owned up to it,
            # or the suite printed nothing at all: it did not run.
            err = "\n".join([ln for ln in (p.stderr or "").splitlines()
                             if ln.strip()][:6]) or "(no output)"
            return False, err, fails, False
        return p.returncode == 0, (lines[-1] if lines else "(no output)"), fails, True
    except subprocess.TimeoutExpired:
        return False, "the suite ran past 600s and was killed", frozenset(), False
    except Exception as e:                                   # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", frozenset(), False


def _suite_call():
    """_update_suite(), normalised. Older stubs (and any caller written before
    the crash-detecting fourth value) return a 3-tuple; treat that as "it ran"."""
    r = _update_suite()
    if len(r) == 3:
        ok, tail, fails = r
        return ok, tail, fails, True
    return r


def _update_restamp():
    """After a pull: new code can bring new hooks, permissions or template
    scaffolding - the same idempotent stamps install runs, so an updated
    instance is a whole one, not a code drop. Its own function so tests never
    stamp a real machine."""
    for tool in ("fix_hook_paths.py", "sync_permissions.py"):
        try:
            subprocess.run([sys.executable, str(ROOT / "tools" / "discord" / tool)],
                           capture_output=True, timeout=120, creationflags=NO_WINDOW)
        except Exception:                                    # noqa: BLE001
            pass


# --- !update runs OFF the main loop --------------------------------------------
# A fetch is 120s of network and the suite is up to 600s of subprocess, and both
# used to run inside the message handler - which is the main loop. For those
# minutes nothing was delivered, no outbox was flushed, and above all no beacon
# was stamped: autostart.ps1 treats a beacon older than 120s as a dead service
# and restarts it, so a long `!update go` could get itself killed halfway
# through. The work goes to one worker thread; the loop keeps ticking and keeps
# stamping, and the RELOAD is handed back to the loop rather than done from the
# thread (execv from a worker replaces the process out from under it).
_update_job = {"thread": None, "reload_cid": None, "startedAt": 0.0}


def _update_running():
    t = _update_job.get("thread")
    return bool(t is not None and t.is_alive())


def _update_reload(cid):
    """Reload after a successful update. Called from wherever handle_update ran:
    on the worker thread it is PARKED for the main loop, on the main thread
    (a desk verb, the tests) it happens right here."""
    if threading.current_thread() is _update_job.get("thread"):
        _update_job["reload_cid"] = cid
        return
    do_reload(cid, announce=False)


def start_update(text, cid):
    """Dispatch entry for !update: hand the slow half to the worker thread."""
    if _update_running():
        api.send_message(cid, "⏳ an update is already running here — one at a time, "
                              "because two rebases in the same tree is how a "
                              "half-updated instance happens. I report back when it "
                              "is done.")
        return
    _update_job["reload_cid"] = None
    _update_job["startedAt"] = time.time()
    t = threading.Thread(target=_update_thread, args=(text, cid),
                         name="omnius-update", daemon=True)
    _update_job["thread"] = t
    t.start()


def _update_thread(text, cid):
    try:
        handle_update(text, cid)
    except Exception as e:                                   # noqa: BLE001
        log(f"!update worker failed: {type(e).__name__}: {e}")
        try:
            api.send_message(cid, f"⛔ the update failed unexpectedly and nothing was "
                                  f"reloaded: {type(e).__name__}: {e}")
        except Exception:                                    # noqa: BLE001
            pass


def update_job_tick():
    """Called once per main-loop tick: pick up a finished update's reload.
    The gate ran in the thread; the process replacement happens here, on the
    loop, with no half-finished tick underneath it."""
    cid = _update_job.get("reload_cid")
    if cid and not _update_running():
        _update_job["reload_cid"] = None
        do_reload(cid, announce=False)


def handle_update(text, cid):
    """!update - fetch and preview what origin/main has; !update go - apply it.

    The whole self-update story in one verb: rebase local work onto the new
    release, run the suite, and a red suite ROLLS BACK; the reload reuses
    !reload's compile-check. Personal files never move - they are gitignored,
    which is the whole design of the update path. A zip install that never
    attached is told how to, not left confused.

    REBASE, not fast-forward (2026-08-19). Every instance but one is somebody's
    own copy, and they change things: a prompt, an allow-list, a fix they need
    today. ff-only made each of those a one-way door - that instance could
    never update again - and the owner of a public clone cannot push the change
    upstream to get out of it. Rebasing replays their commits on top of each
    release: local work survives the update instead of blocking it, and a
    genuine conflict is reported by name with the tree left exactly as it was.
    """
    go = text.strip().lower().split()[1:2] == ["go"]
    rc, _out = _git("rev-parse", "--is-inside-work-tree")
    if rc != 0:
        api.send_message(cid, "this install is not attached to GitHub - run `install.bat` "
                              "once (it attaches without touching your files), then "
                              "`!update` works from anywhere")
        return
    rc, out = _git("fetch", "origin", "main", timeout=120)
    if rc != 0:
        api.send_message(cid, f"could not reach GitHub:\n```\n{out.strip()[:300]}\n```")
        return
    _rc, head = _git("rev-parse", "--short", "HEAD")
    head = head.strip()
    _rc, head_full = _git("rev-parse", "HEAD")
    rc, behind = _git("rev-list", "--count", "HEAD..origin/main")
    behind_n = int(behind.strip()) if rc == 0 and behind.strip().isdigit() else 0
    if behind_n == 0:
        # "Nothing new on origin" is not the same as "everything is shipped",
        # and on the MAINTAINER's machine the difference is the whole point.
        # 2026-09-01: he ran !update here with four unpushed commits sitting on
        # the disk - one of them an !update fix - and got a green tick. He read
        # it as "shipped", filed the feature as still broken, and he was right
        # to: nothing had reached a single other instance. A check that can
        # only ever say "current" is a check that cannot report the one failure
        # this instance is actually capable of.
        #
        # Only for a maintainer. On a USER instance local commits are normal and
        # expected - !update rebases them on every release - so the same line
        # there would be noise about something they cannot act on anyway.
        ahead_line = ""
        try:
            import repo_access
            if repo_access.can_push()[0]:
                _rc, ahead = _git("rev-list", "--count", "origin/main..HEAD")
                ahead_n = int(ahead.strip()) if _rc == 0 and ahead.strip().isdigit() else 0
                if ahead_n:
                    _rc, lg = _git("log", "--oneline", "origin/main..HEAD", "-n", "8")
                    more = "" if ahead_n <= 8 else f"\n… and {ahead_n - 8} more"
                    ahead_line = (f"\n\n⚠ **but {ahead_n} commit(s) here have never been "
                                  f"pushed** - they exist on this machine only, and no "
                                  f"other instance can update to them:\n```\n"
                                  f"{lg.strip()}{more}\n```\n"
                                  f"`/release` cuts and ships them when you are ready.")
        except Exception:  # noqa: BLE001 - a check must never break the check
            pass
        api.send_message(cid, f"✅ already current at `{head}` - nothing new on "
                              f"origin/main{ahead_line}")
        return
    if not go:
        _rc, lg = _git("log", "--oneline", "HEAD..origin/main", "-n", "8")
        more = "" if behind_n <= 8 else f"\n… and {behind_n - 8} more"
        api.send_message(cid, f"⬆ **{behind_n} commit(s) behind** origin/main:\n```\n"
                              f"{lg.strip()}{more}\n```\n`!update go` applies them "
                              f"(rebase → test suite → reload). Anything you changed here "
                              f"is replayed on top, and your own files never move — "
                              f"everything personal is gitignored.")
        return
    # THE UPDATER UPDATES ITSELF FIRST.
    #
    # Everything below this line is update logic living inside the thing being
    # updated - so a mistake in it strands every instance at once, and the fix
    # cannot reach machines their owner only talks to through Discord. That
    # happened twice on 2026-08-19. Checking out just update.ps1 from origin and
    # running THAT means the logic doing the work is always the newest published
    # version, never the one this process was born with. A bug here now costs
    # one bad update instead of every future one.
    #
    # The in-process path below stays as the fallback for an instance whose
    # update.ps1 is missing (installed before it existed) - which is exactly
    # the case that most needs a fallback.
    updater = ROOT / "update.ps1"
    _dirty, _ = _git("diff", "--quiet", "--", "update.ps1")
    if _dirty != 0 and updater.is_file():
        # A locally edited updater is his work, not ours to clobber: run what
        # he has, and say so, instead of silently replacing it (sweep 2026-09-02).
        api.send_message(cid, "ℹ `update.ps1` has local edits — running your version, "
                              "not the fetched one. Commit or discard them to get "
                              "the newest updater logic next time.")
        _rc = 0
    else:
        _rc, _ = _git("checkout", "origin/main", "--", "update.ps1")
    if _rc == 0 and updater.is_file():
        api.send_message(cid, f"⬆ updating `{head}` → origin/main via `update.ps1` "
                              f"(fetched fresh, so the newest logic runs) …")
        try:
            p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                "-File", str(updater), "-Path", str(ROOT), "-NoRestart"],
                               capture_output=True, text=True, timeout=900,
                               creationflags=NO_WINDOW)
            tail = "\n".join([ln for ln in (p.stdout or "").splitlines() if ln.strip()][-12:])
            if p.returncode != 0:
                api.send_message(cid, f"⛔ update stopped — nothing was reloaded:\n"
                                      f"```\n{tail[-1200:]}\n```")
                return
            _rc, new_full2 = _git("rev-parse", "HEAD")
            _rc, new2 = _git("rev-parse", "--short", "HEAD")
            api.send_message(cid, f"```\n{tail[-1200:]}\n```")
            write_json_atomic(_pending_path(), {
                "fromCommit": head_full.strip(), "toCommit": new_full2.strip(),
                "channelId": cid, "startedAt": now_iso(), "startedTs": time.time(),
                "bootAttempts": 0})
            log(f"!update: {head} -> {new2.strip()} via update.ps1 - reloading")
            _update_reload(cid)
            return
        except Exception as e:                                   # noqa: BLE001
            log(f"update.ps1 failed ({type(e).__name__}: {e}) - falling back to the "
                f"in-process path")
    # BASELINE first: this instance's failures BEFORE the pull are its own
    # housekeeping, not the update's fault. The gate judges only the delta.
    base_ok, base_tail, base_fails, base_ran = _suite_call()
    if not base_ran:
        # No baseline means no gate: every later verdict would be measured
        # against nothing. Stop BEFORE the pull - the tree is untouched.
        api.send_message(cid, f"⛔ update stopped: the test suite could not run on the "
                              f"CURRENT code, so there is nothing to judge the update "
                              f"against. Nothing was pulled.\n```\n{base_tail[:600]}\n```\n"
                              f"Run `python tools\\discord\\test_watchdog.py` at the desk "
                              f"to see why.")
        return
    if base_fails:
        api.send_message(cid, f"ℹ {len(base_fails)} check(s) already failing on the "
                              f"CURRENT code - noted as this machine's baseline, not "
                              f"held against the update. Tidy at the desk when "
                              f"convenient: {', '.join(sorted(base_fails)[:3])}"
                              + (" …" if len(base_fails) > 3 else ""))
    # REBASE, not fast-forward. Every instance except the one that owns the
    # remote is somebody's working copy: they change a prompt, widen an
    # allow-list, fix something for themselves - and ff-only turned each of
    # those into "your instance can never update again", which is the opposite
    # of what a personal tool should do. Rebasing replays their work on top of
    # each release, so local changes SURVIVE updates instead of blocking them.
    #
    # --autostash carries uncommitted work through the same way; git restores
    # it on abort, so the failure path leaves the tree exactly as it was.
    _rc, stash_before = _git("rev-parse", "-q", "--verify", "refs/stash")
    rc, out = _git("-c", "rebase.autoStash=true", "pull", "--rebase",
                   "origin", "main", timeout=300)
    # rc IS NOT ENOUGH. When the rebase itself succeeds but restoring the
    # autostash conflicts, git exits 0 and leaves the tree with merge markers in
    # it - proven in a scratch repo before this shipped. Reloading on that would
    # be worse than any refusal, so the tree is asked directly.
    _rc, unmerged = _git("diff", "--name-only", "--diff-filter=U")
    files = [f for f in unmerged.splitlines() if f.strip()][:8]
    if rc != 0 or files:
        # A real conflict: their edit and an upstream edit touch the same lines,
        # and nothing but a human can say which wins. Put the instance back
        # exactly as it was, keeping both versions.
        _git("rebase", "--abort")               # no-op once the rebase finished
        _git("checkout", "--", ".")             # drop half-merged content
        _git("reset", "--hard", head_full.strip() or "HEAD")
        _rc, stash_after = _git("rev-parse", "-q", "--verify", "refs/stash")
        kept = ""
        if stash_after.strip() and stash_after.strip() != stash_before.strip():
            # OUR autostash, identified by ref rather than assumed - popping
            # blind would restore some stash of theirs from last week.
            pop_rc, pop_out = _git("stash", "pop")
            if pop_rc != 0:
                # "Nothing was lost" has to be TRUE. The work is still in the
                # stash; say so, and say how to get it back, instead of a
                # reassurance the tree does not support.
                kept = (f"\n⚠ your uncommitted work could not be put back "
                        f"automatically — it is **safe in the stash** "
                        f"`{stash_after.strip()[:12]}`. At the desk: "
                        f"`git stash list`, then `git stash pop`.\n"
                        f"```\n{pop_out.strip()[:300]}\n```")
        listing = ("```\n" + "\n".join(files) + "\n```\n") if files else ""
        api.send_message(cid,
            f"⛔ update stopped: your local changes and the new release both edit the "
            f"same lines.\n{listing}{kept}"
            + (f"Nothing was lost — the instance is exactly as it was, still on `{head}`, "
               f"with your version of those files intact.\n" if not kept else
               f"The instance is still on `{head}`.\n")
            +
            f"Both sides changed the same lines, so somebody has to choose — and you "
            f"can do it from here, no shell needed. Say **take the new version of "
            f"`<file>`** (or *of all of them*) and the desk runs it, then `!update go`. "
            f"If your change was deliberate, say **fold my change into the new one** "
            f"instead. At a terminal it is `git checkout -- <file>`.\n"
            f"```\n{out.strip()[:300]}\n```")
        return
    _rc, new = _git("rev-parse", "--short", "HEAD")
    new = new.strip()
    _rc, new_full = _git("rev-parse", "HEAD")
    # STAMP BEFORE JUDGING. New code can raise the bar the machine is measured
    # against - a wider allow-list, a new hook - and the stamps that meet it are
    # the same idempotent ones install runs. Judged first, every such release
    # looked like "the update BROKE a check" on any machine with local desks
    # (projects\ is gitignored, so its desks never travel) and rolled itself
    # back. 2026-08-19: the owner's second machine could not take an update at
    # all, and the failing check was literally "every desk carries the full
    # shared allow-list" - a thing the next line fixes.
    _update_restamp()
    ok, tail, post_fails, post_ran = _suite_call()
    new_fails = sorted(post_fails - base_fails)
    if new_fails or not post_ran:
        # THE RESET IS A DEMOLITION. --autostash has already replayed their
        # uncommitted edits onto the new code, so `reset --hard` here would
        # delete work that was on the disk before the update started. Park it
        # first, put it back after - and if putting it back fails, it stays in
        # the stash and he is told where, never dropped silently.
        _rc, rb_before = _git("rev-parse", "-q", "--verify", "refs/stash")
        _git("stash", "push", "-u", "-m", "omnius-update-rollback")
        _rc, rb_after = _git("rev-parse", "-q", "--verify", "refs/stash")
        parked = bool(rb_after.strip() and rb_after.strip() != rb_before.strip())
        _git("reset", "--hard", head)
        kept = ""
        if parked:
            pop_rc, pop_out = _git("stash", "pop")
            if pop_rc != 0:
                kept = (f"\n⚠ your uncommitted work is **safe in the stash** "
                        f"`{rb_after.strip()[:12]}` (`omnius-update-rollback`) — it "
                        f"could not be re-applied automatically. At the desk: "
                        f"`git stash list`, then `git stash pop`.\n"
                        f"```\n{pop_out.strip()[:300]}\n```")
    if not post_ran and not new_fails:
        _update_restamp()
        log(f"!update: the suite did not run after {head} -> {new} - rolled back")
        api.send_message(cid, f"⛔ pulled `{head}` → `{new}` and **the test suite could "
                              f"not run** on the new code — that is not a pass, so the "
                              f"update was **rolled back** to `{head}`.\n```\n"
                              f"{str(tail)[:600]}\n```{kept}\n"
                              f"Nothing was reloaded. Report this - a released commit "
                              f"should never do it.")
        return
    if new_fails:
        # Restamp on the RESTORED code too: the stamps above were written by the
        # version we just threw away, and a machine left half-stamped by a
        # rejected update is exactly the drift this whole path exists to avoid.
        _update_restamp()
        log(f"!update: {len(new_fails)} NEW failure(s) after {head} -> {new} - rolled back")
        api.send_message(cid, f"⛔ pulled `{head}` → `{new}` and the update BROKE "
                              f"{len(new_fails)} check(s) that were green before - "
                              f"**rolled back** to `{head}`.\n```\n"
                              + "\n".join(new_fails[:5])
                              + ("\n…" if len(new_fails) > 5 else "") + "\n```" + kept
                              + "\nNothing was reloaded; investigate at the desk.")
        return
    # (already stamped above, before the suite - kept idempotent, not repeated)
    # O2: the handoff. The new watchdog must confirm it took over (healthy
    # beacon + a Discord exchange) or boot-counting reverts to head_full on
    # its own. Written BEFORE the re-exec, or a crash in the gap would leave
    # no handshake at all.
    write_json_atomic(_pending_path(), {
        "fromCommit": head_full.strip(), "toCommit": new_full.strip(),
        "channelId": cid, "startedAt": now_iso(), "startedTs": time.time(),
        "bootAttempts": 0})
    verdict = ("suite green" if not post_fails else
               f"no NEW failures ({len(post_fails)} pre-existing local one(s) ride along)")
    log(f"!update: {head} -> {new}, {verdict} - reloading")
    api.send_message(cid, f"✅ updated `{head}` → `{new}` - {verdict} (`{tail}`). "
                          f"Reloading now; the new watchdog reports back when it "
                          f"is up - or reverts itself if it cannot get healthy.")
    _update_reload(cid)


# --- the update handshake (docs\OBSERVABILITY.md O2) ----------------------------
# !update validates BEFORE the reload; this is the half that validates AFTER.
# The handoff is a file, the proof is a healthy tick, and the fallback needs
# no human: the old commit takes back over on its own.

UPDATE_BOOT_ATTEMPTS_MAX = 3     # a third still-unhealthy boot means crash-loop
UPDATE_HEALTH_SECONDS = 600      # pending older than this at boot = it sat deaf


def _pending_path():
    # Derived at call time so the test sandbox's WD_STATE redirect covers it.
    return WD_STATE / "update-pending.json"


def _load_pending():
    try:
        d = json.loads(_pending_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _reexec_self():
    """Replace this process with a fresh watchdog, through the supervisor when
    it started us (the do_reload lesson). Its own function because tests must
    stub the one line that would replace the test runner's process image."""
    here = Path(__file__).resolve()
    runner = os.environ.get("OMNIUS_SERVICE_RUNNER") or ""
    argv = [sys.executable]
    if runner and Path(runner).is_file():
        argv.append(runner)
    argv += [str(here)] + sys.argv[1:]
    os.execv(sys.executable, argv)


def _update_revert(rec):
    """The new code never proved it took over, so the old commit takes back
    over - no human in the loop, because the human may be asleep and the
    fleet deaf. fromCommit was running minutes earlier; a revert that itself
    cannot boot is out of scope by design (the supervisor's restart loop and
    channel silence remain the last signal)."""
    frm = str(rec.get("fromCommit") or "")
    if not frm:
        _pending_path().unlink(missing_ok=True)
        return
    log(f"update handshake: {str(rec.get('toCommit') or '?')[:7]} never came up "
        f"healthy (boot {rec.get('bootAttempts')}, started {rec.get('startedAt')})"
        f" - reverting to {frm[:7]}")
    rc, out = _git("reset", "--hard", frm, timeout=120)
    if rc != 0:
        # Cannot revert (git broken?). Do not loop on it: stop the counting,
        # keep booting the new code - a running-but-suspect watchdog beats
        # none, and the first healthy tick tells the owner exactly this.
        log(f"update revert FAILED: {out.strip()[:200]}")
        write_json_atomic(_pending_path(), dict(rec, revertFailed=True))
        return
    _update_restamp()
    # The reverted record replaces the counter: boots stop counting, and the
    # OLD code's first healthy tick breaks the bad news.
    write_json_atomic(_pending_path(), {
        "reverted": True, "fromCommit": rec.get("fromCommit"),
        "toCommit": rec.get("toCommit"), "channelId": rec.get("channelId"),
        "revertedAt": now_iso()})
    release_lock()   # same as do_reload: the replacement must not exit(3) on our lock
    _reexec_self()


def update_pending_boot():
    """Boot half of the handshake: count this boot against the pending update;
    a crash-looping or deaf-aged one reverts before this process does anything
    else. A boot with no pending file does none of this."""
    rec = _load_pending()
    if rec is None or rec.get("reverted") or rec.get("revertFailed"):
        return
    rec["bootAttempts"] = int(rec.get("bootAttempts") or 0) + 1
    try:
        write_json_atomic(_pending_path(), rec)
    except OSError:
        return
    age = time.time() - float(rec.get("startedTs") or time.time())
    if rec["bootAttempts"] >= UPDATE_BOOT_ATTEMPTS_MAX or age > UPDATE_HEALTH_SECONDS:
        _update_revert(rec)


def update_pending_confirm():
    """Healthy-tick half: the beacon just stamped and Discord is answering, so
    the running code has PROVEN it took over. Say so once and clear the file -
    or, after a revert, the old code breaks the bad news."""
    rec = _load_pending()
    if rec is None:
        return
    frm = str(rec.get("fromCommit") or "")[:7]
    to = str(rec.get("toCommit") or "")[:7]
    cid = rec.get("channelId")
    if rec.get("revertFailed"):
        text = (f"⛔ update `{to}` never came up healthy AND the automatic revert "
                f"failed - running `{to}` anyway. Check `state\\logs\\watchdog.log` "
                f"at the desk.")
    elif rec.get("reverted"):
        text = (f"⛔ update `{to}` did not come up healthy - **reverted** to `{frm}` "
                f"on its own. The commits are still on origin; investigate at the "
                f"desk, then `!update` again.")
    else:
        text = (f"✅ update live: `{frm}` → `{to}` - the new watchdog took over "
                f"and is healthy.")
    if cid:
        try:
            api.send_message(cid, text)
        except api.ApiError as e:
            log(f"update handshake post failed ({e}) - retrying next healthy tick")
            return                       # keep the file; a healthy tick will recur
    state = "reverted" if (rec.get("reverted") or rec.get("revertFailed")) else "healthy"
    log(f"update handshake: {state} ({frm} -> {to})")
    _pending_path().unlink(missing_ok=True)


RELEASE_CHECK_SECONDS = 24 * 3600   # poll-loop re-check cadence (boot checks too)
_release_last = [0.0]               # when update_boot_notice last ran, this process


def update_boot_notice(mapping):
    """Release check (owner ask, 2026-08-16): if origin/main has commits this
    install lacks, post WHAT they are and that `!update go` applies them -
    then stay quiet. It never applies anything by itself, and it never speaks
    when current, unattached or offline: a dead network is a log line, not a
    page. Runs at boot and once a day from the poll loop (a watchdog can run
    for weeks without a boot). "Only tell once" is enforced per ORIGIN TIP,
    stamped in state - a crash-looping service boots every minute, and the
    same news must not be broken every time."""
    _release_last[0] = time.time()
    rc, _out = _git("rev-parse", "--is-inside-work-tree")
    if rc != 0:
        return                # unattached zip install - !update explains when asked
    rc, out = _git("fetch", "origin", "main", timeout=20)
    if rc != 0:
        log(f"boot release check: fetch failed ({out.strip()[:120] or 'no output'}) - skipped")
        return
    rc, behind = _git("rev-list", "--count", "HEAD..origin/main")
    behind_n = int(behind.strip()) if rc == 0 and behind.strip().isdigit() else 0
    if behind_n == 0:
        return
    _rc, tip = _git("rev-parse", "origin/main")
    tip = tip.strip()
    stamp = WD_STATE / "update-announced.json"
    try:
        if json.loads(stamp.read_text(encoding="utf-8")).get("tip") == tip:
            return            # an earlier boot already broke exactly this news
    except (OSError, ValueError, AttributeError):
        pass
    cid = primary_channel_id(mapping, "orchestrator")
    if not cid:
        log(f"boot release check: {behind_n} commit(s) behind but no orchestrator "
            f"channel - log only")
        return
    _rc, lg = _git("log", "--oneline", "HEAD..origin/main", "-n", "8")
    more = "" if behind_n <= 8 else f"\n… and {behind_n - 8} more"
    try:
        api.send_message(cid, f"⬆ **{behind_n} new commit(s)** on origin/main since this "
                              f"machine last updated:\n```\n{lg.strip()}{more}\n```\n"
                              f"`!update go` applies them (ff-only pull → test suite → "
                              f"reload, auto-rollback on a red suite). Nothing is "
                              f"applied without that word.")
    except api.ApiError as e:
        log(f"boot release check: could not post ({e})")
        return                # unstamped on purpose - the next boot may reach Discord
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(json.dumps({"tip": tip, "behind": behind_n,
                                     "at": int(time.time())}), encoding="utf-8")
    except OSError as e:
        log(f"boot release check: stamp failed ({e})")
    log(f"boot release check: {behind_n} commit(s) behind origin/main - announced")


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
                loops = schedule.list_loops()
                if not jobs and not loops:
                    api.send_message(cid, "no routines yet. Ask in #omnius, e.g. "
                                     "*\"check my gmail every hour on weekdays "
                                     "during work hours\"*.")
                else:
                    me = api.MACHINE
                    body = "\n".join(schedule.describe(j, me) for j in
                                     sorted(jobs, key=lambda x: x.get("nextRun") or ""))
                    # A fenced block, never a markdown table - Discord renders
                    # no tables, and this is columnar (/omnius §4, 2026-08-06).
                    out = (f"⏱ **{len(jobs)} routine(s)**\n```\n{body}\n```"
                           if jobs else "⏱ no routines")
                    if loops:
                        lbody = "\n".join(schedule.describe_loop(led) for led in loops)
                        out += f"\n🔁 **{len(loops)} work loop(s)**\n```\n{lbody}\n```"
                    api.send_message(cid, out)
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
    elif cmd == "!update":
        start_update(text, cid)
    elif cmd == "!trace":
        handle_trace(text, cid)
    elif cmd == "!reload":
        do_reload(cid)
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
        # By identity, not by name: every project #general also maps to the
        # orchestrator, and the owner may have renamed his own channel.
        home = primary_channel_id(mapping, "orchestrator")
        # No map to judge by (or no orchestrator channel in it): fall back to
        # what the target says. Refusing on a lookup failure would take the
        # fleet stop away exactly when things are already going wrong.
        ok = (cid == home) if home else (target.session == "orchestrator")
        if not ok:
            where = f"#{mapping[home].channel_name}" if home in mapping else f"#{agent_slug()}"
            api.send_message(cid, f"!killall only works in {where}")
            return
        results = [kill_session(f.stem) for f in sorted(SESSIONS.glob("*.json"))]
        api.send_message(cid, "**fleet stop**\n" + ("\n".join(results) or "nothing was running"))
    else:
        # Only reachable if CONTROL_COMMANDS and this chain drift apart: the
        # dispatch admits exact listed verbs alone, so an unmatched cmd means a
        # verb was added to the tuple without a branch. Never swallow his
        # message silently - that is the one failure he cannot see.
        api.send_message(cid, f"`{cmd}` is listed as a control verb but nothing handles it - "
                              "that is a wiring bug; the message was not delivered anywhere")
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
    # Resolved by id, so a renamed #alerts is still the flag channel.
    broadcast = {fleet_channel_id(mapping, n, "tool.fleet" if n == "fleet-status" else None)
                 for n in BROADCAST_CHANNELS}
    broadcast.discard(None)

    def allowed(k, t):
        return t.session == session or k in broadcast

    cid = data.get("channelId")
    if cid and cid in mapping:               # unknown id: fall through to name/primary
        return cid if allowed(cid, mapping[cid]) else REFUSED

    name = data.get("channel")
    if name:
        matches = [k for k, t in mapping.items() if t.channel_name == name]
        mine = [k for k in matches if allowed(k, mapping[k])]
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
        return fleet_channel_id(mapping, name)
    return None


def outbox_files(data):
    """The `files` of an outbox entry, as a list, whatever the desk wrote.

    A bare string instead of a list is the single most common malformed reply
    (2026-08-18, one missing bracket), and iterating it hands Path() one
    CHARACTER at a time. Normalising here means a desk that writes
    `"files": "C:\\x.mp3"` still gets its file sent instead of nothing.
    """
    f = data.get("files")
    if isinstance(f, str):
        return [f]
    return [p for p in (f or []) if isinstance(p, str)]


def post_reply(channel_id, data):
    """Post one outbox entry - and send audio as a native VOICE NOTE.

    His ask, 2026-09-01: *"puedes responder con audio en vez de adjuntarlo?"*
    An `.mp3` attachment is a download; a voice note plays in the chat with its
    waveform, which is the accessible shape for him. So any audio attachment
    now leaves as its own voice note, and the text travels as its own message -
    Discord allows a voice note NO content and exactly one attachment
    (tools\\discord\\voice.py). Non-audio files still ride with the text.

    `"voice": false` in the outbox entry opts out and attaches the audio the
    old way - for the desk that means the file itself, not the sound of it.

    **Every failure falls back to a plain attachment.** The docs do not promise
    bots may send voice notes, ffmpeg can refuse a file, and none of that is
    worth an audio reply that never arrives - a download beats silence.
    """
    files = outbox_files(data)
    text = data.get("text", "") or ""
    as_voice, plain = [], []
    for p in files:
        (as_voice if data.get("voice", True) and voice.is_audio(p) else plain).append(p)
    if text.strip() or plain or not as_voice:
        # The `not as_voice` arm keeps an empty reply behaving exactly as before
        # (Discord refuses it, the error is logged); only a voice-note-only
        # reply is allowed to skip the text message.
        api.send_message(channel_id, text, files=plain)
    for p in as_voice:
        try:
            note = voice.prepare(p)
            api.send_voice_message(channel_id, note["path"],
                                   note["duration_secs"], note["waveform"])
        except Exception as e:
            # BROAD ON PURPOSE, and the reason is a real outage: on 2026-09-02
            # ffmpeg was blocked by Windows App Control (WinError 4551, a bare
            # OSError), the narrow `except (VoiceError, ApiError)` did not hold
            # it, and three replies died as `.bad` - the owner got neither the
            # audio nor the text that came with it. A prettier reply is never
            # worth a lost one: whatever goes wrong on the voice-note path, the
            # file still goes out as an attachment.
            log(f"voice note refused for {Path(p).name} ({e!r}) - attaching it instead")
            api.send_message(channel_id, "", files=[p])


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
            if data.get("to"):
                # Desk mail (docs\DELEGATION.md): addressed to a DESK, not a
                # channel - routed, never posted. Runs before channel
                # resolution so a `to` file can never fall through to Discord.
                deliver_desk_mail(mapping, session, f, data)
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
                post_reply(cid, data)
                for p in outbox_files(data):  # sent copies -> durable archive
                    src = Path(p)
                    if src.exists() and MEDIA not in src.parents:
                        dest = MEDIA / "sent" / datetime.now().strftime("%Y-%m") / src.name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        if not dest.exists():
                            shutil.copy2(src, dest)
                transcribe(session, "out", data.get("text", ""),
                           channel=mapping[cid].channel_name if cid in mapping else None,
                           channel_id=cid, files=outbox_files(data))
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
            except Exception as e:                               # noqa: BLE001
                # A MALFORMED REPLY MUST NOT STOP THE BUS. Every outbox file is
                # JSON a language model wrote against a schema nothing
                # validates, and only ApiError was caught here: `"files"` as a
                # bare string (one missing bracket) reaches Path().read_bytes()
                # and raises FileNotFoundError, which escaped this loop and
                # took the whole tick with it - flush, backlogs, reap and
                # ensure_runners all skipped, every 3 seconds, forever, while
                # the file was retried unchanged. Drilled 2026-08-18.
                #
                # Quarantine it like unreadable JSON: the desk keeps working,
                # the evidence survives as .bad, and the owner is told once.
                log(f"outbox {session}/{f.name}: BAD REPLY ({type(e).__name__}: {e}) "
                    f"- renamed .bad, the desk keeps running")
                try:
                    f.rename(f.with_suffix(".bad"))
                except OSError:
                    f.unlink(missing_ok=True)
                try:
                    cid2 = primary_channel_id(mapping, session)
                    if cid2:
                        api.send_message(cid2, f"⚠️ `{session}` wrote a reply I could not "
                                               f"post (`{type(e).__name__}`) — kept as "
                                               f"`{f.name}.bad`. Its next reply is unaffected.")
                except Exception:                                # noqa: BLE001
                    pass


# --- desk mail (delegation) -----------------------------------------------------
# docs\DELEGATION.md D1-D4. A desk delegates by writing an outbox file with a
# `to` field; THIS side routes it. Senders never write foreign inboxes: one
# gate validates every target (the phantom-desk class closes at its last entry
# point), hops and cross-project policy live in one place, and the visible
# copy the doctrine promises (ARCHITECTURE par.3.4: "delegation is always
# watchable") can only be posted by the one process allowed to speak in every
# channel - this one.

THREAD_IDLE_SECONDS = 48 * 3600   # open chains idle this long are swept (breadcrumb logged)
# A CLOSED chain is the post-mortem, so it outlives the incident. This was 600s
# until 2026-08-18: a chain that hit its hop limit or was gate-denied at 03:00
# had already deleted itself by the time he read the alert at 08:00, and
# `!trace <id>` - the verb whose whole job is explaining that - answered
# "nothing called that". Evidence for a failure must outlive a night's sleep.
CLOSED_THREAD_KEEP_SECONDS = 72 * 3600
GATE_WAIT_SECONDS = 3600          # an unanswered cross-project ask fails CLOSED after this
_BOOT_TS = time.time()            # gate asks re-post once per watchdog boot, never per tick
_desk_id_cache = {}               # id -> (verdict, checked_at); envelope scans run per tick
DESK_ID_CACHE_SECONDS = 30.0


def _hop_ttl():
    """Forward hops a chain may spend (replies are free). Config, default 3."""
    return max(1, ocfg.get_int(OMNIUS_CFG, "delegation", "hop_ttl",
                               "OMNIUS_HOP_TTL", 3, env=api.ENV))


def _gate_required():
    """Is cross-project desk mail held for an ok? Default ON - fail closed."""
    return ocfg.get_bool(OMNIUS_CFG, "delegation", "cross_project_requires_ok",
                         "OMNIUS_CROSS_PROJECT_OK", True, env=api.ENV)


def legal_desk_shape(s):
    """The id grammar, mirroring inbox_watch.py's own table: orchestrator |
    daybook | tool.<name> | <project>.<component>. Kebab-case, one dot."""
    if s in ("orchestrator", "daybook"):
        return True
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*", s))


def is_desk_id(who):
    """A legal id whose folder EXISTS. The registry is the filesystem - the
    same check routing uses - cached briefly because the per-tick envelope
    scans consult this for every queued envelope."""
    now = time.time()
    hit = _desk_id_cache.get(who)
    if hit and now - hit[1] < DESK_ID_CACHE_SECONDS:
        return hit[0]
    ok = legal_desk_shape(who) and cwd_for(who).is_dir()
    _desk_id_cache[who] = (ok, now)
    return ok


def is_fleet_sender(who):
    """The fleet talking to itself: the three system tags, tool job handoffs
    (`*-job` - transcribe-job predates this and used to count as a person, a
    latent window-popper), and any real desk id (docs\\DELEGATION.md D2)."""
    w = str(who or "").strip().lower()
    if not w:
        return False
    if w in SYSTEM_SENDERS or w.endswith("-job"):
        return True
    return is_desk_id(w)


def free_pair(sender, to):
    """Desk mail that skips the gate: the orchestrator delegating downward (its
    whole job, and the pre-desk-mail hand path was never gated), or two desks
    of the SAME project. Everything else - project<->project, anything->
    orchestrator, tool desks, daybook - holds for an ok. Ambiguity fails
    closed."""
    if sender == "orchestrator":
        return True
    sp, sdot, _ = sender.partition(".")
    tp, tdot, _ = to.partition(".")
    return bool(sdot and tdot and sp == tp and sp != "tool")


# --- the thread ledger: one file per chain, the AUTHORITATIVE hop count ---------
# (an envelope's `hops` field is informational; a desk cannot refill a budget
# it merely echoes)

def _thread_path(tid):
    return THREADS / f"{tid}.json"


def _load_thread(tid):
    if not tid:
        return None
    try:
        d = json.loads(_thread_path(tid).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("id") else None
    except (OSError, ValueError):
        return None


def _save_thread(led):
    led["lastAt"] = now_iso()
    THREADS.mkdir(parents=True, exist_ok=True)
    write_json_atomic(_thread_path(led["id"]), led)


def _storm_cap(led):
    """How many messages one chain may carry before it is called a storm.

    Scaled by the desks actually involved, because breadth is legitimate: a
    four-desk project doing ONE honest round - a brief out to three siblings and
    three answers home - is eight messages, and the old flat cap of twelve
    closed such chains in the middle of real work. Depth is what the hop budget
    bounds; this bounds volume, and volume grows with participants.
    """
    people = {d for edge in (led.get("edges") or []) for d in edge}
    return max(_hop_ttl() * 4, 6 * max(2, len(people)))


def _new_thread(sender, origin, stem):
    led = {"id": f"t-{stem}-{sender}", "origin": origin, "hopsLeft": _hop_ttl(),
           # depth: how far each desk sits from the one that started the chain.
           # The starter is 0; the budget bounds the deepest, not the busiest.
           "depth": {sender: 0},
           "deliveries": [], "edges": [], "lastDeliveredTo": None,
           "startedAt": now_iso(), "lastAt": now_iso(), "closed": None}
    _save_thread(led)
    return led


def _close_thread(led, reason):
    led["closed"] = reason
    _save_thread(led)


def _delivery_ids(led):
    """Delivery ids from a ledger whose entries may be bare strings (pre-trace)
    or the enriched {id, from, to, ts, reply} dicts (OBSERVABILITY O1,
    2026-08-15). Mid-flight ledgers carry both; readers must accept both."""
    out = set()
    for d in (led.get("deliveries") or []):
        out.add(d.get("id") if isinstance(d, dict) else d)
    return out


def _clean_origin(sender, origin):
    """Keep only what the spec names. The chain STARTER supplies origin (it
    just drained that human envelope); later hops echo `thread` instead."""
    if not isinstance(origin, dict):
        return None
    out = {"channelId": str(origin.get("channelId") or "") or None,
           "from": str(origin.get("from") or "") or None,
           "session": sender}
    return out if (out["channelId"] or out["from"]) else None


def _infer_thread(sender):
    """The chain that last delivered TO this sender - glues a threadless reply
    to its chain. Newest open ledger wins; a wrong guess is bounded by the hop
    and storm caps, so inference is a convenience, not a trust decision."""
    best = None
    try:
        for f in THREADS.glob("*.json"):
            led = _load_thread(f.stem)
            if not led or led.get("closed") or led.get("lastDeliveredTo") != sender:
                continue
            if best is None or str(led.get("lastAt") or "") > str(best.get("lastAt") or ""):
                best = led
    except OSError:
        pass
    return best


def _thread_notice(mapping, led, sender, text):
    """One line where the humans are: the chain's origin channel, else the
    sender's own. The watchdog's voice, never an envelope - a notice cannot
    wake a run, so notices cannot loop."""
    cid = (led.get("origin") or {}).get("channelId") if led else None
    if not (cid and cid in mapping):
        cid = primary_channel_id(mapping, sender)
    if not cid:
        log(f"desk mail notice (no channel): {text}")
        return
    try:
        api.send_message(cid, text)
    except api.ApiError as e:
        log(f"desk mail notice failed: {e}")


def _post_desk_mail_copy(mapping, sender, to, env):
    """The visible copy - the transport keeps ARCHITECTURE par.3.4's promise.
    Posted in the RECIPIENT's channel, best-effort and AFTER delivery: Discord
    being down delays visibility, never delivery. ~200 chars of redacted
    preview; whole briefs belong in transcripts, not chat (the no-narration
    rule is about exactly that noise)."""
    preview = api.redact(str(env.get("text") or ""))[:200]
    head = (f"📨 `[desk mail]` `{sender}` → `{to}` · `{env.get('thread')}` · "
            f"{env.get('hops')} hop(s) left")
    cid = primary_channel_id(mapping, to) or primary_channel_id(mapping, sender)
    if not cid:
        log(f"desk mail copy (no channel): {sender} -> {to}")
        return
    try:
        api.send_message(cid, f"{head}\n> {preview}")
    except api.ApiError as e:
        log(f"desk mail copy failed: {e}")


def _refuse_desk_mail(mapping, sender, path, why):
    """Uniform refusal: rename `.refused` (the outbox idiom - evidence, never
    deletion), log, one watchdog-voice line in the sender's channel. No run is
    woken for a refusal."""
    log(f"desk mail {sender}/{path.name}: refused - {why}")
    try:
        path.rename(path.with_suffix(".refused"))
    except OSError:
        path.unlink(missing_ok=True)
    cid = primary_channel_id(mapping, sender)
    if cid:
        try:
            api.send_message(cid, f"✗ could not deliver desk mail from `{sender}` - {why}")
        except api.ApiError:
            pass
    return "refused"


def deliver_desk_mail(mapping, sender, path, data, gate_approved=False):
    """Route one desk-addressed outbox file (docs\\DELEGATION.md D1).

    -> delivered | refused | held | duplicate (status tokens, also for tests).
    The step order is the crash-safety story: the inbox write lands before the
    outbox file is unlinked, and the DETERMINISTIC envelope id makes the
    in-between window redeliver-safe instead of duplicate-prone."""
    to = str(data.get("to") or "").strip().lower()
    text = str(data.get("text") or "")

    # 1. Grammar + reserved names. Refused by name for a crisp message; they
    #    would fail the folder check anyway.
    if not legal_desk_shape(to) or to in ocfg.RESERVED_SENDERS:
        return _refuse_desk_mail(
            mapping, sender, path,
            f"`{to or '(empty)'}` is not a desk id (orchestrator, daybook, "
            f"tool.<name>, <project>.<component>)")

    # 2. Self-address. Doctrine: a session ignores its own origin - and the
    #    sanctioned self-continuation is the schedule, whose envelopes arrive
    #    as system mail with a budget (Phase D).
    if to == sender:
        return _refuse_desk_mail(mapping, sender, path,
                                 "self-mail is a loop - queue a continuation "
                                 "with schedule.py instead")

    # 3. Existence. The registry IS the filesystem, and refusing WITHOUT ever
    #    creating the folder is what closes the phantom-desk class
    #    (_unrunnable's docstring carries the incident this prevents).
    if not cwd_for(to).is_dir():
        return _refuse_desk_mail(mapping, sender, path,
                                 f"no such desk `{to}` (no folder)")

    # 4. Thread: echoed id (unknown ones are NOT resurrected) -> inferred ->
    #    fresh with the configured TTL.
    led = _load_thread(str(data.get("thread") or "").strip())
    if led is None:
        led = _infer_thread(sender)
    if led is None:
        led = _new_thread(sender, _clean_origin(sender, data.get("origin")), path.stem)
    if led.get("closed"):
        return _refuse_desk_mail(mapping, sender, path,
                                 f"chain `{led['id']}` is closed ({led['closed']})")

    # 5. Idempotence: a crash between inbox-write and unlink redelivers here.
    env_id = f"dm-{sender}-{path.stem}"
    if env_id in _delivery_ids(led):
        path.unlink(missing_ok=True)
        return "duplicate"

    # 6. Storm backstop: bounds every pathological shape, including the reply
    #    ping-pong that hop-free replies would otherwise permit. Scaled by how
    #    many desks are actually in the chain, because a four-desk project doing
    #    one honest round of fan-out and replies is ~8 messages and the flat cap
    #    of 12 closed it mid-conversation.
    if len(led.get("deliveries") or []) >= _storm_cap(led):
        _close_thread(led, "storm")
        return _refuse_desk_mail(mapping, sender, path,
                                 f"chain `{led['id']}` hit its message cap")

    # 7. Hops = DEPTH, not volume. A reply is free (it reverses a recorded
    #    edge), and so is breadth: one desk asking three siblings is normal
    #    coordination, not a runaway.
    #
    #    Counting every forward message made a four-desk project trip the limit
    #    constantly - orchestrator → server, server → tenant, server → web and
    #    the budget of 3 was gone, mid-task, with the owner told to "re-instruct
    #    to continue" for doing nothing wrong (2026-08-19: "I get it very
    #    often, why?"). The shape worth bounding is a chain travelling FURTHER
    #    from the person who started it - A→B→C→D→E, each desk handing off
    #    again - because that is what runs away unattended. Depth bounds that
    #    exactly, and breadth stays as cheap as it should be.
    is_reply = [to, sender] in (led.get("edges") or [])
    depth = dict(led.get("depth") or {})
    next_depth = depth.get(sender, 0) + 1
    # Deeper means: a desk this chain has never involved, further out than the
    # chain has ever reached. A sibling at a level already in use is breadth,
    # and writing again to a desk already involved is a conversation.
    deeper = (not is_reply) and to not in depth \
        and next_depth > max(depth.values() or [0])
    if deeper and next_depth > _hop_ttl():
        _close_thread(led, "hops")
        _thread_notice(mapping, led, sender,
                       f"⛔ delegation chain `{led['id']}` is already "
                       f"{_hop_ttl()} desks deep and `{sender}` → `{to}` would go "
                       f"further - re-instruct to continue (fresh mail starts a "
                       f"fresh chain). Asking a desk you have already involved is "
                       f"free; handing off to a new one is what counts.")
        return _refuse_desk_mail(mapping, sender, path, "hops exhausted")

    # 8. The cross-project gate (D4) - held mail leaves the outbox entirely.
    #
    # A REPLY IS NEVER GATED. The gate exists to stop a desk STARTING a
    # conversation across a boundary; answering someone who wrote to you first
    # is not starting one - the earlier message on this very thread is the
    # authorisation, and it was the owner's own instruction that produced it.
    #
    # Gating replies made delegation from the orchestrator useless, because
    # `daybook -> orchestrator` is not a free pair: on 2026-08-19 the owner
    # asked #omnius for one daybook note, the orchestrator delegated it, daybook
    # WROTE the note and answered - and the answer sat behind an ok he never saw
    # and dropped itself an hour later. The work was done and the fleet reported
    # nothing. Every chain the orchestrator starts ended that way.
    if not gate_approved and _gate_required() and not is_reply and not free_pair(sender, to):
        return hold_for_gate(mapping, sender, to, led, path, data)

    # 9. Deliver.
    # Depth is the budget now, so what a desk sees as "hops left" is how
    # much FURTHER this chain may travel - not how many messages remain.
    if deeper:
        depth[to] = next_depth
    elif to not in depth:
        depth[to] = min(next_depth, depth.get(to, next_depth))
    depth.setdefault(sender, max(0, next_depth - 1))
    hops_after = max(0, _hop_ttl() - max(depth.values() or [0]))
    files = [{"path": str(p), "name": Path(str(p)).name, "type": None}
             for p in (data.get("files") or [])]
    env = {"id": env_id, "from": sender, "channel": None, "channelId": None,
           "category": None, "ts": now_iso(), "text": text, "files": files,
           "kind": "desk", "thread": led["id"], "origin": led.get("origin"),
           "hops": hops_after, "replyTo": data.get("replyTo"), "slash": None}
    box = INBOX / to
    box.mkdir(parents=True, exist_ok=True)
    write_json_atomic(box / f"{env_id}.json", env)
    led["depth"] = depth
    led["hopsLeft"] = hops_after
    if not is_reply:
        led.setdefault("edges", []).append([sender, to])
    # Enriched since O1: the ledger is the chain's story, so each delivery
    # records who, whom, when and whether it travelled free. !trace reads
    # exactly this - state, never logs.
    led.setdefault("deliveries", []).append(
        {"id": env_id, "from": sender, "to": to, "ts": now_iso(),
         "reply": bool(is_reply)})
    led["lastDeliveredTo"] = to
    _save_thread(led)
    # Both halves reach the bus transcript - the paper trail must not go dark
    # just because no Discord channel was involved.
    paths = [f["path"] for f in files]
    transcribe(sender, "out", text, who=sender, files=paths)
    transcribe(to, "in", text, who=sender, files=paths)
    path.unlink(missing_ok=True)
    try:
        # Proof-of-reply: a desk that ONLY delegated is not a silent desk. The
        # Stop hook reads this stamp exactly like a posted reply's.
        (OUTBOX / sender / ".last-posted").write_text(now_iso(), encoding="utf-8")
    except OSError:
        pass
    _post_desk_mail_copy(mapping, sender, to, env)
    log(f"desk mail {sender} -> {to} ({env_id}, thread {led['id']}, "
        f"{hops_after} hop(s) left)")
    ensure_runner(to)
    return "delivered"


# --- the cross-project gate: holds a FILE, never a hook -------------------------

def pending_gates():
    """-> list of held cross-project asks, oldest first (mirror of
    pending_permissions)."""
    out = []
    if not GATE.is_dir():
        return out
    for p in sorted(GATE.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("id"):
                out.append(d)
        except (OSError, ValueError):
            continue
    return out


def _post_gate_ask(mapping, rec, led=None):
    """Ask where the human is: the chain's origin channel, else the sender's,
    else #alerts. Redacted preview - a brief can quote anything."""
    preview = api.redact(str((rec.get("data") or {}).get("text") or ""))[:200]
    cid = (led.get("origin") or {}).get("channelId") if led else None
    if not (cid and cid in mapping):
        cid = primary_channel_id(mapping, rec["sender"]) \
            or broadcast_channel_id("alerts")
    if not cid:
        log(f"gate ask {rec['id']}: no channel to ask in - held silently")
        return
    try:
        # The gate is a HUMAN authorization boundary, usually crossed from a
        # phone - so the thing being authorized is spelled out, not implied.
        api.send_message(cid, f"🔀 **cross-project desk mail — needs your ok**\n"
                              f"from  `{rec['sender']}`\n"
                              f"to    `{rec['to']}`\n"
                              f"> {preview}\n"
                              f"reply `ok {rec['code']}` to deliver · `no {rec['code']}` to drop"
                              f" — bare `ok`/`no` works while this is the only thing waiting."
                              f" Unanswered, it drops itself in 60m.")
    except api.ApiError as e:
        log(f"gate ask failed: {e}")


def hold_for_gate(mapping, sender, to, led, path, data):
    """Park a cross-project envelope for the owner's ok/no (docs\\DELEGATION.md
    D4). Reuses the permission relay's INTERACTION (ok/no + code) but holds a
    file instead of blocking a hook - nothing anywhere waits in-process. The
    sender's honest last word was "queued"; the owner decides from the
    channel."""
    GATE.mkdir(parents=True, exist_ok=True)
    gid = f"gate-{sender}-{path.stem}"
    rec = {"id": gid, "code": hashlib.md5(gid.encode()).hexdigest()[:6],
           "sender": sender, "to": to, "thread": led["id"], "stem": path.stem,
           "data": data, "askedAt": now_iso(), "askedTs": time.time(),
           "lastAskTs": time.time()}
    write_json_atomic(GATE / f"{gid}.json", rec)
    path.unlink(missing_ok=True)
    try:
        # The sender DID act; being held must not read as silence to its Stop hook.
        (OUTBOX / sender / ".last-posted").write_text(now_iso(), encoding="utf-8")
    except OSError:
        pass
    _post_gate_ask(mapping, rec, led)
    log(f"desk mail {sender} -> {to}: HELD for cross-project ok (code {rec['code']})")
    return "held"


def _resolve_gate(mapping, rec, behavior, how):
    """Apply a verdict to a held envelope. `how` is "answered" or "timeout" -
    the difference matters only for the ledger note and the wording."""
    gp = GATE / f"{rec['id']}.json"
    led = _load_thread(rec.get("thread") or "")
    if behavior == "allow":
        box = OUTBOX / rec["sender"]
        box.mkdir(parents=True, exist_ok=True)
        p = box / f"{rec['stem']}.json"
        data = dict(rec.get("data") or {})
        # Pin the chain resolved at hold time - inference must not re-guess,
        # and the approval travels in-process, never as a field a desk could
        # write into its own outbox.
        data["thread"] = rec.get("thread")
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        gp.unlink(missing_ok=True)
        r = deliver_desk_mail(mapping, rec["sender"], p, data, gate_approved=True)
        log(f"gate {rec['id']}: allowed ({how}) -> {r}")
        if r == "delivered":
            return f"✅ delivered — `{rec['sender']}` → `{rec['to']}`"
        return f"⚠️ approved, but delivery came back `{r}` — see `{rec['sender']}`'s channel"
    try:
        gp.rename(gp.with_suffix(".refused"))
    except OSError:
        gp.unlink(missing_ok=True)
    if led and not led.get("closed"):
        _close_thread(led, "gate-denied" if how == "answered" else "gate-timeout")
    why = "dropped" if how == "answered" else "dropped (no answer within 60m)"
    log(f"gate {rec['id']}: {why}")
    return f"🗑 {why} — `{rec['sender']}`'s mail to `{rec['to']}` was not delivered"


def answer_gate(text, mapping):
    """Interpret an owner reply as a verdict on a held cross-project envelope.

    Mirrors answer_permission's grammar, with the spec's precedence rule: a
    blocked hook outranks a parked envelope. handle_message consults the
    permission answerer FIRST, so while permission asks are pending a bare ok
    never reaches here; and with several gates pending the code is required."""
    pend = pending_gates()
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
    codes = {d.get("code"): d for d in pend}
    rec = None
    for w in words[1:]:
        w = w.strip("`.,!")
        if w in codes:
            rec = codes[w]
            break
    if rec is None:
        if pending_permissions():
            return None       # the permission answerer owns bare words right now
        if len(pend) > 1:
            listing = ", ".join(f"`{d.get('code')}` ({d['sender']} → {d['to']})"
                                for d in pend)
            return (f"⚠️ {len(pend)} desk-mail asks are waiting - answer with "
                    f"the code: {listing}")
        rec = pend[0]
    return _resolve_gate(mapping, rec, behavior, "answered")


def sweep_gates(mapping):
    """Per tick: fail closed on stale asks; re-post pending asks once per boot.
    Files are the whole state, so a restart keeps the code AND the original
    deadline - `askedTs` never moves."""
    if GATE.is_dir():
        for f in sorted(GATE.glob("*.json")):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict) or not rec.get("id"):
                continue
            if time.time() - float(rec.get("askedTs") or 0) > GATE_WAIT_SECONDS:
                msg = _resolve_gate(mapping, rec, "deny", "timeout")
                _thread_notice(mapping, _load_thread(rec.get("thread") or ""),
                               rec["sender"], msg)
            elif float(rec.get("lastAskTs") or 0) < _BOOT_TS:
                _post_gate_ask(mapping, rec, _load_thread(rec.get("thread") or ""))
                rec["lastAskTs"] = time.time()
                write_json_atomic(f, rec)
    sweep_threads()


def sweep_threads():
    """Ledger hygiene: closed chains linger briefly (post-mortems read them),
    open chains idle past THREAD_IDLE_SECONDS are swept with a breadcrumb."""
    if not THREADS.is_dir():
        return
    for f in THREADS.glob("*.json"):
        led = _load_thread(f.stem)
        if led is None:
            continue
        try:
            idle = time.time() - f.stat().st_mtime
        except OSError:
            continue
        if led.get("closed") and idle > CLOSED_THREAD_KEEP_SECONDS:
            f.unlink(missing_ok=True)
        elif idle > THREAD_IDLE_SECONDS:
            if not led.get("closed"):
                log(f"thread {led['id']} expired unfinished (48h idle) - swept")
            f.unlink(missing_ok=True)


# --- !trace: one story per chain, from state alone (docs\OBSERVABILITY.md O1) ---
# A delegated instruction crosses a dozen surfaces; every hop is recorded
# SOMEWHERE. This is the join - ledgers, gate records, loop files - assembled
# into one screen. Never the logs: logs are for humans, state is for machines.

def _short_ts(iso):
    s = str(iso or "")
    return s[11:19] if len(s) >= 19 else (s or "?")


def _gate_notes_for(thread_id):
    """Gate holds touching this chain: pending asks and preserved .refused
    records. An ALLOWED hold leaves no file on purpose - its delivery is the
    record, and it appears in the hop list like any other."""
    notes = []
    if not GATE.is_dir():
        return notes
    for f in sorted(GATE.glob("*.json")) + sorted(GATE.glob("*.refused")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("thread") != thread_id:
            continue
        try:
            deadline = time.strftime(
                "%H:%M:%S", time.localtime(float(rec.get("askedTs") or 0)
                                           + GATE_WAIT_SECONDS))
        except (ValueError, OverflowError, OSError):
            deadline = "?"
        if f.suffix == ".refused":
            notes.append(f"gate  {rec.get('sender')} → {rec.get('to')}  held "
                         f"{_short_ts(rec.get('askedAt'))} → DROPPED "
                         f"(deadline was {deadline})")
        else:
            notes.append(f"gate  {rec.get('sender')} → {rec.get('to')}  held "
                         f"{_short_ts(rec.get('askedAt'))} · WAITING for ok "
                         f"`{rec.get('code')}` · drops at {deadline}")
    return notes


def _trace_thread(led):
    state = f"closed: {led['closed']}" if led.get("closed") else "open"
    lines = [f"THREAD {led.get('id')}", f"state    {state}"]
    o = led.get("origin") or {}
    if o:
        lines.append(f"origin   channel {o.get('channelId') or '-'} · "
                     f"from {o.get('from') or '?'} · starter {o.get('session') or '?'}")
    lines.append(f"span     {_short_ts(led.get('startedAt'))} → {_short_ts(led.get('lastAt'))}")
    dels = led.get("deliveries") or []
    spent = sum(1 for d in dels if isinstance(d, dict) and not d.get("reply"))
    free = sum(1 for d in dels if isinstance(d, dict) and d.get("reply"))
    legacy = sum(1 for d in dels if not isinstance(d, dict))
    budget = f"hops     {spent} spent · {free} free · {led.get('hopsLeft', '?')} left"
    if legacy:
        budget += f" · {legacy} pre-trace (id only)"
    lines.append(budget)
    for i, d in enumerate(dels, 1):
        if isinstance(d, dict):
            kind = "reply" if d.get("reply") else "hop"
            lines.append(f"{i:02d}  {_short_ts(d.get('ts'))}  "
                         f"{d.get('from')} → {d.get('to')}   [{kind}]")
        else:
            lines.append(f"{i:02d}  --:--:--  {d}   [pre-trace entry]")
    lines.extend(_gate_notes_for(led.get("id")))
    return "\n".join(lines)


def _trace_loop(led):
    state = f"closed: {led['closed']}" if led.get("closed") else "open"
    last = _short_ts(led.get("lastFiredAt")) if led.get("lastFiredAt") else "-"
    lines = [f"LOOP {led.get('id')}",
             f"desk     {led.get('session')}",
             f"state    {state}",
             f"budget   {led.get('fired', 0)}/{led.get('max', 0)} run(s) used",
             f"opened   {_short_ts(led.get('openedAt'))} · last fire {last}",
             f"channel  {led.get('channelId') or '-'}"]
    try:
        queued = [j for j in schedule.load_jobs() if j.get("loop") == led.get("id")]
        note = f"queued   {len(queued)} continuation(s)"
        if queued:
            note += f" · next {queued[0].get('nextRun') or '?'}"
        lines.append(note)
        if queued and queued[0].get("text"):
            lines.append(f"task     {str(queued[0].get('text'))[:120]}")
    except Exception:                                        # noqa: BLE001
        pass
    return "\n".join(lines)


def _trace_listing():
    lines, leds = [], []
    if THREADS.is_dir():
        for f in THREADS.glob("*.json"):
            led = _load_thread(f.stem)
            if led:
                leds.append(led)
    leds.sort(key=lambda x: str(x.get("lastAt") or ""), reverse=True)
    if leds:
        lines.append("CHAINS (newest first)")
        for led in leds[:10]:
            state = f"closed:{led['closed']}" if led.get("closed") else "open"
            n = len(led.get("deliveries") or [])
            who = (led.get("origin") or {}).get("session") or "?"
            lines.append(f"{led['id']}  {state} · {n} delivery(ies) · starter {who}")
    loops = schedule.list_loops()
    if loops:
        lines.append("LOOPS")
        for led in loops:
            state = f"closed:{led['closed']}" if led.get("closed") else "open"
            lines.append(f"{led['id']}  run {led.get('fired', 0)}/{led.get('max', 0)} · {state}")
    return "\n".join(lines) if lines else \
        "no chains and no loops yet - delegation and work loops write them"


def handle_trace(text, cid):
    """!trace [id] - the lifecycle of one chain, loop or envelope; bare !trace
    lists what exists. Zero tokens, watchdog-handled, like every control verb:
    seeing what the fleet did must never cost a run."""
    parts = text.split()
    arg = parts[1].strip("`.,") if len(parts) > 1 else ""
    body = None
    if not arg:
        body = _trace_listing()
    else:
        led = _load_thread(arg)
        if led:
            body = _trace_thread(led)
        else:
            loop = schedule.load_loop(arg)
            if loop:
                body = _trace_loop(loop)
            elif arg.startswith("dm-") and THREADS.is_dir():
                for f in THREADS.glob("*.json"):
                    t = _load_thread(f.stem)
                    if t and arg in _delivery_ids(t):
                        body = f"envelope {arg} travelled on:\n\n" + _trace_thread(t)
                        break
        if body is None:
            body = (f"nothing called '{arg}' - no chain, loop or delivery by that id.\n"
                    f"What exists:\n\n{_trace_listing()}")
    try:
        api.send_message(cid, f"```\n{body}\n```")
    except api.ApiError as e:
        log(f"!trace post failed: {e}")


# --- 2FA codes over Discord (docs\WEB.md W3) ------------------------------------
# The permission relay wearing a different hat: a held FILE, never a blocked
# anything. A one-time code is not a credential the fleet keeps - it is a
# 30-second value the owner reads off his own phone, so relaying it is HIM
# doing the 2FA with the desk as hands. It therefore travels the bus, and it
# leaves NO trace: never delivered as mail, never transcribed, single use.

TWOFA_WAIT_SECONDS = 120          # unanswered asks fail closed, never retry
_TWOFA_RE = re.compile(r"^\s*(\d{6})\s*$")


def pending_twofa():
    """-> list of pending code requests, oldest first."""
    out = []
    if not TWOFA.is_dir():
        return out
    for p in sorted(TWOFA.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("id"):
                out.append(d)
        except (OSError, ValueError):
            continue
    return out


def sweep_twofa(mapping):
    """Per tick: post asks that have not been posted, expire stale ones.

    Fail closed - an unanswered ask is DROPPED, so a login stalls and reports
    rather than retrying into an account lockout."""
    for rec in pending_twofa():
        p = TWOFA / f"{rec['id']}.json"
        age = time.time() - float(rec.get("askedTs") or 0)
        if age > TWOFA_WAIT_SECONDS:
            p.unlink(missing_ok=True)
            log(f"2FA ask {rec['id']} ({rec.get('site')}) timed out - dropped")
            cid = primary_channel_id(mapping, rec.get("session") or "orchestrator")
            if cid:
                try:
                    api.send_message(cid, f"⌛ no code for `{rec.get('site')}` within "
                                          f"{TWOFA_WAIT_SECONDS // 60}m - that login stopped. "
                                          f"Ask again when you are ready.")
                except api.ApiError:
                    pass
            continue
        if rec.get("posted"):
            continue
        cid = primary_channel_id(mapping, rec.get("session") or "orchestrator") \
            or broadcast_channel_id("alerts")
        if not cid:
            continue
        try:
            api.send_message(cid, f"🔢 **`{rec.get('site')}` wants a 6-digit code** "
                                  f"(for `{rec.get('user') or 'your account'}`).\n"
                                  f"Reply with the six digits — nothing else. "
                                  f"It expires in {TWOFA_WAIT_SECONDS // 60} minutes and is "
                                  f"used once.")
        except api.ApiError as e:
            log(f"2FA ask post failed: {e}")
            continue
        rec["posted"] = True
        write_json_atomic(p, rec)
        log(f"2FA ask {rec['id']} posted for {rec.get('site')}")


def answer_twofa(text):
    """Consume a bare 6-digit reply as the answer to a pending code request.

    -> a confirmation string, or None if this was not an answer. Six digits
    collide with nothing else on the bus: control verbs start `!`, permission
    and gate answers are words. THE CODE IS NEVER ECHOED and never reaches an
    envelope or a transcript - handle_message returns before write_envelope,
    which is the whole point."""
    pend = pending_twofa()
    if not pend:
        return None
    m = _TWOFA_RE.match(text or "")
    if not m:
        return None
    rec = pend[0]                                  # oldest; codes are serial by nature
    try:
        (TWOFA / f"{rec['id']}.code").write_text(m.group(1), encoding="utf-8")
    except OSError as e:
        return f"could not hand the code over: {e}"
    (TWOFA / f"{rec['id']}.json").unlink(missing_ok=True)
    log(f"2FA code delivered for {rec.get('site')} ({rec['id']})")   # the CODE is never logged
    extra = ""
    if len(pend) > 1:
        extra = f" ({len(pend) - 1} more code request(s) still waiting)"
    return f"🔢 code passed to `{rec.get('site')}`{extra} — it is not stored anywhere."


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
        # Exact FIRST-TOKEN match, not startswith: "!killswitch ideas" and
        # "!modelo fable" prefix-match !kill / !model, and a prefix dispatch
        # sent them into a chain where no branch fires - the message was
        # swallowed whole: no action, no reply, no mail (found 2026-08-16).
        # An unknown !word is just text; the desk should read it.
        first_token = text.lower().split(None, 1)[0] if text else ""
        if first_token in CONTROL_COMMANDS:
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
        # Held cross-project desk mail (docs\DELEGATION.md D4). AFTER the
        # permission answerer on purpose: a blocked hook outranks a parked
        # envelope, so a bare "ok" reaches a gate only when no permission ask
        # consumed it first.
        gate = answer_gate(text, mapping)
        if gate:
            try:
                api.send_message(cid, gate)
            except api.ApiError:
                pass
            return "gate"
        # A 6-digit code is an ANSWER, not mail (docs\WEB.md W3). Consumed here
        # so it never reaches write_envelope: no envelope, no transcript, no
        # log line - a one-time code should leave no trace once used.
        code = answer_twofa(text)
        if code:
            try:
                api.send_message(cid, code)
            except api.ApiError:
                pass
            return "twofa"
    if not target.session:
        # An ok/no in #alerts that arrives after the request timed out matched
        # nothing, so the owner got "nobody listens here" for answering exactly
        # where he was told to - which is how a working rail comes to look
        # broken (2026-08-02). Say what actually happened instead.
        if (cid == fleet_channel_id(mapping, "alerts")
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
        # A project channel with no desk is a DIFFERENT problem from a status
        # channel, and needs a different sentence: routing is by channel name,
        # so renaming one in Discord (or creating one by hand) points it at a
        # component folder that does not exist. Saying "read-only status
        # channel" there would be a lie and a dead end.
        cat = target.category_name or ""
        if cat.startswith(api.load_schema()["prefixes"]["project"]):
            project = cat[len(api.load_schema()["prefixes"]["project"]):].strip()
            try:
                comps = ", ".join(f"#{c}" for c in api.project_components(project)) or "(none yet)"
            except Exception:                                    # noqa: BLE001
                comps = "(could not list them)"
            try:
                api.send_message(cid,
                    f"🔇 `#{target.channel_name}` answers to no desk: routing is by "
                    f"channel NAME, and there is no folder "
                    f"`projects\\{project}\\{target.channel_name}\\`. Either rename the "
                    f"channel back to a component, or ask me to create that component. "
                    f"This project's desks are: {comps}")
            except api.ApiError:
                pass
            log(f"note: message in #{target.channel_name} ({cat}) - no matching component")
            return "unmapped"
        try:
            api.send_message(cid, f"\U0001f507 #{target.channel_name} is a read-only status channel - "
                                  f"nobody listens here. Talk to me in: {', '.join(talk)}")
        except api.ApiError:
            pass
        log(f"note: message in unmapped #{target.channel_name} - redirected owner")
        return "unmapped"
    # Slash pass-through (docs\DELEGATION.md D6). Owner mail only - a guest's
    # /anything is ordinary text. /omnius is an always-allowed no-op alias (the
    # run already IS `-p "/omnius"`; stamping it would recurse the skill into
    # itself). Anything unlisted delivers NOTHING: handing it over as plain
    # text would make the desk improvise the verb he asked for, which is worse
    # than refusing out loud.
    slash = None
    if sender == "owner" and text.startswith("/"):
        _m = re.match(r"/([A-Za-z0-9_-]+)", text)
        name = _m.group(1).lower() if _m else ""
        if name in SLASH_SKILLS:
            slash = name
        elif name != "omnius":
            try:
                # Only reachable when config\skills.ini NARROWS the default -
                # since 2026-08-19 an instance with no such file passes every
                # slash through, so this message means "you restricted it".
                api.send_message(cid, f"⛔ `/{name or '?'}` is not on the pass-through "
                                      f"list your `config\\skills.ini` sets - nothing "
                                      f"was delivered. Add it there, delete that file "
                                      f"to allow every skill again, or say it in words.")
            except api.ApiError:
                pass
            log(f"slash refused: /{name or '?'} in #{target.channel_name}")
            return "slash-refused"
    try:
        api.add_reaction(cid, m["id"])
    except api.ApiError:
        pass
    files = save_attachments(m)
    write_envelope(target.session, target.channel_name, m, files,
                   channel_id=cid, category=target.category_name, sender=sender,
                   slash=slash)
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
    # O2 (docs\OBSERVABILITY.md): count this boot against a pending update.
    # New code that crash-loops or sat deaf is reverted RIGHT HERE, before it
    # can do anything else - after the lock, so a stray manual start next to a
    # healthy watchdog can never bump the counter.
    update_pending_boot()
    load_failure_ledger()   # strikes survive a restart (drilled 2026-08-18)
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
    update_boot_notice(mapping)

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
                # A watchdog can run for weeks without a boot, so the boot-time
                # release notice alone would never fire on the machines that
                # need it most. Re-check daily on the refresh cadence - the
                # per-tip stamp inside keeps it to one announcement per news.
                if time.time() - _release_last[0] >= RELEASE_CHECK_SECONDS:
                    update_boot_notice(mapping)

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
            sweep_gates(mapping)
            sweep_twofa(mapping)
            check_backlogs()
            reap_runs()
            ensure_runners()
            ensure_telegram_bridge()
            show_working(mapping)
            fleet_board(mapping)
            fire_due_schedules(mapping)
            fire_heartbeat()
            update_job_tick()
            # Either transport being demonstrably healthy earns the stamp, so the
            # beacon stays ~3s fresh however messages are currently arriving.
            if consecutive_deaf == 0 or live:
                write_beacon(len(mapping), gateway=live)
                # O2: beacon stamped on a healthy tick = the running code has
                # PROVEN it took over. Confirm a pending update (or break the
                # news of a revert), exactly once.
                update_pending_confirm()
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
