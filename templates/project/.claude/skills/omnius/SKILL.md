---
name: omnius
description: Connect this project session to the Omnius bus (delegates to the workspace-root skill).
---

# /omnius — bus connect (project stub)

This project lives inside an Omnius workspace, but project sessions only
discover skills from the project's own `.claude\` — so this stub delegates.

1. Resolve the **workspace root**: the nearest ancestor folder containing
   `tools\`, `projects\`, and `memory\` (from a component folder it is three
   levels up: `<root>\projects\<project>\<component>`).
2. Read `<workspace-root>\.claude\skills\omnius\SKILL.md` — the single source
   of truth — and follow it exactly.
