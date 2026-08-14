# tools\desktop — screen-read + named verbs (Phase 3, built 2026-08-01)

Remote eyes and hands for the PC, reachable from Discord. Design decision of
2026-08-01: **screen-read + a closed registry of named verbs**, explicitly *not*
raw `pyautogui.click(x, y)` driven from chat.

```
python tools\desktop\desktop.py windows
python tools\desktop\desktop.py screenshot [--window "Code"] [--out path.png]
python tools\desktop\desktop.py focus "Visual Studio Code"
python tools\desktop\desktop.py open notepad
python tools\desktop\desktop.py close "Bloc de notas"
python tools\desktop\desktop.py key "Bloc de notas" ctrl+s          # local only
python tools\desktop\desktop.py type-into "Bloc de notas" "hello"   # local only
```

From Discord (owner only, any channel):

```
!screen                     the whole screen, posted as a PNG
!screen Code                just that window
!desktop windows            what is open
!desktop focus Code    ·    !desktop open notepad    ·    !desktop close Notepad
```

The watchdog runs these **in-process**, not by spawning a session: looking at the
screen has to work precisely when every desk is dead.

## Why named verbs and not raw GUI automation

This system's whole safety model is the Claude Code permission layer — scoped
allow-lists, absolute `deny` entries, and the `#alerts` escalation hook
(`docs\PERMISSIONS.md`). Unrestricted GUI automation defeats all of it at once:
it can click "Allow" on any permission dialog, type into any window, and read
`.env` off the screen. Every `deny` stops being absolute and becomes advisory.

That matters more than usual here — a second instance runs on **employer
hardware against a company Discord server**, and the threat model already says
*"whoever can write in these channels can drive your PC."*

So: **the registry is the allowlist.** `VERBS` in `desktop.py` is the complete
list of things this can do. There is no `click x y`, no `run <command>`, and
`open` takes a **name from the `APPS` table**, never a path or a command line —
an `open <anything>` verb would be arbitrary code execution from a chat message.
Adding a verb or an app is a git commit somebody can read (ARCHITECTURE §3.6).

Every invocation is appended to `state\logs\desktop.log` with the verb, its
argument and the caller (`discord:#omnius`, `cli`, …). The **text passed to
`type-into` is deliberately not logged** — only its length — because that is the
obvious place for a password to land in a file nobody treats as a secret.

## Two limits worth knowing before you rely on this

**1. A screenshot cannot be redacted.** The outbound token filter cannot help
here: if `.env`, a password manager, a private chat or a customer's data is on
screen, `!screen` publishes it to Discord, where it may persist beyond deletion.
The `DISCORD_OWNER_ID` allowlist is the *only* control on this, and there is no
plan to pretend otherwise. `--window` narrows the capture, which helps, but it
grabs the window's **screen region** — anything overlapping it is captured too
(the verb focuses the window first to reduce that).

**2. `key` and `type-into` cannot confirm they worked, so they are local-CLI
only.** Measured 2026-08-01 against the Store-app Notepad: `SendInput` returned
the full event count and the audit log recorded `"result": "ok"` for every call,
while the application did nothing at all — a clipboard sentinel survived a
Ctrl+A/Ctrl+C with the target confirmed in the foreground. Earlier in the same
session, injected text arrived mangled (`"hola desde Omnius"` → `"hola
mmmmmmmmnius"`) while the verb still reported success.

A verb that reports success it cannot verify is the worst possible failure mode
for remote control, because the person trusting it is not in the room. So they
are excluded from `REMOTE_VERBS`, they say `delivery NOT verified` in their own
output, and **the honest confirmation is the read side: take a screenshot and
look.** Fixing this properly means an app-specific delivery check, not a longer
sleep — treat the current behaviour as unfinished rather than as a quirk.

Two real bugs found the same way, both worth not reintroducing:

- The usual "tap ALT to lift the foreground restriction" trick puts a Win32 app
  into **menu mode**, so typed characters become menu accelerators. `_focus_hwnd`
  uses `AttachThreadInput` instead and synthesises no keys at all.
- `ctypes` defaults every undeclared return type to 32-bit `int`, which
  **truncates a 64-bit HWND**. Every prototype used here is declared explicitly.

## Dependencies

Only **Pillow** (already installed; used solely to encode the PNG). Windows is
driven through `ctypes`/`user32`. `pyautogui` is not needed for normal Win32
apps and `pydirectinput` would only matter for DirectInput targets (games, some
anti-cheat apps) — do not take either dependency speculatively.
