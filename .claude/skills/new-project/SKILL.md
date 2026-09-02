---
name: new-project
description: Create a new project - stamp the template into projects\<name>\, fill its CLAUDE.md, create the Discord category and channels, and optionally open a desk per component. Run when the user asks for a new project.
---

# /new-project — stamp a project into existence

Orchestrator verb (ARCHITECTURE §5.1). **You own the judgement; the script owns
the mechanics** — do not improvise `mkdir` + `git init` + Discord calls, because
"templates over improvisation" (§2.7) is what keeps every project identical.

## 1. Settle the inputs first (one short question, not an interview)

You need a **name** and its **components**. Everything else has a sane default.

- **Name:** kebab-case, lowercase, no spaces. The script rejects anything else —
  the name becomes a folder, a Discord category *and* a session id (a git repo
  too, later), and only one of those would have complained on its own.
- **Components:** the desks this project will have (`app`, `backend`, `web`, …).
  One component = one folder = one Discord channel = one session. If the user did
  not say, propose a shape and ask in one line: *"recipe-app with app + backend?"*
- **Description:** one sentence for the project's `CLAUDE.md`. If they did not
  give one, take it from what they said rather than asking again.

**Do not spawn a desk for a component that has no work yet** (user decision:
*"a component folder existing does not mean it deserves a live desk"*).

## 2. Run it

```
python tools\orchestrator\fleet_ops.py new-project <name> --components <c1> <c2> --description "<one sentence>"
```

Flags: `--no-discord` (folder only), `--git` (opt in to `git init` + first
commit). `--json` for machine output.

**No repo by default** (owner, 2026-08-04): a new project may well be
temporary, so it gets a folder and channels and nothing else. The desk
creates the repo later, when he asks it to — do not offer to do it for him.

This is **idempotent** — folder created if missing, template files copied only
where absent (never clobbering an edited `CLAUDE.md`), git initialised once if
`--git`,
Discord category and channels found-or-created. If it half-fails, **just run it
again**; that is the designed recovery, and the `discord:` line will say
`FAILED … re-run to finish` when there is more to do.

## 3. Hand the task to the desk (this is the delegation step)

**The owner's model, verbatim (2026-08-01):** *"I tell the orchestrator do
this or do that, the orchestrator starts a new project folder + the Discord
category and runs a new session for that task, and this session has all
context window and new project memory for the task."* You coordinate; the
desk works. Never do the project's work in your own context.

For each component with work starting **now**, send the brief as **desk mail**:
one file written with the `Write` tool into your **own outbox** —
`state\outbox\orchestrator\<unix-ms>.json` (`docs\DELEGATION.md`):

```json
{ "to": "<name>.<component>",
  "text": "<the brief: goal, constraints, where to reply>" }
```

`to` is the whole discriminator. **Never write into `state\inbox\` yourself** —
the watchdog owns that directory: it converts your outbox file into a proper
`kind: "desk"` envelope (deterministic id, `thread`, `hops`, `origin`), mirrors
the hop into Discord and dedupes redeliveries. A hand-written inbox file gets
none of that. Add `origin` (`{"channelId": "...", "from": "owner"}`) when the
chain started from a message of his, so the answer finds its way back to him.

The watchdog starts a headless session on that desk within seconds — fresh
context window, the project's own `CLAUDE.md` and `memory\`, no terminal, no
window. Put everything the desk needs INTO the brief (or into the project's
`memory\` first and point at it): it starts with your envelope, not with your
conversation.

Only open a **visible terminal** (`python tools\orchestrator\fleet_ops.py
spawn <name>.<component>`) when the user wants to watch or drive that desk
themselves. A refusal is information, not an error — the desk is already
occupied or running.

## 4. Write through, then confirm

**Before you reply**, update `memory\orchestrator\status.md` with the new project
and its desks (root CLAUDE.md §3: every fleet mutation updates status in the same
action — you must survive your own restart).

Then confirm in one phone-readable line, e.g.
*"✅ recipe-app live — app + backend, both briefed, #recipe-app created."*
Say what actually happened: if Discord failed, say so and say you can re-run.

## The demo project

`templates\demo-project` is a shipped three-desk example (back, front, and a
read-only auditor). When he asks for "the demo" or "the demo project", stamp it
with:

```
python tools\orchestrator\fleet_ops.py new-project demo --components back front auditor --template templates\demo-project
```

`--template` works for any richer skeleton; the default stays `templates\project`.
