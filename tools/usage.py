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
import re
import subprocess
import sys

# Claude Code prints one of these to stderr for every dead `Write(path)` deny
# rule in settings.json (only `Edit(path)` rules cover file-editing tools). They
# are harmless and unrelated to usage, but there are a dozen and they would bury
# the answer. Dropped by shape, not by count, so a NEW warning still shows up.
NOISE = re.compile(
    r"Permission deny rule|is not matched by file permission checks"
    r"|Use Edit\(|Edit rules cover all file-editing tools", re.I)

# The lines he asked for, in the order the panel shows them.
WANTED = re.compile(r"Current session:|Current week", re.I)


def run(timeout=120):
    """-> (text, error). Never raises: a usage check must not break a desk."""
    try:
        p = subprocess.run(["claude", "-p", "/usage", "--max-turns", "1"],
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return "", "the `claude` CLI is not on PATH"
    except subprocess.TimeoutExpired:
        return "", f"the CLI did not answer within {timeout}s"
    out = "\n".join(l for l in (p.stdout or "").splitlines() if not NOISE.search(l))
    if "Current session" not in out and "subscription" not in out:
        detail = (p.stderr or "").strip().splitlines()
        return out.strip(), ("no usage block came back"
                             + (f" ({detail[-1][:120]})" if detail else ""))
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
