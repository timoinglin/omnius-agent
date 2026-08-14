---
name: archive-project
description: Wind a project down - final memory write, kill its desks, archive its Discord category, optionally move the folder to projects\_archive. Also un-archives. Run when the user asks to archive, retire, close or reopen a project.
---

# /archive-project — wind a project down (reversibly)

Orchestrator verb (ARCHITECTURE §5.5). **This is destructive. Confirm with the
user first** (root CLAUDE.md §3) — say exactly what will happen and wait.

## 1. Let the desks write their memory FIRST

Before killing anything, give each live desk of that project a chance to record
where it got to, in `projects\<name>\memory\sessions\<component>.md`. Once the
session is gone that context is gone with it, and the folder outlives it.

If a desk is mid-work, ask the user before interrupting.

## 2. Confirm, then run

```
python tools\orchestrator\fleet_ops.py archive <name>            # keep the folder
python tools\orchestrator\fleet_ops.py archive <name> --move     # also move it to projects\_archive\
```

It kills every `<name>.*` desk, renames the Discord category from `📁 ` to `🗄 `
(history preserved — nothing is deleted), and moves the folder only if asked.

**Default to keeping the folder in place.** `--move` is for genuine clear-out;
the project is its own git repo and moving it is easy to undo but easy to forget.

Idempotent — safe to re-run if it was interrupted.

## 3. Reversing

```
python tools\orchestrator\fleet_ops.py unarchive <name>
```

Same skill, same script: folder back from `_archive\`, category back to `📁 `.
It will not overwrite a live `projects\<name>\` if one exists — it says so and
leaves the archived copy alone rather than guessing which one you meant.

## 4. Write through, then confirm

Update `memory\orchestrator\status.md` in the **same action** — mark the project
archived with the date. An archived project that still reads as live in status is
how a future session spawns a desk into a folder nobody meant to reopen.

Confirm in one line, saying what was kept: *"🗄 recipe-app archived — 2 desks
closed, channels kept as history, folder left in `projects\`."*
