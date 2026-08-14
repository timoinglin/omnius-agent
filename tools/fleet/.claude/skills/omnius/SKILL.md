---
name: omnius
description: Connect this fleet-status session to the Omnius bus (delegates to the workspace-root skill).
---

# /omnius — bus connect (fleet stub)

A session only discovers skills from its own `.claude\`, so this stub delegates.

1. Resolve the **workspace root**: the nearest ancestor containing `tools\`,
   `projects\` and `memory\`. From here that is **two levels up**
   (`<root>\tools\fleet`).
2. Read `<workspace-root>\.claude\skills\omnius\SKILL.md` — the single source of
   truth — and follow it exactly.
3. Your session id is **`tool.fleet`**. Your channel is `#fleet-status`.

## Before answering anything, read `README.md` in this folder

It defines what this desk may touch (read `state\`, nothing else) and — more
importantly — **how to tell a *listening* session from a merely *alive* one**.
Reporting a stalled desk as healthy is the specific failure this desk exists to
prevent; it happened on 2026-07-31 and cost three hours.

## Hard limits

- **Report only.** Never kill, spawn or restart anything — those are the user's
  calls (`!kill` / `!restart` / `!killall`) or the orchestrator's.
- **Never write into `state\`** except your own `state\outbox\tool.fleet\`.
  Every other file there has exactly one writer.
- The pinned board in `#fleet-status` is edited **in place** via `message_id`,
  never re-posted — a status board is one message that updates, not a channel
  full of snapshots.
