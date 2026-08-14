# Heartbeat — proactive checklist (read on every heartbeat)

**Built 2026-08-01.** The watchdog *checks* every `HEARTBEAT_MINUTES` (`.env`, default
30; `0` = off) but only wakes you when it already sees something mechanical to do —
stale claims, the daily briefing after 07:00, Monday's gardening. The envelope lists
what it noticed. Work through this list. **If nothing actually needs attention once
you look, end the turn silently — no message anywhere.** Nobody is waiting for a reply;
a heartbeat is not a message from the user.

Why the pre-filter: waking an Opus session every 30 minutes just to conclude "nothing
to say" is ~48 sessions a day against goal 6 (*nothing runs and nothing spends unless
there is work*). The quiet rule is therefore enforced in the transport too, not only
here. Judgement is still yours — the watchdog only decides whether there is a candidate
worth the wake.

Edit this file freely; it is the single source of Omnius' proactive behavior.

## Every heartbeat
- Fleet health: prune dead/foreign claims in `state\sessions\`; if reality drifted from
  the pinned `#fleet-status` embed, refresh it.
  **Re-check staleness yourself before pruning anything.** The envelope's list is a
  snapshot from when it was composed, and a desk whose run started moments earlier
  shows a dead pid until that run's check-in rewrites the claim. On 2026-08-01 a
  heartbeat named `orchestrator` as stale while it was very much alive; obeying it
  would have freed the desk and invited a SECOND orchestrator. Run `stale_claims()`
  again at the moment you act.

## Once per day (first heartbeat after 07:00)
- Morning briefing: **the `daybook` desk owns this, not the orchestrator.** Proved
  2026-08-01 — an orchestrator post to `#daybook` is renamed `.refused` by the
  outbox scoping, correctly: that channel belongs to the `daybook` desk. The duty
  here is only to **hand it the job and let it brief** — write a `from: "omnius"`
  envelope into `state\inbox\daybook\` and stop; the watchdog starts that desk.
  Never post to `#daybook` yourself. Content is its business: open tasks + today's
  notes, short and friendly, no repeat of yesterday's unchanged items.
- **Backup check:** if no `omnius-<today>.zip` sits in the backup folder, say so in one
  line — don't pack silently. The user's word ("make a backup") is the trigger; daily is
  only the target. Procedure and the `-Work` trap are in `status.md`. On the permanent PC
  this becomes a Task Scheduler job and this line can go.

## Weekly (first heartbeat after 07:00 on Monday)
- Memory gardening: walk the `memory\` indexes — delete stale facts, merge duplicates,
  compact oversized topic files, fix broken links. Commit the cleanup.

## Scheduled items (add your own)
- *(none yet — add lines like: "2026-08-15: remind me to review the Discord token")*
