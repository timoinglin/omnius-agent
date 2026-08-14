# tools\discord — Discord layer (Phase 2 — built, in live testing)

Home of everything Discord (design: `docs\ARCHITECTURE.md` §3.4 · blueprint: `docs\DISCORD.md`):

- **`watchdog.py`** — the only always-on piece: polls mapped channels, enforces the owner
  allowlist, downloads media to `media\inbox\`, feeds session inboxes, spawns sessions on
  demand, executes control commands (`!kill` `!restart` `!status` `!killall`), posts outbox
  replies back (chunked, redacted; sent files archived to `media\sent\`). Non-interactive:
  missing config → clear message + exit. Run via `start-omnius.bat` or directly.
- **`api.py`** — REST helper library + admin CLI: send/edit/delete messages, embeds via send,
  reactions, pins, file uploads, history, create/rename/delete channels & categories, topics.
  Reads the token from root `.env` itself — Claude invokes it without ever seeing secrets.
  `python tools\discord\api.py --help` for the surface.
- **`gateway.py`** — Discord Gateway websocket client, stdlib only (no deps). Pushes
  messages to the watchdog instead of making it poll (below). Never the authority.
- **`inbox_watch.py`** — session-side watcher + claim keeper: run as a background task by
  `/omnius`; heartbeats `state\sessions\<id>.json`, exits when envelopes arrive.
- **`schema.json`** — machine-readable day-one server structure + project template;
  `api.py ensure` / the watchdog stamp it idempotently (find-or-create by exact name).
- **`setup.ps1`** — guided per-instance Discord configuration: called by the root
  launchers/installer when `.env` lacks the Discord values; validates token + server live.
- **`autostart.ps1`** — owns the Task Scheduler jobs that keep the watchdog and the
  daybook server up (below). `-Action status|install|repair|uninstall`.

Reads from root `.env`: `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_OWNER_ID`,
`MACHINE_NAME` (optional — defaults to the computer name).

## Gateway websocket (Phase 5 — built 2026-08-01)

The watchdog holds one Discord Gateway websocket and blocks on its event queue
instead of sleeping between REST polls. Measured on one clock: **the pushed
event arrives before our own `POST` response does** — Discord sends it down the
socket while the HTTP ack is still in flight. The old path cost a 0–3 s poll
wait plus a ~300 ms fetch; there is now no wait to speak of.

**REST never stops running.** A sweep over every mapped channel with
`after=<lastId>` still runs every `RECONCILE_SECONDS` (60 s), and the moment the
socket is down or was never available it goes back to once per `POLL_SECONDS`
(3 s) — bit-for-bit the old behaviour. This is deliberate and load-bearing: the
websocket client is hand-rolled, so its worst failure mode has to be *latency*,
not a lost message. Anything the socket drops the sweep still finds, and a
rescued message is logged (`gateway missed N`) so a real bug in `gateway.py`
shows up as a pattern instead of as silence. Keep that property in any change.

- **`DISCORD_GATEWAY=0`** in root `.env` forces plain REST polling.
- **Presence is a free health indicator**: the bot shows online in Discord
  exactly while the socket is up, so "is the bus alive?" is answerable from the
  phone with no command. `state\watchdog\beacon.json` also carries `"gateway"`.
- **`MESSAGE CONTENT INTENT` must be ticked** in the Developer Portal (Bot →
  Privileged Gateway Intents) — it is privileged, and without it Discord closes
  IDENTIFY with 4014. That, a bad token (4004) and the other unrecoverable close
  codes stop the retry loop, log exactly which box to tick, and leave the
  watchdog polling. A gateway problem must never take the bus down with it.
- Why hand-rolled rather than `websockets`/`discord.py`: `api.py` is stdlib-only
  by design and the workspace travels as a zip to machines that fill their own
  `.env`. A pip dependency *on the transport* means a machine where the install
  quietly failed is a machine with no bus at all — the one component that has to
  work before anything can report that anything else is wrong.

## Autostart (Phase 4 — built 2026-08-01)

Two scheduled tasks, one per always-on service, registered by `autostart.ps1`:

```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\discord\autostart.ps1 -Action status
                                                                               -Action repair
```

`status` exits 0 only if both services are **registered and answering** — the
watchdog via a fresh `state\watchdog\beacon.json` (a successful poll pass, not
merely a live pid), the daybook via `localhost:5111`. `repair` re-registers and
restarts whatever drifted. `install.bat` calls both; nothing else needs to.

**Run `-Action repair` after moving the workspace to another PC.** A task action
is necessarily an absolute path, so a moved workspace leaves both tasks pointing
at a folder that no longer exists — and they fail silently. `status` names that
case explicitly (*"points at a different workspace/script"*).

Three things this gets right that the earlier inline version in `install.ps1`
did not, each of them measured on 2026-08-01 rather than assumed:

- **The self-heal is a repeating time trigger (1 min), not restart-on-failure.**
  With `RestartCount 999` / `RestartInterval 1 min` and an at-logon trigger,
  killing the watchdog left it dead: the task went `Ready` with
  `LastTaskResult=0x1` and no next run. Task Scheduler does not treat an action
  exiting non-zero as a task failure, and a *logon* trigger's repetition only
  runs inside the window logon opened — a task registered after logon has no
  window at all. A plain `-Once` trigger with an indefinite 1-minute repetition
  has a real `NextRunTime` and fires regardless of how the service died.
  Re-tested after the fix: **killed → back on its own in 21 s.**
  It costs nothing while healthy — `MultipleInstances=IgnoreNew` means the
  scheduler never even starts the duplicate, and `acquire_lock()` catches it again.
- **Services run hidden**, via `pythonw.exe tools\service_runner.py <script>`.
  A console task puts a window on the desktop at every logon, and closing that
  window kills the service. `pythonw` leaves `sys.stdout`/`sys.stderr` as `None`,
  so `service_runner.py` points them at `state\logs\<name>.out.log` first —
  otherwise a startup traceback would die with the process, unseen.
- **One owner.** The task definitions lived inline in `install.ps1`, behind a
  `Read-Host`; the tasks on the live machine had drifted from it with nothing to
  notice. `install.ps1` now owns only the question.

To stop a service for more than a minute, `-Action uninstall` or
`Disable-ScheduledTask` — the repeat trigger will otherwise bring it back.
`start-omnius.bat` still gives visible panes; a pane started while a task holds
the lock says so and exits, which is correct.

## Local bus transcript

The watchdog appends every inbound and outbound bus message to
`state\transcripts\<session>\<YYYY-MM>.jsonl`. Envelopes and outbox files are
deleted once handled, so before this the only copy of a remote conversation
lived in Discord — it did not survive `!kill`, a fresh `--continue`, or leaving
the channel.

```
python tools\discord\transcript.py sessions
python tools\discord\transcript.py tail -n 20
python tools\discord\transcript.py search "api contract" --session recipe-app.backend --days 30
```

Secrets are redacted on write. `state\` is git-ignored and excluded from the
zip, so this is a log, not luggage. **No index, deliberately** — a measured
10-year corpus scans in ~55 ms with plain stdlib while an FTS5 index over it
rebuilds in 0.38 s, so a persistent index can never repay its own staleness
tracking (see `memory\orchestrator\status.md`).

## Scheduled envelopes

`schedule.py` gives the bus exact timing and one-shots — distinct from the
heartbeat, which is a batched approximate loop.

```
python tools\discord\schedule.py add --every 20m  --to orchestrator   --text "check the deploy"
python tools\discord\schedule.py add --daily 07:30 --weekdays --to orchestrator --text "morning briefing to #daybook"
python tools\discord\schedule.py add --at 2026-08-01T09:00 --to recipe-app.app --text "ship the beta"
python tools\discord\schedule.py list / remove <id>
```

A due job is written as an ordinary inbox envelope, so it wakes or spawns its
target session through exactly the same path a Discord message does — there is
no second delivery mechanism to keep in step.

Times are **local**. Schedules live in `state\` and are therefore **per-machine**
(like `.env` and session claims); a job firing on two machines would double-post.

**Catch-up policy:** a job whose time passed while the PC was off is
**rescheduled, not run** — waking to fourteen stale reminders is worse than
missing them — and an expired one-shot is dropped rather than fired late.

⚠ A recurring job spawns a Claude session each time it fires, which costs
tokens. Check `schedule.py list` before leaving the machine.
