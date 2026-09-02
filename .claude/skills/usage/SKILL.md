---
name: usage
description: Report how much of the Claude plan is spent - the 5-hour and weekly limit percentages, the same numbers the Claude Code /usage panel shows. Run when the user asks how much plan is left, whether they are near a limit, or for usage.
---

# /usage — how much of the plan is left

He is on a **subscription**, so this is the number that can actually run out.
It is **not** cost: `memory\orchestrator\topics\claude-cost.md` estimates what
the work would cost per token, which is a different question and settled.

## Steps

1. `python <root>\tools\usage.py`

   Add `--full` when he asks *why* a number is high — it appends the
   contributing-factors breakdown (requests, long sessions, top skills).

2. Post what it printed, as-is. Three short lines is the whole answer:

   ```
   Current session: 5% used · resets Sep 2, 12:19pm
   Current week (all models): 1% used · resets Sep 4, 6:59pm
   Current week (Fable): 0% used
   ```

   Do not convert it to a table — Discord renders none, and this is already
   phone-shaped.

## Notes

- **Why a script rather than telling him to type `/usage`:** he reads Discord
  from a phone. Owner, 2026-09-02: *"si estoy en discord no puedo poner /usage
  en terminal"*. That is the entire reason this exists.
- The numbers come from Anthropic at request time — nothing on disk caches
  them, so this needs the machine to be online. It takes a few seconds because
  it starts a one-shot `claude -p` run.
- **The percentages are the machine's view.** The panel says so itself:
  approximate, local sessions on this machine only, not other devices or
  claude.ai. Pass that on rather than reporting them as exact.
- On failure it prints `[X] could not read plan usage:` and the reason. Report
  that reason — never a bare "it did not work", and never a guessed number.
