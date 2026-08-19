#!/usr/bin/env python3
"""Does THIS instance own the repo it updates from? -> maintainer / user.

    python tools\\repo_access.py          say which, and why
    python tools\\repo_access.py --json   the same, for a script

WHY THIS EXISTS. Every instance runs the same code and reads the same CLAUDE.md,
so every instance believed it was the source of the project: desks improved
something, committed it, tried to push, and hit a wall they could not reason
about - the remote is a public repo they have no write access to. Meanwhile the
one instance that DOES own it needs exactly the opposite behaviour.

Nothing in the tree distinguishes them, so ASK GIT. `git push --dry-run` is the
only honest test of write access: it authenticates against the real remote and
changes nothing. The answer is cached, because it is a network round trip and it
changes about once in the life of an install.

NEVER PROMPTS. Every credential helper is muzzled before the probe runs - a
watchdog blocked on a Windows credential dialog nobody is looking at is a worse
failure than any wrong answer here. No credentials means "user", which is both
the safe default and the true one.

An explicit `[fleet] maintainer` in config\\omnius.ini overrides the probe, for
an owner who is offline or works through a mirror.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import omnius_config as ocfg  # noqa: E402

CACHE = ROOT / "state" / "watchdog" / "repo-access.json"
# Asymmetric on purpose. "You can push" is a durable fact about an account and
# is worth a week. "You cannot" is also what a dropped wifi, a locked credential
# store or a laptop on a train looks like - and an owner's machine that answered
# once while offline should not be demoted for a week over it.
CACHE_TTL_PUSH = 7 * 24 * 3600
CACHE_TTL_NO_PUSH = 6 * 3600
PROBE_TIMEOUT = 25             # seconds; a stuck probe must never hold the loop


def _quiet_env():
    """git, with every way of asking a human for a password switched off."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"        # no "Username for ..." on the console
    env["GCM_INTERACTIVE"] = "never"        # no Windows credential-manager window
    env["GIT_ASKPASS"] = ""                 # no GUI askpass helper
    env["SSH_ASKPASS"] = ""
    env["GIT_CONFIG_PARAMETERS"] = "'credential.interactive=never'"
    return env


def _git(*args, timeout=PROBE_TIMEOUT):
    try:
        p = subprocess.run(["git", "-C", str(ROOT)] + list(args),
                           capture_output=True, text=True, timeout=timeout,
                           env=_quiet_env(),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:                                       # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def _cached():
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        ttl = CACHE_TTL_PUSH if d.get("push") else CACHE_TTL_NO_PUSH
        if time.time() - float(d.get("at") or 0) < ttl:
            return d
    except (OSError, ValueError, TypeError):
        pass
    return None


def _remember(push, why):
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"push": bool(push), "why": why, "at": time.time()}),
                       encoding="utf-8")
        tmp.replace(CACHE)
    except OSError:
        pass


def can_push(refresh=False):
    """-> (bool, why). True only when a push to origin/main would be accepted."""
    # default=False, not "0": get_bool returns the default UNCHANGED when the key
    # is absent, and the string "0" is truthy - which would have made every
    # instance a maintainer, the exact bug this module exists to end.
    if ocfg.get_bool(ocfg.load("omnius"), "fleet", "maintainer",
                     "OMNIUS_MAINTAINER", False):
        return True, "config\\omnius.ini says [fleet] maintainer = 1"
    if not refresh:
        hit = _cached()
        if hit:
            return bool(hit.get("push")), str(hit.get("why") or "cached")
    rc, _out = _git("rev-parse", "--is-inside-work-tree", timeout=10)
    if rc != 0:
        return False, "this install is not a git workspace"
    # --dry-run does everything a push does except write: it resolves the
    # remote, authenticates, and reports what WOULD move. Refusal here is what
    # read-only access looks like.
    rc, out = _git("push", "--dry-run", "--porcelain", "origin", "HEAD:refs/heads/main")
    if rc == 0:
        _remember(True, "git push --dry-run was accepted by origin")
        return True, "git push --dry-run was accepted by origin"
    first = next((ln.strip() for ln in out.splitlines() if ln.strip()), "refused")
    _remember(False, first[:200])
    return False, first[:200]


def describe():
    push, why = can_push()
    role = "maintainer" if push else "user"
    print(f"role: {role}")
    print(f"why : {why}")
    if not push:
        print("commit locally as much as you like - !update rebases your commits "
              "onto each release, so local work survives updates. Just do not "
              "push: this instance does not own the remote.")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--json" in argv:
        push, why = can_push(refresh="--refresh" in argv)
        print(json.dumps({"role": "maintainer" if push else "user",
                          "canPush": push, "why": why}))
        return 0
    if "--refresh" in argv:
        can_push(refresh=True)
    return describe()


if __name__ == "__main__":
    sys.exit(main())
