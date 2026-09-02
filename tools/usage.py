#!/usr/bin/env python3
"""Plan usage - the 5-hour and weekly limit percentages, readable from Discord.

    python tools\\usage.py           the summary he actually asked for
    python tools\\usage.py --full    ...plus the contributing-factors breakdown
    python tools\\usage.py --raw     exactly what the CLI printed

WHY THIS EXISTS. Those percentages live on Anthropic's servers, not on disk -
nothing under `~\\.claude\\` caches them and `claude --help` has no `usage`
subcommand, so they look unreachable from a script. They are not: the built-in
`/usage` works in HEADLESS mode too, and prints the same numbers the TUI panel
draws. Owner, 2026-09-02: "si estoy en discord no puedo poner /usage en
terminal" - which is the whole point, since he reads Discord from a phone.

NOT the same thing as cost. `memory\\orchestrator\\topics\\claude-cost.md`
estimates what the work WOULD cost per token; this is how much of the flat
plan is spent. He is on a subscription, so this is the number that can
actually run out.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

# There used to be a NOISE filter here, dropping the warning Claude Code prints
# for every dead `Write(path)` deny rule in settings.json. It was treating the
# symptom: the rules did nothing except produce those lines. The four of them
# were deleted from sync_permissions.py on 2026-09-02, so there is no noise left
# to filter - and filtering warnings is how a real one goes unread.

# The lines he asked for, in the order the panel shows them.
WANTED = re.compile(r"Current session:|Current week", re.I)


def run(timeout=240):
    """-> (text, error). Never raises: a usage check must not break a desk.

    TWO THINGS HERE ARE SCARS, both from the hour this shipped (2026-09-02).

    NO --max-turns. The first version capped it at 1. That passed my test,
    because that run happened to answer in one turn - then failed the very
    first time he typed `/usage`, the cap swallowing the output and reporting
    only `Error: Reached max turns (1)`. How many turns the CLI needs to render
    its own panel is not ours to predict; the timeout is the real bound.

    RUN FROM A NEUTRAL DIRECTORY, and this one is the important one. Started
    inside the workspace, `claude -p "/usage"` loads THIS repo's CLAUDE.md and
    finds the /usage SKILL - which tells it to run this script, which starts
    another `claude -p "/usage"`. I built that loop by adding the skill, so the
    tool worked when tested and broke the moment it was wired up: a desk sat
    there for 218 seconds answering itself. An empty cwd has no CLAUDE.md and
    no skills, so the built-in slash command is all that remains. It also drops
    the round trip from minutes to ~5 seconds.
    """
    neutral = os.path.join(tempfile.gettempdir(), "omnius-usage-cwd")
    try:
        os.makedirs(neutral, exist_ok=True)
    except OSError:
        neutral = tempfile.gettempdir()
    try:
        p = subprocess.run(["claude", "-p", "/usage"], cwd=neutral,
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return "", "the `claude` CLI is not on PATH"
    except subprocess.TimeoutExpired:
        return "", f"the CLI did not answer within {timeout}s"
    out = (p.stdout or "").strip("\n")
    if "Current session" not in out and "subscription" not in out:
        # Report what the CLI actually said. The first failure here printed only
        # "no usage block came back", which named the symptom and hid the cause
        # ("Error: Reached max turns (1)") - one line that would have pointed
        # straight at the bug instead of at a mystery.
        detail = [l.strip() for l in (out + "\n" + (p.stderr or "")).splitlines()
                  if l.strip()]
        return out.strip(), ("no usage block came back"
                             + (f" - CLI said: {detail[-1][:150]}" if detail else "")
                             + f" (exit {p.returncode})")
    return out.strip(), None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true", help="include the breakdown")
    ap.add_argument("--raw", action="store_true", help="print the CLI output verbatim")
    a = ap.parse_args(argv)

    text, err = run()
    if err:
        print(f"[X] could not read plan usage: {err}")
        if text:
            print(text[:400])
        return 1
    if a.raw:
        print(text)
        return 0

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    head = [l for l in lines if WANTED.search(l)]
    print("\n".join(head) if head else text)

    if a.full:
        # Everything from the "what's contributing" heading down - it explains a
        # high number, which is the only time he will ask why.
        for i, l in enumerate(lines):
            if l.lower().startswith("what's contributing"):
                print()
                print("\n".join(lines[i + 1:]))
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
