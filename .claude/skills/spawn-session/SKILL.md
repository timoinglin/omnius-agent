---
name: spawn-session
description: Open a terminal desk on a folder with Claude running and connected to the bus (/omnius). Run when the user asks to start, wake or restart a session for a project component, a tool or the daybook.
---

# /spawn-session — open a desk and plug it into the bus

Orchestrator verb (ARCHITECTURE §5.3). One desk = one folder = one session id.

## Session ids

| Desk | id |
|---|---|
| workspace root | `orchestrator` |
| `projects\<p>\<c>` | `<p>.<c>` |
| `tools\<t>` | `tool.<t>` |
| `daybook` | `daybook` |

## Run it

```
python tools\orchestrator\fleet_ops.py spawn <session-id>
```

Options: `--force`, `--model <opus|sonnet|…>`, `--effort <low|medium|high|xhigh|max>`.
Defaults come from `fleet.json` (every desk gets Opus 5 at xhigh unless told
otherwise) and are code-level on purpose — `.env` never travels, so a fresh
machine still spawns the right thing.

## What a refusal means — read it, don't fight it

`spawned: false` is almost always correct:

- **desk already occupied** — a live claim on that folder. **One session per
  desk** is a hard rule (root CLAUDE.md §6): two brains in one folder drain the
  same inbox and race on the same envelope files, and a claim holds only one pid
  so the duplicate is invisible to `!status` and the banner. It happened on
  2026-07-31 exactly this way, from a "restart it" request.
- **active run on the desk** — the watchdog is mid-run there (`state\watchdog\runs\`);
  Discord mail does not need a terminal, so only open one if the user wants to watch.
- **folder missing** — check the path before blaming the spawn.

`--force` exists for the deliberate kill-then-respawn, where you already emptied
the desk. If the user wants a restart, kill first (`!restart` in that channel
does both), then spawn.

## After it comes up

The desk runs `/omnius` itself and writes its own claim — **never hand-author
`state\sessions\<id>.json`**. Verify with:

```
python tools\orchestrator\fleet_ops.py status
```

and look for `[listening]`. `alive-not-listening` means the session is up but its
inbox watcher is not — it will not hear Discord. Note it in your reply.

Then update `memory\orchestrator\status.md` (write-through) and confirm briefly.
