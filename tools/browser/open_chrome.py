#!/usr/bin/env python
"""Open Chrome on the RIGHT profile, without anyone clicking a picker.

Why this exists (2026-09-04). A desk was asked to read a site while Chrome was
closed. It launched `chrome` with no arguments, Chrome showed its profile
picker, and the run only worked because the owner happened to be at the machine
to click "Tu Chrome". From a phone that click does not exist - the same failure
mode `[browser] device_id` already solved for "which browser", one level down:
"which profile".

So the profile is a SETTING, not a question:

    config\\omnius.ini
    [browser]
    profile_directory = Default

Usage:
    python tools\\browser\\open_chrome.py --list          what profiles exist
    python tools\\browser\\open_chrome.py                 launch the configured one
    python tools\\browser\\open_chrome.py --url <url>     ... and open a page
    python tools\\browser\\open_chrome.py --status        is Chrome running?

It never types a password and never touches a profile's data - it only starts
chrome.exe with `--profile-directory`, which is what the picker would have done.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import omnius_config as ocfg  # noqa: E402

USER_DATA = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"

CHROME_CANDIDATES = [
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Google" / "Chrome" / "Application" / "chrome.exe",
]


def chrome_exe() -> Path:
    for c in CHROME_CANDIDATES:
        if c.is_file():
            return c
    raise SystemExit("chrome.exe not found in the usual places")


def profiles() -> list[dict]:
    """-> [{dir, name, account, active_time}], newest-used first.

    Read from Chrome's own Local State, so the list is whatever the picker
    would show - no guessing, no hard-coded names.
    """
    state = USER_DATA / "Local State"
    if not state.is_file():
        return []
    data = json.loads(state.read_text(encoding="utf-8", errors="replace"))
    cache = data.get("profile", {}).get("info_cache", {}) or {}
    out = []
    for key, info in cache.items():
        out.append({
            "dir": key,
            "name": info.get("name", key),
            "account": info.get("user_name", ""),
            "active_time": info.get("active_time", 0),
        })
    out.sort(key=lambda p: p["active_time"], reverse=True)
    return out


def running() -> int:
    """-> number of chrome.exe processes (0 = closed)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception:
        return 0
    return sum(1 for line in out.splitlines() if "chrome.exe" in line.lower())


def launch(profile_dir: str, url: str = "", wait: float = 12.0) -> dict:
    args = [str(chrome_exe())]
    if profile_dir:
        args.append(f"--profile-directory={profile_dir}")
    if url:
        args.append(url)
    subprocess.Popen(args, close_fds=True)
    # The extension needs a moment to connect its websocket; a desk that calls
    # list_connected_browsers too early sees [] and wrongly concludes "no
    # browser". Wait here rather than making every caller re-learn that.
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(1.0)
        if running():
            break
    time.sleep(4.0)
    return {"launched": True, "profile": profile_dir or "(no flag)",
            "url": url or None, "processes": running()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list Chrome profiles and exit")
    ap.add_argument("--status", action="store_true", help="report whether Chrome runs")
    ap.add_argument("--url", default="", help="page to open once Chrome is up")
    ap.add_argument("--profile", default="", help="override the configured profile directory")
    ap.add_argument("--force", action="store_true",
                    help="launch even if Chrome already runs (opens a window in that profile)")
    a = ap.parse_args()

    if a.list:
        configured = ocfg.browser_profile_directory()
        for p in profiles():
            mark = " <- configured" if p["dir"] == configured else ""
            print(f"{p['dir']:<12} {p['name']:<14} {p['account']}{mark}")
        if not configured:
            print("\n[browser] profile_directory is EMPTY in config\\omnius.ini "
                  "-> Chrome will show its picker, which nobody can click remotely.")
        return 0

    if a.status:
        print(json.dumps({"processes": running(),
                          "configured_profile": ocfg.browser_profile_directory()}))
        return 0

    profile_dir = a.profile or ocfg.browser_profile_directory()
    known = {p["dir"] for p in profiles()}
    if profile_dir and known and profile_dir not in known:
        print(f"unknown profile directory {profile_dir!r}; --list shows what exists")
        return 2

    n = running()
    if n and not a.force:
        # Already up: a second launch would only add a window. If a URL was
        # asked for, hand it to the running Chrome (it lands in that profile's
        # window) - otherwise say nothing needed doing.
        if a.url:
            subprocess.Popen([str(chrome_exe())]
                             + ([f"--profile-directory={profile_dir}"] if profile_dir else [])
                             + [a.url], close_fds=True)
            time.sleep(3.0)
        print(json.dumps({"launched": False, "alreadyRunning": True, "processes": n,
                          "url": a.url or None}))
        return 0

    print(json.dumps(launch(profile_dir, a.url)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
