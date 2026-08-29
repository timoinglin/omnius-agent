#!/usr/bin/env python3
"""Refuse a commit MESSAGE that names this instance.

    python tools\\audit_message.py <file>          # what a commit-msg hook passes
    git log -1 --format=%B | python tools\\audit_message.py -

config\\audit-sentinels.txt opens by saying these names must never ship "in a
release **or a commit**", and until 2026-08-29 only FILES were ever gated. That
day a commit whose prose named a project, its domain and this machine's
hostname reached the public repo with every tracked file clean: the suite scans
tracked files, the release audit scans the zip, and neither reads the one place
the same prose also lives. It was found while cutting a release - after the
push, which is the half that cannot be recalled. So the gate belongs at the
moment the prose is written, not at the moment it ships.

Reuses release_sanitize's rules on purpose: one sentinel parser, one set of
structural patterns, no second list to drift out of step (the codebase already
learned that with omnius_config).

Exit 0 clean, 1 refused, 2 usage. A missing sentinel file is a valid state - a
fresh instance has no names to protect - and means "no name rules", never an
error.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_sanitize as rs  # noqa: E402

# Git writes its own instructions into the commit-msg file as `#` lines: the
# branch, the file list, the path of the repo. Scanning those would refuse
# every commit on a machine whose checkout sits under a real home directory.
COMMENT = re.compile(r"^\s*#")

# Trailers are metadata, not prose, and the standard ones carry an address by
# design - every commit in this tree ends with a Co-Authored-By naming a
# no-reply mailbox. Gating them would block all work; they are also not where a
# leak hides, because nobody writes an incident record in a trailer.
#
# Note the address is DESCRIBED and not quoted here. The release audit refuses
# any file carrying one outside example.com, and it duly refused this file on
# its first build - a guard is not exempt from the rule it enforces.
TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:\s")


def prose(text):
    """-> [(line_no, line)] of the lines a human actually wrote.

    Git's `#` comments go, and so does the trailer block. "Trailer block" is
    git's own definition - the LAST paragraph, and only when every line in it
    is a trailer. Popping trailer-shaped lines off the end one at a time is the
    obvious version and it is wrong: it walks straight past the blank line and
    eats a `Note: ...` paragraph out of the body, which is prose, and prose is
    the only thing this file exists to read.
    """
    lines = [(i, ln) for i, ln in enumerate(text.splitlines(), 1)
             if not COMMENT.match(ln)]
    while lines and not lines[-1][1].strip():
        lines.pop()
    last = len(lines)
    while last and lines[last - 1][1].strip():
        last -= 1
    if last < len(lines) and all(TRAILER.match(ln) for _, ln in lines[last:]):
        lines = lines[:last]
    return lines


def scan(text, rules=None):
    """-> [(label, line_no, line)] for every rule that matches, not just the first.

    Every rule against every line. The suite's file scan `break`s on the first
    hit per file, which is why the hostname in that commit stayed invisible
    behind the project name until someone scanned again by hand - one report,
    two leaks, one of them unmentioned.
    """
    rules = rs._all_identifying() if rules is None else rules
    hits = []
    for n, line in prose(text):
        for label, rx in rules.items():
            if rx.search(line):
                hits.append((label, n, line.strip()))
    return hits


def main(argv):
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: audit_message.py <file>|-", file=sys.stderr)
        return 2
    src = argv[0]
    try:
        text = (sys.stdin.read() if src == "-"
                else Path(src).read_text(encoding="utf-8-sig", errors="replace"))
    except OSError as e:
        print(f"audit_message: cannot read {src}: {e}", file=sys.stderr)
        return 2

    hits = scan(text)
    if not hits:
        return 0

    print("", file=sys.stderr)
    print("COMMIT REFUSED - the message identifies this instance:", file=sys.stderr)
    for label, n, line in hits:
        print(f"    line {n}  ({label})  {line[:100]}", file=sys.stderr)
    print("", file=sys.stderr)
    print("  A pushed commit message cannot be recalled: rewording it means a", file=sys.stderr)
    print("  force-push, which still leaves the old commit reachable by SHA.", file=sys.stderr)
    print("  Say the same thing without the name - 'the site this desk answers", file=sys.stderr)
    print("  for', 'the PC's own hostname' - and commit again.", file=sys.stderr)
    print("", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
