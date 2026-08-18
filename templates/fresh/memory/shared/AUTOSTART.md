# The always-on services start themselves — don't nurse them by hand

Built 2026-08-01 by the `tool.discord` desk (ARCHITECTURE §Phase 4). Full detail
and the measurements behind it: `tools\discord\README.md`, "Autostart".

## What is true now

The **watchdog** and the **daybook server** each run from a Windows scheduled
task (`Omnius Watchdog`, `Omnius Daybook`), **hidden** — no console window, logs
in `state\logs\<name>.out.log`. A third, `Omnius Telegram`, is registered only
once `config\telegram.ini` exists (nobody invited, no service). Each task fires
at logon *and* every minute, so a service comes back on its own within ~a minute
of dying, however it died.
Verified live: watchdog killed → back, unattended, in **21 s**.

## What this changes for you

- **From the phone, the bot's own presence answers it first.** Since the Gateway
  swap (2026-08-01) the watchdog holds a websocket, so **Omnius shows online in
  Discord exactly while the bus is alive** — no command, no terminal. Offline bot
  + your message unanswered is the one combination worth reporting.
- **"The bus is down" is a claim to check, not to act on.** One command answers it:
  `powershell -NoProfile -ExecutionPolicy Bypass -File tools\discord\autostart.ps1 -Action status`
  — exit 0 only if both services are registered **and answering** (watchdog: a
  fresh `state\watchdog\beacon.json`; daybook: `localhost:5111`). `-Action repair`
  fixes whatever it names.
- **Don't start a second watchdog to "help".** It will exit on the single-instance
  lock, which is correct but looks like a failure. If one is genuinely wanted,
  the task must go first (`-Action uninstall`), or the repeat trigger revives it
  inside a minute.
- **After the workspace moves to another PC, run `-Action repair`.** A scheduled
  task action is necessarily an absolute path, so a moved workspace leaves both
  tasks pointing at a folder that no longer exists — and they fail *silently*.
  The permanent work PC is expected the week of 2026-08-07; this is the step that
  gets forgotten. (Related: `[[machine-move]]` checklist in the orchestrator's
  backup/transport topic — this line belongs there too.)

## The trap that was already in the tree

`RestartCount`/`RestartInterval` ("if the task fails, restart every 1 min") reads
like a safety net and **is not one**: Task Scheduler does not treat an action
exiting non-zero as a task failure. Measured — a killed watchdog stayed dead with
the task sitting at `Ready`, `LastTaskResult=0x1`, no next run. The self-heal is
the repeating **time** trigger; a repeating *logon* trigger looks identical in the
UI and does nothing for a task registered after logon. Don't "simplify" it back.
