---
name: goal
description: Take a high-level goal, decompose it into checkable outcomes, do or delegate the work, verify with real commands, and report what exists. Run when the user hands over an objective ("make X work", "get Y stable and shipped") rather than a single concrete task.
---

# /goal — an outcome, not a task list

The argument is the goal, verbatim. Your job ends when the goal is **verifiably
true** or when you have reported exactly how far it got and what stands in the
way. Never when a plan exists.

## The ritual

1. **Restate the goal as done-conditions** — each one a command with an exit
   code wherever possible (`test suite exits 0`, `git push` accepted, `curl`
   answers 200). A condition you cannot check mechanically gets a named
   observation instead ("the reply appears in the channel"). Write them down
   first; they are what "done" means, and the report at the end walks this
   list.
2. **Survey what EXISTS before building** — run the checks now. Some are
   already green; the gap is the work. State drift found here (failing suites,
   unpushed commits, stale config) is part of the goal even if unnamed.
3. **Route per the constitution** — your own scope you do here and now;
   substantial work in someone else's scope goes to that desk as desk mail
   (root CLAUDE.md §3, docs\DELEGATION.md). The goal holder tracks the chain,
   it does not do the siblings' work.
4. **Work, verifying as you go** — after each step, re-run that step's check.
   A green return code is the floor, not the proof: verify the *effect* where
   the two can differ (docs\RELIABILITY.md). Re-drain the inbox between major
   steps — a queued redirect beats finishing the wrong thing.
5. **Outward steps keep their brakes.** A goal saying "ship it" authorises the
   pushes and posts it names — but anything irreversible or wide-blast the
   goal did NOT name is still asked about in words first (/omnius §4).
6. **Spans runs? Open a work loop** (`schedule.py add --loop auto` with the
   done-condition in the text) instead of ending on a promise. Budget applies;
   the checkpoint at budget is the honest ending.
7. **Report against the list from step 1** — each condition: green, or what
   exists instead and why. Receipts, not adjectives: counts, commit ids,
   filenames. One phone-readable message; detail into files/memory, pointed at.

## Shape of a good run

- Goal: *"check if anything is missing, make sure all is working stable, make
  more tests, and then push"* → done-conditions: all suites exit 0 · new
  coverage exists for the gaps found · `git push` accepted. Survey found two
  red checks and a swallowed-message bug; the report named 1367/0, the two
  commits, and the one thing left for the owner (`!reload`).

## Refusals worth making

- A goal whose done-condition cannot be stated even as an observation is not a
  goal yet — ask ONE sharp question rather than guessing the intent.
- A goal that quietly implies destroying something (drop, wipe, force-push)
  gets the §4 ask-first treatment before any step runs, however clear the rest
  is.
