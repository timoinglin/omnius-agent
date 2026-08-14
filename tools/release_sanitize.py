#!/usr/bin/env python3
"""Post-process a -Fresh release zip: neutralise this machine, then audit it.

    python tools\\release_sanitize.py <zip>

Two jobs, in order.

NEUTRALISE. Hook commands are absolute paths on the machine that built the
release (they have to be - see fix_hook_paths.py), so they are written into the
gitignored settings.local.json and never into a tracked settings.json. Any that
turn up in one here are dropped: install.bat writes the target machine's own,
and shipping a path that is not there BLOCKS every prompt at that desk.

AUDIT. Then read every text entry back and refuse if anything still identifies
the building instance. The build is only a release if this passes; a leak found
here costs a rebuild, a leak found after distribution cannot be recalled.
"""
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

PLACEHOLDER = "<OMNIUS_ROOT>"

IDENTIFYING = {
    # STRUCTURAL patterns only - things that identify any instance, shaped the
    # same everywhere: Discord snowflakes, real mailboxes, this workspace's own
    # absolute path. The NAME patterns (owner, machines, orgs, projects) live in
    # config\audit-sentinels.txt, which is gitignored. They used to be right
    # here, which meant every release shipped the exact list of names it
    # existed to catch - found 2026-08-14 while preparing the public repo.
    "discord/guild id": re.compile(r"\b\d{17,20}\b"),
    # config\ (2026-08-05) is the first thing here that can hold a real mailbox.
    # RFC 2606 reserves example.com/.net/.org for documentation, so the
    # templates can show an address without tripping this - anything else in a
    # shipped file is somebody's actual account.
    "email address": re.compile(
        r"[\w.+-]+@(?!example\.(?:com|net|org)\b)[\w-]+\.[\w.-]*[\w]", re.I),
}
# Files whose job is to NAME what remains above. Exempting them is not a
# loophole: a guard must contain the structural patterns it checks for.
AUDIT_EXEMPT = ("pack.ps1", "tools/release_sanitize.py", "tools/discord/test_watchdog.py")
# .ini added 2026-08-05 with config\: an unlisted suffix is neither scrubbed nor
# audited, so a settings file would have shipped completely unchecked.
TEXT_SUFFIXES = (".md", ".json", ".py", ".ps1", ".bat", ".txt", ".yml", ".yaml", ".ini")

SENTINELS = Path(__file__).resolve().parent.parent / "config" / "audit-sentinels.txt"


def load_sentinels(path=None):
    """-> (identifying: dict, subs: list) from config\\audit-sentinels.txt.

    One rule per line: `<regex>` or `<regex> => <replacement>`. Missing file is
    a valid state - a fresh install has no names yet - so it means "no name
    rules", never an error. A line that does not compile is reported and
    SKIPPED rather than silently weakening its neighbours.
    """
    path = Path(path) if path else SENTINELS
    ident, subs = {}, []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return ident, subs
    for i, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pat, _, repl = line.partition("=>")
        pat, repl = pat.strip(), (repl.strip() or "<redacted>")
        try:
            rx = re.compile(pat, re.I)
        except re.error as e:
            print(f"  [!] audit-sentinels.txt:{i}: bad pattern ({e}) - line skipped")
            continue
        ident[f"sentinel:{i}"] = rx
        subs.append((rx, repl))
    return ident, subs


# Comments and examples across the codebase name this instance's projects,
# machine and paths - they were written as incident records and they earn their
# place in the SOURCE. The release is a derived artifact, so it is sanitised on
# the way out rather than the history being gutted to make it shippable.
# The workspace's own absolute path is derived, not hard-coded, so this file
# never needs to contain anybody's username.
_ROOT = Path(__file__).resolve().parent.parent
SUBS = [
    (re.compile(re.escape(str(_ROOT)).replace(re.escape("\\"), r"\+"), re.I), PLACEHOLDER),
    (re.compile(re.escape(str(_ROOT).replace("\\", "/")), re.I), PLACEHOLDER),
]

# Never rewritten: a source file whose JOB is to hold these strings would stop
# working if they were replaced.
SUBS_EXEMPT = ("tools/release_sanitize.py", "pack.ps1", "tools/discord/test_watchdog.py")


def _all_subs():
    return load_sentinels()[1] + SUBS


def _all_identifying():
    d = dict(IDENTIFYING)
    d.update(load_sentinels()[0])
    return d


def scrub_text(zip_path):
    """Replace identifying tokens in every text entry. -> entries changed."""
    src = Path(zip_path)
    tmp = src.with_suffix(".scrub.zip")
    changed = []
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            rel = item.filename.split("/", 1)[1] if "/" in item.filename else item.filename
            rel = rel.replace("\\", "/")
            if item.filename.endswith(TEXT_SUFFIXES) \
                    and not any(rel.endswith(x) for x in SUBS_EXEMPT):
                try:
                    text = original = data.decode("utf-8")
                    for pat, rep in _all_subs():
                        text = pat.sub(rep, text)
                    if text != original:
                        data = text.encode("utf-8")
                        changed.append(rel)
                except UnicodeDecodeError:
                    pass
            zout.writestr(item, data)
    shutil.move(str(tmp), str(src))
    return changed


def neutralise(zip_path, root_name):
    """Take hooks out of every settings.json in the archive. -> files changed.

    Since 2026-08-14 hooks live in the gitignored settings.local.json, which
    pack.ps1 excludes, so a settings.json carrying any is either an instance
    installed before that or a hand-edit. Either way the answer is to DROP the
    block, not to rewrite the path to `<OMNIUS_ROOT>` as this used to.

    Measured that day: a hook whose script is not there BLOCKS every prompt at
    that desk. A placeholder path is such a hook, so the old behaviour shipped
    an archive that bricked its own desks in the window between unzipping and
    running install.bat. No hooks is the safe state - install writes the real
    ones for the target machine, which it has to do regardless.
    """
    src = Path(zip_path)
    tmp = src.with_suffix(".tmp.zip")
    changed = []
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith(".claude/settings.json"):
                try:
                    cfg = json.loads(data.decode("utf-8"))
                    if cfg.pop("hooks", None) is not None:
                        data = (json.dumps(cfg, indent=2) + "\n").encode("utf-8")
                        changed.append(item.filename)
                except (UnicodeDecodeError, ValueError):
                    pass
            zout.writestr(item, data)
    shutil.move(str(tmp), str(src))
    return changed


def strip_fleet_desks(zip_path):
    """Empty `desks` in config\\fleet.json. -> True if it changed anything.

    fleet.json ships on purpose: a fresh machine must spawn the right thing on
    day one, and `.env` never travels. But its `desks` map is INSTANCE STATE,
    not product - per-desk model/effort overrides keyed by session id, and a
    session id is `<project>.<component>`, i.e. the owner's project names.

    Found 2026-08-10, the first time -Fresh ran after `!model` shipped: a
    single `!restart max` on a project desk wrote a real key into fleet.json
    and the release refused. The guard was right; the file was wrong to carry
    it. A new instance has no desks yet, so the correct value is empty.

    The `_comment` survives (it explains the shape) with a neutral example.
    """
    src = Path(zip_path)
    tmp = src.with_suffix(".fleet.zip")
    hit = False
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.replace("\\", "/").endswith("config/fleet.json"):
                try:
                    cfg = json.loads(data.decode("utf-8"))
                    desks = cfg.get("desks")
                    if isinstance(desks, dict):
                        keep = {k: v for k, v in desks.items() if k.startswith("_")}
                        if keep != desks:
                            cfg["desks"] = keep
                            hit = True
                        data = (json.dumps(cfg, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
                except (UnicodeDecodeError, ValueError):
                    pass
            zout.writestr(item, data)
    shutil.move(str(tmp), str(src))
    return hit


def audit(zip_path):
    """-> list of (entry, what) that still identifies the building instance."""
    bad = []
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            rel = name.split("/", 1)[1] if "/" in name else name
            if name.endswith("/") or not name.endswith(TEXT_SUFFIXES):
                continue
            if any(rel.replace("\\", "/").endswith(x) for x in AUDIT_EXEMPT):
                continue
            try:
                text = z.read(name).decode("utf-8")
            except (UnicodeDecodeError, KeyError):
                continue
            for what, pat in _all_identifying().items():
                if pat.search(text):
                    bad.append((rel, what))
    return bad


def main(argv):
    if not argv:
        print("usage: release_sanitize.py <zip>", file=sys.stderr)
        return 2
    zip_path = Path(argv[0])
    if not zip_path.is_file():
        print(f"no such archive: {zip_path}", file=sys.stderr)
        return 2
    with zipfile.ZipFile(zip_path) as z:
        root = z.namelist()[0].split("/", 1)[0]
    changed = neutralise(zip_path, root)
    print(f"  {len(changed)} settings file(s) now use {PLACEHOLDER}")
    scrubbed = scrub_text(zip_path)
    print(f"  {len(scrubbed)} text file(s) scrubbed of names, paths and ids")
    if strip_fleet_desks(zip_path):
        print("  fleet.json: per-desk overrides removed (instance state, not product)")
    bad = audit(zip_path)
    if bad:
        print("\n  RELEASE REFUSED - these still identify the building instance:")
        for rel, what in sorted(set(bad)):
            print(f"    {rel}  ({what})")
        return 1
    print("  audit clean: nothing in the archive identifies this instance")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
