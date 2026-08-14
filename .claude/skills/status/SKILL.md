---
name: status
description: Fleet overview - what projects exist, which desks are listening, whether the watchdog is actually delivering, and prune stale claims. Run when the user asks how things are, what is running, or after moving to a new machine.
---

# /status — what exists and what is actually alive

Orchestrator verb (ARCHITECTURE §3.6).

```
python tools\orchestrator\fleet_ops.py status            # report
python tools\orchestrator\fleet_ops.py status --prune    # and drop stale claims
```

## Read the states properly — this is the whole point of the verb

| State | Means |
|---|---|
| `listening` | claim fresh **and** an inbox watcher process alive — it will hear Discord |
| `alive-not-listening` | the session is up but nothing is watching its inbox — **it is deaf** |
| `stale` | dead pid on this machine, or a claim from another machine |
| `none` | no claim |

**Never report health from claim data alone** (RELIABILITY R2). The claim
heartbeat is written by a *separate* process, so it keeps ticking while the
session sits frozen on a permission dialog — which is why `listening` checks the
watcher process, not just the timestamp.

Same for the watchdog line: it comes from `state\watchdog\beacon.json`, stamped
only after a pass that actually reached Discord (or a live gateway socket).
A watchdog can be running, holding its lock and logging happily, while
delivering nothing — no process check can see that, the beacon can.

## Pruning

`--prune` removes only `stale` claims: a dead pid on this machine, or a claim
belonging to another machine. Neither can be a session you would disturb. This
is the **normal** state after the workspace moves to a new PC — expect a handful
and prune them without ceremony.

## Then

- If anything is `alive-not-listening` or the watchdog is not `listening`, say so
  first — that is the finding, not the inventory.
- Refresh `#fleet-status` if it has drifted, and update
  `memory\orchestrator\status.md` if the picture changed (write-through).
- Keep the reply phone-readable: what is wrong first, then the short inventory.
  Nothing wrong → say that in one line, not in a table.
