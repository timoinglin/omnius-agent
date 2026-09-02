# tools\orchestrator — mechanics behind the orchestrator verbs

`fleet_ops.py` is the deterministic half of `/new-project`, `/spawn-session`,
`/status` and `/archive-project` (ARCHITECTURE §3.6 / §5.1 / §5.5). Built
2026-08-01.

```
python tools\orchestrator\fleet_ops.py new-project recipe-app --components app backend --description "..."
python tools\orchestrator\fleet_ops.py spawn recipe-app.app [--force --model opus --effort xhigh]
python tools\orchestrator\fleet_ops.py status [--prune] [--json]
python tools\orchestrator\fleet_ops.py archive recipe-app [--move]
python tools\orchestrator\fleet_ops.py unarchive recipe-app
```

## The split, and why

The **skills** (`.claude\skills\<verb>\SKILL.md`) own the judgement: what to ask,
what to confirm before destroying, what to write to memory. **This file** owns
the mechanics that must come out identical every time — stamping the template,
filling placeholders, `git init`, the Discord call, classifying and pruning
claims.

An agent improvising `mkdir` + `git init` + a Discord call gets it subtly
different every time, and "templates over improvisation" (§2.7) is precisely
what makes every later automation trivial. Anything a script can do
deterministically should not be left to a prompt.

## Everything is idempotent — this is a requirement, not a nicety

These verbs are driven **from a phone**, where a dropped reply looks exactly like
a failure and the user's natural next move is to say it again. So:

- `new-project` creates the folder if missing, copies template files **only where
  absent** (never clobbering a `CLAUDE.md` the project has since edited),
  `git init`s once, and finds-or-creates the Discord category and channels.
  Re-running a completed project reports `created: []` and changes nothing.
- `archive` / `unarchive` rename the Discord category between the `📁 ` and `🗄 `
  prefixes — history is preserved, nothing is deleted — and move the folder only
  when asked. `unarchive` refuses to overwrite a live `projects\<name>\`.
- A half-finished run is fixed by running it again; the `discord:` line says
  `FAILED … re-run to finish` when there is more to do.

## `status` tells a busy desk from an empty one

`claim_state()` returns exactly these:

| State | Means |
|---|---|
| `working` | an active headless run — the run lease's pid is alive |
| `live` | an interactive terminal — the claim's pid is alive |
| `stale` | dead pid on this machine, or a claim from another machine |
| `none` | no claim at all |

Since the run model (2026-08-01) there is no per-desk watcher and no claim
heartbeat, so there is no `listening` state to report: an empty desk is still
reachable, because the watchdog starts a run when mail arrives. Never report
health from claim data alone (RELIABILITY R2) — every state above is decided by
checking a real pid, not by trusting a timestamp. `--prune` removes only `stale`
claims — a dead pid here, or a claim from another machine, neither of which can
be a session you would disturb. Expect a handful right after the workspace moves
to a new PC.

## Notes

- Project names are enforced kebab-case. The name becomes a folder, a git repo,
  a Discord category *and* a session id, and only one of those four would have
  complained on its own.
- Component folders get a `.gitkeep` — an empty folder is not tracked by git, so
  a fresh clone would come back missing the desk.
- `fail()` raises `ValueError`, never `SystemExit`: this is a library first, and
  `SystemExit` sails straight through `except Exception` and takes the caller
  down with it. The CLI turns it back into an exit code.
