---
name: brief
description: On-demand fleet briefing - what moved today, what is running, what is open, what needs the owner. Run when the user asks what's new, what happened today, or for a briefing.
---

# /brief — the fleet in one phone screen

He asked, so this is the one report where "nothing to say" is still said —
in one line. Gather, then compose; never paste raw command output.

## Gather (all read-only)

1. **Fleet**: `python <root>\tools\orchestrator\fleet_ops.py status`
   — desks up/down, watchdog beacon. Anything dead or deaf leads the brief.
2. **Today's product work**: `git log --oneline --since=midnight` at the
   root (and in any project repo he has been active in, if relevant).
3. **Open work**: the "Open work" section of
   `memory\orchestrator\status.md` — the standing list, not a dump of it.
4. **In flight**: open loop ledgers (`state\watchdog\loops\*.json`, not
   closed), **open workflows** (`state\watchdog\threads\*.json` with a
   `workflow` block whose `status` is `open` or `stalled` — goal, holder,
   `runs`/`budget`, `lastStep`), pending gate holds (`state\gate\*.json`), and
   anything waiting in `state\inbox\*\` older than a few minutes.

## Compose — strictly in this order

- **Needs him** — pending gate ok, a stuck desk, a failing routine. Nothing →
  skip the section entirely, never write "nothing needs you".
- **Moved today** — commits/results in one line each, plain words.
- **Running now** — active loops/runs, one line.
- **Open** — the standing items, one line each.

Bullets, `**label** — value`, no tables, no headers. Aim under ~1,200 chars:
a briefing that needs scrolling has stopped being one. Personal daybook
content stays out unless he asked for it by name (root CLAUDE.md §3 —
personal data never surfaces into fleet reporting on its own).
