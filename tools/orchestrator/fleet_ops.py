#!/usr/bin/env python3
"""Mechanics behind the orchestrator verbs (ARCHITECTURE par. 3.6 / 5.1 / 5.5).

    python tools\\orchestrator\\fleet_ops.py new-project recipe-app --components app backend
    python tools\\orchestrator\\fleet_ops.py spawn recipe-app.app
    python tools\\orchestrator\\fleet_ops.py status [--json] [--prune]
    python tools\\orchestrator\\fleet_ops.py archive recipe-app [--move]
    python tools\\orchestrator\\fleet_ops.py unarchive recipe-app

The SKILLS (`.claude\\skills\\new-project` etc.) own the judgement: asking the
user, confirming destructive steps, writing memory. THIS file owns the mechanics
that must come out identical every time - stamping the template, filling
placeholders, git init, the Discord call, pruning claims.

Why split them at all: an agent improvising `mkdir` + `git init` + a Discord
call gets it subtly different every time, and "templates over improvisation"
(ARCHITECTURE par. 2.7) is what makes every later automation trivial. Anything a
script can do deterministically should not be left to a prompt.

EVERY VERB IS IDEMPOTENT - find-or-create, safe to re-run after a half-finished
attempt. That is a hard requirement, not a nicety: these run over Discord from a
phone, where a dropped reply looks exactly like a failure and the user's natural
next move is to say it again.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "discord"))
import api          # noqa: E402
import watchdog as wd   # noqa: E402

# A visible console for the desk window, without `start` and its shell quoting.
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)   # 0 off-Windows

TEMPLATE = ROOT / "templates" / "project"
PROJECTS = ROOT / "projects"
ARCHIVE = PROJECTS / "_archive"

# kebab-case, lowercase, no spaces (root CLAUDE.md par. 5). Enforced rather than
# suggested: the name becomes a folder, a git repo, a Discord category and a
# session id, and only one of those four would have complained on its own.
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

NOT_COMPONENTS = api.NOT_COMPONENTS


def fail(msg):
    """A refusal, as a normal exception.

    Deliberately NOT SystemExit: this module is a library first (the suite and
    the skills call into it) and SystemExit inherits from BaseException, so it
    sails straight through `except Exception` and takes the caller down with it.
    main() turns it back into an exit code for the CLI."""
    raise ValueError(msg)


def check_name(name):
    if not NAME_RE.match(name or ""):
        fail(f"{name!r} is not kebab-case - lowercase letters, digits and single "
             f"hyphens only (e.g. recipe-app)")
    return name


# --- new-project --------------------------------------------------------------

def fill_placeholders(text, project, description, components):
    """Replace the template's {{...}} markers.

    The components table is a single row in the template, so it is EXPANDED to
    one row per component rather than replaced in place - otherwise a two-desk
    project ships a CLAUDE.md that documents one desk."""
    rows = "\n".join(
        f"| {c}\\ | {{{{COMPONENT_PURPOSE}}}} | #{c} |".replace(
            "{{COMPONENT_PURPOSE}}", f"the {c} of {project}")
        for c in components)
    out = []
    for line in text.splitlines():
        if "{{COMPONENT}}\\" in line:
            out.append(rows)
        else:
            out.append(line)
    text = "\n".join(out)
    return (text.replace("{{PROJECT_NAME}}", project)
                .replace("{{PROJECT_DESCRIPTION}}", description or
                         "*(describe this project - one paragraph)*"))


def new_project(name, components, description="", discord=True, git=False, template=None):
    """Stamp a project. git defaults to OFF - owner's call, 2026-08-04:
    "a new project just needs the folder structure and the discord channels,
    the github part i do separately with the new session. Because maybe it is
    just a temp project." Pass git=True (--git) to opt in."""
    check_name(name)
    for c in components:
        check_name(c)
        if c in NOT_COMPONENTS:
            fail(f"{c!r} is a reserved folder name ({', '.join(sorted(NOT_COMPONENTS))})")
    if not components:
        fail("a project needs at least one component (e.g. --components app)")
    # --template lets a richer skeleton stand in for the default - the shipped
    # demo project (templates\demo-project) is the reason this exists. Same
    # copy rules either way: never overwrite, placeholders filled.
    tmpl = Path(template).resolve() if template else TEMPLATE
    if not tmpl.is_dir():
        fail(f"template missing: {tmpl}")

    dest = PROJECTS / name
    created, existed = [], dest.is_dir()
    dest.mkdir(parents=True, exist_ok=True)

    # Copy template files WITHOUT overwriting: re-running must never clobber a
    # CLAUDE.md the project has since edited.
    for src in sorted(tmpl.rglob("*")):
        rel = src.relative_to(tmpl)
        tgt = dest / rel
        if src.is_dir():
            tgt.mkdir(parents=True, exist_ok=True)
            continue
        if tgt.exists():
            continue
        tgt.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8") if src.suffix in (".md", ".json", ".gitignore") \
            or src.name == ".gitignore" else None
        if text is None:
            shutil.copy2(src, tgt)
        else:
            tgt.write_text(fill_placeholders(text, name, description, components),
                           encoding="utf-8")
        created.append(str(rel))

    for c in components:
        d = dest / c
        if not d.is_dir():
            d.mkdir(parents=True)
            # A component folder must not be empty, or git will not track it and
            # a fresh clone comes back missing the desk.
            (d / ".gitkeep").write_text("", encoding="utf-8")
            created.append(c + "\\")
    (dest / "memory" / "sessions").mkdir(parents=True, exist_ok=True)

    git_note = "skipped"
    if git and shutil.which("git"):
        if (dest / ".git").is_dir():
            git_note = "already a repo"
        else:
            try:
                subprocess.run(["git", "init", "-q"], cwd=dest, check=True,
                               capture_output=True)
                # A headless run has no --global git identity, so the first
                # commit died with "Author identity unknown" (<project>,
                # 2026-08-01). The root repo's local identity is the fleet's -
                # copy it into every stamped repo.
                for key in ("user.name", "user.email"):
                    val = subprocess.run(["git", "-C", str(ROOT), "config", key],
                                         capture_output=True, text=True).stdout.strip()
                    if val:
                        subprocess.run(["git", "config", key, val], cwd=dest,
                                       capture_output=True)
                subprocess.run(["git", "add", "-A"], cwd=dest, check=True,
                               capture_output=True)
                subprocess.run(["git", "commit", "-q", "-m",
                                f"{name}: stamped from templates/project"],
                               cwd=dest, check=True, capture_output=True)
                git_note = "initialised + first commit"
            except subprocess.CalledProcessError as e:
                git_note = f"git failed: {e.stderr.decode(errors='replace')[:120]}"

    # The stamped settings deliberately carry NO hooks - a hook command is an
    # absolute path, and the template travels between machines (see
    # fix_hook_paths.py). This writes them, per component folder, into the
    # gitignored .claude\settings.local.json each desk reads. Without it a
    # brand-new desk starts with no turn stamps at all, so the watchdog cannot
    # tell "terminal mid-turn" from "idle" on it.
    try:
        subprocess.run([sys.executable, str(ROOT / "tools" / "discord" / "fix_hook_paths.py")],
                       capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass

    discord_note = "skipped"
    if discord:
        try:
            api.ensure_project(name, log=lambda m: None)
            discord_note = "category + channels ensured"
        except Exception as e:
            # A Discord hiccup must not lose the folder work that already
            # succeeded - report it and let the caller re-run.
            discord_note = f"FAILED ({type(e).__name__}: {e}) - re-run to finish"

    return {"project": name, "path": str(dest), "existed": existed,
            "components": components, "created": created,
            "git": git_note, "discord": discord_note}


# --- status -------------------------------------------------------------------

def disk_projects():
    if not PROJECTS.is_dir():
        return {}
    out = {}
    for d in sorted(PROJECTS.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name in NOT_COMPONENTS:
            continue
        out[d.name] = api.project_components(d.name)
    return out


def claim_state(session):
    """-> (state, detail). Never trusts the claim file alone (RELIABILITY R2).

    States since the run model (2026-08-01): `working` = an active headless run
    (lease pid alive), `live` = an interactive terminal (claim pid alive),
    `stale` = provably nothing (prunable), `none` = no claim at all. There is no
    'listening' state any more because there is nothing that listens per-desk -
    reachability belongs to the watchdog, which starts a run when mail waits."""
    lease = wd.read_lease(session)
    if lease and wd.pid_alive(lease.get("pid")):
        return "working", f"headless run pid {lease['pid']}"
    c = wd.read_claim(session)
    if not c:
        return "none", ""
    if str(c.get("machine") or "") != api.MACHINE:
        return "stale", f"claimed by {c.get('machine')}"
    if not wd.pid_alive(c.get("pid")):
        return "stale", f"pid {c.get('pid')} is gone"
    return "live", f"terminal pid {c.get('pid')}"


def status(prune=False):
    projects = disk_projects()
    desks = []
    ids = {f.stem for f in wd.SESSIONS.glob("*.json")}
    if wd.RUNS.is_dir():
        ids |= {f.stem for f in wd.RUNS.glob("*.json")}
    for sid in sorted(ids):
        st, detail = claim_state(sid)
        pruned = False
        if prune and st == "stale":
            # Safe by construction: a dead pid on this machine, or a claim from
            # another machine, cannot be a session we would disturb. This is the
            # normal state of things after the workspace moves to a new PC.
            (wd.SESSIONS / f"{sid}.json").unlink(missing_ok=True)
            pruned = True
        desks.append({"session": sid, "state": st, "detail": detail, "pruned": pruned})

    beacon, wd_state = {}, "down"
    try:
        beacon = json.loads((wd.WD_STATE / "beacon.json").read_text(encoding="utf-8"))
        age = (datetime.now(timezone.utc)
               - datetime.strptime(beacon["at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                   tzinfo=timezone.utc)).total_seconds()
        wd_state = "listening" if age < 120 else f"stale beacon ({int(age)}s)"
        beacon["ageSeconds"] = int(age)
    except (OSError, ValueError, KeyError):
        pass
    return {"machine": api.MACHINE, "projects": projects, "desks": desks,
            "watchdog": {"state": wd_state, "beacon": beacon}}


# --- open a desk (visible terminal) --------------------------------------------

# The title logic went back into the watchdog with window:"terminal" mode
# (2026-08-01) - one owner, aliased here so callers and tests keep working.
tab_title = wd.tab_title


def open_desk(session, model=None, effort=None, force=False):
    """Open a VISIBLE terminal on a desk (the /spawn-session verb).

    Since the run model (2026-08-01) the watchdog opens no terminals - Discord
    mail is handled by headless one-shot runs it owns. This verb is the HUMAN
    case: "put a window on that folder so I can work there / watch it". The
    window is interactive claude, no -p; its session checks in via /omnius,
    which claims the desk and thereby holds headless runs off it while a turn
    is live (watchdog.interactive_busy).

    The occupancy guard is here because 2026-07-31 proved callers cannot be
    trusted to check first: a second session on a busy desk drains the same
    inbox and races on the same envelope files, invisibly to !status."""
    if not force and wd.session_alive(session):
        return {"session": session, "spawned": False,
                "note": "desk already occupied (live terminal or active run) - --force overrides"}
    cwd = wd.cwd_for(session)
    if not cwd.is_dir():
        return {"session": session, "spawned": False, "note": f"folder missing: {cwd}"}
    if not shutil.which("claude"):
        return {"session": session, "spawned": False, "note": "claude CLI not found on PATH"}
    wd.ensure_trusted_for(session, cwd)
    desk = wd.desk_config(session)
    model = str(model or desk["model"] or wd.DEFAULT_MODEL).strip()
    effort = str(effort or desk["effort"] or wd.DEFAULT_EFFORT).strip().lower()
    if effort not in wd.VALID_EFFORTS:
        effort = "xhigh"
    flags = f'--add-dir "{wd.ROOT}" --model {model} --effort {effort}'
    # --permission-mode, for the reason desk_bridge.claude_argv spells out:
    # passing no flag means Claude Code's default, and 2.1.261 made that `auto`,
    # whose classifier asks over the top of the allow-list and freezes a desk on
    # a dialog Discord cannot answer (2026-09-04). fleet.json names the mode.
    pm = str(desk.get("permissionMode") or "").strip()
    if pm in wd.VALID_PERMISSION_MODES:
        flags += f" --permission-mode {pm}"
    proj_settings = cwd.parent / ".claude" / "settings.json"
    if session != "orchestrator" and proj_settings.is_file():
        flags += f' --settings "{proj_settings}"'
    # --continue only with own history: in a virgin folder it attaches to the
    # most recent conversation from SOMEWHERE ELSE (see watchdog.start_run).
    #
    # ...and only when the desk's RESUME POLICY allows it. This was the third
    # and last launch path still deaf to `resume: "fresh"` (2026-08-16): the
    # watchdog's headless runs honoured it from 2026-08-01, the bridge from
    # earlier today, and a hand-opened `/spawn-session` orchestrator desk still
    # dragged the 5.9 MB dev transcript in behind it. Same ~200x context tax,
    # just entered through a different door. All three paths now read one
    # policy from fleet.json, so a future desk inherits it by ROLE with no
    # configuration at all.
    resume_mode = str(desk.get("resume") or "transcript").strip().lower()
    inner = (f'claude {flags} --continue "/omnius" || claude {flags} "/omnius"'
             if resume_mode != "fresh" and wd.has_history(cwd) else f'claude {flags} "/omnius"')
    wt = shutil.which("wt")
    try:
        if wt:
            subprocess.Popen([wt, "-w", "0", "new-tab", "--title", tab_title(session),
                              "-d", str(cwd), "cmd", "/k", inner])
        else:
            # Windows Terminal is not on stock Windows 10, so this is the normal
            # branch there. It must NOT go through `cmd /c start`: that treats
            # its first unquoted token as the program to run, and an argv list
            # never quotes a bare word - so a one-word title like "Omnius" or
            # "Daybook" asked Windows to run a program by that name and popped
            # an error dialog. (A title WITH spaces got quoted by accident and
            # worked, which is why this survived: the desks that broke were the
            # ones named after a single word.) 2026-08-15, watchdog.open_tab
            # had the identical bug.
            # A command STRING, not a list: `inner` contains quotes and `||`,
            # which cmd must parse - and Python's list quoting uses \" , which
            # cmd does not read as an escape. See watchdog.open_tab for the
            # failure that produced (2026-08-15).
            subprocess.Popen(f'cmd /k {inner}', cwd=str(cwd),
                             creationflags=NEW_CONSOLE)
    except OSError as e:
        return {"session": session, "spawned": False, "note": f"spawn failed: {e}"}
    return {"session": session, "spawned": True, "note": f"terminal opened on {cwd}"}


# --- archive / unarchive ------------------------------------------------------

def _rename_category(project, to_archived):
    """Move the Discord category between the 📁 and 🗄 prefixes. Idempotent."""
    schema = api.load_schema()
    live = schema["prefixes"]["project"] + project
    arch = schema["prefixes"]["archived"] + project
    want, other = (arch, live) if to_archived else (live, arch)
    chans = api.guild_channels()
    if api.find_channel(chans, want, api.CHANNEL_CATEGORY):
        return f"already named {want}"
    cat = api.find_channel(chans, other, api.CHANNEL_CATEGORY)
    if not cat:
        return f"no category found for {project}"
    api.rename_channel(cat["id"], want)
    return f"renamed to {want}"


def archive(project, move=False):
    check_name(project)
    dest = PROJECTS / project
    killed = []
    for f in sorted(wd.SESSIONS.glob(f"{project}.*.json")):
        killed.append(wd.kill_session(f.stem))
    try:
        discord_note = _rename_category(project, True)
    except Exception as e:
        discord_note = f"FAILED ({type(e).__name__}: {e}) - re-run to finish"
    moved = "left in place"
    if move and dest.is_dir():
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        target = ARCHIVE / project
        if target.exists():
            moved = f"already at {target}"
        else:
            shutil.move(str(dest), str(target))
            moved = f"moved to {target}"
    return {"project": project, "killed": killed, "discord": discord_note,
            "folder": moved}


def unarchive(project):
    check_name(project)
    src = ARCHIVE / project
    moved = "was not archived on disk"
    if src.is_dir():
        target = PROJECTS / project
        if target.exists():
            moved = f"{target} already exists - left the archived copy alone"
        else:
            shutil.move(str(src), str(target))
            moved = f"restored to {target}"
    try:
        discord_note = _rename_category(project, False)
    except Exception as e:
        discord_note = f"FAILED ({type(e).__name__}: {e}) - re-run to finish"
    return {"project": project, "discord": discord_note, "folder": moved}


# --- CLI ----------------------------------------------------------------------

def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    np_ = sub.add_parser("new-project", help="stamp a project (folder + git + Discord)")
    np_.add_argument("name")
    np_.add_argument("--components", nargs="+", required=True)
    np_.add_argument("--description", default="")
    np_.add_argument("--no-discord", action="store_true")
    np_.add_argument("--template", default=None,
                     help=r"stamp from this folder instead of templates\project (e.g. templates\demo-project)")
    np_.add_argument("--git", action="store_true",
                     help="also git init + first commit (default: off - the desk "
                          "does it later, if the project turns out to be worth a repo)")

    sp = sub.add_parser("spawn", help="open a VISIBLE terminal desk (Discord mail needs no spawn - the watchdog runs desks headless)")
    sp.add_argument("session")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--model")
    sp.add_argument("--effort")

    st = sub.add_parser("status", help="fleet overview from claims + beacon + disk")
    st.add_argument("--prune", action="store_true", help="remove stale claims")

    ar = sub.add_parser("archive", help="wind a project down")
    ar.add_argument("project")
    ar.add_argument("--move", action="store_true", help="also move the folder to _archive")

    un = sub.add_parser("unarchive", help="bring an archived project back")
    un.add_argument("project")

    for q in (np_, sp, st, ar, un):
        q.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args(argv)

    try:
        if args.cmd == "new-project":
            out = new_project(args.name, args.components, args.description,
                              discord=not args.no_discord, git=args.git,
                              template=args.template)
        elif args.cmd == "spawn":
            out = open_desk(args.session, force=args.force, model=args.model,
                            effort=args.effort)
        elif args.cmd == "status":
            out = status(prune=args.prune)
        elif args.cmd == "archive":
            out = archive(args.project, move=args.move)
        else:
            out = unarchive(args.project)
    except ValueError as e:
        print(f"[X] {e}")
        return 1

    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(render(args.cmd, out))
    return 0


def render(cmd, out):
    if cmd == "status":
        lines = [f"fleet @ {out['machine']}",
                 f"  watchdog: {out['watchdog']['state']}"]
        lines.append("  projects: " + (", ".join(
            f"{k} ({'+'.join(v) or 'no components'})" for k, v in out["projects"].items())
            or "none"))
        for d in out["desks"]:
            mark = " (pruned)" if d["pruned"] else ""
            lines.append(f"  [{d['state']}] {d['session']} {d['detail']}{mark}")
        return "\n".join(lines)
    return "\n".join(f"  {k}: {v}" for k, v in out.items())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
