# Architecture — Claude Multi-Agent Orchestrator

> Single source of truth for the system design. Status: **Phase 2 — Discord layer built** (watchdog + helper CLI + file bus + `/omnius` + whisper; live test pending). Orchestrator verbs = Phase 3, designed only. Human intro: [../README.md](../README.md) · Agent rules: [../CLAUDE.md](../CLAUDE.md)

## 1. Vision

A personal multi-agent coding environment with **full overview and full control** — at the PC or from anywhere via Discord — that can be resurrected on **any machine with any Claude account** by moving one folder (zip) or cloning one repo.

**The experience it must deliver:**

> From the phone: *"new project: recipe-app, with app + backend"* in `#orchestrator`.
> The watchdog wakes Omnius, which stamps the project from the template, creates the Discord category `RECIPE-APP` with `#app` `#backend`, opens two terminal tabs on the PC running Claude sessions plugged into their channels, and replies *"✅ recipe-app live, 2 sessions listening."*
> Typing in `#recipe-app → #app` talks to the app session. Sitting down at the PC later, the same sessions are there in their terminals — same conversation, two doors.

**Goals, in priority order:**
1. **Overview** — one place (orchestrator / `#fleet-status`) always knows what exists and what's running.
2. **Individual control** — every session directly addressable (its terminal or its channel), no middleman required.
3. **Delegation** — or just tell the main agent and let it coordinate.
4. **Local = remote** — full parity is the bar: continue any project from anywhere with the **same effectiveness**, whether at the desktop terminal or away via Discord. Two doors to the same session, never two experiences.
5. **Portability** — any PC, any account: unzip (or clone) → `claude` → alive.
6. **Quiet by default** — nothing runs and nothing spends unless there is work.

## 2. Principles

1. **Everything is a Claude session + skills + conventions.** No privileged daemon runtime — the one always-on piece is a dumb script (§3.4). The orchestrator is a normal session at root with admin skills. This is what makes the system portable and hackable.
2. **The filesystem is the org chart.** Root = orchestrator, `projects\<x>\<component>` = one agent. Discord mirrors it 1:1.
3. **cwd = identity = permissions.** A session derives who it is, what it may write, and which Discord channels it may use from where it sits. No configuration to drift.
4. **The repo is the system; machines are disposable.** Everything durable is committed (docs, memory, skills, templates, settings). Everything machine-local regenerates (`state\`, terminals, claims).
5. **Transparent reads, scoped writes.** Every session may read shared memory and all project memories (awareness, reuse, consistency). Writing is confined to your own scope. Only `memory\orchestrator\` is orchestrator-private.
6. **Two doors, one session.** Terminal and Discord channel reach the *same* session — never two parallel brains for one job.
7. **Templates over improvisation.** Identical project skeletons are what make fleet-wide control and automation possible.
8. **Dumb transport, smart endpoints — on demand.** The only always-on component is a tiny watchdog script; intelligence (Claude sessions) starts when a message needs it and costs nothing while idle.

## 3. Components

### 3.1 Orchestrator (main agent) — persona: **Omnius**
A regular Claude Code session in the workspace root — started manually at the PC or **on demand by the watchdog** when a message arrives; it does not need to run 24/7. Loads `CLAUDE.md` → role ORCHESTRATOR. Powers: create/archive projects, manage Discord structure, spawn/kill sessions, read everything, curate shared memory, report status. Discipline: **delegates implementation** to project sessions (by posting into their channels — visible delegation) to keep its own context clean for control. Delegation has two forms: persistent component sessions for long-lived work, and **one-shot jobs** — `claude -p` in the target folder for small bounded tasks (a report, a quick fix), output landing in project memory or the outbox, nothing long-lived spawned. Print mode skips all interaction, so a job must fit entirely inside the permission profile. **Write-through rule:** every fleet mutation updates `memory\orchestrator\status.md` in the same action — Omnius must survive its own restart. The persona is the Dune evermind — fitting, since copies of this repo wake up on any machine.

### 3.2 Project sessions
One Claude Code session per component folder (`projects\x\app`, `…\backend`, …). Each is a full-power coding agent, scoped by convention (§2.5), with its own terminal (attachable) and its own Discord channel. Started manually in a terminal or spawned by the watchdog when its channel receives a message. Multiple sessions per project are normal and expected.

### 3.3 Memory system

| Layer | Path | Read | Write | Committed |
|---|---|---|---|---|
| Shared | `memory\shared\` | every session | any session adds, orchestrator curates | yes |
| Orchestrator | `memory\orchestrator\` | orchestrator | orchestrator | yes |
| Project | `projects\<x>\memory\` | all sessions of all projects | that project's sessions + orchestrator | yes (in project repo) |
| Machine scratch | Claude's built-in auto-memory | that session | that session | no — per machine/account |

Inside a project: `MEMORY.md` (index), `sessions\<component>.md` (each agent's live notes — status, decisions, interfaces; the sibling-coordination mechanism), plus topic files (`api-contract.md`, `architecture.md`, …).

**Rationale:** the user must have full overview, and agents must never work blind to each other — so reads are maximally open. Writes stay scoped so ownership and accountability stay crisp. Durable knowledge in the repo = portability; built-in memory is treated as disposable cache.

### 3.4 Discord layer — watchdog, file bus, helper library

> **Run model (2026-08-01 rebuild — supersedes every "watcher"/"re-arm" mention below):** a desk is a **series of one-shot headless runs**, not a terminal that stays armed. Mail lands in `state\inbox\<id>\`; the watchdog starts `claude -p "/omnius"` in the desk's folder (`--continue` when the folder has its own history) and **owns the child process** — one run per desk at a time, new mail queues while one is active, `state\watchdog\runs\<id>.json` is the pid-validated lease that survives a watchdog restart. The run drains the box, replies via outbox, exits; continuity lives in the conversation transcript. Failed runs back off (`RUN_BACKOFF_SECONDS`) and repeated failure alerts the owner instead of looping. **Deleted on purpose:** session-side inbox watchers (turn-based sessions cannot host daemons — they died at every turn boundary and invited duplicate orchestrators), claim heartbeats (a sidecar stamping `lastSeenAt` made dead desks read alive), and the whole heal-deaf-desks apparatus (nothing has to stay armed, so nothing can go deaf). A person's own terminal coexists via two hook stamps: UserPromptSubmit writes `state\turns\<id>.busy`, Stop clears it — the watchdog never runs headless on a desk whose terminal is mid-turn, and follows up in the same conversation the moment the turn ends. `/spawn-session` (fleet_ops `open_desk`) remains the way to open a *visible* terminal for a human.

**The transport is dumb and always-on; intelligence starts on demand.** One small script — the **watchdog** — is the only component that *listens* to Discord. Claude sessions never poll Discord; they speak through local files.

```
Discord ⇄ WATCHDOG (tools\discord\watchdog.py — the only always-on piece)
              ⇅
   state\inbox\<session>\ · state\outbox\<session>\ · state\media\
              ⇅
   Claude sessions (spawned on demand, visible terminal tabs)
```

**Watchdog** (`tools\discord\watchdog.py`, stdlib-only Python):
1. **Gateway websocket (built 2026-08-01)** — one socket, messages pushed; the watchdog blocks on the event queue instead of sleeping. Measured: the push lands before our own `POST` ack returns. REST polling (`GET …/messages?after=<lastId>`) stays as a **60 s reconciliation sweep**, and reverts to the old ~3 s cadence whenever the socket is down — the hand-rolled client's worst failure mode must be latency, never a lost message. Bot presence = free health indicator; `DISCORD_GATEWAY=0` forces REST.
2. On a new message: **enforce the sender allowlist** (`DISCORD_OWNER_ID` — the single security chokepoint, §7) → download attachments to `media\inbox\` → write an envelope into `state\inbox\<target>\`, stamped with **who wrote it**.
   - **Guests (2026-08-12).** The allowlist is no longer only him: `config\guests.ini` may name other people, each confined to an explicit list of channels. A guest's envelope carries `from: "<label>"` instead of `from: "owner"`, so a desk can tell them apart — before this, `write_envelope` stamped `"owner"` on everything and threw the Discord author away one step before any session could see it. Guests are **mail only**: control verbs, permission answers and takeover answers stay his alone, and a guest writing anywhere outside their channel list is dropped *silently* (an explanatory refusal would map the fleet for them). The list **fails closed** — no channels, a malformed id, or a label colliding with a system sender means the entry is ignored and reported.
   - ⚠️ **A guest role is only half a boundary.** Discord permissions are additive, so confining someone needs the server-wide baseline closed too (`@everyone` without VIEW_CHANNEL) — otherwise the per-channel grant adds nothing to what they could already see. `api.set_baseline_view(False)` is the switch; it is the one wide-blast call in `api.py` and belongs to him, not to a session.
3. **Deliver and run (run model, §3.4):** the envelope lands in the inbox and `ensure_runner` — the single choke point — starts one headless run on that desk (`--continue` when the folder has history), unless a run already owns it, a live terminal is mid-turn there, or the desk is backing off after failures. Bursts queue in the inbox; one run drains the whole box.
4. **Control commands** it executes itself — no session involved, so they work even when a session hangs, confused or mid-loop: `!kill` (end the channel's session), `!restart` (kill + fresh spawn), `!status` (liveness from claims), and from `#orchestrator` only: `!killall` — the fleet-wide red button. Kills are always cheap: every project is a git repo, so the worst case lost is a conversation turn, never work.
5. Posts everything sessions drop into their outbox back to the right channel (chunked to Discord's 2000-char limit, code blocks preserved, **redaction filter** for token-shaped strings).
6. May acknowledge receipt with a 👀 reaction (cheap "seen" feedback on the phone).
7. Logs to `state\logs\watchdog.log`. All loop state (last message IDs) lives in `state\` — a restart resumes seamlessly; messages missed while the PC was off are caught up via `after=<lastId>`.

**Helper library / CLI** (`tools\discord\api.py`): the full Discord surface as callable scripts — send / edit / delete messages, embeds, reactions, pin/unpin, upload files, read history, create / rename / delete **channels and categories**, set channel topics. Reads the token from `.env` itself — Claude invokes helpers and never sees the token. Used by the watchdog (relay) and directly by sessions for ad-hoc actions (status embeds, pinning, structure management). Scoping: project sessions may only target their own category (derived from cwd, checked in the helper wrappers); the orchestrator holds the admin surface. Threads/forums: add later if ever needed.

**File bus** — JSON envelopes, one file per message:

```json
{ "id": "1698...", "from": "owner", "channel": "app",
  "ts": "2026-07-23T14:02:11Z", "text": "fix the login bug",
  "files": [ { "path": "media\\inbox\\2026-07\\voice-1698.ogg", "type": "audio/ogg", "name": "voice-message.ogg" } ] }
```

`from` is the **origin tag**: `owner`, `omnius`, `heartbeat`, `schedule`, a **guest label** from `config\guests.ini` (2026-08-12), or a **session id — fleet mail, not a person** (desk-to-desk delegation, 2026-08-15: `docs\DELEGATION.md`). The guards that open a window or raise the deaf-desk alarm test for a **person waiting**, via `is_human_sender` — whose negative space is the system tags, `*-job` tool handoffs, and any id the desk registry recognises, so a guest added later is still covered without editing the predicate. A session ignores envelopes with its own origin (self-continuation rides the schedule); desk mail is routed by the watchdog, which mirrors every hop into the recipient's channel, so delegation is always watchable in Discord. Sessions are Discord-agnostic: any session can be driven in tests by dropping a file into its inbox. *(Superseded by the run model above: the "inbox watcher background task" this paragraph once ended with is deleted — mid-run steering arrives because `/omnius` re-drains between steps, not because anything stays armed.)*

**Media pipeline** — full support in both directions:

| Inbound | Handling (by the receiving session, not the watchdog) |
|---|---|
| Image | saved to `media\inbox\` → session Reads it natively (screenshots from the phone just work) |
| Audio / voice note (.ogg) | transcribe via `tools\whisper\` → treat the text as the user's message → **voice-driving Omnius** |
| Video | analyze via the `/watch` skill (frames + transcript); fallback: ffmpeg frame extraction in `tools\` |
| Any other file | saved → session reads/uses it (PDFs, code, zips, …) |

Outbound: sessions attach local file paths in outbox envelopes; the watchdog uploads them (mind Discord's ~10 MB default upload cap — bigger artifacts get summarized or linked). So "show me the app" from the phone can be answered with a real screenshot. Media handlers ship **inside the repo** (`tools\whisper\`, ffmpeg wrappers) so the zip stays self-sufficient; machine prerequisites (Python, ffmpeg) are listed in the README.

**Nothing is ever lost:** the durable asset archive is root **`media\`** — every received file lands in `media\inbox\YYYY-MM\`, everything posted out is copied to `media\sent\YYYY-MM\` (even when the source was a temp file). `media\` is git-ignored (binary bloat) but **travels in the zip** like daybook notes. Filing rule: assets that belong to a project get moved/copied into that project, personal ones into daybook — the archive always keeps the original.

**Staying reachable — how the deaf-desk problem DISSOLVED (2026-08-01, evening).** A Claude session is **turn-based, not a daemon**: it runs a turn and stops. The morning's answer was session-side watchers plus a `heal_deaf_desks()` apparatus (a Stop-hook marker and a claim heartbeat) that noticed dead watchers and respawned sessions — all deleted the same evening, because turn-based sessions cannot host daemons: the watchers died at every turn boundary and the healer invited duplicate orchestrators. The run model replaced the whole class: **nothing session-side stays armed, so nothing can go deaf.** Reachability is purely the watchdog's: mail in the inbox and no active run means it starts one (§3.4). What remains of "deaf" detection is on the watchdog's side and is about *people*: the deadman pages the owner when a **person's** mail sits unhandled with nothing alive to explain it, the failure ledger backs off and alerts on desks whose runs keep dying, and the bridge-delivery deadline replaces a warm window that stops delivering.

**Finished work announces itself (built 2026-08-01, still current).** The check-in stamps `state\watchdog\turn_started\<id>.json` when it drains mail, because it is the step that knows a turn is beginning. The `Stop` hook reads it at the end: if nothing reached the outbox since that moment (a surviving reply file, or the `.last-posted` proof the watchdog stamps at every post — including desk-mail deliveries), it posts a one-liner naming the files that changed (`git status --porcelain`, mtime-filtered to this turn so another session's work is never claimed). It speaks only when there is something to answer for. This exists because a desk once built a PDF correctly and stopped, and the result died on disk while the owner asked all afternoon whether anything was happening.

**What the hooks deliberately do not do: keep anything armed.** Hooks stamp facts (`turn_started`, `.busy`, its clearing) and never launch processes — re-arming machinery from a hook was considered on 2026-08-01 and rejected, and the run model then made the question moot: there is nothing left to re-arm.

**Where hooks live, and why not in git (2026-08-14).** A hook command is an absolute path — that was settled on 2026-08-02, when depth-relative `${CLAUDE_PROJECT_DIR}/../..` spellings proved unfixable (one settings file is loaded by sessions at different depths). Absolute paths are *machine* facts, so they are written per install into **`.claude\settings.local.json`** — gitignored, excluded from release zips — while the tracked `settings.json` carries permissions only. The writer is `tools\discord\fix_hook_paths.py`, run by `install.ps1`, `autostart.ps1 -Action install|repair`, and `fleet_ops.py` after every stamp; it wires each desk's own folder, including project *components* (their cwd is where the hooks must be, since the watchdog passes the project's settings with `--settings`). This split is the one `sync_permissions.py` already uses for learned allow-entries. It exists because the alternative shipped: six tracked settings files carried one machine's home directory into the public repo, and a hook whose script is absent **blocks every prompt** at that desk — so a clone was unusable until the paths were repaired by hand.

**Honest trade-offs:** the Gateway swap (2026-08-01) removed the poll latency; the REST sweep it kept as a backstop is the reason a bug in the hand-rolled websocket costs slowness rather than silence. The watchdog is a single point of failure for *remote* only — the PC keeps working locally without it, and Task Scheduler restarts it on failure (Phase 4); Discord retains messages meanwhile.

### 3.5 Session claims (registry)

`state\sessions\<id>.json` — **one claim file per session**, single writer, no write races. `<id>` = `orchestrator`, `<project>.<component>`, `tool.<name>`, `daybook`. Written on boot (overwrite = claim the desk), refreshed when convenient:

```json
{ "role": "project", "project": "recipe-app", "component": "app",
  "cwd": "D:\\...\\projects\\recipe-app\\app", "machine": "main-pc", "pid": 12345,
  "startedAt": "2026-07-23T10:00:00Z", "lastSeenAt": "2026-07-23T12:00:00Z",
  "discordChannel": "app" }
```

- **Liveness** = PID alive on this machine. The watchdog uses claims to decide deliver-vs-start; `/status` aggregates them.
- **Two-brains guard:** a session booting onto a cwd with a live foreign claim stops and tells the user (§2.6).
- **Self-healing after a move:** claims from another machine or with dead PIDs are stale — any session may prune them. Unzipping on a new PC needs no cleanup ritual.
- Best effort until SessionStart/SessionEnd hooks automate it (Phase 4). Cross-machine view = Discord channel topics.

### 3.6 Skills (the system's verbs)

| Skill | Runs in | Purpose |
|---|---|---|
| `/omnius` | any session | handle this desk's mail: check in (claim written, envelopes printed), work every envelope, reply via outbox, end the turn — nothing stays armed |
| `/new-project` | orchestrator | stamp template → folder, git init, Discord category+channels, spawn sessions, register |
| `/spawn-session` | orchestrator | open a terminal tab in a folder with Claude auto-running `/omnius` |
| `/status` | orchestrator | fleet overview from claims + memories + Discord; update `#fleet-status`; prune stale claims |
| `/archive-project` | orchestrator | wind down sessions, archive channels, final memory write, move/flag folder |

All live in `.claude\skills\<name>\SKILL.md` → committed → identical on every machine. The **watchdog is not a skill** — it is a standalone script (§3.4) started by root `start-omnius.bat` (or Task Scheduler, Phase 4). Root launchers — logic lives in PowerShell, double-clickable `.bat` shims run it past execution policy: **`install.bat` → `install.ps1`** = idempotent setup & doctor (checks git / Claude Code / Python 3.10+ / Node / ffmpeg / Windows Terminal, **offers winget installs for anything missing**, `.env` from template, tools set: watch/whisper/remotion; `-CheckOnly` = report only); **`pack.bat` → `pack.ps1`** = builds the portable zip (§5.6); **`start-omnius.bat`** = system ON — the **services terminal**: one window with daybook (`localhost:5111`) and the watchdog (once built) as side-by-side panes; starts **no** Claude session — the brain wakes on demand (§2.8); **`wakeup-omnius.bat`** = summon the brain now — opens the Omnius terminal (`claude --continue`, fallback fresh). Services stay **non-interactive** — they must run unattended from Task Scheduler (Phase 4), so the watchdog never prompts in a console; interactivity lives in launchers. All three entry points run a **Discord setup check** (`tools\discord\setup.ps1`): if `.env` lacks the Discord values they offer the guided setup — checklist, developer portal, `.env` in the editor, then live token/server validation via the API; declining = local mode, and `start-omnius.bat` then skips the watchdog pane. **All orchestrator verbs are idempotent**: find-or-create semantics, safe to re-run after a half-completed attempt (Discord tolerates duplicate names — always match by name first). **Self-improvement with receipts:** when a workflow repeats, Omnius may author or extend a skill here and commit it — every self-improvement is a reviewable git commit that travels to every future instance; uncommitted improvements don't exist.

### 3.7 Templates
`templates\project\` — the stamped skeleton: project `CLAUDE.md` (placeholders: `{{PROJECT_NAME}}`, components), `memory\MEMORY.md` + `memory\sessions\`, standard component folders as chosen at creation (`app`, `backend`, `web`, …), project `.gitignore`. Consistency here is what makes every later automation trivial.

### 3.8 Shared tools
`tools\` — capabilities any session may **use** but only tool sessions/orchestrator maintain. Each tool ships a README with its contract.

| Tool | Purpose | Status |
|---|---|---|
| `discord\` | watchdog + REST helper CLI (§3.4) | designed, Phase 2 |
| `whisper\` | audio → text (Discord voice notes, any audio) | default engine: local faster-whisper; API key optional |
| `remotion\` | video rendering ([remotion-dev/remotion](https://github.com/remotion-dev/remotion)) | placeholder; npm-installed on demand |

The tools set is installed by root **`install.bat`** on first run: the `watch` skill vendored from claude-video into `.claude\skills\watch` (committed → travels in the zip), `pip install faster-whisper`, `npm install` remotion into `tools\remotion\` (skipped without Node).

### 3.9 Daybook (personal notes & tasks)
`daybook\` — the user's personal notes+tasks app (single Python file, stdlib-only; the markdown files in `daybook\notes\` ARE the data, one file per month). Runs as a service pane in the `start-omnius.bat` terminal (server on `localhost:5111`). For Omnius it is the user's personal inbox/log: capture notes & tasks arriving via Discord (`#daybook` — including **voice notes**, transcribed then filed), answer "what's on my plate today", surface open tasks in the morning. Access strictly per `daybook\README.md`: prefer the API (`localhost:5111`) while the app runs; append-only direct file edits otherwise. **Personal data — orchestrator context only, never exposed into project channels or project memories.**

### 3.10 Heartbeat — proactive Omnius (built 2026-08-01)
While services run, the watchdog **checks** every `HEARTBEAT_MINUTES` (`.env`, default 30, `0` = off) and drops a **heartbeat envelope** into Omnius' inbox **only when it already sees something mechanical to do** — stale claims, the daily briefing after 07:00, Monday's gardening. The quiet rule is therefore enforced in the transport as well as in the prompt: read literally it is a rule about *messages*, and would still spawn an Opus session every 30 minutes around the clock (~48/day) merely to conclude there was nothing to say, against goal 6. The checks cost a few file stats and a clock read; judgement still belongs to Omnius. Omnius works through `memory\orchestrator\HEARTBEAT.md`: fleet health (prune dead claims, refresh `#fleet-status` if drifted), a daily **morning briefing** into `#daybook` built from daybook's open tasks, weekly **memory gardening** (curate, dedupe, compact, commit — memory that stays sharp instead of silting up), plus user-added scheduled items. **Quiet rule:** nothing needing attention → no message anywhere. Costs tokens only when it fires; fires only while the watchdog runs.

## 4. Discord mapping

```
Discord Server (private, single-user)
├── 🎛 ORCHESTRATOR            (category)
│   ├── #orchestrator          ← chat with the main agent
│   ├── #fleet-status          ← pinned, always-current fleet overview
│   ├── #daybook               ← quick capture: notes & tasks → daybook (via Omnius)
│   └── #alerts                ← errors, approval requests (Phase 4: buttons)
├── 📁 RECIPE-APP              (category = project)
│   ├── #general               ← project-wide topics (relayed by the orchestrator)
│   ├── #app                   ← session projects\recipe-app\app
│   └── #backend               ← session projects\recipe-app\backend
└── 📁 <NEXT-PROJECT> …
```

Rules: category name = project name; channel name = component; channel topic = `path | machine | started` (self-healing mapping — if `state\` is lost, everything re-derives from Discord itself). **One host machine per project at a time** — the topic's machine field is the lock; moving a project = archive on A → pull/unzip on B → spawn on B.

Full blueprint (day-one schema, prefixes, bot permissions, per-instance setup): [DISCORD.md](DISCORD.md) · machine-readable structure: `tools\discord\schema.json` — automation stamps it idempotently.

## 5. Flows

### 5.0 A message from the phone
1. User posts in `#app` — text, or a voice note, or a screenshot.
2. Watchdog: allowlist check → attachments to `media\inbox\` → envelope to `state\inbox\recipe-app.app\` → 👀.
3. Claim says nobody home → watchdog spawns the session (§5.3), resuming its previous conversation.
4. Session wakes: reads the envelope; audio → whisper first; image → Read; then does the work.
5. Reply (text ± files) → outbox → watchdog → channel. Follow-up messages while it works queue in the inbox.
6. Back at the PC later: the terminal tab is open, same conversation — take over seamlessly.

### 5.1 New project
1. User (terminal or `#orchestrator`): *"new project recipe-app with app + backend"*
2. Orchestrator stamps `templates\project\` → `projects\recipe-app\` (chosen components)
3. Fills placeholders in project `CLAUDE.md`, seeds `memory\MEMORY.md`
4. `git init` + first commit inside the project (private remote offered, optional)
5. Creates Discord category `RECIPE-APP` + `#general` `#app` `#backend` (find-or-create)
6. Spawns one terminal session per component (§5.3)
7. Updates claims + `memory\orchestrator\status.md` + `#fleet-status` (write-through)
8. Confirms: *"✅ recipe-app live, 2 sessions listening"*

### 5.2 `/omnius` handshake
Identity from cwd → check in (`inbox_watch.py <id> --once`: claim written with real pid, waiting envelopes printed, exit) → handle the mail → reply via outbox → end the turn. No watcher, no hello-post: reachability is the watchdog's job, and startup is not news.

### 5.3 Spawn session (concept, Windows)
`wt -w 0 new-tab --title "recipe-app/app" -d "<abs path>" claude --continue "/omnius <initial message>"` — a real, visible, attachable terminal tab (`--continue` resumes the folder's previous conversation when one exists; omit for a fresh session). Used identically by `/spawn-session` and by the watchdog's deliver-or-start.

### 5.4 Fleet status
Orchestrator merges `state\sessions\*.json` (this machine) + Discord channel topics (other machines) + memory indexes → posts/updates the pinned embed in `#fleet-status`, pruning stale claims as it goes.

### 5.5 Archive project
Confirm with user → sessions write final memory → kill sessions (claims removed) → archive Discord category (rename to the `🗄 ` prefix, lock) → mark archived (`status.md`) → optionally move folder to `projects\_archive\`. Idempotent — safe to re-run if interrupted. Un-archiving = the reverse, driven by the same skill.

### 5.6 Moving to a new PC (the zip story)
1. Old PC: run `pack.bat` — builds `omnius-<date>.zip` next to the folder. It carries **everything** — system, docs, memory, full git history, `projects\`, `daybook\notes\` — and excludes secrets & machine junk: `.env` (only `.env.example` travels), `state\`, `node_modules\`, caches, logs. Stopping sessions first is optional — claims self-heal.
2. New PC: install prerequisites (git, Claude Code, Python; Node/ffmpeg for media tools), unzip anywhere, run `install.bat` once (idempotent — fills in whatever is missing), then `start-omnius.bat` (services) and `wakeup-omnius.bat` (the orchestrator terminal).
3. Omnius boots, prunes foreign claims, and is fully oriented from `memory\` + `docs\`. Git history travelled inside the zip; remotes stay configured. For remote: fill the fresh `.env` (Discord values are entered per instance, when that instance is set up) and start the watchdog.

## 6. Git & portability

**Two complementary layers:** the **zip is the transport** (moves the entire living workspace, including projects and notes); **git is the history** (versioning, diffs, optional remotes). A private root remote is recommended but not required for portability.

- **Root repo** commits: `README.md`, `CLAUDE.md`, `.claude\` (settings.json, skills, agents), `docs\`, `memory\`, `templates\`, `tools\` (code/configs/READMEs, not `node_modules`), `.env.example`, the daybook app.
- **Gitignored:** `.env`, `state\`, `projects\*` (keep `projects\.gitkeep`), `daybook\notes\` (personal — remove the line if you want notes in the private repo), `media\` (asset archive — zip-travels, not git), `memory\` (this instance's biography — the seed in `templates\fresh\memory\` is what ships), `config\*` except the `.example` files, `.claude\settings.local.json` (**every desk's hooks — machine-absolute paths, written at install**, §3.4), build artifacts.
- **Each project = own repo**, created at stamp time; remote optional per project. No nested-repo pain because the root repo never tracks `projects\`.
- **Clone path (alternative to zip):** clone → `.env` → `claude` → alive. Note: projects and daybook notes arrive only via *their* remotes/copies on this path — the zip is the only transport that carries literally everything.
- **Warning:** never host this workspace inside OneDrive/Dropbox/Drive sync — sync + `.git` + live agents corrupts things. Zip and git remotes are the sync mechanisms.

## 7. Security

- **Threat model: whoever can write in these channels can drive your PC.** Private server, no public invites, 2FA, bot token only in `.env`. Rotate the token if it ever leaks.
- **The watchdog is the single enforcement chokepoint:** only messages from `DISCORD_OWNER_ID` become envelopes — other users, bots, webhooks are ignored before any session sees them.
- **Permission profiles per role** (prerequisite for remote work — a watchdog-spawned session has nobody at the keyboard to click "allow"): project sessions run with pre-approved edit/bash **inside their own folder**; the orchestrator additionally holds fleet operations. Destructive or out-of-scope actions (delete project, force-push, spending money, touching other scopes) still require explicit user confirmation — via terminal, or Approve/Deny in `#alerts` (Phase 4). Profiles live in committed `.claude\settings.json` files (root + template). **Hard parity rule:** a remotely driven session must **never** hit a harness permission prompt — prompts are invisible to the bridge and stall the session silently. The profile covers everything routine; everything beyond it is asked **in conversation**, which relays to Discord like any other message.
- **Outbound redaction:** the watchdog filters token-shaped strings before anything reaches a channel.
- **Honesty note:** the `.env` Read-deny in settings is a guardrail, not a boundary — a shell command can still read the file. The real boundary is: private server, trusted machine, secrets only in `.env`. Never in git, memory files, Discord messages, or logs.

## 8. Roadmap

| Phase | Deliverable | Done when… |
|---|---|---|
| **0 — Foundation** | structure + these docs | you can explain the system from the repo alone ✅ |
| **1 — Skeleton** | folders, `.gitignore`, `.env.example`, `templates\project\`, memory seeds, root repo committed | fresh unzip/clone passes the Quick-start (minus Discord) ✅ |
| **2 — Discord layer v1** | watchdog (polling) + file bus + helper CLI + whisper; `/omnius` | phone → text/voice/image in `#orchestrator` → Omnius answers (spawned on demand); attachments flow both ways |
| **3 — Orchestrator verbs** | ~~`/new-project`, `/spawn-session`, `/status`, `/archive-project`~~ **(done 2026-08-01 — skills in `.claude\skills\`, mechanics in `tools\orchestrator\fleet_ops.py`, all idempotent)** | the §1 scenario works end-to-end |
| **4 — Robustness** | hooks→claims, approval buttons, `#alerts`, ~~**heartbeat** (§3.10)~~ **(done 2026-08-01)**, ~~autostart hardening~~ **(done 2026-08-01 — `autostart.ps1`, hidden services, 1-min self-heal trigger)**, security audit script | survives reboots + unattended weeks — and speaks up on its own |
| **5 — Comfort** | ~~Gateway swap in the watchdog~~ **(done 2026-08-01 — `gateway.py`, stdlib websocket, REST kept as backstop)**, `#fleet-status` dashboard, multi-PC awareness | it feels instant and effortless |
| **PC control** | **(done 2026-08-01 — `tools\desktop\`)** screen-read (`!screen`) + a **closed registry of named verbs**; deliberately not raw coordinate clicking (§7). `key`/`type-into` are local-CLI only — they cannot verify the app acted | look at the PC, and drive it, from the phone |

## 9. Decisions & open questions

**Decided:**
1. **Always-on = watchdog only; Claude sessions start on demand** (2026-07-23 — cost, robustness, latency all improve).
2. **Multi-PC concurrency:** one host machine per project at a time; the channel topic is the lock (§4).
3. **`#general`:** relayed by the orchestrator; no per-project lead session until proven needed.
4. **Auto-start (built 2026-08-01):** Task Scheduler runs the watchdog **and** the daybook server, hidden, at logon *and* on a 1-minute repeating trigger that brings either back within ~a minute of any death — `restart-on-failure` alone was measured not to fire on a non-zero exit. Owned by `tools\discord\autostart.ps1` (`-Action status|repair`, run `repair` after a machine move); Omnius itself is never auto-started without a reason.
5. **Registry = per-session claim files** with PID liveness (§3.5); machine-move self-healing.
6. **Tools set & root launchers (2026-07-23):** logic in PowerShell, `.bat` shims for double-click. `install.ps1` = idempotent setup & doctor — prereq checks with **guided winget installs**, `.env` from template, vendor the `watch` skill (claude-video) into `.claude\skills\watch`, `pip install faster-whisper` (**whisper default = local**, API key upgrade optional), `npm install` remotion into `tools\remotion\`. `start-omnius.bat` = **services only** — one terminal window: daybook + watchdog panes; no Claude session auto-started (quiet by default). `wakeup-omnius.bat` = summon the Omnius terminal (`claude --continue`, fallback fresh). `pack.ps1` = builds the portable zip (§5.6).
7. **`.env` never travels in the zip (2026-07-23):** only `.env.example` does — `pack.ps1` enforces it. Each instance fills its own `.env`; Discord bot values are per-instance, entered when that instance is set up (not during prep).
8. **Adopted from the ecosystem, personalized (2026-07-23):** heartbeat (§3.10 — OpenClaw-inspired, tied to daybook + fleet + memory gardening), one-shot print-mode delegation (Hermes-inspired, §3.1), self-improvement with receipts (§3.6), installer autostart option (Startup shortcut, `/auto` quiet mode), Phase 4 security audit script. **Deliberately skipped:** multi-messenger gateways (the file bus keeps that door open), TTS voice replies, model-agnosticism, canvas UI.

**Open:**
1. **Approval granularity** — exactly which operations need a button press (Phase 4 decision).
2. **Video-skill code read** — `install.ps1` vendors **bradautomates/claude-video** (`/watch`: yt-dlp + ffmpeg frames + captions/Whisper fallback — exactly our pipeline; MIT, widely adopted). Give it one quick code read before first heavy use — it is third-party code running locally. Its transcript fallback can use a Groq/OpenAI key from `.env` if you add one.
3. **Threads/forum posts** — out of scope until a real need appears.
4. **Multi-PC Discord model** — one server per instance (today's default) vs one shared fleet server with topic-based machine routing (Phase 5 candidate; see DISCORD.md §6).
