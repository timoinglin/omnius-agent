# Discord Blueprint — server schema & setup

> What the Discord side of Omnius looks like: the day-one structure, how it grows, naming and topic conventions, and the per-instance setup checklist. Machine-readable structure: [`tools\discord\schema.json`](../tools/discord/schema.json) — automation stamps it **idempotently** (find-by-pin, then by name, else create); this doc explains the rules. Design context: [ARCHITECTURE.md](ARCHITECTURE.md) §3.4, §4, §7.

## 1. Day one — a fresh instance

Stamped from `tools\discord\schema.json` — that file is the source of truth, this is its picture:

```
Discord Server "Omnius"          (private: you + the bot, nobody else)
├── 🎛 ORCHESTRATOR
│   ├── #omnius           ← the main door: talk to Omnius. Named after the agent, so it is
│   │                        #jarvis if you called it Jarvis (#orchestrator also accepted)
│   ├── #daybook          ← quick capture → daybook: notes, tasks, voice notes (transcribed)
│   ├── #fleet-status     ← bot-maintained pinned overview — mute it, don't post
│   ├── #transcribe       ← drop a recording, get transcript + frames + summary back
│   └── #alerts           ← errors + approval requests from all sessions
└── 📧 EMAIL                (only once config\email.ini has accounts)
    └── #<address>        ← one channel per account: you@example.com → #you-example
```

Project categories appear only when projects are stamped (§3); nothing exists "in advance".

| Channel | Who answers | Behavior |
|---|---|---|
| `#omnius` | `orchestrator` | any message wakes it; full fleet control from the phone. Named after the agent (`[omnius] name`); `#orchestrator` and the old name are accepted as the same door, and renaming it in Discord changes nothing |
| `#daybook` | the **`daybook` desk** | text/voice → note or task in `daybook\` (voice transcribed first). The orchestrator never posts here — its outbox to this channel is refused by design |
| `#fleet-status` | `tool.fleet` | one embed, edited in place by the watchdog itself; treat as read-only |
| `#transcribe` | `tool.transcribe` | recordings become detached zero-token jobs; the result comes back here |
| `#alerts` | the session that asked | errors, permission asks (`ok`/`no`, with a 6-char code when several are pending), cross-project gate asks |
| `#<address>` | `tool.email` | one per account in `config\email.ini`, created automatically when you add one |

## 2. Conventions

- **Names:** kebab-case, lowercase (Discord renders category names uppercase on its own).
- **Rename any channel you like, in the Discord app, whenever you like.** Routing is by
  **channel id**, not by name: the first time a channel is created or recognised, the desk
  behind it is pinned to its id in `state\watchdog\channels.json`, and the name becomes a
  label. Rename `#web` to `#frontend` and its desk still answers; rename `#omnius` to
  `#maikel` and it is still the main door; the structure stamp will not recreate the old
  name beside it. (Before 2026-08-24 routing was by name, and a rename made the desk deaf.)
  Deleting a channel is *not* a rename - the pin is dropped and the next stamp recreates it.
  The pins are machine-local and are **not** in the backup zip (`state\` never is), so a
  workspace restored onto a new PC matches by name again for one round: a channel you had
  renamed says out loud that it is unmapped - a project channel even names the folder
  it expected - instead of going quiet, and renaming it back re-pins it.
- **The orchestrator's channel is named after the agent.** `#omnius` is only the default:
  set `[omnius] name` in `config\omnius.ini` (install asks for it) and a fresh instance
  stamps `#jarvis` or `#maikel` instead. The install folder, the repo and the `/omnius`
  skill keep their name regardless - those are machinery, not identity.
- **Prefixes:** project category = `📁 <project>` · archived = `🗄 <project>` (renamed in place, channels locked).
- **Topics are the durable map:** every session channel's topic = `{path} | {machine} | {started}`. If `state\` is ever lost, the bridge re-derives the whole mapping from topics — treat them as **data, not decoration**.
- **One channel = one session** (`#app` ↔ `projects\x\app`). `#general` is relayed by Omnius — no extra session.

## 3. How it grows — a project is stamped

```
└── 📁 recipe-app                    (created by /new-project)
    ├── #general           ← project-wide topics — relayed by Omnius
    ├── #app               ← session projects\recipe-app\app
    └── #backend           ← session projects\recipe-app\backend
```

Archiving (ARCHITECTURE §5.5): category renamed to `🗄 recipe-app`, channels locked, history kept.

## 4. Bot & permissions (per instance)

**Guided:** `tools\discord\setup.ps1` runs automatically from `install.bat`, `start-omnius.bat` and `wakeup-omnius.bat` whenever Discord isn't configured yet — it asks first, walks through the steps below, opens the portal and `.env`, then validates the token and server live against the Discord API.

The bot is created **fresh for every instance** (decided 2026-07-23) at <https://discord.com/developers/applications>:

1. New Application → Bot → copy the **token** → `.env: DISCORD_BOT_TOKEN`. Disable **Public Bot**.
2. Enable **Message Content Intent** — the watchdog reads message text.
3. Invite the bot (OAuth2 → URL Generator → scope `bot`) with either:
   - **Simple** (fine for a private server): Administrator — `permissions=8`
   - **Minimal:** Manage Channels, View Channels, Send Messages, Read Message History, Embed Links, Attach Files, Add Reactions, Manage Messages (pin/delete) — `permissions=126032`
4. Server ID → `.env: DISCORD_GUILD_ID` · your user ID → `.env: DISCORD_OWNER_ID` (Settings → Advanced → Developer Mode on, then right-click → Copy ID).

## 5. Server settings (once, at creation)

- Private server: **no invite links**, no community features, no other members. 2FA on your account.
- Notifications: server-wide **mentions only**, and additionally **mute `#fleet-status`** (it changes often).
- Remember the boundary (ARCHITECTURE §7): the watchdog obeys **only** `DISCORD_OWNER_ID` — messages from anyone or anything else are dropped before any session sees them.

## 6. Multi-machine (design note)

- **Default today: one server per instance.** Matches the bot-per-instance decision, zero routing ambiguity; a moved zip = a new instance = its own server.
- **Phase 5 candidate: one shared fleet server** — per-machine bots, each watchdog serving only channels whose topic `machine` field matches, giving a single phone view of every PC. Open question in ARCHITECTURE §9.
