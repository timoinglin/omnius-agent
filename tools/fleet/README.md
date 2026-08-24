# fleet — the status desk

Session id **`tool.fleet`** · desk `tools\fleet\` · channel **`#fleet-status`**

## What this desk is for

Answering **"how is the fleet doing?"** in natural language, so the user does not
have to remember `!status` or wait behind whatever the orchestrator is busy with.

Created 2026-07-31 on the same reasoning that gave the daybook its own desk:
*asking about state should never occupy the agent doing the work.*

## What it may do

**Read.** That is the whole job.

- `state\sessions\*.json` — one claim per desk (`role`, `cwd`, `machine`, `pid`,
  `startedAt`, `lastSeenAt`), written **once** at check-in: no heartbeat, no `watcherPid`
- `state\watchdog\runs\<id>.json` — the watchdog's active-run lease for a desk
  (pid-validated): a desk with no claim at all can still be working
- `state\turns\<id>.busy` — a person's terminal is mid-turn on that desk
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

**A live claim does not mean a live desk — though not for the reason this file
used to give.** There is no heartbeat and no watcher process: `inbox_watch.py
<id> --once` writes the claim with a real pid and exits, and `lastSeenAt` is
stamped once at check-in, never ticked. (A sidecar ticking it while the desk
itself hung is the lie that made a dead desk read as alive all evening —
deleted 2026-08-01; see the superseded banner in `docs\RELIABILITY.md`.) A
claim therefore means *a terminal opened this desk*, nothing more. On
2026-07-31 a desk sat stalled for three hours while every signal read healthy:
the lesson stands, the mechanism named here does not — never report health
from claim data alone.

Report what the code can actually distinguish (`watchdog.session_alive()` is
the claim pid on this machine **or** an active run lease):

| state | how to tell |
|---|---|
| `busy` | a live run lease in `state\watchdog\runs\<id>.json`, or a `state\turns\<id>.busy` stamp (a person's terminal mid-turn) |
| `stalled` | the desk's newest outbox file is `*-perm-timeout.json` — it is waiting at a dialog nobody can see |
| `open` | a claim whose pid is alive on this machine: a terminal is sitting on the desk. Idle is normal — reachability is the watchdog's job, not the desk's |
| `dead` | no live pid and no lease (claim is prunable, root `CLAUDE.md` §6) — and still reachable, because the watchdog starts a run for it |

Detecting a **duplicated desk**: count process trees, not claims — a claim file
holds one pid, so a second session on the same desk is invisible in `state\`.

```
Get-CimInstance Win32_Process | Where-Object CommandLine -like '*claude*'
```

Compare that against `state\sessions\*.json` (one claim pid per desk) and
`state\watchdog\runs\*.json` (the runs the watchdog owns): a `claude` process
matching neither is a second brain somebody opened by hand, racing on the same
envelopes. There is no `inbox_watch` process to count — the check-in exits
immediately (2026-08-01).

## Style

Answer like a colleague glancing at a dashboard: what is up, what is wrong, what
needs the user. Short enough to read on a phone. If everything is healthy, say so
in one line — do not pad it into a report.
