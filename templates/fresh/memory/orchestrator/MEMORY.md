# Orchestrator Memory — Index (Omnius only)

Fleet facts, decisions, and what this instance has learned. **Read `status.md`
at boot; open a topic only when you need it** (root CLAUDE.md §2).

- [System status](status.md) — **start here.** Current state, standing rules, open work. Deliberately short.
- [Heartbeat checklist](HEARTBEAT.md) — proactive duties, read on every heartbeat.

## Topics

These three ship with a fresh instance because they are how Omnius WORKS —
hard-won rules a new instance would otherwise re-learn the same painful way:

- [Discord & the fleet](topics/discord-fleet.md) — channel→desk map, control commands, the bus pattern, one-desk-per-folder.
- [Naming](topics/naming.md) — channels route by id (rename freely); the agent's name is a setting.
- [Permissions](topics/permissions.md) — the autonomy dial, `fleet.json` posture, escalation and what is actually true about it.
- [Lessons](topics/lessons.md) — **read before "fixing" anything that looks wrong.** Several entries exist to stop a future session undoing a deliberate choice.

Add your own as this instance accumulates facts — a `backup-transport.md` for
how this machine is backed up, a `history.md` once there is history, a
`roadmap.md` for capabilities announced but not scheduled.

## Curation rules

- `status.md` holds only what is **current**. When a fact stops being current,
  move it to a topic file. The suite enforces a character budget on both, and
  it exists because `status.md` once reached 65 KB.
- One topic per file. Absolute dates, never "yesterday". Delete what is proven
  wrong rather than appending a correction beside it.
- **Never secrets.** They live in `.env` at the root and nowhere else.
