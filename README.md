<p align="center">
  <img src="assets/omnius.png" alt="Omnius" width="230">
</p>

<h1 align="center">Omnius</h1>

<p align="center"><b>A fleet of Claude agents you run from Discord — or straight from the terminal. Your call, same conversation.</b></p>

<p align="center">
  <a href="https://github.com/timoinglin/omnius-agent/releases/latest"><img src="https://img.shields.io/github/v/release/timoinglin/omnius-agent?label=release&color=5865F2" alt="latest release"></a>
  <img src="https://img.shields.io/badge/platform-Windows%2010%2F11-0078D4" alt="Windows 10/11">
  <img src="https://img.shields.io/badge/built%20on-Claude%20Code-D97757" alt="built on Claude Code">
  <img src="https://img.shields.io/badge/runs%20on-your%20Claude%20subscription-D97757" alt="no API key">
  <img src="https://img.shields.io/badge/core-Python%20stdlib%20·%20no%20database-3776AB" alt="Python stdlib, no database">
  <a href="https://github.com/timoinglin/omnius-agent/releases"><img src="https://img.shields.io/github/downloads/timoinglin/omnius-agent/total?color=2ea44f" alt="downloads"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT license"></a>
</p>

<p align="center"><i>Named after the Dune evermind: an AI with synchronized copies on every world — which is exactly the point.</i></p>

---

```powershell
irm https://raw.githubusercontent.com/timoinglin/omnius-agent/main/get.ps1 | iex
```

One line in PowerShell. It fetches the latest release, unpacks it where you choose, and hands over to a guided setup that installs its own prerequisites — winget or not — and walks you through the Discord bot. Proven on stock Windows 10 with nothing preinstalled.

## Why this exists

I needed a multi-agent agent, so I built one. **XD**

Seriously: I work across several machines and I'm not always at any of them. Omnius gives every machine a fleet of Claude desks — one orchestrator, one desk per project component — and mirrors the whole thing to Discord channels. At the desk I type into the native CLI; on the road I type into the channel; **it's the same conversation either way.** I'm much more productive like this, so I'm sharing it as-is: a personal tool with opinionated choices and edges shaped by my own workflow, not a supported product.

**It runs on the Claude subscription you already have.** The CLI signs in with your account — no API key, no second bill, and nothing spends a token while the fleet is idle.

### Not a replacement for OpenClaw or Hermes

For **me** it works far better than either — and that sentence means nothing without the *for me*. They are broader products with big communities, and I kept finding them overkill where I don't need breadth while still missing the things my days actually depend on. Judge the shape, not the size:

**What Omnius deliberately is**

- **One messenger: Discord.** I don't need 27 channels — I need the one already on my phone. Doing exactly one means push delivery, live *typing…*, a channel per desk, ok/no permission asks and voice notes all get real attention instead of lowest-common-denominator support. (The file bus keeps the door open if you ever want another.)
- **One folder = the whole instance.** Code, memory, notes, projects, history. Zip it, unzip it on any PC, sign in with any Claude account — it wakes up knowing everything it knew. Portability was my missing feature everywhere else.
- **The agents are plain Claude Code sessions.** A desk is a folder plus a real terminal you can sit down at, not a runtime of its own. Whatever Claude Code learns to do, the whole fleet can do the same day — and there is no framework between you and it when something needs debugging.
- **Small enough to own.** One always-on stdlib-Python script, files as the database, no accounts, no server. You can read the entire moving part in an afternoon.

**What it deliberately is not**

- Not multi-messenger, not model-agnostic, no canvas UI, no TTS — recorded as *decisions*, not gaps, in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- Not multi-user: the bot obeys exactly one Discord account (plus explicitly invited guests, confined to their project).
- Not cross-platform: Windows is load-bearing, see below.
- Not a supported product — a personal tool, shared for the curious.

Both tools left fingerprints here happily: the proactive heartbeat is OpenClaw-inspired, the one-shot delegation Hermes-inspired, both credited in the architecture doc. If their shape fits your life, use them. This one fits mine.

## Why this actually makes a coder faster

- **Agents work; you don't watch them work.** Kick off a refactor from the sofa, redirect it from the supermarket queue. The answer finds *you* — every minute an agent grinds is a minute you spend elsewhere, and the *typing…* indicator plus the fleet board mean you glance, not babysit.
- **Context-switching costs nothing.** Every project component is its own desk with its own conversation and its own memory. Switching projects is switching Discord channels — you never re-explain a codebase, and neither does the agent: `--continue` and the memory files mean Monday's desk still knows what Friday's decided.
- **Parallel by default.** The backend desk migrates a schema while the web desk builds the UI against the *interface notes the backend desk wrote* — siblings coordinate through project memory, not through you repeating yourself.
- **Nothing blocks on your screen.** Permission asks arrive as ok/no in the channel; one `ok` teaches the whole fleet permanently. The class of "came back after lunch, it was stuck on a dialog" is designed out.
- **Recurring work stops being your job.** Say the schedule in plain words once; routines run it and speak only when something needs you. Silence is the feature — no notification fatigue.
- **Friction becomes a commit.** Anything that annoys you, you type into the orchestrator's channel — and Omnius modifies Omnius, gated by its own test suite (1,300+ checks). Its verbs are ordinary committed skills (`/new-project`, `/status`, `/goal`, `/backup`, `/release`, …), so every self-improvement is a reviewable commit that travels to every future instance. The tool sharpens itself while you use it.
- **The chat is the human-facing audit trail.** Every decision, every answer, every screenshot is already threaded per project and searchable in Discord — no separate tracker to feed. The machine keeps its own records underneath: per-desk transcripts, run logs and chain ledgers in `state\`.

## What a day with it looks like

| You | Omnius |
|---|---|
| Send a **voice note** to `#my-project` → `#app` from your phone | Transcribes it **locally** (Whisper, no API), wakes that project's desk, shows *typing…* while it works, answers in the channel |
| *"check my gmail every hour on weekdays during work hours"* | Creates the routine, echoes the schedule and the next three fire times. It only messages you **when something needs you** — silence means nothing did |
| *"create project recipe-app with app and backend"* | Stamps the project from a template: folder, memory, Discord category, one channel per desk. Start talking in `#app` immediately |
| A desk needs something outside its allow-list | You get an **ok / no** question in the channel. `ok` also teaches the fleet — that tool never asks again, on any desk |
| `!status` · `!model sonnet low` · `!screen` · `!cron` · `!reload` | Instant answers from the always-on watchdog — zero tokens, no desk spawned |
| *"auditor: audit the project and get the findings fixed"* | The auditor **desk-mails each finding to the desk that owns the fix**, every hop mirrored in the channels; the fixes come back on the same thread and you get one verified summary. A full chain closes in about a minute |
| *"Omnius, add a `!weather` command to yourself"* | The orchestrator desk edits Omnius's own code, runs the test suite, commits — **you can code Omnius from inside Omnius.** `!reload` and it's live |

## What it can do

**From any Discord channel**
- Text, images, files and voice notes in — answers, files and screenshots back. Voice is transcribed locally; nothing leaves the machine for it.
- Live *typing…* while a desk works, a **fleet board** in `#fleet-status` edited in place, and a deadman alarm that pages you if mail ever sits with nothing alive to take it.
- Push delivery over Discord's Gateway websocket, with a REST sweep as the authority behind it — a dropped frame costs latency, never a message.
- **Thirteen instant control commands**, handled by the watchdog itself: `!status` `!model` `!restart` `!stop` `!kill` `!killall` `!cron` `!config` `!screen` `!desktop` `!reload` `!update` `!trace`.
- **Guests**: give a client or collaborator write access to exactly one project's channels — they're answered in their own language and can't touch anything else.

**Running the fleet**
- Create, archive and reopen whole projects by asking. Desks spawn on demand, one per component, each with its own Discord channel.
- Switch any desk's **model and effort from Discord** (`!model sonnet low`, `!restart opus`), per desk, persisted.
- A **shared memory layer** every desk reads, per-project memory its desks write, and read-transparency across projects — solutions travel, conventions stay aligned.
- Several machines? Each runs its own Omnius on its own Discord server, and your phone drives them all — every claim, topic and `!status` names its machine.

**Desks delegating to desks** *(live-proven 2026-08-15 — [docs/DELEGATION.md](docs/DELEGATION.md))*
- A desk hands work to a sibling by **addressing an envelope to it**; the watchdog validates the target, delivers, and mirrors every hop into the recipient's channel — the fleet talking to itself is always on your screen. Fan-out workflows are just envelopes in sequence.
- **Hop budgets** with free replies stop runaway chains; a **cross-project gate** holds anything that crosses a project boundary for your ok/no, failing closed on silence.
- **Budgeted work loops**: a desk grinds toward a checkable done-condition across runs — five by default, then it must report what exists and ask. Loops never extend themselves.
- **Slash pass-through**: `/goal <objective>`, `/status`, `/watch <url>` or any skill you allow-list runs *as that skill* on the desk; the list ships empty on purpose, and guests never pass one.

**Automation**
- Routines created by talking, managed with `!cron` (list / pause / resume / remove / adopt): intervals, daily times, one-shots, weekday and time-window clamps.
- Machine-stamped so a restored backup never double-fires; missed runs are counted and alerted **once**, never silently rescheduled.

**Tools that ship**
| Tool | What it does |
|---|---|
| 🎙 **whisper** | Local speech-to-text, offline, any format ffmpeg reads |
| 📼 **transcribe** | A desk that turns recordings into transcript + key frames + summary, as detached zero-token jobs |
| 📧 **email** | IMAP/SMTP **and** Microsoft Graph, one contract — a Discord channel per account, auto-created from config |
| 📄 **documents** | PDF text locally, OCR fallback for scans, structured extraction (invoices) with checksum validation |
| 🌐 **playwright** | Headless browsing + a polite parallel crawler, and `weblogin` for sites you register: the **tool** holds the password (from `.env`, which desks may not read) and hands the desk a signed-in session — **a 6-digit 2FA code is asked for in your channel** and used once. Or skip scripting entirely: sign in yourself and let desks drive your real browser through the Chrome extension |
| 🎬 **watch** | Watch a video (URL or file) and answer questions about it — frames + transcript, captions or local Whisper |
| 📓 **daybook** | Notes & tasks app: plain markdown files are the data, stdlib-only server, web UI at `localhost:5111`. Its Today page replays **any day** — your notes plus every commit and desk that worked that day |

**Ops that don't need you**
- Services run as self-healing scheduled tasks (down ≤60 s after a crash, back after every reboot). One desktop icon starts everything; `stop-omnius.bat` genuinely stops it.
- One-zip backups (secrets excluded by design) and a gated release build that **refuses to ship** if anything identifying or a broken installer is inside.
- **Every instance updates itself from this repo**: `!update` in Discord previews what's new, `!update go` applies it — fast-forward pull, the full test suite (a red suite **rolls the update back**), then a self-reload that compile-checks first and refuses to kill the bus with a broken version. And the update isn't done until the **new watchdog reports back healthy** — one that crash-loops or sits deaf **reverts to the previous commit on its own** and says so. Zip installs attach to the repo at install time, born on the exact commit their release was cut from.

## Mechanisms, not promises

Every guard below exists because its failure mode happened for real first — and each one was then moved out of "the agent remembers" into code the test suite enforces. This is the part a quick skim misses, so here it is on one screen:

- **Delegation cannot run away.** Chains spend a hop budget (replies travel free, so an answer can always come home), a per-chain message cap stops ping-pong, a typo'd desk name is refused out loud instead of inventing a phantom desk — and `!trace <id>` replays any chain's whole story from state: every hop, timestamps, what spent budget, how it ended.
- **Projects cannot contaminate each other.** Desk mail crossing a project boundary parks and asks you `ok`/`no`; unanswered, it drops itself after an hour. Fail-closed is the house rule for every authorization surface: guests, slash commands, the gate — empty list means nothing passes, silence means no.
- **Loops cannot self-extend.** A desk grinding toward a done-condition gets five runs (configurable), then must report what *exists* and ask — enforced when it re-queues itself *and* again at fire time, so even a hand-edited job can't sneak past. Your fresh instruction is the only thing that buys more.
- **Updates cannot strand the fleet.** `!update go` is a fast-forward pull gated by the full suite — judging only failures the update *introduces* (named, and rolled back), never a machine's own housekeeping — and the reload ends with a handshake: the new watchdog reports back healthy, or the previous commit takes back over **on its own** and says so.
- **Silence is detected, never assumed.** A person's mail sitting with nothing alive to take it pages you; a desk that changed files but reported nothing is called out by name; the fleet talking to itself never pages anyone — that's a log line.
- **1,300+ checks gate every push**, and the docs keep an honest ledger of the difference between what the suite proves and what only live machines caught.

## Try the demo fleet (10 minutes)

A demo project ships with the box: **linkbox**, a tiny link-sharing board built by three desks — `back`, `front`, and an **auditor** whose only job is to find what the other two missed. Nothing is pre-written; the desks build it when you ask.

```
python tools\orchestrator\fleet_ops.py new-project demo --components back front auditor --template templates\demo-project
```

Then, from Discord (or the terminals — same thing):

1. **`#back`** — *"build the API from the brief"*. It builds a stdlib JSON server, and publishes its API in the project memory.
2. **`#front`** — *"build the page against back's notes"*. It builds `index.html` from those notes, never from back's code.
3. **`#auditor`** — *"audit the project and get the findings fixed"*. The auditor is **read-only**: it hunts leaks, XSS in rendered titles, `javascript:` URLs, tracebacks that disclose paths — files findings with severity, then **desk-mails each one to the desk that owns the fix** and verifies what comes back.

The brief plants those tensions on purpose. Whether the builders handled them — that's the demo.

## The idea in 30 seconds

- **The filesystem is the org chart.** Root = orchestrator. `projects\<name>\app\` = one desk. Discord mirrors it: one category per project, one channel per desk.
- **One tiny always-on piece.** A stdlib-Python watchdog listens to Discord; Claude desks are one-shot headless runs it starts on demand. Idle fleet = zero tokens.
- **Memory lives in files, not scrollback.** Shared layer + per-project layer, all plain markdown. Your instance's memory is yours — it travels in your backups, never in this repo.
- **Two doors, same desk.** Terminal at the PC or channel from anywhere — `--continue` keeps it one conversation.

## What you bring, honestly

- **Windows 10/11** — paths, ConPTY and Task Scheduler are load-bearing; this is Windows-only.
- **A Claude subscription** — the CLI logs in with it. No API key.
- **A Discord server of your own** plus a bot token (guided; the bot answers only you and needs eight scoped permissions — `permissions=126032` — not admin).
- Everything else the installer brings itself — via winget when you have it, **straight from the official sources when you don't** (Git, Python, Node, ffmpeg, the Claude CLI), including the repair a clean Windows usually needs (the MSVC runtime that C++ wheels want). Optional extras stay off until you add a key: OCR for scans (Mistral), email passwords, image/video/speech providers. Playwright's ~150 MB browser is asked about, not assumed.

## Security posture, in one paragraph

Desks run with a wide allow-list and **no permission prompts** — a prompt on a screen nobody watches is a hung desk, and Omnius is meant to be used from a phone. Three fences hold instead: anything outside the allow-list becomes an **ok/no question in your channel**; anything irreversible the model must ask about **in words** before doing; and `.env` — the only place secrets live — is deny-listed from reading and excluded from every backup and release. The bot obeys exactly one Discord user: you. Read [docs/PERMISSIONS.md](docs/PERMISSIONS.md) before loosening any of it.

## Reading order

[GETTING-STARTED.md](GETTING-STARTED.md) — written for someone who has never seen it · **[docs/GUIDE.md](docs/GUIDE.md) — the owner's manual: everything it does and how to ask for it** · [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — why it's built this way · [docs/DISCORD.md](docs/DISCORD.md) — the server blueprint · [docs/DELEGATION.md](docs/DELEGATION.md) — the delegation/loops/slash design, built and live-proven · [docs/WEB.md](docs/WEB.md) — driving websites: the site registry, scripted login, 2FA over Discord · [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) — `!trace` and the self-verifying update, built; the demolition derby still on paper · [docs/PERMISSIONS.md](docs/PERMISSIONS.md) · [docs/RELIABILITY.md](docs/RELIABILITY.md) · [docs/TESTING.md](docs/TESTING.md) — the 1,300+ automated checks that gate every release, **plus** the live-machine validation the suite can't substitute for (documented where it happened, including what only real machines caught)

There is no roadmap: this repo ships what exists, specs land in `docs\` before their code does, and the [commit log](https://github.com/timoinglin/omnius-agent/commits/main) is the honest history of both.

---

<p align="center"><i>Built for one person, shared for the curious. If it makes you more productive too — that's the point of publishing it.</i></p>
