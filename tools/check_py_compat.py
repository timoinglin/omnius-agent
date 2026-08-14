#!/usr/bin/env python3
"""Refuse Python syntax that needs a newer interpreter than we promise.

    python tools\\check_py_compat.py            # whole repo, exit 1 on a finding

README, install.ps1 and GETTING-STARTED all say **Python 3.10+**. On 2026-08-11
a fresh install on Python 3.11 crash-looped every 60 seconds because
`watchdog.py` used PEP 701 syntax - reusing the SAME quote character inside an
f-string expression:

    f"{f' (pid {", ".join(...)})' if pids else ''}. "
                 ^ SyntaxError: f-string: unterminated string

That is valid from 3.12 and a hard SyntaxError before it. It never failed here
because this machine runs 3.14, and nothing checked - so the promise and the
code had quietly disagreed for who knows how long.

`ast.parse(..., feature_version=(3,10))` does NOT catch it: feature_version
tunes a few grammar decisions, while PEP 701 changed the TOKENIZER, so a modern
interpreter simply accepts it. Hence this scanner, which walks the source and
flags a quote of the same kind reopened while inside an f-string's `{...}`.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = {"node_modules", ".venv", "__pycache__", ".git"}
# The scan below reads FSTRING_START tokens, which only exist from 3.12. Run on
# 3.10/3.11 it finds nothing and would print "clean" over a repo full of PEP 701
# - a guard that silently stops guarding, which is worse than no guard at all
# (this whole file exists because of one of those). Below 3.12 the interpreter
# IS the check: the offending syntax is a hard SyntaxError, so compile() sees
# exactly what a fresh install would hit, on the very version at risk.
CAN_TOKENIZE_PEP701 = sys.version_info >= (3, 12)


def nested_fstring_quotes(src):
    """-> [line numbers] where an f-string expression reuses its own quote char.

    Uses the real tokenizer, so comments, plain strings and docstrings cannot
    produce false hits - a hand scanner flagged this file's own example.

    On 3.12+ an f-string arrives as FSTRING_START / ... / FSTRING_END, and the
    expression parts are ordinary tokens in between. So the check is exact: a
    STRING token inside that span whose quote character matches the one the
    f-string opened with is precisely what PEP 701 legalised, and precisely
    what a 3.11 tokenizer rejects.
    """
    import io
    import tokenize as tk

    hits, stack = [], []
    try:
        for tok in tk.generate_tokens(io.StringIO(src).readline):
            name = tk.tok_name.get(tok.type, "")
            if name == "FSTRING_START":
                stack.append(tok.string[-1])            # the quote it opened with
            elif name == "FSTRING_END":
                if stack:
                    stack.pop()
            elif name == "STRING" and stack:
                text = tok.string.lstrip("rRbBuUfF")
                # Against EVERY open f-string, not just the innermost. The real
                # case was `f"{f' (pid {", ".join(...)})' ...}"`: the inner
                # f-string uses ', and the clash is with the OUTER ". Checking
                # only stack[-1] reported the file clean.
                if text[:1] in stack:
                    hits.append(tok.start[0])
    except (tk.TokenError, SyntaxError, IndentationError):
        pass                                            # unparseable: Python will say so
    return sorted(set(hits))


def parse_error(src, name):
    """-> (line, message) when THIS interpreter cannot parse the file at all.

    Only used below 3.12, where that is the whole point: the syntax we promise
    not to use is a SyntaxError there, so a failed compile on 3.11 is the same
    finding the token scan reports on 3.12+ - arrived at by the shortest route,
    and blind to nothing.
    """
    try:
        compile(src, str(name), "exec")
    except SyntaxError as e:
        return (e.lineno or 0, e.msg)
    except ValueError as e:                  # null bytes and friends
        return (0, str(e))
    return None


def main():
    bad = []
    for p in sorted(ROOT.rglob("*.py")):
        if SKIP & set(p.parts):
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if CAN_TOKENIZE_PEP701:
            for ln in nested_fstring_quotes(src):
                text = src.splitlines()[ln - 1].strip()
                bad.append((p.relative_to(ROOT), ln, text))
        else:
            hit = parse_error(src, p)
            if hit:
                ln, msg = hit
                lines = src.splitlines()
                text = lines[ln - 1].strip() if 0 < ln <= len(lines) else msg
                bad.append((p.relative_to(ROOT), ln, f"{msg}: {text}"))
    how = ("token scan (3.12+)" if CAN_TOKENIZE_PEP701
           else f"parsed by this interpreter (Python {sys.version_info.major}."
                f"{sys.version_info.minor})")
    if not bad:
        print(f"py-compat: clean - nothing needs newer than Python 3.10  [{how}]")
        return 0
    print(f"py-compat: syntax found that a supported interpreter rejects  [{how}]:\n")
    for rel, ln, text in bad:
        print(f"  {rel}:{ln}\n      {text[:110]}")
    print("\n  Fix: use a different quote inside the expression, or hoist it to a variable.")
    return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
