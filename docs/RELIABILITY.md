# Reliability plan — make the system enforce what the agent currently remembers

**Status:** proposed 2026-07-31, awaiting go. Nothing here is built yet.
**Goal (user):** *"un asistente personal para programación mucho mejor que OpenClaw o Hermes"* —
achieved by **improving what exists**, not by adding features.

## Why

On 2026-07-31 four things failed in one afternoon:

1. A spawned session sat frozen on a permission dialog while **every** health signal said healthy.
2. The orchestrator's inbox watcher died with a closed tab; the session went **deaf with no signal**.
3. Two sessions ended up on **one desk**, because `spawn_session()` was called without the guard.
4. The user waited **five minutes in silence** and reasonably assumed a crash.

They look like four bugs. They are one: **every invariant that keeps the bus working depends on an
agent remembering to do something** — re-arm the watcher, acknowledge receipt, check the desk is
free, delete the handled envelope.

All four were already written in memory before they were broken. **Documentation is not a
mechanism.** The fix is to move each invariant from discipline into code.

---

## R1 — A watcher that does not depend on being relaunched

**Problem.** `inbox_watch.py` exits when envelopes arrive (that exit *is* the wake-up). Re-arming is
therefore a manual step, and a forgotten one makes the session silently deaf. Happened twice in one
afternoon, to an agent that had written the warning itself.

**Fix.** A **`Stop` hook** that runs at the end of every turn and guarantees a watcher exists for
this session: check for a live `inbox_watch.py <id>` process, start one detached if absent.

- Idempotent, cheap, and it must **never block** the turn.
- It only *launches a process* — it does not inject context. (Injecting on `Stop` continues the
  conversation, which is a runaway risk on an always-on fleet; that stays off the table.)
- Belongs in the root profile and in `templates\project\.claude\settings.json` so spawned desks
  inherit it.

**Done when:** killing the watcher by hand, then ending a turn, leaves a live watcher.

## R2 — `!status` must tell *alive* from *listening*

**Problem.** The claim heartbeat is written by `inbox_watch.py`, a **separate process**. It keeps
stamping `lastSeenAt` while the session is blocked on a dialog, so a stalled desk is indistinguishable
from a working one in `!status`, the banner and the pinned embed.

**Fix.** One `session_state(id)` helper returning:

| state | how it is detected |
|---|---|
| `listening` | claim fresh **and** an `inbox_watch <id>` process is alive |
| `stalled` | last outbox write is `*-perm-timeout.json` (a dialog nobody can see) |
| `alive-not-listening` | session pid alive, no watcher process |
| `dead` | no live pid |

Used by `!status`, `status_banner.py` and the `#fleet-status` embed. **Never report health from
claim data alone again.**

**Done when:** a session parked on a permission dialog shows `stalled`, not `on`.

## R3 — A spawn that cannot duplicate a desk

**Problem.** `spawn_session()` has no occupancy guard; the check lives in its *caller*
(`watchdog.main()` tests `session_alive()` and `spawn_pending()`). Calling the primitive directly —
as the orchestrator legitimately does — silently skips both, and the second session is invisible
because a claim holds only one pid.

**Fix.** Move the guard **inside** `spawn_session()`: refuse and return a reason unless the desk is
free, with an explicit `force=True` for the deliberate case. Callers that already check stay correct;
callers that forget can no longer cause it.

**Done when:** calling `spawn_session()` twice in a row spawns exactly one session.

## R4 — Visible liveness in Discord

**Problem.** A terminal shows thinking, tool calls and progress. Discord shows nothing between the
user's message and the reply, so ten seconds and ten minutes look identical from a phone.

**Fix.** Already half-done — the ack rule is in `/omnius` §4. Add the reaction protocol
*(pending the user's confirmation)*:

- 👀 watchdog — *arrived*
- ✅ session — *read, quick, answer coming*
- 🔨 session — *read, working, this will take a while* + one line saying what

Reactions cost no channel noise, which is why they beat a "got it" message for the common case.

## R5 — Why does a plain `ls` prompt? *(prerequisite for real unattended operation)*

**Problem.** A spawned session escalated `ls "…/state/outbox/…"` even though the settings passed via
`--settings` allow `Bash(ls:*)`. It then froze for the rest of its life. **Until this is understood,
spawn-on-message is not genuinely unattended** — a desk can freeze on its first bus command.

**Fix.** Diagnose first, patch second. Leading hypothesis: a **compound** command (`ls … && …`)
evaluated as a whole rather than per part. Reproduce in a scratch session, then either narrow the
skill's commands to single forms or widen the profile deliberately.

**Done when:** a freshly spawned desk completes a full envelope round-trip with zero prompts.

## R6 — Answer an escalation from Discord, once

**Problem.** Permission escalation has fired twice and **failed safe** both times — but nobody has
ever replied `ok`/`no` in `#alerts`, so that half is unproven.

**Fix.** Deliberately trigger one and answer it from the phone.

**Done when:** a `#alerts` reply unblocks a waiting session, end to end.

---

## Order

`R3` (minutes, removes an incident class) → `R1` (the big reliability win) → `R2` (makes failure
visible) → `R5` (unblocks unattended) → `R4` (cheap polish) → `R6` (a test, not code).

## Rules for this work

- **Live-test everything.** The 194-check suite passed all day while four real failures happened in
  front of the user. A green suite is not evidence.
- Every item gets suite coverage **and** a live verification, with the live one written into memory.
- Verify the thing itself, never a proxy: not "a claim exists" but "the claim I just created"; not
  "the push succeeded" but "the remote tree contains what it should".
