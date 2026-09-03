#!/usr/bin/env python3
"""Desk bridge - one always-open terminal that Discord can also type into.

    python tools\\bridge\\desk_bridge.py <session-id>       (e.g. orchestrator)

Run it IN a terminal window. It starts `claude` inside a ConPTY and pumps
bytes both ways, so what you see and type is the ordinary Claude CLI. The
difference is that a second writer - the bus - can type into the same live
session when Discord mail arrives.

WHY THIS EXISTS (2026-08-01, the whole day's lesson): the fleet answered
Discord by starting a fresh `claude -p` per message. Measured on the bus log:
31s, 86s, 87s for one-word replies, nearly all of it CLI boot + skill load
before the first word was read. A warm session makes reply time equal
thinking time. Every previous attempt to keep a session warm failed because
"you cannot type into a live terminal from outside" - which is false on
Windows: a ConPTY owner can. That is the only new primitive here.

WHAT THIS IS NOT: it is a KEYBOARD, NOT A BRAIN. It never parses Claude's
output, never decides anything, never talks to Discord. Replies keep flowing
the proven way: session -> state\\outbox\\ -> watchdog -> Discord. If this
process dies, the desk is still reachable - the watchdog falls back to a
headless run. Slow, never deaf.

Design: docs\\ARCHITECTURE.md par.3.4, memory\\orchestrator\\topics\\discord-fleet.md.
"""
import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path


def _console_host():
    """-> pid of the cmd.exe that owns our window, or None.

    That process dying IS the window being closed, and it is the only signal
    available: a hard terminate runs no handler here."""
    try:
        return os.getppid()
    except Exception:
        return None


def _pid_alive(pid):
    """Same identity-free liveness check the watchdog uses (OpenProcess +
    GetExitCodeProcess). No psutil, so the bridge keeps no extra dependency."""
    if not pid:
        return False
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259          # STILL_ACTIVE
    except Exception:
        return True          # unreadable: assume alive rather than self-destruct

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "discord"))

STATE = ROOT / "state"
INBOX = STATE / "inbox"
TURNS = STATE / "turns"          # <id>.busy while a turn is running (hooks own it)
BRIDGES = STATE / "bridges"      # <id>.json: a live bridge owns this desk

READ_CHUNK = 16384
POLL_SECONDS = 0.02          # 50 Hz: below the threshold where typing feels laggy
RETRY_FRESH_SECONDS = 30     # a --continue refusal kills the CLI this fast at boot
INBOX_POLL_SECONDS = 0.5     # mail latency; the gateway push already did the waiting
HUMAN_QUIET_SECONDS = 4.0    # never type into a line the owner is still writing
NUDGE = "/omnius"            # what the bus types; the skill does the rest
NUDGE_COOLDOWN = 20.0        # after a nudge that actually STARTED a turn
NUDGE_RETRY = 4.0            # after a nudge that vanished (session not ready)
NUDGE_FAST_TRIES = 3         # ...and only this many times for one unread count
NUDGE_REPEAT_SECONDS = 60.0  # then: one nudge a minute while nothing changes
OUTPUT_STAMP_SECONDS = 5.0   # how often the desk says "still printing" (see _note_output)
BOOT_ALLOWANCE = 9.0         # claude needs this long before it can read a keystroke
# Measured on the first real takeover (2026-08-03): the bridge typed the nudge
# the same second it launched claude, into a session that did not exist yet,
# then served the full 20s cooldown for that miss. 25 wasted seconds on a
# 48-second reply. A nudge that produces no turn was never seen, so retrying
# it quickly is right; only a nudge that DID start a turn needs the long floor.

# Claude Code names its own window: ESC ]0;<title> BEL. With six desks open,
# every tab therefore read "Claude Code" and the owner could not tell the
# orchestrator from a project (2026-08-02: "I'd like to have them properly
# named"). wt's --title loses that race because the app sets the title AFTER
# the tab is created. The bridge already sits in the byte stream, so it strips
# the app's title sequences and re-asserts the desk's name instead.
OSC_TITLE = re.compile(r"\x1b\][02];[^\x07\x1b]*(?:\x07|\x1b\\)")


LOGS = ROOT / "state" / "logs"
_log_path = None


def log(msg):
    """Never lose a startup failure.

    A bridge lives in a window the owner may close, and its errors would
    otherwise scroll past inside a TUI or die with the tab. Everything that
    matters also goes to state\\logs\\bridge-<id>.log.
    """
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
    try:
        print(f"[bridge] {msg}", flush=True)
    except Exception:
        pass
    if _log_path is None:
        return
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(_log_path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# A GENUINE BOOT FAILURE, and nothing else, keeps the window open.
#
# open_tab used to append `|| pause` to ANY non-zero exit, so every ordinary
# end of life left a dead "Press any key" window on his desktop: a !kill, a
# !restart, a bridge replaced during an outage. On 2026-09-03 six such
# replacements in 85 minutes produced six of them, which is the window pile-up
# he described. A window is only worth keeping when it holds the ONLY copy of
# why the desk could not start - and that is this code, nothing else. Anything
# claude itself exits with passes straight through and closes.
BOOT_FAILURE = 3


def fail(msg, code=BOOT_FAILURE):
    log(f"FATAL: {msg}")
    raise SystemExit(code)


def cwd_for(session):
    """Session id -> desk folder. Same table as CLAUDE.md par.1 / inbox_watch."""
    if session == "orchestrator":
        return ROOT
    if session == "daybook":
        return ROOT / "daybook"
    if session.startswith("tool."):
        tool = session.partition(".")[2]
        return (ROOT / "tools" / tool) if tool else None
    if "." in session:
        project, _, component = session.partition(".")
        return ROOT / "projects" / project / component
    return None


def claude_argv(session, cwd, model=None, effort=None):
    """The command a human would type, plus the flags a desk needs.

    Mirrors watchdog.start_run's flag set on purpose - a desk must behave the
    same whether a person or the bus is driving it. --add-dir because a
    component's sandbox is its own folder while the bus and shared memory live
    at the root; --settings because settings do NOT inherit across folders.
    """
    import shutil
    exe = shutil.which("claude")
    if not exe:
        fail("claude CLI not found on PATH")
    resume_mode = "transcript"
    try:
        import watchdog as wd
        desk = wd.desk_config(session)
        model = model or desk.get("model")
        effort = effort or desk.get("effort")
        resume_mode = str(desk.get("resume") or "transcript").strip().lower()
    except Exception:
        pass                                   # fleet.json must never block a desk
    argv = [exe, "--add-dir", str(ROOT)]
    proj_settings = cwd.parent / ".claude" / "settings.json"
    if session != "orchestrator" and proj_settings.is_file():
        argv += ["--settings", str(proj_settings)]
    if model:
        argv += ["--model", str(model)]
    if effort:
        argv += ["--effort", str(effort)]
    # Resume this desk's own conversation. A live bridge is warm because it
    # never restarts - but the moment it DOES (!restart, a crash, a reboot), a
    # bare `claude` starts an empty one and the day's work is gone from the
    # session's view. Both other launch paths already resume (watchdog
    # start_run, fleet_ops.open_desk); this one did not, so "restart the desk"
    # quietly meant "forget the desk". Found 2026-08-13 while a desk was stuck
    # and !restart was the obvious remedy - it would have cost him the whole
    # morning's discovery conversation.
    #
    # Gated on has_history for the reason start_run states: in a virgin folder
    # --continue attaches to the most recent conversation from SOMEWHERE ELSE.
    #
    # ...and gated on the desk's RESUME POLICY, which this path ignored until
    # 2026-08-16. start_run honours `resume: "fresh"`; this did not, so the
    # orchestrator - the one desk configured fresh, because its dev transcript
    # dwarfs the chat it is answering - resumed a 5.9 MB conversation to reply
    # to a Discord line the moment a takeover moved it onto the bridge. That is
    # the ~200x context tax fleet.json calls "the owner's 'really slow'",
    # fixed for headless runs on 2026-08-01 and still live here. He felt it as
    # "after the takeover your responses were really slow" and was right.
    try:
        import watchdog as wd
        if resume_mode != "fresh" and wd.has_history(cwd):
            argv += ["--continue"]
    except Exception:
        pass                                   # cold start is always safe
    return argv


class Bridge:
    """Owns the ConPTY. Two pumps: pty->console, console->pty."""

    def __init__(self, session, argv, cwd):
        self.session, self.argv, self.cwd = session, argv, cwd
        self.pty = None
        self.stop = threading.Event()
        # Serialises the two writers (human keystrokes, injected bus text) so a
        # nudge can never land in the middle of a line the owner is typing.
        self.write_lock = threading.Lock()
        self.last_human = 0.0        # monotonic-ish stamp of the last keystroke
        self.last_nudge = 0.0
        self.nudge_took = False      # did the last nudge actually start a turn?
        self.last_nudge_count = -1   # unread count the last nudge was aimed at
        self.nudge_tries = 0         # fast retries spent on THAT count
        self.started_at = time.time()
        self.box = INBOX / self.session
        self.title_seq = f"\x1b]0;{session}\x07"
        self._tail = ""              # a title sequence may straddle two reads
        # A refused `--continue` kills the CLI one line after boot ("No
        # conversation found to continue", interactive only - 2026-08-18, the
        # 112-window daybook loop). The pump watches for that line so main()
        # can relaunch once WITHOUT the flag instead of dying as if the desk
        # had closed normally.
        self.saw_continue_refusal = False
        self._last_out_stamp = 0.0    # throttles the progress touch
        self._refusal_tail = ""      # the marker may straddle two reads too

    @staticmethod
    def clean_env():
        """The desk's own environment - NOT its launcher's.

        Caught on the first live boot (2026-08-02): a bridge started from
        inside a Claude session inherits CLAUDE_CODE_CHILD_SESSION, and the
        desk came up announcing "Transcript saving is off". A desk whose
        conversation is not saved cannot be warm - `--continue` would have
        nothing to resume - which would have silently defeated the entire
        point of the bridge while looking like it worked.

        Also drops OMNIUS_RUN_ID: that token identifies a headless run, and a
        bridge desk is not one.
        """
        env = dict(os.environ)
        for k in list(env):
            if k.startswith("CLAUDE_CODE_") or k in ("OMNIUS_RUN_ID", "OMNIUS_SESSION"):
                env.pop(k, None)
        return env

    def start(self):
        from winpty import PtyProcess
        cols, rows = self._console_size()
        env = self.clean_env()
        env["OMNIUS_DESK"] = self.session           # so the session can name itself
        self.pty = PtyProcess.spawn(self.argv, cwd=str(self.cwd),
                                    dimensions=(rows, cols), env=env)
        return self

    @staticmethod
    def _console_size():
        try:
            sz = os.get_terminal_size()
            return max(40, sz.columns), max(10, sz.lines)
        except OSError:
            return 120, 30

    def write(self, text):
        """The ONLY way anything reaches the session. Both writers go through here."""
        with self.write_lock:
            try:
                self.pty.write(text)
                return True
            except Exception as e:
                print(f"\r\n[bridge] write failed: {e}\r\n", file=sys.stderr)
                return False

    def name_tab(self):
        """Put the DESK id in the tab, so six open windows are tellable apart."""
        try:
            sys.stdout.write(self.title_seq)
            sys.stdout.flush()
        except Exception:
            pass

    def _rename(self, data):
        """Drop the app's own title sequences and re-assert the desk name.

        Nothing else in the stream is touched - the TUI must render exactly as
        it would without us. A sequence can straddle two reads, so an
        unterminated tail is held back rather than passed through and shown to
        the owner as garbage.
        """
        data = self._tail + data
        self._tail = ""
        cut = data.rfind("\x1b]")
        if cut != -1 and not OSC_TITLE.search(data[cut:]):
            self._tail, data = data[cut:], data[:cut]   # incomplete: wait for the rest
        out, n = OSC_TITLE.subn("", data)
        return out + (self.title_seq if n else "")

    CONTINUE_REFUSED = "No conversation found to continue"

    def _scan_refusal(self, data):
        """Notice the CLI refusing `--continue`, across chunk boundaries."""
        if self.saw_continue_refusal:
            return
        joined = self._refusal_tail + data
        if self.CONTINUE_REFUSED in joined:
            self.saw_continue_refusal = True
            self._refusal_tail = ""
        else:
            self._refusal_tail = joined[-(len(self.CONTINUE_REFUSED) - 1):]

    def _note_output(self):
        """Touch `state\\bridges\\<id>.out` while the desk is producing output.

        THE PROOF OF LIFE THE WATCHDOG WAS MISSING. It replaces a desk's window
        when mail waits 90s with no turn started - the right rule for a session
        that is wedged, and the wrong one for a session that is BUSY BOOTING.
        Resuming a long conversation compacts it first, which prints a progress
        bar for minutes and starts no turn, so the window was shot mid-compaction
        and replaced (2026-08-19, his screenshot: 1m21s in, 59%, killed).

        A wedged session prints nothing; a compacting one prints constantly. So
        the byte stream itself is the signal, and it costs one throttled touch -
        no parsing of a message whose wording will change with the next CLI.
        """
        now = time.time()
        if now - self._last_out_stamp < OUTPUT_STAMP_SECONDS:
            return
        self._last_out_stamp = now
        try:
            BRIDGES.mkdir(parents=True, exist_ok=True)
            (BRIDGES / f"{self.session}.out").write_text(str(int(now)), encoding="utf-8")
        except OSError:
            pass

    def pump_out(self):
        """ConPTY -> this console. Passthrough, minus the app's title grab."""
        out = sys.stdout
        while not self.stop.is_set():
            try:
                data = self.pty.read(READ_CHUNK)
            except EOFError:
                break
            except Exception:
                break
            if data:
                self._scan_refusal(data)
                self._note_output()
                try:
                    out.write(self._rename(data))
                    out.flush()
                except Exception:
                    break
            else:
                time.sleep(POLL_SECONDS)
        self.stop.set()

    def pump_in(self):
        """This console -> ConPTY, key by key.

        msvcrt rather than line-buffered stdin: Claude Code is a TUI, so arrow
        keys, Ctrl-C and single-key menus must arrive as they are pressed, not
        at the end of a line.
        """
        import msvcrt
        while not self.stop.is_set():
            if not msvcrt.kbhit():
                time.sleep(POLL_SECONDS)
                continue
            ch = msvcrt.getwch()
            if ch == "\x00" or ch == "\xe0":        # special key: 2-byte sequence
                nxt = msvcrt.getwch()
                seq = {"H": "\x1b[A", "P": "\x1b[B", "M": "\x1b[C", "K": "\x1b[D",
                       "G": "\x1b[H", "O": "\x1b[F", "S": "\x1b[3~",
                       "R": "\x1b[2~", "I": "\x1b[5~", "Q": "\x1b[6~"}.get(nxt)
                if seq:
                    self.write(seq)
                continue
            self.mark_human_typed()
            self.write(ch)
        self.stop.set()

    def mark_human_typed(self):
        self.last_human = time.time()

    # --- the bus side ---------------------------------------------------------

    def has_mail(self):
        try:
            return any(self.box.glob("*.json"))
        except OSError:
            return False

    def mail_count(self):
        """-> how many envelopes are queued. The nudge's rate limit keys on it:
        NEW mail is a new reason to type, the same mail is not."""
        try:
            return sum(1 for _ in self.box.glob("*.json"))
        except OSError:
            return 0

    def turn_running(self):
        """True while this desk is mid-turn.

        The UserPromptSubmit hook writes state\\turns\\<id>.busy and the Stop
        hook removes it, so this is the session's own account of itself - the
        one signal that cannot be faked by a sidecar (the whole 2026-08-01
        heartbeat lesson). While it stands, a nudge would queue a second
        prompt behind work already underway.
        """
        return (TURNS / f"{self.session}.busy").is_file()

    def may_nudge(self):
        """-> (bool, why-not). Deliberately conservative: a wrong nudge lands
        inside the owner's half-typed sentence, and that is unforgivable in a
        window he is sitting at."""
        if not self.has_mail():
            return False, "no mail"
        if self.turn_running():
            return False, "turn in progress"
        booting = time.time() - self.started_at
        if booting < BOOT_ALLOWANCE:
            return False, f"session still booting ({booting:.0f}s)"
        quiet = time.time() - self.last_human
        if quiet < HUMAN_QUIET_SECONDS:
            return False, f"owner typing ({quiet:.1f}s ago)"
        # THE SAME MAIL IS NOT A NEW REASON TO TYPE. On 2026-09-03 the API was
        # down for 85 minutes: no nudge ever started a turn, so nudge_took
        # stayed False, so the 4-second retry floor applied for the whole
        # outage - 1,577 "mail waiting" lines typed into a session that could
        # not answer. The fast retry is still right for the case it was
        # measured on (2026-08-03: a nudge typed into a session that was not
        # ready yet, resolved in seconds), so it survives as a BOUNDED number
        # of tries; after that the same unread count buys one nudge a minute.
        # A change in the count is always a new reason and resets the budget.
        n = self.mail_count()
        if n != self.last_nudge_count:
            self.last_nudge_count = n
            self.nudge_tries = 0
        floor = NUDGE_COOLDOWN if self.nudge_took else (
            NUDGE_RETRY if self.nudge_tries < NUDGE_FAST_TRIES else NUDGE_REPEAT_SECONDS)
        if time.time() - self.last_nudge < floor:
            return False, "cooling down"
        return True, ""

    def pump_bus(self):
        """Type the nudge when mail is waiting and the coast is clear.

        It types `/omnius` and Enter - nothing else. The skill drains the
        inbox, does the work and replies through the outbox exactly as it does
        for a headless run, so there is ONE handling path, not two.
        """
        last_why = None
        while not self.stop.is_set():
            # A turn running means the previous nudge landed. Remember that:
            # only a nudge that was actually READ deserves the long cooldown.
            if self.turn_running():
                self.nudge_took = True
            ok, why = self.may_nudge()
            if ok:
                self.last_nudge = time.time()
                self.nudge_took = False
                self.nudge_tries += 1
                last_why = None
                if self.write(NUDGE + "\r"):
                    log("mail waiting -> typed the nudge into the live session")
            elif why and why not in ("no mail", "turn in progress"):
                # Only the interesting refusals, and only when the reason
                # CHANGES: this loop runs at 2 Hz, so logging every pass wrote
                # forty identical "cooling down" lines per cooldown and buried
                # the one line that mattered.
                if why.split(" (")[0] != last_why:
                    last_why = why.split(" (")[0]
                    log(f"holding the nudge: {why}")
            time.sleep(INBOX_POLL_SECONDS)

    def announce(self):
        """Tell the fleet a live bridge owns this desk (pid liveness only).

        No heartbeat, on purpose: a stamp refreshed by a sidecar while the
        thing it describes is gone is exactly the lie that cost 2026-08-01.
        The watchdog validates this pid before deciding not to start a run.
        """
        try:
            BRIDGES.mkdir(parents=True, exist_ok=True)
            (BRIDGES / f"{self.session}.json").write_text(json.dumps({
                "session": self.session, "pid": os.getpid(),
                "cwd": str(self.cwd), "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                time.gmtime()),
            }), encoding="utf-8")
        except OSError:
            pass

    def withdraw(self):
        try:
            (BRIDGES / f"{self.session}.out").unlink(missing_ok=True)
            (BRIDGES / f"{self.session}.json").unlink(missing_ok=True)
        except OSError:
            pass

    def claim_desk(self):
        """Check the desk in immediately, without spending a model turn.

        A desk that has not claimed is invisible to !status AND has the
        permission relay switched off (bus_connected reads the claim), so it
        would prompt at the local screen instead of in #alerts - exactly the
        thing the owner was complaining about. The bridge only nudges when mail
        arrives, so before this a freshly opened window sat unclaimed until
        someone wrote to it.

        The claim is still written by inbox_watch - never hand-authored - and
        it is told the pty's claude pid, which the ancestor walk cannot find
        from here (claude is our CHILD, not our parent).
        """
        pid = getattr(self.pty, "pid", None)
        cmd = [sys.executable, str(ROOT / "tools" / "discord" / "inbox_watch.py"),
               self.session, "--once"] + (["--pid", str(pid)] if pid else [])
        try:
            # utf-8 explicitly: the check-in prints the agent's name and emoji;
            # under the ANSI codepage the reader thread died on byte 0x81 and
            # the output was silently lost (2026-09-03).
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                               encoding="utf-8", errors="replace",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                log(f"desk claimed (session pid {pid})")
            else:
                log(f"check-in returned {r.returncode}: {(r.stderr or '').strip()[:200]}")
        except (OSError, subprocess.SubprocessError) as e:
            log(f"check-in failed: {e}")     # never fatal: mail still nudges later

    def run(self):
        self.announce()
        self.name_tab()
        threading.Thread(target=self.claim_desk, daemon=True).start()
        t_out = threading.Thread(target=self.pump_out, daemon=True)
        t_in = threading.Thread(target=self.pump_in, daemon=True)
        t_bus = threading.Thread(target=self.pump_bus, daemon=True)
        t_out.start()
        t_in.start()
        t_bus.start()
        # Closing the window must actually close the desk. Measured 2026-08-05:
        # killing the console host leaves BOTH this process and the claude it
        # owns alive and invisible - `finally` never runs, because a window
        # close is a hard terminate, not an exit (lessons.md). The desk then
        # looks served by a bridge nobody can see, and mail gets typed into a
        # session with no window. So: watch the console host, and when it goes,
        # leave properly - withdraw the presence file and take claude with us.
        host = _console_host()
        try:
            while not self.stop.is_set() and self.pty.isalive():
                if host and not _pid_alive(host):
                    log("console window closed - shutting the desk down")
                    break
                time.sleep(0.1)
        except KeyboardInterrupt:
            self.write("\x03")                  # pass Ctrl-C to claude, don't die on it
            time.sleep(0.2)
        finally:
            self.stop.set()
            self.withdraw()          # the desk falls back to headless runs at once
            try:
                if self.pty.isalive():
                    self.pty.terminate(force=True)
            except Exception:
                pass
        return 0


def retry_without_continue(attempt, cmd, saw_refusal, alive_seconds):
    """One relaunch, only for the one failure it exists for: the CLI refused
    `--continue` and died at boot. A long-lived desk whose CONVERSATION merely
    contained the marker text is not a refusal - the fast-exit window is what
    tells them apart - and the second attempt never loops: it runs without the
    flag, so the refusal cannot recur."""
    return (attempt == 1 and "--continue" in cmd and saw_refusal
            and alive_seconds <= RETRY_FRESH_SECONDS)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Omnius desk bridge (ConPTY)")
    ap.add_argument("session", help="desk id: orchestrator, <project>.<component>, tool.<t>, daybook")
    ap.add_argument("--model")
    ap.add_argument("--effort")
    args = ap.parse_args(argv)

    global _log_path
    _log_path = LOGS / f"bridge-{args.session}.log"
    log(f"starting desk {args.session} (pid {os.getpid()})")

    cwd = cwd_for(args.session)
    if not cwd or not cwd.is_dir():
        fail(f"no folder for desk {args.session!r} ({cwd})")
    try:
        import winpty  # noqa: F401
    except ImportError:
        fail("pywinpty is not installed - run: pip install pywinpty")

    cmd = claude_argv(args.session, cwd, args.model, args.effort)
    log(f"cwd {cwd}")
    rc = 0
    for attempt in (1, 2):
        log("exec " + " ".join(cmd))
        try:
            b = Bridge(args.session, cmd, cwd).start()
        except Exception as e:
            import traceback
            log(f"FATAL: could not start the pty: {type(e).__name__}: {e}")
            for ln in traceback.format_exc().rstrip().splitlines():
                log("  " + ln)
            return BOOT_FAILURE
        print()
        try:
            rc = b.run()
        finally:
            # rc and uptime belong in the log: a desk that dies inside the boot
            # window with rc=0 is the CLI answering a dialog, not a crash.
            log(f"desk closed (rc={rc}, alive {int(time.time() - b.started_at)}s)")
        alive = time.time() - b.started_at
        if retry_without_continue(attempt, cmd, b.saw_continue_refusal, alive):
            # The 2026-08-18 boot loop: the CLI refused --continue (transcript
            # present but not resumable - e.g. written by an older CLI) and
            # exited one line in. That must cost ONE relaunch and a fresh
            # conversation, never the desk. has_history() cannot predict this
            # (the CLI owns the rule and changes it); this catch does not try.
            log(f"`--continue` refused by the CLI ({int(alive)}s after boot) - "
                f"nothing resumable here; relaunching WITHOUT it, fresh conversation")
            cmd = [a for a in cmd if a != "--continue"]
            continue
        break
    return rc


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
