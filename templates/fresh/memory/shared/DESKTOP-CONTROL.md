# The PC can be seen and driven from Discord — and what that costs

Built 2026-08-01 by the `tool.discord` desk. Detail, risks and the two live bugs
behind the design: `tools\desktop\README.md`.

## What exists

`!screen` (whole screen or `!screen <window>`) and `!desktop windows|focus|open|close`,
executed by the watchdog itself so they still work when every desk is dead.
Locally: `python tools\desktop\desktop.py <verb>`.

**The registry is the allowlist** (user decision 2026-08-01, explicitly *not* raw
`pyautogui.click(x, y)` from chat). `VERBS` in `desktop.py` is the complete list of
things this can do; `open` takes a name from a fixed `APPS` table, never a path or
command line. Adding a verb is a git commit — if you want a new capability, write
the function, don't reach for a generic one. Every call lands in
`state\logs\desktop.log`; the text given to `type-into` is logged as a length only,
because that is where a password would otherwise end up.

## Two things to say out loud before using it

- **A screenshot cannot be redacted.** The outbound token filter cannot help. If
  `.env`, a password manager, a private chat or customer data is on screen,
  `!screen` publishes it to Discord and it may outlive deletion. The owner
  allowlist is the *only* control. Think before capturing on the work machine —
  the employer-hardware instance is real (see [[permissions]] / `docs\PERMISSIONS.md`).
- **`key` and `type-into` are local-CLI only and are NOT finished.** Measured:
  `SendInput` returned success and the audit log said `ok` while the target app
  did nothing, and in an earlier run text arrived mangled — all while the verb
  reported success. They are excluded from the Discord surface for that reason.
  **Do not "fix" this by adding a sleep and declaring it working**; it needs a
  real delivery check. Until then the confirmation is the read side: act, then
  `!screen` and look.

## The general lesson, worth more than the feature

A GUI verb that reports success it cannot verify is worse than one that fails.
The exit code said `ok` three times while the screen said otherwise, and only
looking at a screenshot caught it. When testing anything that drives a UI,
**verify the effect, not the return value** — the same rule already written in
`docs\RELIABILITY.md` ("a green suite is not evidence").
