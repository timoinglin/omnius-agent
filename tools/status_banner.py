#!/usr/bin/env python3
"""Omnius status banner - the face of a running instance (stdlib only).

Probes live state read-only and prints a human dashboard:
    python tools\\status_banner.py            # print once
    python tools\\status_banner.py --watch    # live dashboard, refresh loop

Probes (all local, no Discord API calls, no secrets printed):
  - watchdog   state\\watchdog\\lock.json + pid liveness, bot name from log
  - daybook    HTTP http://localhost:<PORT>/ (default 5111)
  - discord    .env config booleans via tools\\discord\\api.py
  - sessions   state\\sessions\\*.json claims with pid/heartbeat liveness

ASCII-only output: Windows consoles default to cp1252 - a status tool must
never crash on encoding (learned the hard way, see docs\\TESTING.md).
"""
import argparse
import ctypes
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "discord"))
import api  # noqa: E402  (reads .env at import; no network)

STATE = ROOT / "state"
BEACON_STALE_SECONDS = 90   # generous vs POLL_SECONDS=3 + MAP_REFRESH_SECONDS=60
DAYBOOK_PORT = api.ENV.get("PORT", "5111")
DAYBOOK_URL = f"http://localhost:{DAYBOOK_PORT}"
WIDTH = 62


STILL_ACTIVE = 259


def pid_alive(pid):
    """Probe-only liveness check. NEVER use os.kill(pid, 0) on Windows -
    it can terminate the target instead of probing it."""
    if not pid:
        return False
    if os.name == "nt":
        try:
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return bool(ok) and code.value == STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def probe_watchdog():
    """-> (up: bool, detail: str)"""
    lock = STATE / "watchdog" / "lock.json"
    if not lock.exists():
        return False, "not running"
    try:
        d = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "lock unreadable"
    if not pid_alive(d.get("pid")):
        return False, f"stale lock (pid {d.get('pid')} dead)"
    # The process being alive is the cheap half. A watchdog can hold its lock,
    # log happily and still deliver nothing (revoked token, lost permissions) -
    # so trust the beacon, which is only stamped after a pass that reached every
    # channel. Silence here is the failure no process check can see.
    beacon = STATE / "watchdog" / "beacon.json"
    try:
        age = (datetime.now(timezone.utc)
               - datetime.strptime(json.loads(beacon.read_text(encoding="utf-8"))["at"],
                                   "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds()
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        age = None
    if age is None:
        return False, f"running (pid {d.get('pid')}) but no beacon yet"
    if age > BEACON_STALE_SECONDS:
        return False, f"ALIVE BUT NOT POLLING - last good pass {int(age)}s ago"
    bot = ""
    log = STATE / "logs" / "watchdog.log"
    if log.exists():
        try:
            for line in reversed(log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]):
                if "token ok - bot:" in line:
                    bot = line.split("token ok - bot:", 1)[1].strip()
                    break
        except OSError:
            pass
    detail = "listening on Discord (owner allowlisted)"
    if bot:
        detail += f" - bot: {bot}"
    return True, detail


def probe_daybook():
    """-> (up: bool, detail: str)"""
    try:
        req = urllib.request.Request(DAYBOOK_URL, method="GET")
        with urllib.request.urlopen(req, timeout=1.5):
            pass
        return True, DAYBOOK_URL
    except Exception:
        return False, f"not running ({DAYBOOK_URL})"


def probe_discord_config():
    """-> (ok: bool, detail: str) - booleans only, never values."""
    missing = [n for n, v in (("BOT_TOKEN", api.TOKEN), ("GUILD_ID", api.GUILD),
                              ("OWNER_ID", api.OWNER)) if not v]
    if not missing:
        return True, "configured (.env complete)"
    return False, "missing in .env: " + ", ".join("DISCORD_" + m for m in missing)


def stalled_sessions():
    """-> list[str] of desks frozen on a local permission dialog.

    The banner reported liveness purely from claim data, which is exactly the
    signal that lies: the heartbeat comes from inbox_watch, a separate process
    that keeps stamping while the session is stuck on a prompt nobody can see.
    !status learned this on 2026-07-31; the banner is the other surface a human
    trusts, so it has to say the same thing or the hole just moves.

    Read-only probe of state\\permissions\\<session>.stalled, in keeping with
    every other probe here."""
    out = []
    perms = STATE / "permissions"
    if not perms.is_dir():
        return out
    for p in sorted(perms.glob("*.stalled")):
        out.append(p.name[: -len(".stalled")])
    return out


def live_sessions():
    """-> list[str] of desks with a live PROCESS right now.

    'sid' = an interactive terminal (claim pid alive); 'sid (run)' = an active
    headless run (lease pid alive). Strictly pid-validated: claims carry no
    heartbeat since 2026-08-01, because a lastSeenAt stamped by a sidecar while
    the session was gone is how a dead desk read as '1 live' all evening. If
    nothing is listed the system is still fully reachable - the watchdog starts
    a run when mail arrives."""
    out = []
    sess_dir = STATE / "sessions"
    if sess_dir.is_dir():
        for p in sorted(sess_dir.glob("*.json")):
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if pid_alive(c.get("pid")):
                out.append(p.stem)
    runs_dir = STATE / "watchdog" / "runs"
    if runs_dir.is_dir():
        for p in sorted(runs_dir.glob("*.json")):
            if p.stem in out:
                continue
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if pid_alive(r.get("pid")):
                out.append(f"{p.stem} (run)")
    return out


def render():
    wd_up, wd_detail = probe_watchdog()
    db_up, db_detail = probe_daybook()
    dc_ok, dc_detail = probe_discord_config()
    sessions = live_sessions()

    def mark(ok):
        return "[OK]" if ok else "[--]"

    def row(tag, name, detail):
        return f"   {tag} {name:<11}{detail}"

    lines = []
    bar = "=" * WIDTH
    title = "O M N I U S   -   " + ("O N L I N E" if (wd_up or db_up) else "O F F L I N E")
    lines.append(bar)
    lines.append(f"   {title}{api.MACHINE:>{WIDTH - 5 - len(title)}}")
    lines.append(bar)
    lines.append(row(mark(wd_up), "Watchdog", wd_detail))
    lines.append(row(mark(db_up), "Daybook", db_detail))
    lines.append(row(mark(dc_ok), "Discord", dc_detail))
    sess_txt = f"{len(sessions)} live: " + ", ".join(sessions) if sessions else "none live (wake on demand)"
    lines.append(row("    ", "Sessions", sess_txt))
    # A stalled desk still counts as "live" above - its pid is alive - which is
    # precisely why this line exists. Never let the banner imply health from
    # claim data alone.
    stalled = stalled_sessions()
    if stalled:
        lines.append(row("[!!]", "STALLED", ", ".join(stalled) + "  - waiting at a local dialog, answer or !restart"))
    lines.append("-" * WIDTH)
    if wd_up:
        lines.append("   Omnius wakes on Discord messages.")
    elif dc_ok:
        lines.append("   Watchdog is down - start-omnius.bat brings it up.")
    else:
        lines.append("   Local mode - run install.bat for the Discord setup.")
    lines.append("   Summon the orchestrator now -> wakeup-omnius.bat")
    lines.append(bar)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Omnius status banner")
    ap.add_argument("--watch", action="store_true", help="refresh loop (live dashboard)")
    ap.add_argument("--interval", type=float, default=5.0, help="refresh seconds (default 5)")
    args = ap.parse_args()

    while True:
        out = render()
        if args.watch:
            os.system("cls" if os.name == "nt" else "clear")
        try:
            print(out, flush=True)
        except UnicodeEncodeError:  # belt and braces - output is ASCII already
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            print(out.encode(enc, "replace").decode(enc), flush=True)
        if not args.watch:
            break
        print(f"\n   refreshing every {args.interval:.0f}s - Ctrl+C to close (services keep running)")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
