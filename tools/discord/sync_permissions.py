#!/usr/bin/env python3
"""Give every desk the same allow-list, from ONE definition.

    python tools\\discord\\sync_permissions.py            # apply
    python tools\\discord\\sync_permissions.py --check    # exit 1 if any desk is short

Why this exists (2026-08-13). A desk that hits a tool nobody allow-listed stops
and asks. Over Discord that is a question on a screen he may not be looking at,
which is the failure this whole posture exists to prevent - so the allow-list is
not a convenience, it is the thing keeping desks moving.

Kept by hand, it drifted immediately and invisibly:

- The widening of 2026-08-12 edited the seven settings files that are TRACKED.
  `projects\\*` is gitignored, so every project desk on this machine kept the
  original eleven entries and none of the ten added. Nobody could see it,
  because the files that were fixed and the files that were not look identical
  from the repo.
- `Artifact` was in NO list anywhere. It surfaced 2026-08-13 when a project desk
  publishing a report stopped mid-answer to ask - exactly the interruption the
  posture is meant to remove.

So the list lives here once, and this script is the only writer. Anything the
list does not cover still escalates through the PermissionRequest relay and
becomes an ok/no in Discord - the fence is the deny list, not omission.

What is deliberately NOT here: `--dangerously-skip-permissions`. It would end
the whole class of problem, and it also switches OFF the deny list, including
the `Read(./.env)` fence he explicitly kept. That is his call to make, not a
script's - see `memory\\orchestrator\\topics\\permissions.md`.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# One definition. Order is meaningful only to a human reading the file.
ALLOW = [
    # --- files and search ---
    "Read", "Edit", "Write", "Glob", "Grep", "LS", "NotebookEdit",
    # --- shell ---
    "Bash", "PowerShell", "BashOutput", "KillShell",
    # --- web ---
    "WebFetch", "WebSearch",
    # --- delegation and skills ---
    "Task", "Agent", "Skill", "SlashCommand", "Workflow", "TodoWrite",
    "ToolSearch", "ListSkills", "SearchSkills",
    # --- talking to him: output surfaces a desk uses to ANSWER. A desk that
    # must ask permission to publish its own reply is the case that started
    # this file (Artifact, 2026-08-13).
    "Artifact", "SendUserFile", "ReportFindings",
    "SendMessage", "PushNotification",
    # --- background work and scheduling ---
    "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate",
    "CronCreate", "CronDelete", "CronList", "Monitor",
    # --- planning and worktrees ---
    "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
    # --- everything else a current Claude Code can surface. Listed even where
    # this install has never seen the name: one that arrives with a future
    # release must not stop a desk the first time it is used, which is the whole
    # point of this file (2026-08-19, owner: add every permission you know of).
    "MultiEdit", "NotebookRead", "TodoRead", "KillBash", "ScheduleWakeup",
    "DesignSync", "RemoteTrigger", "SuggestSkills", "SuggestPluginInstall",
    "ListPlugins", "SearchPlugins",
    # --- MCP servers that ship with Claude Code itself, allowed whole.
    # Servers YOU connect are instance-specific and belong in LEARNED below,
    # not here: their names say what this install is wired to, and this file
    # travels into every fresh release.
    "mcp__claude-in-chrome", "mcp__computer-use", "mcp__Claude_Browser",
    "mcp__visualize", "mcp__ccd_session", "mcp__ccd_session_mgmt",
    "mcp__mcp-registry", "mcp__scheduled-tasks", "mcp__ccd_directory",
    "mcp__terminal",
]

# Natural-language standing authorisations for auto mode, stamped onto every
# desk beside the tool list. "$defaults" keeps whatever Claude Code ships with;
# the sentence states the posture for the cases a tool NAME cannot express.
AUTO_ALLOW = [
    "$defaults",
    "This machine belongs to the person running these desks, and every desk acts "
    "with their full authority. Do not ask permission to use a tool: act, then "
    "say what you did. The single exception is .env, which holds the tokens and "
    "passwords and is denied on purpose - if you genuinely need something from "
    "it, say which key and why. Irreversible or outward-facing steps (deleting "
    "broadly, force-pushing, sending mail as them, spending money) are still "
    "described in words first.",
]

# Learned entries: written when he answers "ok" to a permission request, so a
# tool asks ONCE and never again, on any desk (his decision 2026-08-13). Lives
# in config\ because it is per-install - a new MCP server or a tool a newer
# Claude Code added. config\* is gitignored, so it travels in his backup zip
# and never reaches a release. See config\README.md.
# DENIED outright, and it is not about danger - it is about ANSWERABILITY.
# AskUserQuestion draws a menu in the desk's terminal and waits for a keypress.
# No desk terminal has a human in front of it, and the bus can only type into a
# pty while no turn is running - so the widget blocks the very turn that would
# have to end for anyone to reach it. 2026-08-13: a project desk asked which
# of two browsers to drive, he answered "ok" in Discord (which only allowed it
# to ASK), and the desk sat there. Merely leaving it off the allow-list is not
# enough - that prompts, he says ok, and it hangs exactly the same way. A deny
# makes the tool fail, and the desk then asks in plain text, which he CAN answer.
DENY = ["AskUserQuestion"]

# .env IS THE ONE THING STILL DENIED (owner, 2026-08-19: "the only files you
# deny are the .env files, all other you allow"). Everything else in this file
# widens; this is the single exception, and it is worth being precise about what
# it is and is not:
#
#   It IS a guardrail. A desk reaching for the file that holds the bot token,
#   the mail passwords and the API keys is stopped and has to say so instead -
#   and desks answer other people now (guests, Telegram invitees), so the
#   difference between "reads it by habit" and "has to ask" is worth keeping.
#
#   It is NOT a boundary. Bash and PowerShell are allowed, so `type .env` still
#   works: anything with a shell can read any file on the machine. Treating this
#   as containment would be a lie - the containment is that only the owner and
#   people he invites can talk to a desk at all.
#
# Every depth is listed because desks sit at different depths (root, daybook\,
# tools\<x>\, projects\<p>\<c>\) and a relative rule that misses is a rule that
# is not there. Stamped by this script now, so every desk carries the same set
# instead of whatever its file was hand-written with.
DENY_ENV = ["Read(./.env)", "Read(./**/.env)",
            "Read(../.env)", "Read(../**/.env)",
            "Read(../../.env)", "Read(../../**/.env)",
            "Read(../../../.env)", "Read(../../../**/.env)",
            "Edit(./.env)", "Edit(../.env)", "Edit(../../.env)",
            "Edit(../../../.env)",
            "Write(./.env)", "Write(../.env)", "Write(../../.env)",
            "Write(../../../.env)"]

# Claude Code's built-in "Concise" output style: lead with the result, no
# preamble, no narration, short by default - while keeping error reports,
# security warnings and destructive-action confirmations complete. Owner
# instruction 2026-08-29: "i want by default all sessions in consise mode
# responses", the fourth time he has asked for shorter answers (shared\USER.md
# records why it is an accessibility need and not a taste).
#
# Stamped here rather than in his user settings because it must travel: a desk
# on his second PC, and every project stamped from the template, gets it without
# anyone remembering to set it. Needs Claude Code v2.1.237+; an older build
# ignores the key rather than failing, so this is safe to ship.
#
# It is a SYSTEM PROMPT change, so it lands at session start - which for a desk
# is every run. Note it does not reach subagents: they carry their own prompt.
OUTPUT_STYLE = "Concise"

# crossSessionInbound is deliberately NOT stamped here, and this comment is the
# reason - so the next person to read the cross-session docs does not add it back.
#
# The key chooses what a session does with messages from his OTHER Claude Code
# sessions: accept / hold / refuse. A first pass on 2026-08-29 stamped "accept"
# into all eleven desk files. It would have shipped to both PCs doing NOTHING.
# crossSessionInbound is one of the security-sensitive keys with inverted
# precedence (settings docs, "Exceptions to managed settings precedence"): from
# `.claude\settings.json` or `.claude\settings.local.json` Claude Code honors
# only a STRICTER value on the accept < hold < refuse ladder, and "a project or
# local value that isn't stricter is ignored". `accept` is the loosest rung, so
# a desk file can never carry it. A desk file CAN carry `refuse`, which is the
# direction the exception exists to protect.
#
# The scopes that can say accept are user settings, --settings and managed
# settings - and the cross-session page recommends exactly those two for an
# unattended worker, never the project file.
#
# It is also mostly moot, which is why nothing here works around it. With no
# value applying, Claude Code decides per message from the two sessions'
# permission classes, and every desk sets defaultMode acceptEdits, which counts
# as PROMPTING rather than bypassing: a prompting receiver is delivered each
# message, and holds one only when the SENDER declares itself as bypassing
# permission prompts. So a desk already takes mail from his ordinary terminals.
# The only gap is a sender started with --dangerously-skip-permissions, and
# closing that means writing his personal ~\.claude\settings.json (or the
# /config row "Messages from your other sessions"), which is his call to make,
# not a stamp this script gets to apply to his profile.
#
# What a desk should do with the feature lives in docs\DELEGATION.md, not here:
# native messaging reaches a LIVE session, desk mail wakes a desk that is not
# running, and they are not substitutes.

LEARNED = ROOT / "config" / "allow-learned.json"


def learned():
    """-> [tool names] he has approved before. Never raises: an unreadable or
    hand-mangled file means "nothing learned yet", never "allow everything"."""
    try:
        data = json.loads(LEARNED.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [t for t in data if isinstance(t, str) and t.strip()]


def learn(tool):
    """Remember that he allowed `tool`. -> True if this is new.

    Only ever ADDS, and only a plain tool name: a permission request carries
    the tool, and a tool name is not a path or an argument, so there is nothing
    here that could widen into "allow this specific dangerous command". The
    deny list is untouched by design - Read(./.env) survives every approval.
    """
    tool = str(tool or "").strip()
    if not tool or tool in ALLOW or tool in learned():
        return False
    # Defensive: a permission request is machine-written, but this file feeds an
    # allow-list, so refuse anything that is not a bare identifier-ish name.
    if not re.fullmatch(r"[A-Za-z][\w.-]{0,63}", tool):
        return False
    current = learned() + [tool]
    LEARNED.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEARNED.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(set(current)), indent=2) + "\n", encoding="utf-8")
    tmp.replace(LEARNED)
    return True


def effective_allow():
    """The shared list plus everything he has approved since."""
    out = list(ALLOW)
    out += [t for t in learned() if t not in out]
    return out


def settings_files():
    """Every desk's settings.json. Missing trees are skipped, not an error."""
    out = [ROOT / ".claude" / "settings.json",
           ROOT / "templates" / "project" / ".claude" / "settings.json",
           ROOT / "daybook" / ".claude" / "settings.json"]
    for base in (ROOT / "projects", ROOT / "tools"):
        if base.is_dir():
            # A project may put a desk in a component subfolder, so look one
            # level deeper too - that is where the 2026-08-12 widening never
            # reached: project desks live in gitignored folders, so a widening
            # committed to the repo did not touch a single one of them.
            out += sorted(base.glob("*/.claude/settings.json"))
            out += sorted(base.glob("*/*/.claude/settings.json"))
    return [p for p in out if p.is_file()]


def _merge_local(p, learned_entries):
    """Put LEARNED entries in settings.local.json beside settings.json.

    -> True if the local file changed. Claude Code merges both files, so the
    desk sees the union - but only settings.json is tracked. Learned entries
    used to be written straight into it, which put the names of his MCP
    servers into files that ship (found 2026-08-14: an integration's name had
    been stamped into every tracked settings.json in the repo). settings.local
    .json is already gitignored fleet-wide, which is the entire point.
    """
    lp = p.with_name("settings.local.json")
    try:
        local = json.loads(lp.read_text(encoding="utf-8")) if lp.is_file() else {}
    except (OSError, json.JSONDecodeError):
        local = {}
    lperms = local.setdefault("permissions", {})
    lhave = lperms.get("allow") or []
    add = [t for t in learned_entries if t not in lhave]
    if not add:
        return False
    lperms["allow"] = lhave + add
    lp.write_text(json.dumps(local, indent=2, ensure_ascii=False) + "\n",
                  encoding="utf-8")
    return True


def main():
    check_only = "--check" in sys.argv
    extra = learned()
    short, changed = [], 0
    for p in settings_files():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  SKIP  {p.relative_to(ROOT)}: {type(e).__name__}: {e}")
            continue
        perms = data.setdefault("permissions", {})
        have = perms.get("allow") or []
        held = perms.get("deny") or []
        missing = [t for t in ALLOW if t not in have]
        # A tool cannot be both: an entry we now deny must leave the allow list,
        # or the file says two things and which one wins is the reader's guess.
        stale = [t for t in have if t in DENY]
        # Learned entries MIGRATE out of the tracked file into settings.local
        # .json - they name his integrations, and this file ships.
        misplaced = [t for t in have if t in extra]
        # DENY plus the .env rules, at every depth, on every desk. Hand-written
        # per file they drifted: a project desk sits three levels down and its
        # file carried rules for two, so the root .env was reachable from it.
        want_deny = DENY + DENY_ENV
        undenied = [t for t in want_deny if t not in held]
        auto = data.setdefault("autoMode", {})
        auto_have = auto.get("allow") or []
        auto_missing = [a for a in AUTO_ALLOW if a not in auto_have]
        # Two more settings that decide whether a desk stops to ask:
        #
        # defaultMode = acceptEdits - file edits are applied without a prompt.
        # NOT bypassPermissions: that one is A/B-proven to hang an INTERACTIVE
        # spawn (fleet.json roles carry the receipts, 2026-08-01) because Claude
        # Code shows a confirmation screen that -p skips, and desks here open
        # real windows. It would also switch off the deny list, taking the .env
        # rule with it.
        #
        # enableAllProjectMcpServers - an MCP server declared in .mcp.json is
        # trusted rather than asked about, per project, on first use.
        mode_wrong = perms.get("defaultMode") != "acceptEdits"
        mcp_wrong = data.get("enableAllProjectMcpServers") is not True
        # One definition here, stamped on every desk, rather than eleven files
        # kept in step by hand. (See the CROSS_SESSION_INBOUND note above for
        # the key that deliberately did NOT join it.)
        style_wrong = data.get("outputStyle") != OUTPUT_STYLE
        # `ask` is the third list Claude Code reads: anything in it prompts
        # every time, whatever `allow` says. Empty is the posture here.
        asks = [t for t in (perms.get("ask") or [])]
        lp = p.with_name("settings.local.json")
        try:
            lhave = (json.loads(lp.read_text(encoding="utf-8"))
                     .get("permissions", {}).get("allow") or []) if lp.is_file() else []
        except (OSError, json.JSONDecodeError):
            lhave = []
        local_missing = [t for t in extra if t not in lhave]
        if not (missing or stale or undenied or misplaced or local_missing
                or auto_missing or mode_wrong or mcp_wrong or asks
                or style_wrong):
            continue
        short.append((p, missing + local_missing))
        if check_only:
            continue
        # Additive on purpose: a desk may have earned an entry of its own
        # (a project-specific MCP server), and this script must not eat it.
        perms["allow"] = [t for t in have if t not in DENY and t not in extra] + missing
        if undenied:
            perms["deny"] = held + undenied
        if auto_missing:
            # Additive like the allow list: an instance may have added a
            # standing authorisation of its own, and this must not eat it.
            auto["allow"] = auto_have + auto_missing
        perms["defaultMode"] = "acceptEdits"
        data["enableAllProjectMcpServers"] = True
        data["outputStyle"] = OUTPUT_STYLE
        if asks:
            # A tool that always asks is a tool that stops an unattended desk.
            perms.pop("ask", None)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8")
        _merge_local(p, extra)
        changed += 1
        print(f"  +{len(missing):<3} {p.relative_to(ROOT)}"
              + (f" (+{len(local_missing)} learned -> settings.local.json)"
                 if local_missing or misplaced else ""))

    if check_only:
        for p, missing in short:
            print(f"  SHORT {p.relative_to(ROOT)}: missing {len(missing)} "
                  f"({', '.join(missing[:4])}{'...' if len(missing) > 4 else ''})")
        print(f"permissions: {len(short)} desk(s) short of the shared allow-list"
              if short else
              f"permissions: all desks carry the full {len(ALLOW)}-entry allow-list"
              + (f" (+{len(extra)} learned, in settings.local.json)" if extra else ""))
        return 1 if short else 0
    print(f"permissions: {changed} settings file(s) updated"
          if changed else
          f"permissions: already in sync ({len(ALLOW)} entries everywhere"
          + (f", +{len(extra)} learned locally)" if extra else ")"))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
