---
name: omnius
description: Connect this remotion session to the Omnius bus (delegates to the workspace-root skill).
---

# /omnius — bus connect (remotion stub)

A session only discovers skills from its own `.claude\`, so this stub delegates.

1. Resolve the **workspace root**: the nearest ancestor containing `tools\`,
   `projects\` and `memory\`. From here that is **two levels up**
   (`<root>\tools\remotion`).
2. Read `<workspace-root>\.claude\skills\omnius\SKILL.md` — the single source of
   truth — and follow it exactly.
3. Your session id is **`tool.remotion`**.

## Before answering anything, read `README.md` in this folder

It defines what this tool is and what state it is in. Keep it honest: this desk
is the only place the render contract is written down, so a README that lies
costs the next session its whole run.

## Never start a long render inside your own turn

A Remotion render is minutes, not seconds — and the **first** one on a machine
downloads Chrome Headless Shell before it renders a single frame. A desk blocked
on that is a desk that cannot answer, and from Discord a blocked desk and a dead
desk look identical.

Prefer a detached render that reports when it finishes. If you must render in
your turn, say so in the channel **first**, with a realistic estimate, so the
wait is expected rather than mysterious.

## Rendering is CPU-bound and this machine is shared

Other desks are working. Do not spawn unbounded concurrency — Remotion will use
every core it is given. Cap it (`--concurrency`) and say what you used.

## Where output goes

Renders land in this folder's own `out\` unless the brief says otherwise, and
`out\` stays out of git — a video is a build artifact, not source. When a render
is a deliverable for him, hand back the **absolute path**; the orchestrator
attaches the file, you do not post it yourself.

## Hard limits

- **Never write into `state\`** except your own `state\outbox\tool.remotion\`.
- **`node_modules\` stays out of git and out of the zip** — `install.bat`
  recreates it. Never commit it, never work around its absence by vendoring.
- Your memory holds **how to drive this tool** — the render contract, the
  gotchas, what a composition needs. Not the content of any particular video.
