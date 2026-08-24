# Standing up a new Omnius instance

Follow this on a **new machine** (new job, new laptop, second PC). It assumes a
zip built by `pack.ps1` — see §0 for which kind you want.

Two facts drive everything below:

- **`.env` never travels.** `pack.ps1` excludes it on purpose (secrets stay put).
  A fresh instance therefore *always* reports "Discord not set up" on first run.
  **That is correct behaviour, not a bug** — you are expected to set up the bot
  for *this* instance. It caught the author out once; that is why this note exists.
- **`state\` never travels either.** Claims, the bus, logs, the watchdog lock and the
  channel→desk **pins** (`state\watchdog\channels.json`) are machine-local and rebuild
  themselves. An empty `state\` on day one is normal — with one thing worth knowing: with
  no pins the map is derived from channel **names** for one round and pinned again, so a
  channel you had renamed in Discord on the old PC comes back **unmapped**. It says so out
  loud (a project channel even names the folder it expected); rename it back to re-pin it,
  after which you can rename it freely again.

---

## 0. Build the zip (on the OLD machine)

| Command | Use when |
|---|---|
| `pack.bat` or `powershell -File pack.ps1` | Personal machine → personal machine. Carries **everything**: projects, daybook notes, media archive. |
| `powershell -File pack.ps1 -Work` | **Work / employer hardware.** System only. Leaves `daybook\notes\`, every `projects\<name>\` and `media\` behind. |

`-Work` genuinely removes that content: all three paths are gitignored and were
never committed, so the bundled `.git\` cannot leak them back. The suite asserts
this (`== pack -Work ==`) — if someone un-ignores one of those paths, the test
fails rather than silently shipping personal notes to an employer's PC.

The archive lands **next to** the workspace folder, named
`omnius-work-YYYY-MM-DD.zip` (work) or `omnius-YYYY-MM-DD.zip` (full).

---

## 1. Unzip

Unzipping recreates the `omnius\` folder. Put it somewhere you own and that is
**not** synced by OneDrive/Dropbox — the bus writes small files constantly and a
sync client will fight it.

> On employer hardware, check your acceptable-use policy first. This installs a
> coding agent with file access and connects it to a chat server.

## 2. `install.bat`

Idempotent setup **and** health check — safe to re-run any time. It:

- checks git, Claude Code, Python 3.10+, Node, ffmpeg, Windows Terminal, and
  offers `winget` installs for anything missing;
- creates `.env` from `.env.example` (empty Discord values — expected);
- vendors the `watch` skill, pip-installs faster-whisper, npm-installs remotion;
- offers to start Omnius automatically at logon;
- then hands over to the guided Discord setup (§3).

Re-run `install.bat` any time as a doctor. `-CheckOnly` reports without changing
anything.

## 3. Discord — the NEW server

The guided flow (`tools\discord\setup.ps1`) opens the developer portal and
`.env`. Full detail in `docs\DISCORD.md`. In short:

1. **New application → Bot → Reset Token → copy.** Disable *Public Bot*, enable
   **Message Content Intent**.
2. **Create/choose the server.** On a company server you will usually *not* be
   admin — ask whoever is for an invite with either Administrator (`8`) or the
   minimal set (`126032`).
3. **Fill three values in `.env`:** `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`,
   `DISCORD_OWNER_ID` (your own user id — the watchdog obeys **only** this user).
   Optionally set `MACHINE_NAME` so claims and channel topics say `work-laptop`
   rather than the raw computer name.
4. Verify before trusting it:

   ```
   python tools\discord\api.py config-check --verify
   ```

   Exit 0 = token *and* guild actually check out against Discord. This is the
   step that matters: presence of a value is not validity, and a wrong-but-present
   guild id used to pass every launcher check and then kill the watchdog silently.

5. Stamp the channel structure:

   ```
   python tools\discord\api.py ensure
   ```

   Creates `🎛 ORCHESTRATOR` with `#omnius #daybook #fleet-status #transcribe #alerts`.
   The first one is named after the agent, so it is `#jarvis` if that is what you
   answered at install (`config\omnius.ini`, `[omnius] name`).

6. Optional branding: `python tools\discord\api.py set-avatar`

## 4. `start-omnius.bat`

Brings up the services in one window: status banner, daybook server, watchdog.
No Claude session is started — it stays quiet until you talk to it.

The banner should read `[OK] Watchdog listening on Discord`. If it says
**stale lock**, a previous watchdog was killed; that is self-healing (the pid
liveness check takes over) and not an error to chase.

**The watchdog must be hosted by `start-omnius.bat`, not by an agent session** —
a session-hosted watchdog is killed at turn boundaries and survives minutes.
Task Scheduler is the Phase 4 answer for true always-on.

## 5. Confirm the round trip

Post `Hello` in your agent's channel — `#omnius`, or whatever you called it at install
— from the owner account. Expect a session to spawn and answer. Then `!status` for a
fleet view.

`wakeup-omnius.bat` summons the orchestrator at the desktop.

---

## Permission profile

`.claude\settings.json` (orchestrator) and `templates\project\.claude\settings.json`
(inherited by every project) travel with the zip, so every instance starts with
the same posture. Paths in them are **relative on purpose** — an absolute path
would break on each new machine.

The **hooks** are the exception, and they do not travel at all: a hook command
has to be an absolute path, so `install.bat` writes them into each desk's
`.claude\settings.local.json` (gitignored, never packed). Until it has run, a
fresh instance simply has no hooks — which is the safe state, because a hook
pointing at a path that is not there blocks every prompt at that desk. If a desk
ever stops accepting prompts after a move, that is the thing to repair:

```
python tools\discord\fix_hook_paths.py --check
```

If a session hits something not on the allow-list it does **not** stall: the
`PermissionRequest` hook relays the request to `#alerts` and waits for `ok`/`no`.
Silence never allows. Read `docs\PERMISSIONS.md` before widening anything.

On employer hardware, note that relayed permission prompts include the command
line (token-shapes redacted) and land in a channel other people may be able to
read. Choose the `#alerts` channel's visibility accordingly.

---

## When something is wrong

| Symptom | Look at |
|---|---|
| "Discord not set up" on a fresh box | Expected — `.env` does not travel. Do §3. |
| Config accepted but watchdog dies | `api.py config-check --verify`; then `state\logs\watchdog.log` |
| Watchdog runs but never answers | The liveness **beacon**: alive-but-deaf is a real state. `!status`, and check `state\watchdog\beacon.json` |
| Messages ignored | You are not `DISCORD_OWNER_ID`, or the bot lacks Message Content Intent |
| Banner shows stale lock | Normal after a hard kill; pid liveness self-heals |
| Suite | `python tools\discord\test_watchdog.py` — offline, should be all green |

Remember the recorded lesson: **the offline suite is not evidence.** It has passed
green while a tool died on the first real emoji. Do §5's live round trip.
