#!/usr/bin/env python3
"""Search and tail the local bus transcript (stdlib only).

    python tools\\discord\\transcript.py search "vitest"
    python tools\\discord\\transcript.py search "api contract" --session recipe-app.backend
    python tools\\discord\\transcript.py tail -n 20

The watchdog appends every inbound and outbound bus message to
state\\transcripts\\<session>\\<YYYY-MM>.jsonl. Before that existed, both halves
of a remote conversation were write-only - envelopes and outbox files are
deleted once handled - so the only copy lived in Discord and did not survive a
kill, a fresh --continue, or leaving the channel.

No index, deliberately. A measured 10-year corpus scans in ~55ms with plain
stdlib, and an SQLite FTS5 index over it rebuilds in 0.38s - it can never repay
the staleness tracking it would need. See memory\\orchestrator\\status.md.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS = ROOT / "state" / "transcripts"


def entries(session=None, days=None):
    """Yield (session, entry) oldest-first, optionally filtered."""
    if not TRANSCRIPTS.is_dir():
        return
    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for sdir in sorted(TRANSCRIPTS.iterdir()):
        if not sdir.is_dir() or (session and sdir.name != session):
            continue
        for f in sorted(sdir.glob("*.jsonl")):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue          # a torn last line must not kill the scan
                if cutoff:
                    try:
                        ts = datetime.strptime(e.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ")
                        if ts.replace(tzinfo=timezone.utc) < cutoff:
                            continue
                    except ValueError:
                        pass
                yield sdir.name, e


def render(session, e, width=110):
    arrow = "->" if e.get("dir") == "out" else "<-"
    ch = f"#{e['channel']}" if e.get("channel") else "?"
    body = " ".join((e.get("text") or "").split())
    if len(body) > width:
        body = body[:width - 1] + "…"
    files = f"  [{len(e['files'])} file(s)]" if e.get("files") else ""
    return f"{e.get('ts', '?')} {arrow} {session:22} {ch:16} {body}{files}"


def main(argv):
    # Discord messages are full of emoji and the Windows console is cp1252.
    # watchdog.log() already learned this the hard way; a reporting tool must
    # never die on the content it exists to display.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("search", help="case-insensitive substring search")
    p.add_argument("query")
    p.add_argument("--session")
    p.add_argument("--days", type=int)
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("tail", help="most recent messages")
    p.add_argument("--session")
    p.add_argument("-n", type=int, default=20)
    p = sub.add_parser("sessions", help="list sessions with transcripts")
    a = ap.parse_args(argv)

    if a.cmd == "sessions":
        if not TRANSCRIPTS.is_dir():
            print("no transcripts yet")
            return 0
        for d in sorted(x for x in TRANSCRIPTS.iterdir() if x.is_dir()):
            n = sum(1 for _ in entries(session=d.name))
            months = len(list(d.glob("*.jsonl")))
            print(f"{d.name:28} {n:6} messages  {months} month file(s)")
        return 0

    if a.cmd == "search":
        q = a.query.lower()
        hits = [(s, e) for s, e in entries(a.session, a.days)
                if q in (e.get("text") or "").lower()]
        for s, e in hits[-a.limit:]:
            print(render(s, e))
        shown = min(len(hits), a.limit)
        print(f"\n{len(hits)} match(es)" + (f", showing last {shown}" if len(hits) > shown else ""))
        return 0 if hits else 1

    rows = list(entries(a.session))[-a.n:]
    for s, e in rows:
        print(render(s, e))
    if not rows:
        print("no transcript entries yet")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
