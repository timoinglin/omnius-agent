---
name: omnius
description: Connect this Discord-bus maintainer session to the Omnius bus (delegates to the workspace-root skill).
---

# /omnius — bus connect (discord stub)

A session only discovers skills from its own `.claude\`, so this stub delegates.

1. Resolve the **workspace root**: the nearest ancestor containing `tools\`,
   `projects\` and `memory\`. From here that is **two levels up**
   (`<root>\tools\discord`).
2. Read `<workspace-root>\.claude\skills\omnius\SKILL.md` — the single source of
   truth — and follow it exactly.
3. Your session id is **`tool.discord`**. You have no channel of your own: you
   are reached by desk mail, and you answer the sender by desk mail.

## What this desk is

The maintainer of the bus itself: `watchdog.py`, `gateway.py`, `schedule.py`,
the hooks (`permission_relay.py`, `turn_start_hook.py`, `turn_end_hook.py`,
`secret_guard.py`, `mail_notice_hook.py` — all five wired by
`fix_hook_paths.py`), `tools\bridge\desk_bridge.py`, `test_watchdog.py`, and
the docs that describe them (`docs\ARCHITECTURE.md`, `docs\DELEGATION.md`,
`docs\LESSONS.md`). Changes here
reach every desk, so **the suite is the acceptance test, always**:

```
python <root>\tools\discord\test_watchdog.py
```

## Hard limits

- **Commit locally, never push.** The orchestrator releases (`/release`).
- **Never `!reload` the watchdog yourself** — you are running inside a run it
  owns. Say in your reply that a reload is needed.
- Config keys you add need a SPEC row (`docs\DELEGATION.md` D7, one table) or
  `!config` cannot see them.
- Never write into `state\` except your own `state\outbox\tool.discord\`.
