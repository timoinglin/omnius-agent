---
name: watch
description: Watch a video (URL or local path) - downloads, extracts frames, pulls the transcript, and answers questions about what is in it. Delegates to the workspace-root skill.
---

# /watch — video understanding (project stub)

This project lives inside an Omnius workspace, and project sessions only
discover skills from the project's own `.claude\` - so this stub delegates.
Without it, `/watch` fails with *"Unknown skill: watch"* in any project desk
(2026-08-03), even though the skill is installed at the root.

1. Resolve the **workspace root**: the nearest ancestor folder containing
   `tools\`, `projects\` and `memory\` (from a component folder it is three
   levels up: `<root>\projects\<project>\<component>`).
2. Read `<workspace-root>\.claude\skills\watch\SKILL.md` - the single
   source of truth - and follow it exactly.
3. `SKILL_DIR` is that file's directory: `<workspace-root>\.claude\skills\watch`.
   The bundled scripts live at `SKILL_DIR\scripts\`, NOT in this project.
