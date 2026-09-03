# Testing log

How to run the automated tests, and what has been validated. Keep this current as phases land.

## Run the tests

```
python tools\discord\test_watchdog.py      # 1,900+ checks — watchdog, bus, delegation, workflows, outage handling, hooks, skills, installer, packaging
python daybook\test_storage.py             # 141 checks — daybook storage + API
python tools\email\test_email.py           # 116 checks — IMAP/SMTP + Graph contract
python tools\documents\test_documents.py   # 39 checks — PDF text, OCR fallback, schema validation
python tools\telegram\test_telegram.py     # 104 checks — the invite list, both directions, the reply window
python tools\discord\desk_audit.py         # every desk: no stalling prompts, hooks wired, self-recovery
powershell -File tools\update_drills.ps1   # 36 drills — the updater, against instances that are already broken
powershell -File install.ps1 -CheckOnly    # environment doctor (report-only)
```

**Every suite `release.ps1` lists gates every release** — its `$suites` array is
the authority (today: `test_watchdog`, `test_storage`, `test_email`,
`test_documents`, `test_telegram`), and it must be green before it will cut a
zip, so a reader who runs only the first two gets a surprise at release time.
The last three commands above are not in that array — run them anyway, they
catch what the suites cannot. Counts drift upward; the release is the enforcement,
this table is the map.

`test_watchdog.py` needs no Discord and no network: it monkeypatches the API's network calls and isolates all state into a temp sandbox.

**It also runs on a fresh clone, before `install.bat`** — that is a requirement, not a nicety: the suite is what the README offers a stranger as evidence, and until 2026-08-14 it died with a `FileNotFoundError` halfway through on any tree that had not been installed (it read `config\fleet.json`, an install artifact, and several checks read this instance's `memory\`, which is gitignored by design). Anything that must exist only on the author's machine belongs in the check as a fallback to what actually ships — the `.example` config, the `templates\fresh\memory\` seed — or it is testing a machine rather than the product.

## Phase 2 shakedown — 2026-07-23 (offline, no Discord bot)

A full offline exercise: a demo project (`projects\demo-app`) with two real sessions (`app`, `backend`) driven via `claude -p`, plus unit tests of the transport brain. Everything that does **not** require a live Discord bot was covered.

| Area | Result |
|---|---|
| `/new-project` by hand (stamp template, fill placeholders, `git init`, own repo) | ✅ nested repo isolated from root; root ignores `projects\*` |
| Session identity from cwd (`demo-app.backend`, `demo-app.app`) | ✅ correct in booted sessions |
| Backend session builds a working stdlib API | ✅ GET/POST/toggle verified by independent curl |
| App session reads backend's contract **from memory** and builds to it | ✅ cross-session coordination through project memory, no direct talk |
| File bus: `inbox_watch.py` pickup + claim heartbeat | ✅ real-time claim, envelope delivery |
| `/omnius` round-trip: envelope → real work → outbox reply | ✅ inbox drained, work done, valid outbox reply queued |
| Watchdog brain (45 assertions): `session_alive`, chunking, redaction, `!kill/!restart/!status/!killall` + guards, `flush_outboxes` + media archive + corrupt-file handling, `build_map` (folder gating, archived exclusion), **`handle_message` inbound dispatch (owner allowlist, deliver-vs-spawn, attachments)**, helpers | ✅ all pass |
| Daybook: API round-trip (note/task/search/toggle/delete) + file-mode append + server parse | ✅ both write paths work; emptied month pruned |
| Whisper voice pipeline (SAPI-generated speech → `transcribe.py`) | ✅ content recovered end-to-end |
| Packaging: `pack.ps1` with a project present | ✅ project + git history + media travel; `.env`, `state\`, `node_modules\` excluded |

### Bug found & fixed
- **`watchdog.log()` crashed on emoji category names** (Windows cp1252 console can't encode 📁/🎛/🗄, which are baked into our schema). A logging call must never crash the always-on service → `log()` now falls back to an encoding-safe write. Regression covered by `build_map` test.

### Improvements made
- **Sessions no longer hand-write claim files** (that produced placeholder timestamps + dead pids). `inbox_watch.py` owns the claim (real time + pid heartbeat); new `--once` mode writes it and exits for boot check-in. CLAUDE.md §6 and `/omnius` updated.
- **`/omnius` path-robustness:** resolve the absolute workspace root once; always drain via `inbox_watch.py` (which finds its own paths) instead of a manual `ls` that a fresh session miscomputed.
- **Whisper first-run:** `prewarm.py` downloads the model at install time (the model cache doesn't travel in the zip); `WHISPER_MODEL` env selects size.
- **Committed the vendored `watch` skill and remotion's `package.json`** so both travel in the zip / reinstall reproducibly.
- **`test_watchdog.py`** added as a committed, self-contained regression suite.

## Live Discord test — 2026-07-24 (real bot, real server)

Bot `OMNIUS` created by the user; `ensure` stamped the day-one structure. Proven against live Discord:

| Area | Result |
|---|---|
| Inbound: #orchestrator message → inbox envelope → session | ✅ |
| Outbound: outbox → watchdog → Discord post | ✅ |
| Real task via Discord (daybook write, file mode) | ✅ |
| Start a service via Discord (daybook server) | ✅ |
| Image inbound → `media/inbox` → native vision → description reply | ✅ |
| Outbound file attachment → `#fleet-status` → `media/sent` archive | ✅ |
| Multi-channel routing (non-primary channel) | ✅ |
| Control command `!status` (transport-level, no session) | ✅ |
| Owner allowlist enforced | ✅ |

Setup gotcha worth remembering: the `.env` key is `DISCORD_OWNER_ID` (a hand-written `DISCORD_USER_ID` left the watchdog refusing to start with "not configured").

**Deferred to the permissions milestone:** spawn-on-message + `!kill`/`!restart` against a live spawned session — their value is *unattended* operation, which needs the permission profile first (`docs\PERMISSIONS.md`); also needs a project's Discord category stamped (demo-app has none yet, by design).

### Bug found in the live spawn test (2026-07-24)
- **`/remote-control` name collision:** Claude Code ships a *built-in* `/remote-control` command (claude.ai session handoff) that shadows a skill of the same name. A watchdog-spawned session booted into the built-in's dialog instead of connecting to the bus. **Fix: skill renamed to `/omnius`** (folder, frontmatter, watchdog spawn command, all docs). Lesson: skill names must avoid the CLI's built-in command namespace. The pending envelope survived in the session's inbox — no message lost.

### Status banner (2026-07-24)
`tools\status_banner.py` — live dashboard (`--watch`), now the primary pane of `start-omnius.bat`. Probes read-only: watchdog lock + pid, daybook HTTP, Discord config booleans, session claims. **Bug caught during build:** `os.kill(pid, 0)` is NOT a safe liveness probe on Windows (it can terminate the target); banner now uses the same ctypes `OpenProcess` probe as the watchdog. Regression-covered (9 banner checks in the suite).

## The spawn saga — 2026-07-24 (five walls, all found live)

Getting a watchdog-spawned session to connect unattended surfaced five real, distinct blockers — none visible in offline testing. Each fix is committed and regression-covered:

| # | Wall | Fix |
|---|---|---|
| 1 | `/remote-control` collides with a Claude Code **built-in command** | skill renamed **`/omnius`** |
| 2 | Workspace-root skills **invisible** to project sessions (own repo = own root) | stub skill in the project template delegates to the root SKILL.md |
| 3 | Session **sandbox** is the component folder — can't reach bus/skill/memory (permission *rules* cannot widen it) | watchdog spawns with `--add-dir <root>`; template gets `additionalDirectories` |
| 4 | **Settings don't inherit** from ancestor folders — the project profile never loaded | watchdog passes `--settings <project>\.claude\settings.json`; per-component copies |
| 5 | **Folder trust dialog** ("Security guide") silently blocks unattended tabs; `-p` mode *ignores settings permissions* in untrusted workspaces | watchdog pre-stamps `hasTrustDialogAccepted` in `~/.claude.json` before spawning |

**Round-trip PROVEN (headless, 2026-07-24 11:18 UTC):** session launched with the full flag set connected as `demo-app.app`, claimed its desk, drained 3 queued envelopes, answered via outbox; watchdog posted `🟢 online` + both answers to `#app`. Zero prompts, zero human input. Diagnosis method worth keeping: run the spawn command with `-p` — the session *itself* reports exactly which step is permission-blocked.

Remaining checkbox: one interactive-tab spawn after fix #5 (trust pre-stamp) — expected to pass, not yet observed.

## Going public — 2026-08-14

The first run of the suite on a **clone** rather than on the machine that wrote
it. Six problems, all of the same family: things that were true here and
nowhere else.

| Found | Fix |
|---|---|
| Six tracked `settings.json` carried this machine's home directory in their hook commands. A clone's every prompt died with `UserPromptSubmit operation blocked by hook` | hooks moved to the gitignored `.claude\settings.local.json`, written per install by `fix_hook_paths.py` (ARCHITECTURE §3.4) |
| The suite crashed at check 481 on `config\fleet.json` — an install artifact | falls back to `fleet.example.json`, the file that actually ships |
| Four checks read this instance's `memory\`, which never travels | read the `templates\fresh\memory\` seed when there is no instance memory |
| `-Fresh` config checks asserted against whichever files happened to exist here | assert against the rule, with the instance files named explicitly |
| Nine files had a Windows path whose backslash escape had been interpreted — `config\audit-sentinels.txt` was `config` + BEL, and the demo-stamping command in `/new-project` pointed at `tools\orchestratorleet_ops.py` | repaired; the docstring case needs `\\`, since a single backslash is re-eaten at import |
| `templates\demo-project\memory\project-brief.md` was swallowed by the `memory\` ignore rule, so the shipped demo had no brief — and *"build the API from the brief"* is its first instruction | `!templates/demo-project/memory/` un-ignored, brief committed |

Lesson worth keeping: **a green suite proves the product works on the machine
that ran it.** These six were all green here for weeks.

## Known gaps (current, 2026-08-17)

The old list here — orchestrator verbs, heartbeat, autostart, security audit —
all shipped on 2026-08-01 and are covered by the suite; it is deleted rather
than left to rot. What is genuinely open:

- **The demolition derby** (`docs\OBSERVABILITY.md` O3): ten predicted-then-
  verified failure drills, written down and not yet run. The most valuable
  untested surface in the system.
- **The update handshake's revert half.** The healthy path is live-proven
  (a second instance updated itself unattended, 2026-08-17); the auto-revert
  is suite-covered but has never fired on real hardware — that is derby
  drill #11 on purpose.
- **`weblogin` against a real site** (2026-08-17): the 2FA relay is proven end
  to end against the live watchdog, but the browser half has only been driven
  against the tool's own guards, not a genuine third-party login form.
