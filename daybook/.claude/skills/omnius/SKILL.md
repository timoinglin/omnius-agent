---
name: omnius
description: Connect this daybook session to the Omnius bus (delegates to the workspace-root skill).
---

# /omnius — bus connect (daybook stub)

The daybook lives inside an Omnius workspace, but a session only discovers
skills from its own `.claude\` — so this stub delegates.

1. Resolve the **workspace root**: the nearest ancestor folder containing
   `tools\`, `projects\` and `memory\`. From here that is **one level up**
   (`<root>\daybook`), *not* three — this is not a project component.
2. Read `<workspace-root>\.claude\skills\omnius\SKILL.md` — the single source
   of truth — and follow it exactly.
3. Your session id is **`daybook`** (no dot). Your channel is `#daybook`.

## What this desk is for

Capturing and answering questions about the user's personal notes, strictly per
`daybook\README.md`. **Check whether the server is running first**
(`http://localhost:5111`): while it is up you must go through the API, because a
direct file edit bypasses its content-hash conflict check and can clobber a line.
Only when it is down is an append-only file edit correct.

**Not everything in `#daybook` is a note.** A message asking *about* the notes is
a question — answer it, do not store it. Store what is meant to be recorded.
When it is genuinely ambiguous, ask rather than silently writing to the user's
personal notes.

Personal data stays here: never copy it into project folders or project memory.
