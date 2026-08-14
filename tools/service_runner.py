#!/usr/bin/env python3
"""Headless runner for the always-on services (Task Scheduler autostart).

    pythonw tools\\service_runner.py tools\\discord\\watchdog.py

Why this exists instead of pointing the scheduled task straight at the script:

`python.exe` is a console application, so a task that runs it at logon puts a
console window on the desktop - two of them, one per service. That window is
worse than ugly: closing it kills the service, and the whole point of the
autostart job is that nobody has to think about the services at all.

`pythonw.exe` has no console, but it leaves ``sys.stdout`` and ``sys.stderr``
as ``None``. Every ``print`` silently evaporates and, worse, an unhandled
traceback dies with the process leaving no trace anywhere - which is exactly
the failure the watchdog's own comments say must never happen again ("never
lose a startup failure"). The watchdog logs its handled errors to
``state\\logs\\watchdog.log`` itself; this runner covers everything that never
reaches that logger: import errors, tracebacks, third-party output.

So: give the service real streams pointed at ``state\\logs\\<name>.out.log``,
then run the target exactly as ``python <script>`` would (``__name__`` is
``__main__``, ``sys.argv`` is the target's own argv). Exit codes propagate
untouched - Task Scheduler's restart-on-failure depends on them.
"""
import os
import runpy
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "state" / "logs"
MAX_BYTES = 2_000_000   # same budget as watchdog.rotate_log, one generation kept


def rotate(path):
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def main():
    if len(sys.argv) < 2:
        print("usage: pythonw service_runner.py <script.py> [args...]", file=sys.stderr)
        return 2

    target = Path(sys.argv[1])
    if not target.is_absolute():
        target = ROOT / target          # relative paths are relative to the root
    target = target.resolve()

    LOGS.mkdir(parents=True, exist_ok=True)
    logfile = LOGS / f"{target.stem}.out.log"
    rotate(logfile)

    # line-buffered so a crash cannot swallow the last lines before the traceback
    stream = open(logfile, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = sys.stderr = stream
    sys.__stdout__ = sys.__stderr__ = stream   # anything caching the originals

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n===== {stamp} service_runner starting {target.name} "
          f"(pid {os.getpid()}) =====")

    if not target.is_file():
        # A stale task action after the workspace moved. Say so in the log the
        # user will actually look at; autostart.ps1 -Action status also catches it.
        print(f"FATAL: script not found: {target}")
        return 2

    sys.argv = [str(target)] + sys.argv[2:]
    # runpy runs the target IN THIS PROCESS, so a target that re-execs itself
    # (watchdog.py's !reload) would replace us and quietly lose supervision:
    # what came back was a bare `python watchdog.py`, no longer the scheduled
    # task's `service_runner.py watchdog.py`. Nothing looked wrong - the task
    # read Ready instead of Running and self-healed every 60s into a no-op.
    # Leave a way for the target to re-exec THROUGH us and stay supervised.
    os.environ["OMNIUS_SERVICE_RUNNER"] = str(Path(__file__).resolve())
    try:
        runpy.run_path(str(target), run_name="__main__")
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        print(f"----- {target.name} exited with code {code}")
        return code
    except BaseException:
        # Includes KeyboardInterrupt: under a scheduled task there is no console
        # to press Ctrl+C in, so if it shows up it is real and worth recording.
        print(f"----- {target.name} CRASHED")
        traceback.print_exc()
        return 1
    print(f"----- {target.name} returned normally")
    return 0


if __name__ == "__main__":
    sys.exit(main())
