#!/usr/bin/env python3
"""Scheduled envelopes - exact timing for the bus (stdlib only).

    python tools\\discord\\schedule.py add --every 20m  --to orchestrator --text "check the deploy"
    python tools\\discord\\schedule.py add --daily 07:00 --to orchestrator --text "morning briefing to #daybook"
    python tools\\discord\\schedule.py add --at 2026-08-01T09:00 --to recipe-app.app --text "ship the beta"
    python tools\\discord\\schedule.py list
    python tools\\discord\\schedule.py remove <id>

Distinct from the heartbeat, which is a batched approximate loop. This is exact
timing and one-shots: "remind me in 20 minutes", "07:00 weekdays post the
briefing". A due job writes a normal inbox envelope, so it wakes or spawns the
target session through exactly the same path a Discord message does - no second
delivery mechanism to keep in step.

Times are LOCAL, because "07:00" means 07:00 where you are.

Catch-up policy: a job whose time passed while the PC was off fires ONCE if it
is still within its GRACE window, and is otherwise fast-forwarded without firing
(the skip is counted on the job, not swallowed). Grace is half the period,
clamped to 2 minutes .. 2 hours, so a 07:00 briefing still lands at 07:40 but not
at 18:00, while a 20-minute check missed by hours simply resumes. Only ever one
occurrence is considered - nextRun is a single timestamp - so waking to fourteen
stale reminders was never possible. An expired one-shot is dropped.

Schedules live in state\\ and are therefore per-machine, like .env and session
claims - a job firing on two machines at once would double-post.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Routines live in config\ so they TRAVEL. They used to sit in state\, which is
# gitignored AND excluded from the backup zip - so every routine he built would
# have been lost the first time he moved machines (owner's call, 2026-08-07).
# config\ is gitignored except examples, so they survive the move without a
# routine's text - which can name an account - ever reaching GitHub.
JOBS = ROOT / "config" / "routines.json"
LEGACY_JOBS = ROOT / "state" / "schedule" / "jobs.json"
# Work-loop ledgers (docs\DELEGATION.md D5): one file per loop, the counted
# form of the continuation pattern. Machine-local like the claims - a loop is
# THIS machine's work in progress, not luggage.
LOOPS = ROOT / "state" / "watchdog" / "loops"
# Workflow ledgers are the DESK-MAIL thread files (docs\DELEGATION.md D11): a
# workflow is a property of a chain, not a second record beside it, so the
# budget a self-continuation spends is the chain's own.
THREADS = ROOT / "state" / "watchdog" / "threads"


def load_thread(tid):
    """One thread ledger, or None. Read-only here - the watchdog owns writes."""
    try:
        d = json.loads((THREADS / f"{tid}.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("id") else None
    except (OSError, ValueError):
        return None


def thread_workflow(tid):
    """-> the workflow block on that chain, or None."""
    wf = (load_thread(tid) or {}).get("workflow")
    return wf if isinstance(wf, dict) else None


def desk_cwd(session):
    """id -> folder, the same table inbox_watch and the watchdog use. The
    registry IS the filesystem: a --to naming no real folder is a typo, and a
    typo'd inbox folder used to become a phantom desk retried forever."""
    if session == "orchestrator":
        return ROOT
    if session == "daybook":
        return ROOT / "daybook"
    if session.startswith("tool."):
        return ROOT / "tools" / session.split(".", 1)[1]
    project, _, component = session.partition(".")
    return ROOT / "projects" / project / component


def legal_desk_shape(s):
    if s in ("orchestrator", "daybook"):
        return True
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*", s))


def own_session():
    """The desk THIS CLI runs from (hook-grade resolution), or None outside one.
    Lazy and defensive for the same reason machine_name() is."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import desk_identity
        return desk_identity.session_id_for(Path.cwd())
    except Exception:                                     # noqa: BLE001
        return None


def loop_budget_default():
    """config\\omnius.ini [delegation] loop_budget, default 5. Lazy import so a
    bare tree still works; garbage falls back rather than raising."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import omnius_config as ocfg
        return max(1, ocfg.get_int(ocfg.load("omnius"), "delegation",
                                   "loop_budget", "OMNIUS_LOOP_BUDGET", 5))
    except Exception:                                     # noqa: BLE001
        return 5


def load_loop(loop_id):
    try:
        d = json.loads((LOOPS / f"{loop_id}.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("id") else None
    except (OSError, ValueError):
        return None


def save_loop(led):
    LOOPS.mkdir(parents=True, exist_ok=True)
    p = LOOPS / f"{led['id']}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(led, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def open_loop(session, max_runs, channel=None):
    led = {"id": f"loop-{session}-{int(datetime.now().timestamp() * 1000)}",
           "session": session, "max": int(max_runs), "fired": 0,
           "channelId": channel, "openedAt": datetime.now().strftime(FMT),
           "lastFiredAt": None, "closed": None}
    save_loop(led)
    return led


def list_loops():
    if not LOOPS.is_dir():
        return []
    out = []
    for f in sorted(LOOPS.glob("*.json")):
        led = load_loop(f.stem)
        if led:
            out.append(led)
    return out


def describe_loop(led):
    state = f"closed: {led['closed']}" if led.get("closed") else "open"
    return (f"{led.get('id',''):<34} {led.get('session',''):<18} "
            f"run {led.get('fired', 0)}/{led.get('max', 0)}  {state}")


def machine_name():
    """This machine's name, matching what claims and channel topics use.

    Imported lazily and defensively: schedule.py is a stdlib-only CLI that must
    keep working when run straight out of a tree with no .env - a routine you
    cannot list because the config loader threw is worse than an unstamped job.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import api
        return api.MACHINE
    except Exception:                                     # noqa: BLE001
        import os
        return os.environ.get("COMPUTERNAME") or ""

UNITS = {"m": "minutes", "h": "hours", "d": "days"}
FMT = "%Y-%m-%dT%H:%M:%S"


def load_jobs():
    """Read config\\routines.json, falling back to the old state\\ location.

    Same half-migrated-tree rule fleet.json and notes.ini already follow: read
    the new path, fall back to the old one, always WRITE the new one - so an
    older tree still boots and the first save migrates it with no separate step.
    """
    for path in (JOBS, LEGACY_JOBS):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue                       # absent: genuinely nothing here
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # PRESENT BUT UNREADABLE IS NOT EMPTY. Treating it as empty was a
            # silent data-loss bug found by drill 2026-08-18: a torn write -
            # exactly what a power cut produces - was read as [] and then
            # OVERWRITTEN with [] by the next 20s tick, destroying every
            # routine the owner had, with no log and no alert.
            #
            # So: quarantine the bytes beside the file (recoverable by hand),
            # say so loudly, and only then continue as empty. The rename also
            # stops the next save from writing over the evidence.
            try:
                keep = path.with_name(path.name + f".corrupt-{int(datetime.now().timestamp())}")
                path.replace(keep)
                print(f"[!] {path.name} was unreadable and has been kept as {keep.name} "
                      f"- routines start empty until it is repaired", file=sys.stderr)
            except OSError as e:
                print(f"[!] {path.name} is unreadable and could not be quarantined: {e}",
                      file=sys.stderr)
            continue
        return data if isinstance(data, list) else []
    return []


def save_jobs(jobs):
    JOBS.parent.mkdir(parents=True, exist_ok=True)
    tmp = JOBS.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, indent=2), encoding="utf-8")
    tmp.replace(JOBS)


def owns(job, machine=None):
    """Should THIS machine fire this job?

    A job carries the machine that created it. Routines travel now, so without
    this a restored backup on a second PC would double-post every check. An
    unstamped job (pre-migration, or hand-written) belongs to whoever finds it.
    """
    stamped = (job.get("machine") or "").strip()
    return not stamped or stamped == (machine if machine is not None else machine_name())


def parse_every(s):
    m = re.fullmatch(r"(\d+)([mhd])", (s or "").strip().lower())
    if not m:
        raise ValueError("--every wants forms like 20m, 2h, 1d")
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        raise ValueError("--every must be positive")
    return timedelta(**{UNITS[unit]: n})


def parse_daily(s):
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (s or "").strip())
    if not m:
        raise ValueError("--daily wants HH:MM, 24-hour")
    return int(m.group(1)), int(m.group(2))


def parse_between(s):
    """'09:00-18:00' -> ((9, 0), (18, 0)).

    Overnight windows are REFUSED rather than guessed at. "every hour between
    22:00 and 06:00" has to decide whether 23:00 Friday belongs to Friday or
    Saturday for the weekday rule, and getting that subtly wrong produces a job
    that fires at the wrong time for months without anyone noticing. Nobody has
    asked for one; say so plainly instead.
    """
    parts = (s or "").strip().split("-")
    if len(parts) != 2:
        raise ValueError("--between wants HH:MM-HH:MM, e.g. 09:00-18:00")
    start, end = (parse_daily(p.strip()) for p in parts)
    if start >= end:
        raise ValueError("--between must start before it ends on the same day "
                         "(overnight windows are not supported)")
    return start, end


# A candidate can be pushed by the weekday rule and then by the window rule, and
# the push can cross a day and re-trigger the weekday rule. Bounded so an
# impossible combination raises instead of spinning: worst case is Fri evening
# -> Sat -> Sun -> Mon, then into Monday's window. 8 is comfortably above that.
_ADVANCE_MAX = 8


def _advance(cand, job):
    """Push `cand` forward until it satisfies the job's weekday and window rules.

    Shared by `daily` and `every`. It used to live inside the `daily` branch,
    which is why `--weekdays` silently did nothing on a recurring job - the flag
    was accepted, stored, and never read (2026-08-07).
    """
    window = job.get("between")
    start = end = None
    if window:
        start, end = parse_between(window)
    for _ in range(_ADVANCE_MAX):
        if job.get("weekdays") and cand.weekday() >= 5:          # Sat/Sun
            cand = (cand + timedelta(days=1)).replace(
                hour=start[0] if start else cand.hour,
                minute=start[1] if start else cand.minute,
                second=0, microsecond=0)
            continue
        if start and (cand.hour, cand.minute) < start:
            cand = cand.replace(hour=start[0], minute=start[1],
                                second=0, microsecond=0)
            continue
        if end and (cand.hour, cand.minute) > end:
            cand = (cand + timedelta(days=1)).replace(
                hour=start[0], minute=start[1], second=0, microsecond=0)
            continue
        return cand
    raise ValueError(f"cannot find a slot for job {job.get('id')} - "
                     f"check --between and --weekdays together")


def next_run(job, after):
    """-> datetime of the next firing strictly after `after`, or None if the job
    is a one-shot that has already passed."""
    kind = job["kind"]
    if kind == "at":
        when = datetime.strptime(job["at"], FMT)
        return when if when > after else None
    if kind == "every":
        step = parse_every(job["every"])
        nxt = datetime.strptime(job["nextRun"], FMT) if job.get("nextRun") else after
        # Skip forward over everything missed rather than firing a backlog.
        while nxt <= after:
            nxt += step
        # Clamping AFTER stepping, not before: a 1h job whose slot lands at
        # 03:00 must jump to the window's start, not creep an hour at a time.
        return _advance(nxt, job)
    if kind == "daily":
        hh, mm = parse_daily(job["daily"])
        cand = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= after:
            cand += timedelta(days=1)
        return _advance(cand, job)
    raise ValueError(f"unknown job kind: {kind}")


GRACE_MIN, GRACE_MAX = 120, 7200        # 2 minutes .. 2 hours


def grace_seconds(job):
    """How late a job may be and still be worth firing.

    Half the period, clamped. Adopted from Hermes' cron `_compute_grace_seconds`
    after reading its source (2026-08-01), because the trade-off is real on a
    laptop that sleeps: a daily 07:00 briefing is still useful at 07:40, and
    actively unwanted at 18:00; a 20-minute check missed by three hours is pure
    noise. One-shots get the maximum - "remind me at 09:00" is usually still
    wanted at 09:40, and there is no next occurrence to fall back on.
    """
    kind = job.get("kind")
    if kind == "every":
        # add_job stores the interval under the KIND's own key ({kind: value}),
        # so it is job["every"] - not job["value"]. Reading the wrong key silently
        # returned the 2-minute floor for every recurring job, which would have
        # made this whole feature a no-op.
        try:
            value = str(job.get("every") or "")
            n, unit = int(value[:-1]), value[-1]
        except (ValueError, IndexError):
            return GRACE_MIN
        period = n * {"m": 60, "h": 3600, "d": 86400}.get(unit, 60)
        return max(GRACE_MIN, min(period // 2, GRACE_MAX))
    return GRACE_MAX                            # daily and one-shots


def due_jobs(now=None):
    """-> (list of jobs to fire now, jobs list rewritten with new nextRun).

    Pure enough to test offline: pass `now` explicitly.

    A job whose time passed while the PC was off fires ONCE if it is still
    within its grace window, and is otherwise fast-forwarded without firing.
    Either way only one occurrence is ever considered, because nextRun is a
    single timestamp - waking to fourteen stale reminders was never possible.
    Skips are counted on the job rather than dropped silently: a schedule that
    never fires because the machine is always asleep at that hour is something
    you want to be able to see."""
    now = now or datetime.now()
    jobs, fire, keep = load_jobs(), [], []
    me = machine_name()
    for j in jobs:
        # Paused and foreign jobs are KEPT untouched - not fired, and not
        # fast-forwarded either. Advancing nextRun on a job this machine does
        # not own would fight the machine that does.
        if j.get("paused") or not owns(j, me):
            keep.append(j)
            continue
        try:
            nxt = datetime.strptime(j["nextRun"], FMT) if j.get("nextRun") else None
        except (ValueError, TypeError):
            nxt = None
        if nxt is None:
            nxt = next_run(j, now)
            if nxt is None:
                continue                        # expired one-shot: drop it
            j["nextRun"] = nxt.strftime(FMT)
            keep.append(j)
            continue
        if nxt <= now:
            late = (now - nxt).total_seconds()
            if late > grace_seconds(j):
                # Too stale to be useful. Fast-forward instead of firing, and
                # record the skip so it is visible in `list` rather than silent.
                j["missed"] = int(j.get("missed", 0) or 0) + 1
                j["lastMissed"] = now.strftime(FMT)
                following = next_run(j, now)
                if following is None:
                    continue                    # expired one-shot: drop it
                j["nextRun"] = following.strftime(FMT)
                keep.append(j)
                continue
            fire.append(j)
            j["lastRun"] = now.strftime(FMT)
            if j["kind"] == "at":
                continue                        # one-shot: done, do not keep
            following = next_run(j, now)
            if following is None:
                continue
            j["nextRun"] = following.strftime(FMT)
        keep.append(j)
    return fire, keep


def stamp_success(jobs, delivered_ids, when=None):
    """Mark the jobs whose envelope actually reached an inbox. Mutates `jobs`.

    `lastRun` means "we tried": it is written the moment a job is selected to
    fire, before anything is delivered. Hermes keeps ticker heartbeat age and
    ticker SUCCESS age as two separate numbers for exactly this reason, and it
    is the same distinction our watchdog lock/beacon pair makes - running is not
    working. Without this, a schedule whose delivery fails every single time
    still looks perfectly healthy.

    Pure and in-memory on purpose: the caller already rewrites jobs.json once
    after the pass, and a helper that saved separately would race with it.
    """
    stamp = (when or datetime.now()).strftime(FMT)
    ids = set(delivered_ids or ())
    for j in jobs:
        if j.get("id") in ids:
            j["lastSuccess"] = stamp
    return jobs


def add_job(kind, value, to, text, weekdays=False, between=None,
            loop=None, channel=None, workflow=None):
    jobs = load_jobs()
    jid = f"{kind}-{int(datetime.now().timestamp())}"
    job = {"id": jid, "kind": kind, kind: value, "to": to, "text": text,
           "weekdays": weekdays, "between": between, "machine": machine_name(),
           "paused": False, "createdAt": datetime.now().strftime(FMT),
           "nextRun": None, "lastRun": None}
    if loop:
        job["loop"] = loop           # counted against the loop's ledger at fire time
    if workflow:
        job["workflow"] = workflow   # counted against the CHAIN's ledger (D11):
                                     # one budget for a chain that re-queues
                                     # itself on several desks in turn
    if channel:
        job["channelId"] = channel   # rides into the fired envelope (D5): the
                                     # channel a loop answers when it must talk
    if between:
        parse_between(between)                # reject a bad window at ADD time,
                                              # not silently at 3am
    nxt = next_run(job, datetime.now())
    if nxt is None:
        raise ValueError("that time is already in the past")
    job["nextRun"] = nxt.strftime(FMT)
    jobs.append(job)
    save_jobs(jobs)
    return job


def upcoming(job, count=3, after=None):
    """-> the next `count` fire times, for echoing a parsed schedule back.

    A wrong schedule fails silently at a time he is not watching, so the desk
    that creates a routine shows these immediately - reading three timestamps
    catches a misparse that reading the spec back would not.
    """
    out, cursor = [], after or datetime.now()
    for _ in range(count):
        probe = dict(job)
        probe["nextRun"] = cursor.strftime(FMT)
        nxt = next_run(probe, cursor)
        if nxt is None:
            break
        out.append(nxt)
        cursor = nxt
    return out


def spec_of(job):
    """The schedule as a human reads it: 'every 1h 09:00-18:00 Mon-Fri'."""
    bits = [f"{job['kind']} {job.get(job['kind'])}".strip()]
    if job.get("between"):
        bits.append(job["between"])
    if job.get("weekdays"):
        bits.append("Mon-Fri")
    return " ".join(bits)


def describe(job, me=None):
    """One aligned line per job. Shared by the CLI and Discord's `!cron`.

    The markers are the point. `missed` has been counted since this file was
    written and shown NOWHERE - a routine that never fires because the laptop
    sleeps at that hour has looked exactly like one that works.
    """
    me = me if me is not None else machine_name()
    marks = []
    if job.get("paused"):
        marks.append("PAUSED")
    if not owns(job, me):
        marks.append(f"machine:{job.get('machine')}")
    missed = int(job.get("missed", 0) or 0)
    if missed:
        marks.append(f"missed x{missed}")
    if job.get("loop"):
        marks.append("loop")         # `loop list` has the run count
    if not job.get("lastSuccess") and job.get("lastRun"):
        marks.append("never delivered")
    tail = ("  [" + ", ".join(marks) + "]") if marks else ""
    return (f"{job.get('id',''):<20} {spec_of(job):<26} -> {job.get('to',''):<18} "
            f"next {job.get('nextRun') or '-'}  {(job.get('text') or '')[:38]}{tail}")


def set_paused(job_id, paused):
    jobs = load_jobs()
    for j in jobs:
        if j.get("id") == job_id:
            j["paused"] = bool(paused)
            save_jobs(jobs)
            return j
    return None


def adopt(job_id):
    """Re-stamp foreign jobs to this machine. `all` claims every one of them.

    This is the whole machine-migration story: restore the backup on the new PC,
    run it once, and every routine resumes where it left off.
    """
    jobs, me, claimed = load_jobs(), machine_name(), []
    for j in jobs:
        if job_id in ("all", j.get("id")) and (j.get("machine") or "") != me:
            j["machine"] = me
            claimed.append(j)
    if claimed:
        save_jobs(jobs)
    return claimed


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add", help="schedule an envelope")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--every", help="20m / 2h / 1d")
    g.add_argument("--daily", help="HH:MM local")
    g.add_argument("--at", help="YYYY-MM-DDTHH:MM local, one-shot")
    # A one-shot you can write without knowing what time it is. Added
    # 2026-08-13 for the continuation case: a desk whose work spans runs must
    # queue a real envelope rather than promise to carry on, and computing an
    # absolute timestamp for "in a minute" is friction that invites the promise.
    g.add_argument("--in", dest="in_", metavar="DELAY",
                   help="20m / 2h / 1d from now, one-shot (relative --at)")
    p.add_argument("--to", required=True, help="session id, e.g. orchestrator or recipe-app.app")
    p.add_argument("--text", required=True)
    p.add_argument("--weekdays", action="store_true", help="skip Sat/Sun")
    p.add_argument("--between", help="HH:MM-HH:MM, e.g. 09:00-18:00 (with --every)")
    # Work loops (docs\DELEGATION.md D5): the continuation pattern, counted.
    p.add_argument("--loop", metavar="auto|ID",
                   help="budgeted work loop: `auto` opens one, an id continues it. "
                        "REQUIRED when --to names your own desk.")
    p.add_argument("--max", type=int, metavar="N",
                   help="runs before the loop must checkpoint (only lowers the "
                        "config default, never raises it)")
    p.add_argument("--channel", metavar="CHANNEL_ID",
                   help="Discord channel id the fired envelope should carry - "
                        "where the loop answers a human when it must")
    p = sub.add_parser("loop", help="work-loop ledgers: list them, close one")
    p.add_argument("action", choices=["list", "close"])
    p.add_argument("id", nargs="?", help="loop id (for close)")
    sub.add_parser("list", help="show scheduled jobs")
    p = sub.add_parser("remove", help="delete a job"); p.add_argument("id")
    p = sub.add_parser("pause", help="stop a job without losing it"); p.add_argument("id")
    p = sub.add_parser("resume", help="restart a paused job"); p.add_argument("id")
    p = sub.add_parser("adopt", help="claim jobs stamped for another machine")
    p.add_argument("id", help="job id, or `all`")
    a = ap.parse_args(argv)

    if a.cmd == "add":
        if a.in_:
            # Resolve to an absolute time immediately: the job file must say
            # WHEN, not "a minute after whenever this was typed", or a restore
            # from backup would re-fire it relative to the restore.
            try:
                value = (datetime.now() + parse_every(a.in_)).strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                print("[X] --in wants forms like 20m, 2h, 1d")
                return 2
            a.at, kind = value, "at"
        kind = "every" if a.every else "daily" if a.daily else "at"
        value = a.every or a.daily or a.at
        if kind == "at" and len(value) == 16:
            value += ":00"                       # accept YYYY-MM-DDTHH:MM
        # A window on a one-shot or a fixed daily time is meaningless, and
        # honouring it would silently move the time he asked for.
        if a.between and kind != "every":
            print("[X] --between only applies to --every "
                  f"(--{kind} already names an exact time)")
            return 2
        # --to is validated on EVERY add: the schedule was the last writer that
        # could still invent a phantom desk with a typo (docs\DELEGATION.md).
        to = str(a.to or "").strip().lower()
        if not legal_desk_shape(to):
            print(f"[X] '{a.to}' is not a desk id - they look like: orchestrator, "
                  f"daybook, tool.<name>, <project>.<component>")
            return 2
        if not desk_cwd(to).is_dir():
            print(f"[X] no such desk: {to} (no folder) - `list` the fleet or check the spelling")
            return 2
        # Self-continuation must be a counted LOOP (D5). This replaces the old
        # prose cap ("never queue a continuation more than twice") with a budget
        # nothing has to remember.
        me = own_session()
        led = None
        workflow_tid = None
        if a.loop and a.loop.startswith("t-"):
            # A WORKFLOW continuation (D11). The budget lives on the chain's
            # ledger rather than on a per-desk loop file, which is the whole
            # point: the same piece of work may re-queue itself on three desks
            # in turn and still be counted once, against the goal.
            wf = thread_workflow(a.loop)
            tled = load_thread(a.loop) or {}
            home = (tled.get("origin") or {}).get("session") or "the desk that started it"
            if wf is None:
                print(f"[X] no workflow on chain {a.loop} - open one by sending "
                      f"desk mail carrying a `workflow` block, or use "
                      f"`--loop auto` for an ordinary per-desk loop")
                return 2
            if wf.get("status") not in (None, "open", "stalled"):
                print(f"[X] workflow on {a.loop} is {wf.get('status')} - report what "
                      f"EXISTS to {home} and let him decide. A fresh instruction "
                      f"opens a NEW workflow; workflows never extend themselves.")
                return 2
            if int(wf.get("runs") or 0) >= int(wf.get("budget") or 0):
                print(f"[X] workflow on {a.loop} has used its budget "
                      f"({wf.get('runs')}/{wf.get('budget')} runs) - report what "
                      f"EXISTS to {home} and ask whether to continue.")
                return 2
            if any(j.get("workflow") == a.loop for j in load_jobs()):
                print(f"[X] workflow {a.loop} already has a queued continuation - "
                      f"one at a time; a chain is a chain, not a fan")
                return 2
            workflow_tid = a.loop
        elif a.loop:
            budget = loop_budget_default()
            if a.loop == "auto":
                if a.max is not None and a.max > budget:
                    print(f"[X] --max {a.max} exceeds the configured loop_budget "
                          f"({budget}) - raise it in config\\omnius.ini [delegation] "
                          f"if that is really wanted")
                    return 2
                led = open_loop(to, min(a.max or budget, budget), a.channel)
            else:
                led = load_loop(a.loop)
                if led is None:
                    print(f"[X] no loop with id {a.loop} - `loop list` shows them; "
                          f"--loop auto opens a new one")
                    return 2
                if led.get("session") != to:
                    print(f"[X] loop {led['id']} belongs to {led.get('session')}, "
                          f"not {to} - a loop is one desk's chain")
                    return 2
                if led.get("closed"):
                    print(f"[X] loop {led['id']} is closed ({led['closed']}) - a "
                          f"fresh owner instruction opens a NEW loop")
                    return 2
                if int(led.get("fired") or 0) >= int(led.get("max") or 0):
                    print(f"[X] loop {led['id']} has used its budget "
                          f"({led.get('fired')}/{led.get('max')}) - report what "
                          f"EXISTS to the owner and ask whether to continue. A "
                          f"fresh owner instruction opens a NEW loop.")
                    return 2
                if a.channel:
                    led["channelId"] = a.channel
                    save_loop(led)
            if any(j.get("loop") == led["id"] for j in load_jobs()):
                print(f"[X] loop {led['id']} already has a queued continuation - "
                      f"one at a time; a loop is a chain, not a fan")
                return 2
        elif me and to == me:
            print("[X] a self-addressed continuation must carry --loop - the "
                  "budgeted form of exactly this (docs\\DELEGATION.md D5):")
            print("    add --in 2m --to <you> --loop auto "
                  "--text \"Continue: <step>.  Done when: <command> exits 0.\"")
            return 2
        try:
            job = add_job(kind, value, to, a.text,
                          weekdays=a.weekdays, between=a.between,
                          loop=(led or {}).get("id"),
                          workflow=workflow_tid,
                          channel=a.channel or (led or {}).get("channelId"))
        except ValueError as e:
            print(f"[X] {e}")
            return 2
        print(f"scheduled {job['id']} -> {job['to']} at {job['nextRun']}")
        if led:
            print(f"  loop {led['id']} run {int(led.get('fired') or 0) + 1}"
                  f"/{led.get('max')} (fires are counted at delivery)")
        if workflow_tid:
            _wf = thread_workflow(workflow_tid) or {}
            print(f"  workflow {workflow_tid} run {int(_wf.get('runs') or 0) + 1}"
                  f"/{_wf.get('budget')} (fires are counted at delivery)")
        for when in upcoming(job)[1:]:
            print(f"  then {when.strftime(FMT)}")
        return 0

    if a.cmd == "loop":
        if a.action == "list":
            loops = list_loops()
            if not loops:
                print("no work loops")
                return 0
            for led in loops:
                print(describe_loop(led))
            return 0
        if not a.id:
            print("[X] loop close wants the loop id (`loop list` shows them)")
            return 2
        led = load_loop(a.id)
        if led is None:
            print(f"[X] no loop with id {a.id}")
            return 2
        if not led.get("closed"):
            led["closed"] = "done"
            save_loop(led)
        jobs = load_jobs()
        kept = [j for j in jobs if j.get("loop") != led["id"]]
        if len(kept) != len(jobs):
            save_jobs(kept)
            print(f"closed {led['id']} and removed its queued continuation")
        else:
            print(f"closed {led['id']}")
        return 0

    if a.cmd in ("pause", "resume"):
        job = set_paused(a.id, a.cmd == "pause")
        if not job:
            print(f"[X] no job with id {a.id}")
            return 2
        print(f"{a.cmd}d {a.id}")
        return 0

    if a.cmd == "adopt":
        claimed = adopt(a.id)
        if not claimed:
            print("nothing to adopt - every job already belongs to this machine")
            return 0
        for j in claimed:
            print(f"adopted {j['id']} -> {machine_name()}")
        return 0

    if a.cmd == "list":
        jobs = load_jobs()
        if not jobs:
            print("no scheduled jobs")
            return 0
        me = machine_name()
        for j in sorted(jobs, key=lambda x: x.get("nextRun") or ""):
            print(describe(j, me))
        return 0

    jobs = load_jobs()
    kept = [j for j in jobs if j["id"] != a.id]
    if len(kept) == len(jobs):
        print(f"[X] no job with id {a.id}")
        return 2
    save_jobs(kept)
    print(f"removed {a.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
