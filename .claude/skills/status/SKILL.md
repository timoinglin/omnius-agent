---
name: status
description: Fleet overview - what projects exist, which desks are busy or open, whether the watchdog is actually delivering, and prune stale claims. Run when the user asks how things are, what is running, or after moving to a new machine.
---

# /status — what exists and what is actually alive

Orchestrator verb (ARCHITECTURE §3.6).

```
python tools\orchestrator\fleet_ops.py status            # report
python tools\orchestrator\fleet_ops.py status --prune    # and drop stale claims
```

## Read the states properly — this is the whole point of the verb

`fleet_ops.claim_state()` returns exactly four, and nothing else:

| State | Means |
|---|---|
| `working` | an active headless run on that desk — the run lease's pid is alive |
| `live` | an interactive terminal sits on that desk — the claim's pid is alive |
| `stale` | dead pid on this machine, or a claim from another machine |
| `none` | no claim at all |

**There is no per-desk watcher and no claim heartbeat** — both were deleted with
the run model on 2026-08-01 (root CLAUDE.md §6: *PID liveness is the whole
signal*). So a desk showing `none` is **not** unreachable: reachability belongs
to the watchdog, which starts a run whenever mail is waiting. Never report a
quiet desk as a problem.

**Never report health from claim data alone** (RELIABILITY R2): every state
above is decided by checking a real pid — the lease first, then the claim — not
by trusting a timestamp in the file.

The watchdog line is separate and comes from `state\watchdog\beacon.json`,
stamped only after a pass that actually reached Discord (or a live gateway
socket): a fresh beacon (under 120 s) means it is delivering, `stale beacon
(Ns)` and `down` mean it is not. A watchdog can be running, holding its lock and
logging happily, while delivering nothing — no process check can see that, the
beacon can.

## Pruning

`--prune` removes only `stale` claims: a dead pid on this machine, or a claim
belonging to another machine. Neither can be a session you would disturb. This
is the **normal** state after the workspace moves to a new PC — expect a handful
and prune them without ceremony.

## Then

- If the watchdog beacon is stale or down, say so first — that is the finding,
  not the inventory. Desks with no claim are normal; a dead watchdog is not.
- Refresh `#fleet-status` if it has drifted, and update
  `memory\orchestrator\status.md` if the picture changed (write-through).
- Keep the reply phone-readable: what is wrong first, then the short inventory.
  Nothing wrong → say that in one line, not in a table.
