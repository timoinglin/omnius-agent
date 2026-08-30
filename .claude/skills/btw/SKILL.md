---
name: btw
description: Answer a side question without it becoming the desk's work - no memory write, no session notes, no files touched, no task started. Run when the user asks something in passing that should not disturb what this desk is doing.
---

# /btw — a side question, not a new mandate

Claude Code's own `/btw` keeps a question out of the conversation so the main
task's context stays clean. **A desk cannot do that half** — a headless run has
no terminal, and its transcript is the continuity between runs, so the question
is in the transcript the moment it arrives. This skill does the half that
actually matters on a phone: **the question does not become work.**

Owner asked for it 2026-08-29 by name, together with `/design`. Built as an
Omnius skill because the built-in is terminal-only (§ "What cannot travel").

## The contract

Answer the question after `/btw`, then stop. For the length of this answer:

- **Write nothing durable.** No `memory\`, no `memory\sessions\<component>.md`,
  no daybook entry, no `status.md`. A side question is not a decision, and the
  notes are for decisions.
- **Change no files, run no build, start no task.** Read-only commands to *find*
  the answer are fine — `git log`, a `python` query, reading a file.
- **Do not adopt it as the desk's mandate.** If this desk was mid-task, the task
  is still the task. Say so in one line if the answer suggests otherwise, and
  let him choose: *"answered — still on the endpoint unless you want me to
  switch."*
- **Do not delegate it.** Desk mail wakes a whole session and mirrors into a
  channel; that is the opposite of a side question. If it genuinely needs
  another desk, say which and let him send it.
- **No routine, no loop, no schedule.** Those outlive the answer by design.

## Answering

Short. Discord shape (bullets, no tables, under ~1,200 chars) — the rules in
`/omnius` §4 apply unchanged, because the reply goes out the same outbox.

If the honest answer is "I would have to go and look, that is 10 minutes of
work", **say that instead of doing it**. `/btw` is the promise that a passing
question stays cheap; silently spending an hour on one breaks it.

## What cannot travel from Discord at all

`/btw` was the first of a family he will ask about again, so the reason lives
here. A `/<word>` from Discord reaches a desk and is invoked with the **Skill**
tool. That works for a skill; it cannot work for a command implemented by the
terminal UI, because a headless run has no UI to drive:

`/focus` · `/context` · `/usage` · `/cost` · `/rewind` · `/model` (use `!model`)
· `/clear` · `/compact` · `/resume` · `/config` · `/theme` · `/diff` · `/copy`
· `/keybindings` · `/permissions` · `/hooks` · `/memory` · `/doctor` · `/login`
· `/exit` · `/ide` · `/desktop` · `/teleport` · `/remote-control` · `/agents`

Asked for one of those, say which `!verb` or skill does the useful part rather
than reporting a failure. The full mapping is `docs\SLASH.md`.
