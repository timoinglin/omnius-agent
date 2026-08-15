#!/usr/bin/env python3
"""
Minimal personal notes app — one file, stdlib only, no database.

The markdown files under the notes folder ARE the product; this server is
just a convenient pen and viewer over them. One file per month (YYYY-MM.md):

    # 2026-07

    ## 2026-07-15 Wed

    - 09:42 note text here
    - [ ] 10:10 an open task
    - [x] 10:40 a finished task ✅ 2026-07-15 14:32
    - 11:05 another note, markdown allowed: **bold**, `code`, [links](url)

Marking a task done appends the completion timestamp (the Obsidian-style
✅ marker); unchecking removes it.

Multi-line notes indent continuation lines by two spaces so each note stays
one markdown list item. Tasks use GitHub-flavored checkboxes so they render
natively anywhere markdown does.

Writing is append-only. The only mutations are the ones you explicitly
trigger — toggling a task, converting note<->task, editing a note's text,
deleting — and they rewrite only the affected lines. Deleted notes are
appended to notes/.trash.md, so nothing is ever silently lost.

Attachments (pasted / dropped files) live under notes/files/YYYY-MM/ and are
referenced from the markdown with relative links, so external renderers
resolve them too.

Run:  python app.py            (serves http://localhost:5111)

Settings come from config.ini next to this file ([notes] section: notes_dir,
port, host) — every key optional. An environment variable of the same name
(NOTES_DIR, PORT, NOTES_HOST) overrides the file for a single run, and
NOTES_CONFIG points at a different config file. Defaults: ./notes, 5111,
127.0.0.1.

host = 0.0.0.0 publishes the notes to everyone on the network. There is no
authentication: any visitor can read every note and the API will accept
their writes, edits and deletes. Startup prints a warning when the bind
address is not loopback.
"""

import configparser
import hashlib
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _config_path():
    """Where notes settings live. NOTES_CONFIG > config\\notes.ini > config.ini.

    Settings moved into the workspace-wide `config\\` folder on 2026-08-05 so
    there is one place to look instead of four. The old `daybook\\config.ini` is
    still honoured when the new one is absent: this app is also usable on its
    own, outside an Omnius tree, and a half-migrated copy must still serve.
    Deliberately a path check and not an import - nothing here depends on
    `tools\\`, so the notes app stays standalone (README, "Install").
    """
    if os.environ.get("NOTES_CONFIG"):
        return Path(os.environ["NOTES_CONFIG"])
    shared = BASE_DIR.parent / "config" / "notes.ini"
    return shared if shared.is_file() else BASE_DIR / "config.ini"


CONFIG_PATH = _config_path()


def load_config(path):
    """The [notes] section of an ini file, as a dict; {} when there is none.

    A missing file is the normal case — every key has a default. A broken one
    is reported and ignored rather than fatal: a typo in a config file should
    never stand between you and your notes.
    """
    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    try:
        with open(path, encoding="utf-8") as fh:
            parser.read_file(fh)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, configparser.Error) as exc:
        print(f"notes: ignoring {path} ({type(exc).__name__}: {exc})")
        return {}
    return dict(parser["notes"]) if parser.has_section("notes") else {}


CONFIG = load_config(CONFIG_PATH)


def setting(key, env_var, default=""):
    """Environment variable (one-off override) > config file > default."""
    return (os.environ.get(env_var) or CONFIG.get(key) or default).strip()


def int_setting(key, env_var, default):
    raw = setting(key, env_var)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"notes: {key}={raw!r} is not a number, using {default}")
        return default


_notes_dir = setting("notes_dir", "NOTES_DIR")
# Relative paths follow app.py, not the shell's cwd — a config file should
# mean the same thing whichever folder you launch from.
NOTES_DIR = (BASE_DIR / _notes_dir) if _notes_dir else BASE_DIR / "notes"
HOST = setting("host", "NOTES_HOST", "127.0.0.1")
PORT = int_setting("port", "PORT", 5111)

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])\Z")
DAY_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})(?:\s+(\S.*?))?\s*$")
TASK_RE = re.compile(r"^- \[([ xX])\] (\d{2}:\d{2}) (.*)$")
NOTE_RE = re.compile(r"^- (\d{2}:\d{2}) (.*)$")
DONE_RE = re.compile(r"\s*✅ (\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*$")
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"}
KINDS = ("all", "note", "task", "open", "done")
MAX_NOTE_BYTES = 64 * 1024
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Reentrant so mutations (which hold it) can re-parse. Readers take it too:
# on Windows, os.replace fails with PermissionError while any thread has the
# file open, so reads and file swaps must never overlap.
_lock = threading.RLock()


class Conflict(Exception):
    """The note on disk no longer matches what the client saw."""


# ---------------------------------------------------------------- storage

def month_path(month):
    return NOTES_DIR / (month + ".md")


def files_dir():
    return NOTES_DIR / "files"


def clean_lines(text):
    """Split into lines, strip trailing spaces, drop leading/trailing blank lines.

    splitlines() treats every Unicode line separator (U+000B, U+000C, U+0085,
    U+2028, U+2029, ...) as a real break, so none can hide inside a stored
    note line and later be mistaken for a day header. NUL never reaches the
    files (replaced with U+FFFD).
    """
    lines = [ln.rstrip() for ln in text.replace("\x00", "�").splitlines()]
    while lines and not lines[0]:
        del lines[0]
    while lines and not lines[-1]:
        del lines[-1]
    return lines


def list_months():
    with _lock:
        if not NOTES_DIR.is_dir():
            return []
        return sorted(p.stem for p in NOTES_DIR.glob("*.md") if MONTH_RE.match(p.stem))


def _read_lines(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _retry_win(fn):
    """Retry briefly on PermissionError — an external process (editor, agent,
    antivirus) may hold the file open, which blocks replace/unlink on Windows."""
    for _ in range(20):
        try:
            return fn()
        except PermissionError:
            time.sleep(0.025)
    return fn()


def _write_lines(path, lines):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8", newline="\n")
    try:
        _retry_win(lambda: os.replace(tmp, path))
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def append_note(text, now=None, task=False):
    """Append one note (or open task) and return it. Never touches existing bytes."""
    lines = clean_lines(text)
    if not lines:
        raise ValueError("note is empty")
    now = now or datetime.now()
    month = now.strftime("%Y-%m")
    day = now.strftime("%Y-%m-%d")
    weekday = WEEKDAYS[now.weekday()]
    hhmm = now.strftime("%H:%M")

    prefix = "- [ ] " if task else "- "
    block = prefix + hhmm + " " + lines[0] + "\n"
    for ln in lines[1:]:
        block += ("  " + ln).rstrip() + "\n"

    path = month_path(month)
    with _lock:
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        existing = path.read_bytes() if path.exists() else b""
        if not existing:
            chunk = "# " + month + "\n\n## " + day + " " + weekday + "\n\n" + block
        else:
            last_day = None
            decoded = existing.decode("utf-8", errors="replace")
            for raw in decoded.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                m = DAY_RE.match(raw)
                if m:
                    last_day = m.group(1)
            chunk = "" if existing.endswith(b"\n") else "\n"
            if last_day != day:
                chunk += "\n## " + day + " " + weekday + "\n\n"
            chunk += block
        with open(path, "ab") as f:
            f.write(chunk.encode("utf-8"))

    return {"month": month, "date": day, "weekday": weekday, "time": hhmm,
            "text": "\n".join(lines), "type": "task" if task else "note",
            "done": False}


def parse_month(month):
    """Parse a month file into {"month": ..., "days": [...]}, newest day first.

    Every note carries line/end (its block's line range in the file) and a
    short sha of the raw block, which mutation endpoints use as an
    optimistic-concurrency check.
    """
    path = month_path(month)
    days = {}
    order = []
    with _lock:
        lines = _read_lines(path) if path.exists() else None
    if lines is not None:
        cur_day = None
        cur_note = None
        for i, raw in enumerate(lines):
            m = DAY_RE.match(raw)
            if m:
                d = m.group(1)
                if d not in days:
                    days[d] = {"date": d, "weekday": m.group(2) or "", "notes": []}
                    order.append(d)
                cur_day = days[d]
                cur_note = None
                continue
            m = TASK_RE.match(raw)
            if m and cur_day is not None:
                text = m.group(3)
                completed = None
                dm = DONE_RE.search(text)
                if dm:
                    completed = dm.group(1)
                    text = text[:dm.start()].rstrip()
                cur_note = {"time": m.group(2), "text": text, "type": "task",
                            "done": m.group(1).lower() == "x", "completed": completed,
                            "line": i, "end": i + 1}
                cur_day["notes"].append(cur_note)
                continue
            m = NOTE_RE.match(raw)
            if m and cur_day is not None:
                cur_note = {"time": m.group(1), "text": m.group(2), "type": "note",
                            "done": False, "completed": None, "line": i, "end": i + 1}
                cur_day["notes"].append(cur_note)
                continue
            if cur_note is not None and raw.startswith("  "):
                cur_note["text"] += "\n" + raw[2:]
                cur_note["end"] = i + 1
                continue
            if cur_note is not None and raw == "":
                # blank line continues the note only if more indented lines follow
                nxt = next((ln for ln in lines[i + 1:] if ln != ""), None)
                if nxt is not None and nxt.startswith("  "):
                    cur_note["text"] += "\n"
                    cur_note["end"] = i + 1
                else:
                    cur_note = None
                continue
            cur_note = None
        for day in days.values():
            for n in day["notes"]:
                # the date is part of the hash so identical blocks on
                # different days can never satisfy each other's ref
                block = day["date"] + "\n" + "\n".join(lines[n["line"]:n["end"]])
                n["sha"] = hashlib.sha1(block.encode("utf-8")).hexdigest()[:12]
    return {"month": month, "days": [days[d] for d in sorted(order, reverse=True)]}


TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9][\w/-]{1,30})")


def stats(day_window=90, top_tags=12):
    """Everything the Stats view needs, in ONE pass over the months.

    Computed server-side on purpose: the client would otherwise have to fetch
    every month separately just to count them, which is N requests to answer a
    question that is one scan of files already on disk.
    """
    months, per_month, per_day = list_months(), [], {}
    totals = {"notes": 0, "tasks": 0, "open": 0, "done": 0}
    tags = {}
    for month in months:
        m_notes = m_tasks = 0
        for day in parse_month(month)["days"]:
            date = day["date"]
            for note in day["notes"]:
                per_day[date] = per_day.get(date, 0) + 1
                if note["type"] == "task":
                    m_tasks += 1
                    totals["tasks"] += 1
                    totals["done" if note.get("done") else "open"] += 1
                else:
                    m_notes += 1
                    totals["notes"] += 1
                for tag in TAG_RE.findall(note.get("text") or ""):
                    key = tag.lower()
                    tags[key] = tags.get(key, 0) + 1
        per_month.append({"month": month, "notes": m_notes, "tasks": m_tasks})

    today = datetime.now().date()
    days = []
    for back in range(day_window - 1, -1, -1):
        d = today - timedelta(days=back)
        iso = d.isoformat()
        days.append({"date": iso, "count": per_day.get(iso, 0)})
    # Streak counts back from today, but a day with nothing written YET should
    # not read as a broken streak at 09:00 - so today only breaks it if
    # yesterday was also empty.
    streak, cursor = 0, today
    if not per_day.get(cursor.isoformat()):
        cursor -= timedelta(days=1)
    while per_day.get(cursor.isoformat()):
        streak += 1
        cursor -= timedelta(days=1)
    busiest = max(per_day.items(), key=lambda kv: kv[1]) if per_day else None
    return {
        "totals": totals,
        "months": per_month,
        "days": days,
        "streak": streak,
        "activeDays": len(per_day),
        "busiest": {"date": busiest[0], "count": busiest[1]} if busiest else None,
        "tags": [{"tag": t, "count": c} for t, c in
                 sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))[:top_tags]],
    }


# (The Guide tab lived here until 2026-08-15. Once the repo went public the
# tab duplicated GitHub, and the owner's redesign brief was "just add/see
# notes when I am at the desk" - so the guide became a link in Settings and
# the tab count halved. GETTING-STARTED.md itself is unchanged.)

# --- the day view -------------------------------------------------------------
# "What did I do on day X?" is the question this app exists to answer, and
# notes are only half of the record. The other half is what the FLEET did that
# day - commits across the workspace's repos, desk activity on the bus - and
# all of it is already on disk; nothing ever assembled it as a day.

WORKSPACE = "auto"     # tests point this at a sandbox ("" = force standalone)

DATE_ARG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
GIT_TIMEOUT = 10       # seconds per repo; a wedged git must not hang the page


def workspace_root():
    """-> the Omnius workspace root this daybook lives in, or None.

    Same standalone promise as omnius_settings(): a copy of daybook\\ on its
    own has no workspace, so every fleet section simply vanishes - never an
    error. The suite boots the app standalone on purpose."""
    if WORKSPACE != "auto":
        return Path(WORKSPACE) if WORKSPACE else None
    root = BASE_DIR.parent
    return root if (root / "tools").is_dir() and (root / "CLAUDE.md").is_file() else None


def _day_commits(root, date):
    """Commits made on `date`, across the root repo and every project repo.

    cwd=repo rather than `git -C`: the workspace-wide rule against -C is about
    desks and allow-list matching, but the plain form is also simply clearer.
    Any repo that errors or dawdles is skipped - one broken project must not
    take the whole day view down."""
    repos = []
    if (root / ".git").is_dir():
        repos.append(("omnius", root))
    projects = root / "projects"
    if projects.is_dir():
        try:
            for p in sorted(projects.iterdir()):
                if p.is_dir() and (p / ".git").is_dir():
                    repos.append((p.name, p))
        except OSError:
            pass
    out = []
    for name, path in repos:
        try:
            # creationflags: this server is hosted by pythonw (no console), and
            # a console-subsystem child spawned from a console-less parent gets
            # a brand-new VISIBLE console - one cmd flash per repo per Today
            # load (seen live on two machines, 2026-08-15). capture_output
            # pipes the streams but does not stop the console; only this flag
            # does. 0 off-Windows.
            r = subprocess.run(
                ["git", "log", "--since", date + " 00:00:00",
                 "--until", date + " 23:59:59",
                 "--date=format:%H:%M", "--pretty=%h|%ad|%s"],
                cwd=str(path), capture_output=True, text=True,
                errors="replace", timeout=GIT_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            sha, _, rest = line.partition("|")
            hhmm, _, subject = rest.partition("|")
            if sha and subject:
                out.append({"repo": name, "hash": sha, "time": hhmm,
                            "subject": subject})
    out.sort(key=lambda c: c["time"])
    return out


def _day_desks(root, date):
    """Per-desk bus activity on `date`, from state\\transcripts\\ (JSONL,
    one file per session per month - see watchdog.transcribe)."""
    tdir = root / "state" / "transcripts"
    if not tdir.is_dir():
        return []
    out = []
    try:
        sdirs = sorted(d for d in tdir.iterdir() if d.is_dir())
    except OSError:
        return []
    for sdir in sdirs:
        f = sdir / (date[:7] + ".jsonl")
        if not f.is_file():
            continue
        n_in = n_out = 0
        first = last = None
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue                      # a torn line skips, the scan continues
            ts = str(e.get("ts") or "")
            if not ts.startswith(date):
                continue
            hhmm = ts[11:16]
            if first is None:
                first = hhmm
            last = hhmm
            if e.get("dir") == "out":
                n_out += 1
            else:
                n_in += 1
        if n_in or n_out:
            out.append({"session": sdir.name, "in": n_in, "out": n_out,
                        "first": first, "last": last})
    return out


def day_data(date):
    """Everything that happened on one day: notes, tasks, and - inside a
    workspace - the fleet's day too. `fleet` is None standalone."""
    month = date[:7]
    day = next((d for d in parse_month(month)["days"] if d["date"] == date), None)
    root = workspace_root()
    fleet = None
    if root is not None:
        fleet = {"commits": _day_commits(root, date),
                 "desks": _day_desks(root, date)}
    return {"date": date, "month": month,
            "weekday": (day or {}).get("weekday", ""),
            "notes": (day or {}).get("notes", []),
            "fleet": fleet}


def omnius_settings():
    """Effective Omnius configuration, or {"available": false}.

    The import is LAZY AND GUARDED because this app must keep working on its
    own, outside an Omnius tree - that is a promise in its README and the
    reason nothing here imports tools\\ at module level. Inside a workspace the
    page fills in; outside one it says so and everything else still runs.

    Every value here is already secret-safe by construction: omnius_config
    reports credentials as set/NOT SET and never returns one.
    """
    try:
        sys.path.insert(0, str(BASE_DIR.parent / "tools"))
        import omnius_config as ocfg
    except Exception:
        return {"available": False,
                "why": "not running inside an Omnius workspace"}
    try:
        return {
            "available": True,
            "configDir": str(ocfg.CONFIG_DIR),
            "settings": [{"file": n, "key": k, "value": v, "source": s}
                         for n, k, v, s in ocfg.effective()],
            "secrets": [{"key": k, "state": s} for k, s in ocfg.secret_status()],
            "accounts": [{"label": l, "user": u, "key": k, "state": s}
                         for l, u, k, s in ocfg.account_status()],
            "capabilities": [{"name": n, "provider": p, "key": k, "state": s}
                             for n, p, k, s in ocfg.capability_status()],
            "problems": ocfg.problems(),
        }
    except Exception as e:
        # Config must never take a page down; that is the whole rule.
        return {"available": False, "why": f"{type(e).__name__}: {e}"}


def _kind_ok(note, kind):
    if kind in (None, "", "all"):
        return True
    if kind == "note":
        return note["type"] == "note"
    if kind == "task":
        return note["type"] == "task"
    if kind == "open":
        return note["type"] == "task" and not note["done"]
    if kind == "done":
        return note["type"] == "task" and note["done"]
    return True


def search_notes(query, kind=None):
    """Substring search across all months, optionally filtered by kind
    (note/task/open/done). Empty query + a kind lists everything of that kind.
    Newest date first."""
    q = (query or "").lower()
    results = []
    for month in list_months():
        for day in parse_month(month)["days"]:
            for note in day["notes"]:
                if q and q not in note["text"].lower():
                    continue
                if not _kind_ok(note, kind):
                    continue
                r = dict(note)
                r.update({"month": month, "date": day["date"], "weekday": day["weekday"]})
                results.append(r)
    results.sort(key=lambda r: r["date"], reverse=True)
    return results


def day_counts(month):
    return [{"date": d["date"], "weekday": d["weekday"], "count": len(d["notes"])}
            for d in parse_month(month)["days"]]


# ------------------------------------------------------------- mutations
# The only writes that are not appends. Each verifies the note's sha first
# (409 on mismatch), rewrites only the affected lines, and swaps the file in
# atomically.

def _find_notes(month, refs):
    """Locate refs [{'line','sha'}] in a fresh parse; returns [(day, note)]."""
    by_line = {}
    for d in parse_month(month)["days"]:
        for n in d["notes"]:
            by_line[n["line"]] = (d, n)
    found = {}
    for ref in refs:
        line = int(ref["line"])
        if line in found:
            continue
        item = by_line.get(line)
        if item is None:
            raise LookupError("no note at line %d — refresh and retry" % line)
        if item[1]["sha"] != str(ref["sha"]):
            raise Conflict("note changed on disk — refresh and retry")
        found[line] = item
    return [found[k] for k in sorted(found)]


def set_task_done(month, line, sha, done, now=None):
    """Toggle a task; marking done stamps the line with '✅ YYYY-MM-DD HH:MM',
    unchecking removes the stamp."""
    if not MONTH_RE.match(month):
        raise ValueError("month must look like YYYY-MM")
    path = month_path(month)
    with _lock:
        if not path.exists():
            raise LookupError("month not found")
        _, note = _find_notes(month, [{"line": line, "sha": sha}])[0]
        if note["type"] != "task":
            raise ValueError("not a task — convert it first")
        lines = _read_lines(path)
        m = TASK_RE.match(lines[note["line"]])
        text = DONE_RE.sub("", m.group(3)).rstrip()
        stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M") if done else None
        parts = ["- [x]" if done else "- [ ]", m.group(2)]
        if text:
            parts.append(text)
        if stamp:
            parts.append("✅ " + stamp)
        lines[note["line"]] = " ".join(parts)
        _write_lines(path, lines)
    return {"month": month, "line": line, "done": bool(done), "completed": stamp}


def convert_note(month, line, sha, to):
    if not MONTH_RE.match(month):
        raise ValueError("month must look like YYYY-MM")
    if to not in ("task", "note"):
        raise ValueError('to must be "task" or "note"')
    path = month_path(month)
    with _lock:
        if not path.exists():
            raise LookupError("month not found")
        _, note = _find_notes(month, [{"line": line, "sha": sha}])[0]
        lines = _read_lines(path)
        raw = lines[note["line"]]
        if to == "task":
            if note["type"] == "task":
                raise ValueError("already a task")
            m = NOTE_RE.match(raw)
            lines[note["line"]] = "- [ ] %s %s" % (m.group(1), m.group(2))
        else:
            if note["type"] != "task":
                raise ValueError("already a note")
            m = TASK_RE.match(raw)
            text = DONE_RE.sub("", m.group(3)).rstrip()
            lines[note["line"]] = ("- %s %s" % (m.group(2), text)).rstrip()
        _write_lines(path, lines)
    return {"month": month, "line": line, "type": to}


def edit_note(month, line, sha, text):
    """Replace a note's text in place, keeping its timestamp and kind.

    A task keeps its checkbox state; continuation lines are re-indented two
    spaces so the block stays one markdown list item."""
    if not MONTH_RE.match(month):
        raise ValueError("month must look like YYYY-MM")
    new_lines = clean_lines(text)
    if not new_lines:
        raise ValueError("note is empty")
    path = month_path(month)
    with _lock:
        if not path.exists():
            raise LookupError("month not found")
        _, note = _find_notes(month, [{"line": line, "sha": sha}])[0]
        lines = _read_lines(path)
        raw = lines[note["line"]]
        m = TASK_RE.match(raw)
        old_stamp = None
        if m:
            prefix = "- [%s] %s " % (m.group(1), m.group(2))
            old_stamp = DONE_RE.search(m.group(3))
        else:
            prefix = "- %s " % NOTE_RE.match(raw).group(1)
        first = (prefix + new_lines[0]).rstrip()
        # editing the text must not erase when the task was completed —
        # keep the existing stamp unless the new text brings its own
        if old_stamp and not DONE_RE.search(new_lines[0]):
            first += " ✅ " + old_stamp.group(1)
        block = [first]
        block += [("  " + ln).rstrip() for ln in new_lines[1:]]
        lines[note["line"]:note["end"]] = block
        _write_lines(path, lines)
    return {"month": month, "line": note["line"], "text": "\n".join(new_lines)}


def _prune_empty_days(lines, only_dates=None):
    """Drop day headers whose section contains nothing but blank lines.

    With only_dates given, prune just those days — a hand-made empty section
    elsewhere in the file is none of our business.
    """
    out = []
    i = 0
    while i < len(lines):
        m = DAY_RE.match(lines[i])
        if m and (only_dates is None or m.group(1) in only_dates):
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j >= len(lines) or DAY_RE.match(lines[j]):
                i = j
                continue
        out.append(lines[i])
        i += 1
    while out and out[-1].strip() == "":
        out.pop()
    return out


def _append_trash(month, entries, now):
    """entries: [(day label incl. any header annotation, block lines)]."""
    trash = NOTES_DIR / ".trash.md"
    chunk_lines = ["", "## %s deleted from %s.md" % (now.strftime("%Y-%m-%d %H:%M"), month), ""]
    last_label = None
    for label, block in entries:
        if label != last_label:
            chunk_lines.append("### " + label)
            last_label = label
        chunk_lines.extend(block)
    chunk = "\n".join(chunk_lines) + "\n"
    if not trash.exists():
        chunk = "# trash — notes deleted through the app\n" + chunk
    with open(trash, "ab") as f:
        f.write(chunk.encode("utf-8"))


def delete_notes(month, refs, now=None):
    """Delete the referenced notes; their blocks are appended to .trash.md.
    Day headers left empty are pruned; a month file left empty is removed."""
    if not MONTH_RE.match(month):
        raise ValueError("month must look like YYYY-MM")
    if not refs:
        raise ValueError("nothing to delete")
    now = now or datetime.now()
    path = month_path(month)
    with _lock:
        if not path.exists():
            raise LookupError("month not found")
        targets = _find_notes(month, refs)
        lines = _read_lines(path)
        entries = [((day["date"] + " " + day["weekday"]).strip(), lines[n["line"]:n["end"]])
                   for day, n in targets]
        affected = {day["date"] for day, _ in targets}
        for _, n in sorted(targets, key=lambda t: t[1]["line"], reverse=True):
            del lines[n["line"]:n["end"]]
        lines = _prune_empty_days(lines, affected)
        # only the month's own title line counts as ignorable — any other
        # hand-written content keeps the file alive
        meaningful = [ln for ln in lines if ln.strip() and ln.strip() != "# " + month]
        if meaningful:
            _write_lines(path, lines)
        else:
            _retry_win(path.unlink)
        _append_trash(month, entries, now)
    return len(targets)


# ------------------------------------------------------------ attachments

def save_upload(name, data, now=None):
    if not data:
        raise ValueError("file is empty")
    now = now or datetime.now()
    raw = Path(urllib.parse.unquote(name or "file")).name
    stem, ext = os.path.splitext(raw)
    stem = SAFE_NAME_RE.sub("-", stem).strip("-.")[:70].rstrip("-.") or "file"
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", ext or ""):
        ext = ""
    base = stem + ext.lower()
    month = now.strftime("%Y-%m")
    folder = files_dir() / month
    stamp = now.strftime("%d-%H%M%S")
    with _lock:
        folder.mkdir(parents=True, exist_ok=True)
        candidate = stamp + "-" + base
        i = 1
        while (folder / candidate).exists():
            candidate = "%s-%d-%s" % (stamp, i, base)
            i += 1
        (folder / candidate).write_bytes(data)
    rel = "files/%s/%s" % (month, candidate)
    ext = Path(candidate).suffix.lower()
    md = ("![%s](%s)" if ext in IMAGE_EXTS else "[%s](%s)") % (base, rel)
    return {"path": rel, "markdown": md, "bytes": len(data)}


def resolve_upload(rel):
    """Map a /files/<rel> URL to a real path, or None if unsafe/missing."""
    rel = urllib.parse.unquote(rel)
    if not rel or "\\" in rel or rel.startswith("/") or ".." in rel.split("/"):
        return None
    base = files_dir().resolve()
    target = (base / rel).resolve()
    try:
        if target.is_file() and target.is_relative_to(base):
            return target
    except OSError:
        pass
    return None


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "notes"
    protocol_version = "HTTP/1.1"

    def _send(self, status, body, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, status, message):
        self._json({"error": message}, status)

    def do_GET(self):
        try:
            self._get()
        except Exception:
            self._try_500()

    def do_POST(self):
        try:
            self._post()
        except Exception:
            self._try_500()

    def _try_500(self):
        self.close_connection = True
        try:
            self._err(500, "internal error")
        except Exception:
            pass

    def _get(self):
        url = urllib.parse.urlsplit(self.path)
        route = url.path
        if route == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif route in ("/logo.png", "/favicon.ico"):
            # The emblem, served from assets\ so there is ONE copy of it - the
            # README, the desktop icon and this page all point at the same file.
            # Missing is a 404 and nothing worse: a brand asset must never be
            # able to stop the notes app from serving notes.
            # omnius-web.png is a 256px copy: the header mark is 22px and the
            # guide heading 72px, so serving the 1.9 MB original on every page
            # load was ~24x the whole rest of the page. Falls back to the
            # original if the small one was never generated.
            assets = BASE_DIR.parent / "assets"
            if route.endswith(".ico"):
                f = assets / "omnius.ico"
            else:
                f = assets / "omnius-web.png"
                if not f.is_file():
                    f = assets / "omnius.png"
            if not f.is_file():
                return self._err(404, "not found")
            self._send(200, f.read_bytes(),
                       "image/x-icon" if f.suffix == ".ico" else "image/png",
                       {"Cache-Control": "max-age=86400"})
        elif route == "/api/day":
            params = urllib.parse.parse_qs(url.query)
            date = (params.get("date") or [""])[0]
            if not DATE_ARG_RE.match(date):
                return self._err(400, "date must look like YYYY-MM-DD")
            self._json(day_data(date))
        elif route.startswith("/files/"):
            target = resolve_upload(route[len("/files/"):])
            if target is None:
                return self._err(404, "not found")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), ctype, {
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'",
                "Cache-Control": "max-age=86400",
            })
        elif route == "/api/stats":
            self._json(stats())
        elif route == "/api/config":
            # Read-only. Editing settings from a web page is deliberately NOT
            # here: the owner's rule is that config is read remotely and
            # changed at the desk, because a bad value with nobody at the
            # keyboard cannot be undone from a phone.
            self._json(omnius_settings())
        elif route == "/api/months":
            self._json({"months": list_months()})
        elif route.startswith("/api/month/"):
            month = route[len("/api/month/"):]
            if not MONTH_RE.match(month):
                return self._err(400, "month must look like YYYY-MM")
            self._json(parse_month(month))
        elif route == "/api/search":
            params = urllib.parse.parse_qs(url.query)
            q = (params.get("q") or [""])[0].strip()
            kind = (params.get("type") or ["all"])[0]
            if kind not in KINDS:
                return self._err(400, "type must be one of: " + ", ".join(KINDS))
            if not q and kind == "all":
                return self._err(400, "missing query parameter q (or a type filter)")
            self._json({"query": q, "type": kind, "results": search_notes(q, kind)})
        elif route == "/api/days":
            params = urllib.parse.parse_qs(url.query)
            month = (params.get("month") or [datetime.now().strftime("%Y-%m")])[0]
            if not MONTH_RE.match(month):
                return self._err(400, "month must look like YYYY-MM")
            self._json({"month": month, "days": day_counts(month)})
        else:
            self._err(404, "not found")

    def _read_body(self, cap):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("bad Content-Length")
        if length <= 0:
            raise ValueError("empty request body")
        if length > cap:
            raise ValueError("body too large")
        return self.rfile.read(length)

    def _post(self):
        url = urllib.parse.urlsplit(self.path)
        route = url.path
        routes = ("/api/note", "/api/task", "/api/convert", "/api/edit",
                  "/api/delete", "/api/upload")
        # an early error leaves the request body unread, which would poison a
        # kept-alive connection — close it instead
        if route not in routes:
            self.close_connection = True
            return self._err(404, "not found")

        if route == "/api/upload":
            return self._upload(url)

        try:
            body = self._read_body(MAX_NOTE_BYTES)
        except ValueError as e:
            self.close_connection = True
            return self._err(413 if "large" in str(e) else 400, str(e))
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._err(400, "body must be valid JSON")
        if not isinstance(payload, dict):
            return self._err(400, "body must be a JSON object")

        try:
            if route == "/api/note":
                return self._api_note(payload)
            if route == "/api/task":
                return self._api_task(payload)
            if route == "/api/convert":
                return self._api_convert(payload)
            if route == "/api/edit":
                return self._api_edit(payload)
            if route == "/api/delete":
                return self._api_delete(payload)
        except Conflict as e:
            return self._err(409, str(e))
        except LookupError as e:
            return self._err(404, str(e))
        except ValueError as e:
            return self._err(400, str(e))

    def _api_note(self, payload):
        if not isinstance(payload.get("text"), str):
            return self._err(400, 'body must be {"text": "..."}')
        note = append_note(payload["text"], task=bool(payload.get("task")))
        self._json({"ok": True, "note": note}, 201)

    @staticmethod
    def _ref(payload):
        month = payload.get("month")
        if not isinstance(month, str) or not MONTH_RE.match(month):
            raise ValueError("month must look like YYYY-MM")
        try:
            line = int(payload.get("line"))
        except (TypeError, ValueError):
            raise ValueError("line must be an integer")
        if line < 0:
            raise ValueError("line must be >= 0")
        sha = payload.get("sha")
        if not isinstance(sha, str) or not sha:
            raise ValueError("sha is required")
        return month, line, sha

    def _api_task(self, payload):
        month, line, sha = self._ref(payload)
        if not isinstance(payload.get("done"), bool):
            raise ValueError("done must be true or false")
        self._json({"ok": True, **set_task_done(month, line, sha, payload["done"])})

    def _api_convert(self, payload):
        month, line, sha = self._ref(payload)
        self._json({"ok": True, **convert_note(month, line, sha, payload.get("to"))})

    def _api_edit(self, payload):
        month, line, sha = self._ref(payload)
        if not isinstance(payload.get("text"), str):
            raise ValueError('body must include "text"')
        self._json({"ok": True, **edit_note(month, line, sha, payload["text"])})

    def _api_delete(self, payload):
        month = payload.get("month")
        if not isinstance(month, str) or not MONTH_RE.match(month):
            raise ValueError("month must look like YYYY-MM")
        notes = payload.get("notes")
        if not isinstance(notes, list) or not notes or len(notes) > 500:
            raise ValueError('notes must be a list of {"line", "sha"} (1-500 items)')
        refs = []
        for item in notes:
            if not isinstance(item, dict):
                raise ValueError("each item needs line and sha")
            try:
                refs.append({"line": int(item.get("line")), "sha": str(item.get("sha") or "")})
            except (TypeError, ValueError):
                raise ValueError("line must be an integer")
            if refs[-1]["line"] < 0 or not refs[-1]["sha"]:
                raise ValueError("each item needs line >= 0 and sha")
        deleted = delete_notes(month, refs)
        self._json({"ok": True, "deleted": deleted})

    def _upload(self, url):
        params = urllib.parse.parse_qs(url.query)
        name = (params.get("name") or [""])[0]
        try:
            data = self._read_body(MAX_UPLOAD_BYTES)
        except ValueError as e:
            self.close_connection = True
            return self._err(413 if "large" in str(e) else 400, str(e))
        try:
            result = save_upload(name, data)
        except ValueError as e:
            return self._err(400, str(e))
        self._json({"ok": True, **result}, 201)

    def log_message(self, fmt, *args):
        pass


# ---------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#16130f">
<title>Omnius</title>
<link rel="icon" href="/favicon.ico">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 16 16%22><text y=%2213%22 font-size=%2213%22>&#9998;</text></svg>">
<style>
:root {
  color-scheme: dark;
  --paper: #1e1e1e;
  --raise: #252526;
  --hover: #2a2d2e;
  --ink: #d4d4d4;
  --bright: #eeeeee;
  --faint: #858585;
  --line: #3c3c3c;
  --line-soft: #333333;
  --accent: #569cd6;
  --link: #3794ff;
  --focus: #007fd4;
  --btn: #0e639c;
  --btn-hover: #1177bb;
  --danger: #f48771;
  --ok: #89d185;
  --codebg: #2f2f2f;
  --codetext: #d7ba7d;
  --mention: #9cdcfe;
  --tagc: #4ec9b0;
  --field: #313131;
  --gold: #c8963e;          /* api.OMNIUS_GOLD - one brand colour, two surfaces */
  --gold-bright: #e0b661;
  --gold-dim: #6b5326;
  --nav-h: 3rem;
  --mono: "Cascadia Code", "Cascadia Mono", Consolas, ui-monospace, monospace;
}
* { box-sizing: border-box; accent-color: var(--focus); }
[hidden] { display: none !important; }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #424242; border-radius: 5px; }
::-webkit-scrollbar-thumb:hover { background: #525252; }
body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: "Segoe UI", "Noto Sans", system-ui, -apple-system, sans-serif;
  font-size: 15.5px;
  line-height: 1.55;
}
.wrap { max-width: 72rem; margin: 0 auto; padding: 0 1.5rem; }
.top {
  position: sticky; top: 0; z-index: 20;
  background: var(--paper);
  border-bottom: 1px solid var(--line-soft);
}
.nav { display: flex; align-items: center; gap: 1.1rem; height: var(--nav-h); }
/* The emblem's own gold (0xC8963E), so the web and the Discord embeds read as
   one system rather than two products that happen to share a name. */
.brand {
  display: flex; align-items: center; gap: .5rem;
  color: var(--gold); text-decoration: none;
  font-size: .95rem; letter-spacing: .18em; text-transform: lowercase;
}
.brand .mark { width: 22px; height: 22px; display: block; }
.brand span { opacity: .92; }
.brand:hover { color: var(--gold-bright); }

.tabs { display: flex; align-self: stretch; }
.tabs a {
  display: flex; align-items: center;
  padding: 0 .95rem;
  color: var(--faint); text-decoration: none; font-size: .86rem;
  border-bottom: 2px solid transparent;
}
.tabs a.on { color: #fff; border-bottom-color: var(--focus); }
.tabs a:hover { color: var(--bright); }
.pill {
  margin-left: auto;
  font-family: var(--mono); font-size: .68rem;
  color: #fff; background: var(--btn);
  border-radius: 99px; padding: .14rem .6rem;
}
#err {
  position: fixed; right: 1.1rem; bottom: 1.1rem; z-index: 50;
  max-width: 24rem;
  background: var(--raise);
  border: 1px solid var(--line);
  border-left: 3px solid var(--danger);
  border-radius: 4px;
  padding: .5rem .8rem;
  font-size: .84rem;
  box-shadow: 0 6px 16px rgba(0, 0, 0, .45);
}
#err.ok { border-left-color: var(--ok); }
main { padding: 1.2rem 0 6rem; }

.panel { background: var(--raise); border: 1px solid var(--line-soft); border-radius: 6px; }
.panel:focus-within { border-color: var(--focus); }
.panel.dropping { border-color: var(--focus); border-style: dashed; }

.quick { display: flex; align-items: flex-end; gap: .8rem; padding: .6rem .8rem; }
#qinput {
  flex: 1; font: inherit; color: inherit; background: transparent;
  border: 0; outline: none; resize: none;
  min-height: 1.6rem; overflow-y: auto;
}
.opt {
  font-family: var(--mono); font-size: .72rem; color: var(--faint);
  display: inline-flex; align-items: center; gap: .35rem;
  cursor: pointer; white-space: nowrap;
}

h2.sect, h2.day {
  position: sticky; top: calc(var(--nav-h) + 1px); z-index: 10;
  margin: 1.5rem 0 .45rem;
  padding: .3rem 0;
  background: var(--paper);
  display: flex; align-items: baseline; gap: .6rem;
  font-family: var(--mono); font-size: .72rem; font-weight: 600;
  letter-spacing: .08em; text-transform: uppercase;
  color: #9a9a9a;
}
h2.sect .n, h2.day .n { font-weight: 400; letter-spacing: 0; text-transform: none; color: var(--faint); }
h2.day { cursor: pointer; }
h2.day:hover { color: var(--accent); }

.bar { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; }
.months { display: flex; align-items: center; gap: .15rem; }
.months button {
  font: inherit; font-size: 1rem; line-height: 1;
  color: var(--faint); background: transparent;
  border: 0; cursor: pointer; padding: .2rem .4rem; border-radius: 4px;
}
.months button:hover:not(:disabled) { color: #fff; background: var(--hover); }
.months button:disabled { opacity: .3; cursor: default; }
#monthSel, #typeSel, #sortSel, #dayPick, #search {
  font-family: inherit; font-size: .8rem; color: var(--ink);
  background: var(--field);
  border: 1px solid var(--line); border-radius: 4px;
  padding: .26rem .45rem;
}
#monthSel { max-width: 10.5rem; cursor: pointer; }
#typeSel, #sortSel { cursor: pointer; color: #bbbbbb; }
#dayPick { font-family: var(--mono); font-size: .72rem; color: #bbbbbb; }
#search { margin-left: auto; width: 14rem; max-width: 100%; }
#monthSel:focus, #typeSel:focus, #sortSel:focus, #dayPick:focus, #search:focus {
  outline: 1px solid var(--focus); outline-offset: -1px; border-color: var(--focus);
}
button.ghost, button.danger {
  font-family: inherit; font-size: .78rem;
  background: var(--field); border: 1px solid var(--line); border-radius: 4px;
  color: var(--ink); padding: .26rem .6rem; cursor: pointer;
}
button.ghost:hover { background: var(--line); }
button.ghost.on { background: var(--btn); border-color: var(--btn); color: #fff; }
button.danger { color: var(--danger); border-color: rgba(244, 135, 113, .55); background: transparent; }
button.danger:hover:not(:disabled) { background: rgba(244, 135, 113, .12); }
button.danger:disabled { opacity: .4; cursor: default; }
.chip {
  display: inline-flex; align-items: center; gap: .45rem;
  margin-top: .55rem;
  font-family: var(--mono); font-size: .7rem;
  color: var(--accent); background: rgba(86, 156, 214, .13);
  border-radius: 4px; padding: .16rem .5rem;
}
.chip button { border: 0; background: none; color: inherit; cursor: pointer; font-size: .85rem; padding: 0; }

.note {
  display: flex; gap: .7rem; align-items: flex-start;
  background: var(--raise);
  border: 1px solid var(--line-soft);
  border-radius: 6px;
  padding: .5rem .75rem;
  margin: .4rem 0;
}
.note:hover { background: var(--hover); border-color: #4a4a4a; }
.note .selbox { display: none; margin-top: .35rem; }
body.selecting #view-notes .note .selbox { display: inline-block; }
.note .time {
  flex: none; width: 2.8rem; text-align: right;
  font-family: var(--mono); font-size: .7rem;
  color: var(--faint); padding-top: .3rem;
}
.note .tick { margin-top: .32rem; cursor: pointer; }
.note .body { flex: 1; min-width: 0; overflow-wrap: break-word; }
.note.done { opacity: .72; }
.note.done .body .md { color: var(--faint); text-decoration: line-through; text-decoration-color: rgba(133, 133, 133, .55); }
.donestamp { font-family: var(--mono); font-size: .66rem; color: var(--ok); margin-top: .12rem; }
.note .acts { display: flex; gap: .3rem; opacity: 0; transition: opacity .12s; padding-top: .2rem; }
.note:hover .acts, .note:focus-within .acts { opacity: 1; }
.note .acts button {
  font-family: var(--mono); font-size: .66rem;
  background: transparent; border: 1px solid var(--line); border-radius: 4px;
  color: var(--faint); padding: .12rem .4rem; cursor: pointer;
}
.note .acts button:hover { color: #fff; border-color: var(--focus); background: var(--btn); }
.note .acts button.del:hover { color: #fff; border-color: var(--danger); background: rgba(244, 135, 113, .25); }

.md code {
  font-family: var(--mono); font-size: .84em;
  color: var(--codetext);
  background: var(--codebg); border-radius: 3px; padding: .08em .35em;
}
.md a { color: var(--link); text-decoration: none; }
.md a:hover { text-decoration: underline; }
.md strong { color: var(--bright); }
.md img {
  display: block; max-width: 100%; max-height: 420px;
  border: 1px solid var(--line); border-radius: 6px; margin: .35rem 0;
}
.md ul, .md ol { margin: .15rem 0 .15rem 1.4rem; padding: 0; }
.md li { margin: .12rem 0; }
.md li.task-li { list-style: none; margin-left: -1.15rem; }
.md li.task-li input { margin-right: .4rem; vertical-align: -.1em; }
.md p { margin: .35rem 0; }
.md > :first-child { margin-top: 0; }
.md > :last-child { margin-bottom: 0; }
.md h1, .md h2, .md h3, .md h4, .md h5, .md h6 {
  margin: .75rem 0 .35rem; line-height: 1.3;
  color: var(--bright); font-weight: 600;
}
.md h1 { font-size: 1.28em; border-bottom: 1px solid var(--line-soft); padding-bottom: .15rem; }
.md h2 { font-size: 1.16em; }
.md h3 { font-size: 1.07em; }
.md h4, .md h5, .md h6 { font-size: 1em; }
.md blockquote {
  margin: .45rem 0; padding: .3rem .9rem;
  border-left: 3px solid var(--accent);
  background: rgba(86, 156, 214, .07);
  border-radius: 0 4px 4px 0;
  color: #b8b8b8;
}
.md blockquote > :first-child { margin-top: 0; }
.md blockquote > :last-child { margin-bottom: 0; }
.md hr { border: 0; border-top: 1px solid var(--line); margin: .8rem 0; }
.md del { color: var(--faint); }
.tblwrap { overflow-x: auto; margin: .45rem 0; }
.md table { border-collapse: collapse; font-size: .93em; }
.md th, .md td { border: 1px solid var(--line); padding: .28rem .7rem; text-align: left; }
.md th { background: #2d2d2d; color: var(--bright); font-weight: 600; }
.md tbody tr:nth-child(even) { background: rgba(255, 255, 255, .025); }
.cbwrap { position: relative; margin: .45rem 0; }
.md pre {
  margin: 0; padding: .6rem .8rem;
  background: #1a1a1a;
  border: 1px solid var(--line-soft); border-radius: 6px;
  overflow-x: auto;
  font-family: var(--mono); font-size: .84em; line-height: 1.55;
}
.md pre code {
  background: transparent; padding: 0; border-radius: 0;
  color: var(--ink); font-size: 1em;
}
.cblang {
  position: absolute; top: .38rem; right: 3.6rem;
  font-family: var(--mono); font-size: .64rem; color: var(--faint);
}
.copybtn {
  position: absolute; top: .35rem; right: .45rem;
  font-family: var(--mono); font-size: .66rem;
  color: var(--faint); background: var(--raise);
  border: 1px solid var(--line); border-radius: 4px;
  padding: .12rem .45rem; cursor: pointer;
  opacity: 0; transition: opacity .12s;
}
.cbwrap:hover .copybtn, .copybtn:focus-visible { opacity: 1; }
.copybtn:hover { color: #fff; border-color: var(--focus); }
.hl-k { color: #569cd6; }
.hl-s { color: #ce9178; }
.hl-c { color: #6a9955; font-style: italic; }
.hl-n { color: #b5cea8; }
mark { background: rgba(255, 214, 64, .32); color: inherit; border-radius: 2px; padding: 0 .06em; }
.editbox {
  display: block; width: 100%;
  font: inherit; color: inherit;
  background: var(--field);
  border: 1px solid var(--focus); border-radius: 4px;
  padding: .45rem .6rem;
  min-height: 4.5rem; resize: vertical;
  outline: none;
}
.editacts { display: flex; align-items: center; gap: .5rem; margin-top: .4rem; }
.editacts .hint { padding: 0; }
.mention { color: var(--mention); font-weight: 600; }
.tag { color: var(--tagc); background: rgba(78, 201, 176, .12); border-radius: 3px; padding: 0 .25em; font-weight: 600; }
.empty { margin: 2.2rem 0; text-align: center; color: var(--faint); font-style: italic; }
.dim { color: var(--faint); font-style: italic; }

.composer { margin-top: .2rem; }
.ctabs { display: flex; gap: .15rem; border-bottom: 1px solid var(--line-soft); padding: .45rem .6rem 0; }
.ctabs button {
  font-family: inherit; font-size: .78rem;
  background: transparent; border: 1px solid transparent; border-bottom: 0;
  border-radius: 4px 4px 0 0;
  color: var(--faint); padding: .32rem .9rem; cursor: pointer;
}
.ctabs button.on {
  color: #fff; background: var(--paper);
  border-color: var(--line-soft);
  transform: translateY(1px);
}
#input {
  display: block; width: 100%;
  font: inherit; color: inherit; background: transparent;
  border: 0; outline: none;
  padding: .8rem .9rem;
  min-height: 240px; resize: vertical;
}
.pv { padding: .8rem .9rem; min-height: 240px; }
.cbar {
  display: flex; align-items: center; gap: .35rem; flex-wrap: wrap;
  border-top: 1px solid var(--line-soft);
  padding: .5rem .6rem;
}
.cbar .fmt { display: flex; gap: .3rem; }
.cbar .fmt button {
  font-family: var(--mono); font-size: .72rem;
  color: var(--ink); background: var(--field);
  border: 1px solid var(--line); border-radius: 4px;
  padding: .2rem .55rem; cursor: pointer;
}
.cbar .fmt button:hover { background: var(--line); }
.cbar .save { margin-left: auto; display: flex; align-items: center; gap: .8rem; }
button.primary {
  font-family: inherit; font-size: .82rem; font-weight: 600;
  color: #fff; background: var(--btn);
  border: 1px solid var(--btn); border-radius: 4px;
  padding: .32rem 1rem; cursor: pointer;
}
button.primary:hover { background: var(--btn-hover); border-color: var(--btn-hover); }
.hint { font-family: var(--mono); font-size: .68rem; color: var(--faint); padding: .4rem .2rem 0; }

/* ---- the day view's fleet rows + settings ---- */
.daynav { margin-top: 1.2rem; }
.daynav input[type="date"] { font-family: var(--mono); }
.frow { display: grid; grid-template-columns: 4.4rem minmax(7rem, 12rem) 1fr;
        gap: .7rem; padding: .34rem .2rem; align-items: baseline;
        border-bottom: 1px solid var(--line-soft); font-size: .84rem; }
.frow code { font-family: var(--mono); font-size: .72rem; color: var(--faint); }
.frow b { color: var(--gold); font-weight: 600; font-size: .78rem; }
.frow span { color: var(--ink); overflow-wrap: anywhere; }
.rows { font-family: var(--mono); font-size: .74rem; }
.row2 { display: grid; grid-template-columns: minmax(9rem, 14rem) 1fr auto; gap: .7rem;
        padding: .38rem .2rem; border-bottom: 1px solid var(--line-soft); align-items: baseline; }
.row2 code { color: var(--mention); }
.row2 .val { color: var(--bright); word-break: break-all; }
.row2 .src { color: var(--faint); font-size: .66rem; white-space: nowrap; }
.pillv { display: inline-block; padding: .05rem .45rem; border-radius: 999px; font-size: .66rem; }
.pillv.on { background: #1e3a28; color: var(--ok); }
.pillv.off { background: #3a2420; color: var(--danger); }
.pillv.idle { background: var(--line-soft); color: var(--faint); }
.warnbox { border: 1px solid var(--danger); border-radius: 6px; padding: .6rem .8rem; margin: .6rem 0;
           color: var(--ink); font-size: .8rem; }
</style>
</head>
<body>
<header class="top">
  <div class="wrap nav">
    <a class="brand" href="#/" title="Omnius"><img src="/logo.png" alt="" class="mark"><span>omnius</span></a>
    <nav class="tabs">
      <a href="#/" id="tab-dash">Today</a>
      <a href="#/notes" id="tab-notes">Notes</a>
      <a href="#/new" id="tab-new">Write</a>
      <a href="#/settings" id="tab-settings">Settings</a>
    </nav>
    <span id="pill" class="pill" hidden></span>
  </div>
  <div class="wrap"><div id="err" hidden></div></div>
</header>
<main class="wrap">

<section id="view-dash" hidden>
  <div class="quick panel" id="quickPanel">
    <textarea id="qinput" rows="1"
      placeholder="Quick note &mdash; Enter saves, Shift+Enter for a new line"></textarea>
    <label class="opt"><input type="checkbox" id="qtask"> task</label>
  </div>
  <h2 class="sect">Open tasks <span class="n" id="openCount"></span></h2>
  <div id="dashTasks"></div>
  <div class="bar daynav">
    <button id="dayPrev" title="Previous day">&lsaquo;</button>
    <input type="date" id="dayInput" title="Jump to any day">
    <button id="dayNext" title="Next day">&rsaquo;</button>
    <button id="dayHome" class="ghost" hidden>back to today</button>
  </div>
  <h2 class="sect" id="dayHead">Today <span class="n" id="todayLabel"></span></h2>
  <div id="dashToday"></div>
  <div id="dashFleet"></div>
</section>

<section id="view-notes" hidden>
  <div class="bar">
    <nav class="months">
      <button id="prev" title="Older month">&lsaquo;</button>
      <select id="monthSel" title="Jump to month"></select>
      <button id="next" title="Newer month">&rsaquo;</button>
    </nav>
    <input type="date" id="dayPick" title="Show one day only">
    <select id="typeSel" title="Filter by kind">
      <option value="all">everything</option>
      <option value="note">notes</option>
      <option value="task">tasks</option>
      <option value="open">open tasks</option>
      <option value="done">done tasks</option>
    </select>
    <select id="sortSel" title="Sort order">
      <option value="desc">newest first</option>
      <option value="asc">oldest first</option>
    </select>
    <input type="search" id="search" placeholder="Search all notes&hellip;"
           title="Search every month &mdash; Esc clears">
    <button id="selectBtn" class="ghost" title="Select notes for bulk delete">select</button>
    <button id="bulkDelBtn" class="danger" hidden>delete 0</button>
  </div>
  <div id="chip" class="chip" hidden></div>
  <div id="list"></div>
</section>

<section id="view-new" hidden>
  <div class="panel composer" id="composerPanel">
    <div class="ctabs">
      <button id="tabWrite" class="on">Write</button>
      <button id="tabPreview">Preview</button>
    </div>
    <textarea id="input"
      placeholder="Write&hellip; markdown supported. Paste or drop images and files, GitHub-style."></textarea>
    <div id="preview" class="md pv" hidden></div>
    <div class="cbar">
      <div class="fmt">
        <button data-fmt="bold" title="Bold &mdash; wraps selection in **"><strong>B</strong></button>
        <button data-fmt="italic" title="Italic &mdash; wraps selection in *"><em>I</em></button>
        <button data-fmt="strike" title="Strikethrough &mdash; wraps selection in ~~"><s>S</s></button>
        <button data-fmt="code" title="Inline code &mdash; wraps selection in backticks">`</button>
        <button data-fmt="codeblock" title="Code block &mdash; wraps selection in ``` fences">```</button>
        <button data-fmt="quote" title="Quote &mdash; prefixes lines with &gt;">&raquo;</button>
        <button data-fmt="list" title="List &mdash; prefixes lines with -">&ndash;</button>
        <button id="attachBtn" title="Attach files">attach</button>
        <input type="file" id="fileInput" multiple hidden>
      </div>
      <div class="save">
        <label class="opt"><input type="checkbox" id="taskToggle"> save as task</label>
        <button id="saveBtn" class="primary" title="Ctrl+Enter">Save</button>
      </div>
    </div>
  </div>
  <div class="hint">Enter = new line &middot; Ctrl+Enter = save &middot; paste or drop attachments anywhere in the box</div>
</section>

<section id="view-settings" hidden>
  <div id="cfgState"></div>
  <h2 class="sect">Settings <span class="n" id="cfgDir"></span></h2>
  <div id="cfgRows"></div>
  <h2 class="sect">Secrets</h2>
  <p class="hint">Values are never shown here, or anywhere else &mdash; only whether the key has one. They live in the root <code>.env</code>.</p>
  <div id="cfgSecrets"></div>
  <div id="cfgAccountsWrap" hidden>
    <h2 class="sect">Mail accounts</h2>
    <div id="cfgAccounts"></div>
  </div>
  <div id="cfgCapsWrap" hidden>
    <h2 class="sect">AI capabilities</h2>
    <div id="cfgCaps"></div>
  </div>
  <div id="cfgProblemsWrap" hidden>
    <h2 class="sect">Problems</h2>
    <div id="cfgProblems"></div>
  </div>
  <p class="hint" id="cfgFoot"></p>
  <h2 class="sect">Guide</h2>
  <p class="hint">How Omnius works, from install to routines:
    <a href="https://github.com/timoinglin/omnius-agent/blob/main/GETTING-STARTED.md"
       target="_blank" rel="noopener">GETTING-STARTED.md</a>
    &mdash; the same file lives at the top of this workspace.</p>
</section>

</main>
<script>
const $ = s => document.querySelector(s);
const qinput = $("#qinput"), qtask = $("#qtask"), quickPanel = $("#quickPanel");
const listEl = $("#list"), monthSel = $("#monthSel"), searchBox = $("#search"),
      dayPick = $("#dayPick"), typeSel = $("#typeSel"), sortSel = $("#sortSel"),
      chip = $("#chip"), errEl = $("#err"), pill = $("#pill");
const dashTasks = $("#dashTasks"), dashToday = $("#dashToday");
const input = $("#input"), preview = $("#preview"), composerPanel = $("#composerPanel");
const MONTH_NAMES = ["January","February","March","April","May","June",
                     "July","August","September","October","November","December"];
const pad = n => String(n).padStart(2, "0");
const todayStr = () => { const d = new Date(); return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); };
const thisMonth = () => todayStr().slice(0, 7);
const monthLabel = m => MONTH_NAMES[+m.slice(5) - 1] + " " + m.slice(0, 4);
const state = { view: "dash", months: [], month: thisMonth(), day: null, q: "",
                dashDay: null,   /* the Today tab's day; null = actually today */
                type: "all", selecting: false,
                sort: localStorage.getItem("notes-sort") === "asc" ? "asc" : "desc" };
const selected = new Map();
let searchTimer = null, errTimer = null, reqSeq = 0, saving = false;

async function api(path, opts) {
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error((data && data.error) || ("HTTP " + r.status));
  return data;
}
const mutate = (path, payload) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload)
});

function esc(s) {
  return s.replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
}

/* --------------------------------------------------------- markdown
   Full GFM-flavored renderer: fenced code (with light syntax coloring),
   pipe tables, blockquotes, headings, hr, nested ordered/unordered/task
   lists, bold/italic/strikethrough, links, images, autolinks. All output
   text passes through esc() — block structure is decided on raw lines,
   inline content is escaped before any HTML is built. */

function safeUrl(u) {
  if (/^https?:\/\//i.test(u)) return u;
  if (u.indexOf("files/") === 0) return "/" + u;
  if (u.indexOf("//") === 0 || /^[a-zA-Z][\w+.-]*:/.test(u)) return null;
  return u;                       // relative path — rendered like GitHub does
}

function inlineMD(raw) {
  let s = esc(raw);
  const toks = [];
  const stash = html => "\x00" + (toks.push(html) - 1) + "\x00";
  s = s.replace(/`([^`\n]+)`/g, (m, c) => stash("<code>" + c + "</code>"));
  s = s.replace(/!\[([^\]\n]*)\]\(([^()\s]+)\)/g, (m, alt, u) => {
    const h = safeUrl(u);
    return h ? stash('<img src="' + h + '" alt="' + alt + '" loading="lazy">') : m;
  });
  s = s.replace(/\[([^\]\n]+)\]\(([^()\s]+)\)/g, (m, t, u) => {
    const h = safeUrl(u);
    return h ? stash('<a href="' + h + '" target="_blank" rel="noopener">' + t + "</a>") : m;
  });
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s)]+?)(?=[).,;:!?]*(?:\s|$))/g,
      (m, pre, u) => pre + stash('<a href="' + u + '" target="_blank" rel="noopener">' + u + "</a>"));
  s = s.replace(/\*\*([^\n]+?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^\w_])__([^\n]+?)__(?![\w_])/g, "$1<strong>$2</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^\w_])_([^_\n]+)_(?![\w_])/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  s = s.replace(/(^|[\s(])@([\w.-]+)/g, '$1<span class="mention">@$2</span>');
  s = s.replace(/(^|[\s(])#([\w-]+)/g, '$1<span class="tag">#$2</span>');
  while (/\x00\d+\x00/.test(s)) s = s.replace(/\x00(\d+)\x00/g, (m, i) => toks[+i]);
  return s;
}

const inlineLines = t => t.split("\n").map(inlineMD).join("<br>");

const HL_KW = new Set(("def class return if elif else for while import from as with try except finally lambda yield pass break continue raise in is not and or global nonlocal assert del None True False self " +
  "function const let var new delete typeof instanceof this async await export default extends super switch case do throw catch void static get set of null undefined true false " +
  "fn mut impl struct enum match pub use mod crate trait where loop ref move dyn " +
  "public private protected interface abstract final int float double bool boolean string char long short byte record sealed namespace using package").split(" "));
const HASH_LANGS = /^(py|python|sh|bash|shell|zsh|rb|ruby|yaml|yml|toml|ini|conf|cfg|r|pl|perl|makefile|dockerfile|ps1|powershell|txt)$/;
const SLASH_LANGS = /^(js|jsx|ts|tsx|javascript|typescript|c|h|cpp|cc|hpp|cs|csharp|java|go|rs|rust|php|swift|kt|kotlin|scala|dart|css|scss|less|json5|jsonc|sql)$/;

function highlightCode(code, lang) {
  let s = esc(code);
  const l = (lang || "").toLowerCase();
  const toks = [];
  const st = (cls, m) => "\x01" + (toks.push('<span class="hl-' + cls + '">' + m + "</span>") - 1) + "\x01";
  if (!HASH_LANGS.test(l)) {
    s = s.replace(/\/\*[\s\S]*?\*\//g, m => st("c", m));
  }
  s = s.replace(/&quot;(?:[^&\n]|&(?!quot;))*?&quot;|&#39;(?:[^&\n]|&(?!#39;))*?&#39;|`[^`]*`/g,
      m => st("s", m));
  if (!HASH_LANGS.test(l)) {
    s = s.replace(/(^|[^:\\])(\/\/[^\n]*)/gm, (m, a, b) => a + st("c", b));
  }
  if (!SLASH_LANGS.test(l)) {
    s = s.replace(/(^|[\s({[;,])(#(?![0-9a-fA-F]{3,8}\b)[^\n]*)/gm, (m, a, b) => a + st("c", b));
  }
  s = s.replace(/(?<![\w\x01.])(0x[0-9a-fA-F]+|\d+(?:\.\d+)?)/g, m => st("n", m));
  s = s.replace(/\b[A-Za-z_]\w*\b/g, m => HL_KW.has(m) ? st("k", m) : m);
  while (/\x01\d+\x01/.test(s)) s = s.replace(/\x01(\d+)\x01/g, (m, i) => toks[+i]);
  return s;
}

const isTblSep = ln => ln.indexOf("|") !== -1 && /^[\s|:-]+$/.test(ln) && /-{2,}/.test(ln);

function listHTML(items, pos, ind) {
  const ord = items[pos.i].ord;
  let out = ord ? "<ol>" : "<ul>";
  while (pos.i < items.length && items[pos.i].ind >= ind) {
    if (items[pos.i].ind > ind) {
      const sub = listHTML(items, pos, items[pos.i].ind);
      out = /<\/li>$/.test(out)
        ? out.replace(/<\/li>$/, sub + "</li>")
        : out + "<li>" + sub + "</li>";
      continue;
    }
    const it = items[pos.i++];
    out += "<li" + (it.task ? ' class="task-li"' : "") + ">" +
      (it.task ? '<input type="checkbox" disabled' + (it.task === "x" ? " checked" : "") + ">" : "") +
      inlineLines(it.text) + "</li>";
  }
  return out + (ord ? "</ol>" : "</ul>");
}

function renderBlocks(lines) {
  let html = "";
  const para = [];
  const flushP = () => {
    if (para.length) {
      html += "<p>" + para.map(inlineMD).join("<br>") + "</p>";
      para.length = 0;
    }
  };
  let i = 0;
  while (i < lines.length) {
    const ln = lines[i];
    let m = ln.match(/^\s{0,3}```(\S*)\s*$/);
    if (m) {
      flushP();
      const lang = m[1];
      const buf = [];
      i++;
      while (i < lines.length && !/^\s{0,3}```\s*$/.test(lines[i])) buf.push(lines[i++]);
      i++;
      html += '<div class="cbwrap">' +
        (lang ? '<span class="cblang">' + esc(lang) + "</span>" : "") +
        '<button class="copybtn" type="button" title="Copy code">copy</button>' +
        "<pre><code>" + highlightCode(buf.join("\n"), lang) + "</code></pre></div>";
      continue;
    }
    if (ln.indexOf("|") !== -1 && i + 1 < lines.length && isTblSep(lines[i + 1])) {
      flushP();
      const splitRow = r => {
        r = r.trim();
        if (r[0] === "|") r = r.slice(1);
        if (r[r.length - 1] === "|") r = r.slice(0, -1);
        return r.split("|").map(c => c.trim());
      };
      const head = splitRow(ln);
      const aligns = splitRow(lines[i + 1]).map(c =>
        c[0] === ":" && c[c.length - 1] === ":" ? "center" :
        c[c.length - 1] === ":" ? "right" : "");
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].indexOf("|") !== -1) rows.push(splitRow(lines[i++]));
      const row = (tag, cells) => "<tr>" + cells.map((c, j) =>
        "<" + tag + (aligns[j] ? ' style="text-align:' + aligns[j] + '"' : "") + ">" +
        inlineMD(c) + "</" + tag + ">").join("") + "</tr>";
      html += '<div class="tblwrap"><table><thead>' + row("th", head) + "</thead><tbody>" +
        rows.map(r => row("td", r)).join("") + "</tbody></table></div>";
      continue;
    }
    m = ln.match(/^(#{1,6})\s+(.*)$/);
    if (m) {
      flushP();
      html += "<h" + m[1].length + ">" + inlineMD(m[2]) + "</h" + m[1].length + ">";
      i++;
      continue;
    }
    if (/^\s{0,3}(-{3,}|\*{3,}|_{3,})\s*$/.test(ln)) {
      flushP();
      html += "<hr>";
      i++;
      continue;
    }
    if (/^\s{0,3}>/.test(ln)) {
      flushP();
      const buf = [];
      while (i < lines.length && /^\s{0,3}>/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s{0,3}> ?/, ""));
        i++;
      }
      html += "<blockquote>" + renderBlocks(buf) + "</blockquote>";
      continue;
    }
    m = ln.match(/^(\s*)([-*+]|\d{1,9}[.)])\s+\S/);
    if (m) {
      flushP();
      const items = [];
      while (i < lines.length) {
        const lm = lines[i].match(/^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/);
        if (lm) {
          const tk = lm[3].match(/^\[([ xX])\]\s+(.*)$/);
          items.push({ ind: lm[1].length, ord: /^\d/.test(lm[2]),
                       task: tk ? (tk[1] === " " ? "o" : "x") : "",
                       text: tk ? tk[2] : lm[3] });
          i++;
        } else if (lines[i].trim() && items.length &&
                   lines[i].search(/\S/) > items[items.length - 1].ind) {
          items[items.length - 1].text += "\n" + lines[i].trim();
          i++;
        } else break;
      }
      html += listHTML(items, { i: 0 }, items[0].ind);
      continue;
    }
    if (!ln.trim()) { flushP(); i++; continue; }
    para.push(ln);
    i++;
  }
  flushP();
  return html;
}

function renderMD(text) {
  return renderBlocks(text.replace(/[\x00\x01]/g, "�").split("\n"));
}

const noteKey = (month, n) => month + ":" + n.line + ":" + n.sha;
const rawTexts = new Map();   // key -> raw markdown, for in-place editing

function noteRow(n, month) {
  const done = n.type === "task" && n.done;
  rawTexts.set(noteKey(month, n), n.text);
  return '<div class="note' + (done ? " done" : "") + '"' +
    ' data-month="' + month + '" data-line="' + n.line + '" data-sha="' + n.sha + '"' +
    ' data-type="' + n.type + '">' +
    '<input type="checkbox" class="selbox" title="Select"' +
      (selected.has(noteKey(month, n)) ? " checked" : "") + ">" +
    '<span class="time">' + n.time + "</span>" +
    (n.type === "task"
      ? '<input type="checkbox" class="tick" title="Mark done"' + (n.done ? " checked" : "") + ">"
      : "") +
    '<div class="body"><div class="md">' + renderMD(n.text) + "</div>" +
    (n.type === "task" && n.done && n.completed
      ? '<div class="donestamp">&#10003; completed ' + n.completed + "</div>"
      : "") +
    "</div>" +
    '<span class="acts">' +
      '<button class="ed" title="Edit in place">edit</button>' +
      '<button class="cv" title="Convert">' + (n.type === "task" ? "&rarr;note" : "&rarr;task") + "</button>" +
      '<button class="del" title="Delete">&times;</button>' +
    "</span></div>";
}

function dayHeaderHTML(date, weekday, extra) {
  const today = date === todayStr() ? ' <span class="n">&middot; today</span>' : "";
  return '<h2 class="day" data-date="' + date + '" title="Show only this day">' +
         esc(weekday || "") + " " + date +
         ' <span class="n">&middot; ' + extra + "</span>" + today + "</h2>";
}

function groupByDate(results) {
  const groups = [];
  for (const r of results) {
    const g = groups[groups.length - 1];
    if (g && g.date === r.date) g.notes.push(r);
    else groups.push({ date: r.date, weekday: r.weekday, notes: [r] });
  }
  return groups;
}

function orderView(groups) {
  // server order: newest date first, chronological within each date
  const g = groups.map(x => Object.assign({}, x, {
    notes: state.sort === "desc" ? x.notes.slice().reverse() : x.notes.slice()
  }));
  if (state.sort === "asc") g.reverse();
  return g;
}

const groupsHTML = (groups, unit) => groups.map(g =>
  "<section>" + dayHeaderHTML(g.date, g.weekday,
      g.notes.length + " " + unit + (g.notes.length === 1 ? "" : "s")) +
  g.notes.map(n => noteRow(n, n.month || state.month)).join("") + "</section>").join("");

/* ------------------------------------------------------------ views */

const VIEWS = ["dash", "notes", "new", "settings"];

function applyRoute() {
  const h = location.hash || "#/";
  /* A table, not a ternary chain: the chain silently sent every unknown hash
     to the dashboard, so a typo in a bookmark looked like a working link. */
  const named = VIEWS.find(v => v !== "dash" && h.indexOf("#/" + v) === 0);
  state.view = named || "dash";
  for (const v of VIEWS) {
    $("#view-" + v).hidden = v !== state.view;
    $("#tab-" + v).classList.toggle("on", v === state.view);
  }
  if (state.view !== "notes" && state.selecting) setSelecting(false);
  if (state.view === "dash") { renderDash(); qinput.focus(); }
  else if (state.view === "notes") { refreshNotes(); }
  else if (state.view === "settings") { renderSettings(); }
  else { showWrite(); input.focus(); }
}

/* ------------------------------------------------------------ guide */

/* A deliberately small Markdown subset - headings, tables, lists, code, links,
   bold/italic. Enough for GETTING-STARTED.md and nothing more: pulling in a
   parser would be the first dependency this single-file app has ever had, and
   it still has to boot with nothing but the stdlib behind it.
   Everything is escaped BEFORE any markup is added, so the guide file cannot
   inject HTML into this page even if someone edits it. */
function mdEscape(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function mdInline(s) {
  return mdEscape(s)
    .replace(/`([^`]+)`/g, (m, c) => "<code>" + c + "</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    /* Only http(s) and in-repo paths become links - never javascript:. */
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\[([^\]]+)\]\((?!https?:)[^)\s]+\)/g, "$1")
    .replace(/&lt;(https?:\/\/[^&\s]+)&gt;/g,
             '<a href="$1" target="_blank" rel="noopener">$1</a>');
}
function mdRender(src) {
  const out = [];
  const lines = src.split(/\r?\n/);
  let i = 0, inCode = false, code = [];
  const flushCode = () => {
    if (code.length) out.push("<pre><code>" + mdEscape(code.join("\n")) + "</code></pre>");
    code = [];
  };
  while (i < lines.length) {
    const ln = lines[i];
    if (/^```/.test(ln)) { inCode = !inCode; if (!inCode) flushCode(); i++; continue; }
    if (inCode) { code.push(ln); i++; continue; }
    /* The centred logo banner at the top of the .md is raw HTML. Drop the whole
       block - opening tag, img and closing tag - or the stray "</p>" renders as
       literal text, which is exactly what it did the first time. */
    if (/^\s*<\/?(?:p|div|img|br|center)\b/i.test(ln)) { i++; continue; }
    if (/^\s*$/.test(ln)) { i++; continue; }
    if (/^---+\s*$/.test(ln)) { out.push("<hr>"); i++; continue; }
    let m = ln.match(/^(#{1,4})\s+(.*)$/);
    if (m) { const h = m[1].length + 1; out.push("<h" + h + ">" + mdInline(m[2]) + "</h" + h + ">"); i++; continue; }
    if (/^\s*\|/.test(ln)) {                                   // table
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) { rows.push(lines[i]); i++; }
      const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const head = cells(rows[0]);
      const body = rows.slice(/^[\s|:-]+$/.test(rows[1] || "") ? 2 : 1);
      out.push("<table><thead><tr>" + head.map(c => "<th>" + mdInline(c) + "</th>").join("") +
        "</tr></thead><tbody>" + body.map(r =>
          "<tr>" + cells(r).map(c => "<td>" + mdInline(c) + "</td>").join("") + "</tr>").join("") +
        "</tbody></table>");
      continue;
    }
    if (/^\s*>/.test(ln)) {
      const q = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) { q.push(lines[i].replace(/^\s*>\s?/, "")); i++; }
      out.push("<blockquote>" + mdInline(q.join(" ")) + "</blockquote>");
      continue;
    }
    if (/^\s*(?:[-*]|\d+\.)\s+/.test(ln)) {
      const ol = /^\s*\d+\./.test(ln), items = [];
      while (i < lines.length && /^\s*(?:[-*]|\d+\.)\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*(?:[-*]|\d+\.)\s+/, "")); i++;
      }
      const t = ol ? "ol" : "ul";
      out.push("<" + t + ">" + items.map(x => "<li>" + mdInline(x) + "</li>").join("") + "</" + t + ">");
      continue;
    }
    const para = [];
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^\s*[|>#-]/.test(lines[i])
           && !/^```/.test(lines[i])) { para.push(lines[i]); i++; }
    if (para.length) out.push("<p>" + mdInline(para.join(" ")) + "</p>");
    else i++;
  }
  flushCode();
  return out.join("\n");
}

/* (The Guide and Stats views lived here until 2026-08-15 - see the matching
   note where the guide endpoint was. /api/stats still answers for anything
   that asks; only the tab is gone.) */

/* ------------------------------------------------------------ settings */

function cfgPill(state) {
  const on = state === "set" || state === "ready";
  const idle = state === "off";
  return '<span class="pillv ' + (on ? "on" : idle ? "idle" : "off") + '">'
       + esc(state) + "</span>";
}

async function renderSettings() {
  let c;
  try { c = await api("/api/config"); }
  catch (e) { $("#cfgState").innerHTML = '<p class="empty">Could not load settings.</p>'; return; }
  if (!c.available) {
    $("#cfgState").innerHTML = '<div class="warnbox">Settings are not available here — '
      + esc(c.why || "unknown") + ".</div>";
    for (const id of ["cfgRows", "cfgSecrets"]) $("#" + id).innerHTML = "";
    for (const id of ["cfgAccountsWrap", "cfgCapsWrap", "cfgProblemsWrap"]) $("#" + id).hidden = true;
    $("#cfgFoot").textContent = "";
    return;
  }
  $("#cfgState").innerHTML = "";
  $("#cfgDir").textContent = c.configDir || "";
  $("#cfgRows").innerHTML = '<div class="rows">' + c.settings.map(r =>
    '<div class="row2"><code>' + esc(r.file) + "." + esc(r.key) + "</code>"
    + '<span class="val">' + (r.value === "" ? "—" : esc(r.value)) + "</span>"
    + '<span class="src">' + esc(r.source) + "</span></div>").join("") + "</div>";
  $("#cfgSecrets").innerHTML = '<div class="rows">' + c.secrets.map(s =>
    '<div class="row2"><code>' + esc(s.key) + "</code><span></span>"
    + cfgPill(s.state) + "</div>").join("") + "</div>";

  const accs = c.accounts || [];
  $("#cfgAccountsWrap").hidden = accs.length === 0;
  $("#cfgAccounts").innerHTML = '<div class="rows">' + accs.map(a =>
    '<div class="row2"><code>' + esc(a.label) + "</code>"
    + '<span class="val">' + esc(a.user || "") + "</span>"
    + cfgPill(a.state) + "</div>").join("") + "</div>";

  const caps = (c.capabilities || []).filter(x => x.state !== "off");
  $("#cfgCapsWrap").hidden = caps.length === 0;
  $("#cfgCaps").innerHTML = '<div class="rows">' + caps.map(x =>
    '<div class="row2"><code>' + esc(x.name) + "</code>"
    + '<span class="val">' + esc(x.provider || "") + "</span>"
    + cfgPill(x.state) + "</div>").join("") + "</div>";

  const probs = c.problems || [];
  $("#cfgProblemsWrap").hidden = probs.length === 0;
  $("#cfgProblems").innerHTML = probs.map(p =>
    '<div class="warnbox">' + esc(p) + "</div>").join("");

  $("#cfgFoot").textContent =
    "Read-only. Edit the files in " + (c.configDir || "config") +
    " — settings are changed at the desk on purpose, so a wrong value "
    + "with nobody at the keyboard cannot lock you out.";
}

/* The Today tab is really a DAY view: today by default, any day on request.
   "What did I do on day X?" is the question this whole app exists to answer,
   and the answer is more than notes - inside a workspace the API also returns
   the fleet's day (commits across every repo, desk activity on the bus), all
   assembled from what is already on disk. */

function shiftDay(iso, delta) {
  const d = new Date(iso + "T12:00:00");    // noon dodges DST edges
  d.setDate(d.getDate() + delta);
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
}

function fleetHTML(f) {
  if (!f || (!f.commits.length && !f.desks.length)) return "";
  let h = "";
  if (f.commits.length) {
    h += '<h2 class="sect">Commits <span class="n">' + f.commits.length + "</span></h2>";
    h += f.commits.map(c =>
      '<div class="frow"><code>' + esc(c.time || "--:--") + "</code><b>"
      + esc(c.repo) + "</b><span>" + esc(c.subject) + "</span></div>").join("");
  }
  if (f.desks.length) {
    h += '<h2 class="sect">Desk activity</h2>';
    h += f.desks.map(d =>
      '<div class="frow"><code>' + esc((d.first || "") +
        (d.last && d.last !== d.first ? "–" + d.last : "")) + "</code><b>"
      + esc(d.session) + "</b><span>" + d["in"] + " in · "
      + d.out + " out</span></div>").join("");
  }
  return h;
}

async function renderDash() {
  const day = state.dashDay || todayStr();
  const isToday = day === todayStr();
  $("#dayInput").value = day;
  $("#dayHome").hidden = isToday;
  $("#dayHead").firstChild.textContent = isToday ? "Today " : day + " ";
  try {
    const [open, d] = await Promise.all([
      api("/api/search?type=open&q="),
      api("/api/day?date=" + day)
    ]);
    if ((state.dashDay || todayStr()) !== day) return;   // user moved on mid-fetch
    $("#todayLabel").textContent = isToday ? day : (d.weekday || "");
    $("#openCount").textContent = String(open.results.length);
    setPill(open.results.length);
    dashTasks.innerHTML = open.results.length
      ? groupsHTML(orderView(groupByDate(open.results)), "task")
      : '<p class="empty">No open tasks &mdash; enjoy the quiet.</p>';
    const notes = state.sort === "desc" ? d.notes.slice().reverse() : d.notes;
    dashToday.innerHTML = notes.length
      ? notes.map(n => noteRow(n, d.month)).join("")
      : '<p class="empty">' + (isToday ? "Nothing yet today. Write something above."
                                       : "Nothing written this day.") + "</p>";
    $("#dashFleet").innerHTML = fleetHTML(d.fleet);
  } catch (e) { flash(e.message); }
}

async function refreshNotes() {
  const seq = ++reqSeq;
  try {
    if (state.q) {
      const data = await api("/api/search?q=" + encodeURIComponent(state.q) +
                             "&type=" + state.type);
      if (seq !== reqSeq) return;
      renderSearch(data.results);
    } else {
      const data = await api("/api/month/" + state.month);
      if (seq !== reqSeq) return;
      renderMonth(data);
    }
  } catch (e) { if (seq === reqSeq) flash(e.message); }
}

function renderMonth(data) {
  let days = data.days;
  if (state.day) days = days.filter(d => d.date === state.day);
  days = days
    .map(d => ({ date: d.date, weekday: d.weekday, notes: d.notes.filter(n => kindOk(n)) }))
    .filter(d => d.notes.length);
  if (!days.length) {
    const what = state.type === "all" ? "notes" : typeSel.options[typeSel.selectedIndex].text;
    listEl.innerHTML = '<p class="empty">' + (state.day
      ? "No " + what + " on " + state.day + "."
      : "No " + what + " in " + monthLabel(state.month) + " yet.") + "</p>";
    return;
  }
  listEl.innerHTML = groupsHTML(orderView(days), "note");
  reconcileSelection();
}

function renderSearch(results) {
  if (!results.length) {
    listEl.innerHTML = '<p class="empty">No matches for &ldquo;' + esc(state.q) + '&rdquo;.</p>';
    reconcileSelection();
    return;
  }
  listEl.innerHTML = groupsHTML(orderView(groupByDate(results)), "hit");
  if (state.q) listEl.querySelectorAll(".body").forEach(b => markMatches(b, state.q));
  reconcileSelection();
}

function markMatches(root, q) {
  const needle = q.toLowerCase();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const hits = [];
  let node;
  while ((node = walker.nextNode())) {
    if (node.nodeValue.toLowerCase().indexOf(needle) !== -1) hits.push(node);
  }
  for (const tn of hits) {
    const frag = document.createDocumentFragment();
    let text = tn.nodeValue, idx;
    while ((idx = text.toLowerCase().indexOf(needle)) !== -1) {
      frag.appendChild(document.createTextNode(text.slice(0, idx)));
      const mk = document.createElement("mark");
      mk.textContent = text.slice(idx, idx + needle.length);
      frag.appendChild(mk);
      text = text.slice(idx + needle.length);
    }
    frag.appendChild(document.createTextNode(text));
    tn.parentNode.replaceChild(frag, tn);
  }
}

function kindOk(n) {
  const t = state.type;
  if (t === "all") return true;
  if (t === "note") return n.type === "note";
  if (t === "task") return n.type === "task";
  if (t === "open") return n.type === "task" && !n.done;
  if (t === "done") return n.type === "task" && n.done;
  return true;
}

function setPill(count) {
  pill.hidden = !count;
  pill.textContent = count + " open";
}

async function updatePill() {
  try { setPill((await api("/api/search?type=open&q=")).results.length); }
  catch (e) {}
}

function refreshAll() {
  if (state.view === "dash") renderDash();
  else if (state.view === "notes") { refreshNotes(); updatePill(); }
  else updatePill();
}

/* ------------------------------------------------- months / filters */

function renderMonthOptions() {
  monthSel.innerHTML = state.months.slice().reverse()
    .map(m => '<option value="' + m + '">' + monthLabel(m) + "</option>").join("");
  monthSel.value = state.month;
}

function updateNav() {
  const i = state.months.indexOf(state.month);
  $("#prev").disabled = i <= 0;
  $("#next").disabled = i < 0 || i >= state.months.length - 1;
  monthSel.value = state.month;
}

function setMonth(m, doRefresh) {
  if (!state.months.includes(m)) {
    state.months.push(m);
    state.months.sort();
    renderMonthOptions();
  }
  state.month = m;
  updateNav();
  if (doRefresh !== false) refreshNotes();
}

async function loadMonths() {
  const d = await api("/api/months");
  const set = new Set(d.months);
  set.add(thisMonth());
  set.add(state.month);
  state.months = [...set].sort();
  renderMonthOptions();
  updateNav();
}

function updateChip() {
  if (state.day) {
    chip.innerHTML = "showing " + state.day +
      ' only <button type="button" title="Clear day filter">&times;</button>';
    chip.hidden = false;
  } else chip.hidden = true;
}

function clearFilters() {
  state.day = null; dayPick.value = "";
  state.q = ""; searchBox.value = "";
  clearTimeout(searchTimer);
  updateChip();
}

function flash(msg, ok) {
  errEl.textContent = msg;
  errEl.classList.toggle("ok", !!ok);
  errEl.hidden = false;
  clearTimeout(errTimer);
  errTimer = setTimeout(() => { errEl.hidden = true; }, ok ? 2500 : 4000);
}

/* ------------------------------------------------------ note actions */

function noteRef(el) {
  const n = el.closest(".note");
  return { month: n.dataset.month, line: +n.dataset.line, sha: n.dataset.sha, type: n.dataset.type, el: n };
}

function wireNoteEvents(container) {
  container.addEventListener("click", async e => {
    const t = e.target;
    if (t.classList.contains("tick")) {
      const r = noteRef(t);
      try { await mutate("/api/task", { month: r.month, line: r.line, sha: r.sha, done: t.checked }); }
      catch (err) { flash(err.message); }
      refreshAll();
      return;
    }
    if (t.classList.contains("cv")) {
      const r = noteRef(t);
      try { await mutate("/api/convert", { month: r.month, line: r.line, sha: r.sha, to: r.type === "task" ? "note" : "task" }); }
      catch (err) { flash(err.message); }
      refreshAll();
      return;
    }
    if (t.classList.contains("del")) {
      const r = noteRef(t);
      if (!confirm("Delete this note? It moves to notes/.trash.md")) return;
      try { await mutate("/api/delete", { month: r.month, notes: [{ line: r.line, sha: r.sha }] }); }
      catch (err) { flash(err.message); }
      refreshAll();
      return;
    }
    if (t.classList.contains("selbox")) {
      const r = noteRef(t);
      const k = r.month + ":" + r.line + ":" + r.sha;
      if (t.checked) selected.set(k, { month: r.month, line: r.line, sha: r.sha });
      else selected.delete(k);
      updateBulkBar();
      return;
    }
    if (t.classList.contains("ed")) {
      startEdit(t.closest(".note"));
      return;
    }
    if (t.classList.contains("tag")) {
      state.q = t.textContent;
      searchBox.value = state.q;
      state.day = null; dayPick.value = ""; updateChip();
      if (state.view !== "notes") location.hash = "#/notes";
      else refreshNotes();
      return;
    }
    const h = t.closest("h2.day");
    if (h) dayHeaderClick(h);
  });
}

function startEdit(noteEl) {
  if (!noteEl || noteEl.querySelector(".editbox")) return;
  const raw = rawTexts.get(noteEl.dataset.month + ":" + noteEl.dataset.line + ":" + noteEl.dataset.sha);
  if (raw == null) return;
  const body = noteEl.querySelector(".body");
  const prev = body.innerHTML;
  body.innerHTML = "";
  const ta = document.createElement("textarea");
  ta.className = "editbox";
  ta.value = raw;
  const acts = document.createElement("div");
  acts.className = "editacts";
  acts.innerHTML = '<button class="primary" type="button">Save</button>' +
    '<button class="ghost" type="button">Cancel</button>' +
    '<span class="hint">Ctrl+Enter saves &middot; Esc cancels</span>';
  body.appendChild(ta);
  body.appendChild(acts);
  ta.style.height = Math.min(ta.scrollHeight + 4, 400) + "px";
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  const cancel = () => { body.innerHTML = prev; };
  const save = async () => {
    if (!ta.value.trim()) { flash("Note cannot be empty"); return; }
    try {
      await mutate("/api/edit", { month: noteEl.dataset.month, line: +noteEl.dataset.line,
                                  sha: noteEl.dataset.sha, text: ta.value });
      flash("Saved ✓", true);
    } catch (e) { flash(e.message); }
    refreshAll();
  };
  acts.children[0].addEventListener("click", save);
  acts.children[1].addEventListener("click", cancel);
  ta.addEventListener("keydown", e => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save(); }
    else if (e.key === "Escape") { e.preventDefault(); cancel(); }
  });
}

function dayHeaderClick(h) {
  const date = h.dataset.date;
  clearTimeout(searchTimer);
  const wasSearching = !!state.q || !!searchBox.value.trim();
  state.q = ""; searchBox.value = "";
  state.day = (!wasSearching && state.view === "notes" && state.day === date) ? null : date;
  dayPick.value = state.day || "";
  if (state.day) setMonth(state.day.slice(0, 7), false);
  updateChip();
  if (state.view !== "notes") location.hash = "#/notes";
  else refreshNotes();
}

/* ------------------------------------------------------- bulk delete */

function setSelecting(on) {
  state.selecting = on;
  document.body.classList.toggle("selecting", on);
  $("#selectBtn").classList.toggle("on", on);
  if (!on) selected.clear();
  updateBulkBar();
}

function updateBulkBar() {
  const b = $("#bulkDelBtn");
  b.hidden = !state.selecting;
  b.disabled = !selected.size;
  b.textContent = "delete " + selected.size;
}

function reconcileSelection() {
  // lines/shas shift after any mutation — drop selections that no longer
  // match a rendered note so the count can never go stale
  if (!selected.size) return;
  const present = new Set([...listEl.querySelectorAll(".note")]
    .map(n => n.dataset.month + ":" + n.dataset.line + ":" + n.dataset.sha));
  for (const k of [...selected.keys()]) {
    if (!present.has(k)) selected.delete(k);
  }
  updateBulkBar();
}

async function bulkDelete() {
  if (!selected.size) return;
  if (!confirm("Delete " + selected.size + " selected note(s)? They move to notes/.trash.md")) return;
  const byMonth = {};
  for (const v of selected.values()) {
    (byMonth[v.month] = byMonth[v.month] || []).push({ line: v.line, sha: v.sha });
  }
  try {
    for (const m of Object.keys(byMonth)) {
      await mutate("/api/delete", { month: m, notes: byMonth[m] });
    }
  } catch (e) { flash(e.message); }
  setSelecting(false);
  loadMonths();
  refreshAll();
}

/* ---------------------------------------------------------- composer */

function showWrite() {
  $("#tabWrite").classList.add("on");
  $("#tabPreview").classList.remove("on");
  preview.hidden = true;
  input.hidden = false;
}

function showPreview() {
  preview.innerHTML = input.value.trim()
    ? renderMD(input.value.replace(/\r\n?/g, "\n"))
    : '<span class="dim">Nothing to preview.</span>';
  $("#tabPreview").classList.add("on");
  $("#tabWrite").classList.remove("on");
  input.hidden = true;
  preview.hidden = false;
}

async function saveNote(text, task, after) {
  if (saving || !text.trim()) return;
  saving = true;
  try {
    await mutate("/api/note", { text: text, task: task });
    after();
    flash("Saved ✓", true);
    updatePill();
  } catch (e) { flash(e.message); }
  finally { saving = false; }
}

function saveFromWrite() {
  saveNote(input.value, $("#taskToggle").checked, () => {
    input.value = "";
    try { localStorage.removeItem("notes-draft"); } catch (e) {}
    $("#taskToggle").checked = false;
    showWrite();
    input.focus();
  });
}

function saveFromQuick() {
  saveNote(qinput.value, qtask.checked, () => {
    qinput.value = "";
    qtask.checked = false;
    qGrow();
    renderDash();
    qinput.focus();
  });
}

function qGrow() {
  qinput.style.height = "auto";
  qinput.style.height = Math.min(qinput.scrollHeight + 2, window.innerHeight * 0.3) + "px";
}

/* --------------------------------------------------------- uploads */

function insertAtCursor(el, text) {
  const s = el.selectionStart == null ? el.value.length : el.selectionStart;
  el.setRangeText(text, s, el.selectionEnd == null ? s : el.selectionEnd, "end");
}

async function uploadOne(file, el) {
  const ph = "![Uploading " + (file.name || "file") + "…]()";
  insertAtCursor(el, ph);
  try {
    const r = await fetch("/api/upload?name=" + encodeURIComponent(file.name || "file"),
                          { method: "POST", body: file });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
    el.value = el.value.replace(ph, d.markdown);
  } catch (e) {
    el.value = el.value.replace(ph, "");
    flash("Upload failed: " + e.message);
  }
  if (el === qinput) qGrow();
  if (el === input) {
    saveDraft();
    if (!preview.hidden) showPreview();
  }
}

async function handleFiles(files, el) {
  for (const f of files) await uploadOne(f, el);
}

function wirePaste(el) {
  el.addEventListener("paste", e => {
    const files = e.clipboardData ? [...e.clipboardData.files] : [];
    if (files.length) { e.preventDefault(); handleFiles(files, el); }
  });
}

function wireDrop(zone, el) {
  zone.addEventListener("dragover", e => { e.preventDefault(); zone.classList.add("dropping"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("dropping"));
  zone.addEventListener("drop", e => {
    e.preventDefault();
    zone.classList.remove("dropping");
    const fs = [...e.dataTransfer.files];
    if (fs.length) handleFiles(fs, el);
  });
}

/* ------------------------------------------------------------ wiring */

wireNoteEvents(listEl);
wireNoteEvents(dashTasks);
wireNoteEvents(dashToday);

qinput.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); saveFromQuick(); }
});
qinput.addEventListener("input", qGrow);

input.addEventListener("keydown", e => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveFromWrite(); }
});
/* The day navigator: today by default, any day on request. Landing on today
   is normalised back to null so "today" stays TODAY across midnight. */
function setDashDay(iso) {
  const today = todayStr();
  if (iso > today) iso = today;             // the future has nothing to show
  state.dashDay = iso === today ? null : iso;
  renderDash();
}
$("#dayPrev").addEventListener("click", () =>
  setDashDay(shiftDay(state.dashDay || todayStr(), -1)));
$("#dayNext").addEventListener("click", () =>
  setDashDay(shiftDay(state.dashDay || todayStr(), 1)));
$("#dayHome").addEventListener("click", () => setDashDay(todayStr()));
$("#dayInput").addEventListener("change", () => {
  if (/^\d{4}-\d{2}-\d{2}$/.test($("#dayInput").value)) setDashDay($("#dayInput").value);
});

$("#saveBtn").addEventListener("click", saveFromWrite);
$("#tabWrite").addEventListener("click", () => { showWrite(); input.focus(); });
$("#tabPreview").addEventListener("click", showPreview);
$("#attachBtn").addEventListener("click", () => $("#fileInput").click());
$("#fileInput").addEventListener("change", e => {
  handleFiles([...e.target.files], input);
  e.target.value = "";
});

document.querySelectorAll(".cbar [data-fmt]").forEach(b => b.addEventListener("click", () => {
  const kind = b.dataset.fmt;
  const s = input.selectionStart, e = input.selectionEnd, v = input.value;
  if (kind === "list" || kind === "quote") {
    const pre = kind === "list" ? "- " : "> ";
    const ls = s > 0 ? v.lastIndexOf("\n", s - 1) + 1 : 0;
    let le = v.indexOf("\n", e);
    if (le === -1) le = v.length;
    if (le < ls) le = ls;
    const seg = v.slice(ls, le).split("\n")
      .map(l => l.indexOf(pre) === 0 ? l : pre + l).join("\n");
    input.setRangeText(seg, ls, le, "end");
  } else if (kind === "codeblock") {
    const sel = v.slice(s, e);
    const nl = s === 0 || v[s - 1] === "\n" ? "" : "\n";
    input.setRangeText(nl + "```\n" + sel + "\n```", s, e, "end");
    if (!sel) {
      const pos = s + nl.length + 4;
      input.setSelectionRange(pos, pos);
    }
  } else {
    const mark = kind === "bold" ? "**" : kind === "italic" ? "*" :
                 kind === "strike" ? "~~" : "`";
    input.setRangeText(mark + v.slice(s, e) + mark, s, e);
    if (s === e) input.setSelectionRange(s + mark.length, s + mark.length);
    else input.setSelectionRange(s + mark.length, e + mark.length);
  }
  input.focus();
  saveDraft();
}));

/* Draft autosave — navigating away or closing the tab never loses a
   half-written note. Cleared on successful save. */
function saveDraft() {
  try {
    if (input.value.trim()) localStorage.setItem("notes-draft", input.value);
    else localStorage.removeItem("notes-draft");
  } catch (e) {}
}
input.addEventListener("input", saveDraft);

document.addEventListener("click", e => {
  const b = e.target;
  if (!b.classList || !b.classList.contains("copybtn")) return;
  const code = b.parentElement.querySelector("code");
  navigator.clipboard.writeText(code ? code.innerText : "").then(() => {
    b.textContent = "copied";
    setTimeout(() => { b.textContent = "copy"; }, 1200);
  }).catch(() => flash("Copy failed"));
});

wirePaste(input);
wirePaste(qinput);
wireDrop(composerPanel, input);
wireDrop(quickPanel, qinput);

searchBox.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.q = searchBox.value.trim();
    if (state.q && state.day) { state.day = null; dayPick.value = ""; updateChip(); }
    refreshNotes();
  }, 150);
});

searchBox.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    clearTimeout(searchTimer);
    searchBox.value = "";
    state.q = "";
    refreshNotes();
  }
});

typeSel.addEventListener("change", () => { state.type = typeSel.value; refreshNotes(); });

sortSel.addEventListener("change", () => {
  state.sort = sortSel.value === "asc" ? "asc" : "desc";
  localStorage.setItem("notes-sort", state.sort);
  refreshAll();
});

dayPick.addEventListener("change", () => {
  if (!dayPick.value) {
    state.day = null;
    updateChip();
    refreshNotes();
    return;
  }
  state.day = dayPick.value;
  state.q = ""; searchBox.value = ""; clearTimeout(searchTimer);
  setMonth(state.day.slice(0, 7), false);
  updateChip();
  refreshNotes();
});

chip.addEventListener("click", e => {
  if (e.target.tagName === "BUTTON") {
    state.day = null;
    dayPick.value = "";
    updateChip();
    refreshNotes();
  }
});

$("#prev").addEventListener("click", () => step(-1));
$("#next").addEventListener("click", () => step(1));
function step(d) {
  const i = state.months.indexOf(state.month);
  const j = i + d;
  if (j < 0 || j >= state.months.length) return;
  clearFilters();
  setMonth(state.months[j]);
}
monthSel.addEventListener("change", () => { clearFilters(); setMonth(monthSel.value); });

$("#selectBtn").addEventListener("click", () => setSelecting(!state.selecting));
$("#bulkDelBtn").addEventListener("click", bulkDelete);

document.addEventListener("keydown", e => {
  const t = document.activeElement;
  const typing = t === input || t === qinput || t === searchBox ||
                 (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA"));
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "/") {
    e.preventDefault();
    if (state.view !== "notes") location.hash = "#/notes";
    setTimeout(() => searchBox.focus(), 60);
  } else if (e.key === "n") {
    e.preventDefault();
    location.hash = "#/new";
  } else if (e.key === "t") {
    e.preventDefault();
    location.hash = "#/";
  }
});

window.addEventListener("hashchange", applyRoute);

(async () => {
  sortSel.value = state.sort;
  try { input.value = localStorage.getItem("notes-draft") || ""; } catch (e) {}
  try { await loadMonths(); } catch (e) { flash(e.message); }
  applyRoute();
  updatePill();
})();
</script>
</body>
</html>
"""


def is_loopback(host):
    """True when the bind address can only be reached from this machine."""
    return host in ("localhost", "::1") or host.startswith("127.")


def lan_address():
    """This machine's address on the network, or None if it can't be found.

    Connecting a UDP socket sends no packets — it just asks the OS which
    interface would carry traffic to that address. TEST-NET-1 is never routed.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def setting_origin(key, env_var):
    """Where a value came from, so a message can name what to edit."""
    if os.environ.get(env_var):
        return f"{env_var} in the environment"
    if CONFIG.get(key):
        return str(CONFIG_PATH)
    return ""


def exposure_warning(host, port, origin=""):
    """The startup banner shown whenever notes are served beyond this machine."""
    reachable = lan_address() if host in ("0.0.0.0", "::", "") else host
    where = f"http://{reachable}:{port}" if reachable else f"port {port}"
    return [
        "",
        f"  !!  host = {host}: these notes are NOT limited to this machine.",
        f"  !!  Anyone on the same network can open {where}.",
        "  !!  There is NO authentication: every note is readable, and the API",
        "  !!  accepts writes, edits and deletes from any visitor.",
        f"  !!  Set it back to 127.0.0.1 ({origin}) to make them private again."
        if origin else
        "  !!  Set it back to 127.0.0.1 to make them private again.",
        "",
    ]


def main():
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"notes: serving {NOTES_DIR} at http://localhost:{PORT}  (Ctrl+C to stop)")
    if CONFIG_PATH.exists():
        print(f"notes: config {CONFIG_PATH}")
    if not is_loopback(HOST):
        for line in exposure_warning(HOST, PORT, setting_origin("host", "NOTES_HOST")):
            print(line)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
