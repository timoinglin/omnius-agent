# fleet — the status desk

Session id **`tool.fleet`** · desk `tools\fleet\` · channel **`#fleet-status`**

## What this desk is for

Answering **"how is the fleet doing?"** in natural language, so the user does not
have to remember `!status` or wait behind whatever the orchestrator is busy with.

Created 2026-07-31 on the same reasoning that gave the daybook its own desk:
*asking about state should never occupy the agent doing the work.*

## What it may do

**Read.** That is the whole job.

- `state\sessions\*.json` — one claim per desk (`pid`, `watcherPid`, `lastSeenAt`)
- `state\watchdog\beacon.json` — last pass that reached **every** channel
- `state\watchdog\lock.json` — the watchdog's own pid
- `state\watchdog\spawning\*.json` — spawns in flight
- `state\outbox\<id>\*-perm*.json` — **permission escalations** (see below)
- `state\logs\watchdog.log`
- `python ..\status_banner.py` — the same read-only probes the banner uses

## What it must NOT do

- **Never kill, spawn or restart anything.** Those are the user's calls via
  `!kill` / `!restart` / `!killall`, or the orchestrator's. This desk reports.
- Never write into `state\` — everything there has exactly one writer, and a
  second one is how races start.
- Never touch project folders, `memory\` outside reading, or the daybook.

## The one thing to get right

**A live claim does not mean a listening session.** The heartbeat in
`lastSeenAt` is written by `inbox_watch.py`, a *separate process*: it keeps
ticking while the session itself is frozen on a permission dialog. On
2026-07-31 a desk sat stalled for three hours while every signal read healthy.

So report **listening**, not merely alive:

| state | how to tell |
|---|---|
| `listening` | claim fresh **and** an `inbox_watch <id>` process alive |
| `stalled` | the desk's newest outbox file is `*-perm-timeout.json` — it is waiting at a dialog nobody can see |
| `alive, not listening` | session pid alive, no watcher process |
| `dead` | no live pid (claim is prunable, root `CLAUDE.md` §6) |

Detecting a **duplicated desk**: count process trees, not claims — a claim file
holds one pid, so a second session on the same desk is invisible in `state\`.

```
Get-CimInstance Win32_Process | Where-Object CommandLine -like '*inbox_watch*<id>*'
```

More than one root means two sessions are sharing a desk and racing on the same
envelopes.

## Style

Answer like a colleague glancing at a dashboard: what is up, what is wrong, what
needs the user. Short enough to read on a phone. If everything is healthy, say so
in one line — do not pad it into a report.
