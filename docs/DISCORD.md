# Discord Blueprint — server schema & setup

> What the Discord side of Omnius looks like: the day-one structure, how it grows, naming and topic conventions, and the per-instance setup checklist. Machine-readable structure: [`tools\discord\schema.json`](../tools/discord/schema.json) — automation stamps it **idempotently** (find-or-create by exact name); this doc explains the rules. Design context: [ARCHITECTURE.md](ARCHITECTURE.md) §3.4, §4, §7.

## 1. Day one — a fresh instance

```
Discord Server "Omnius"          (private: you + the bot, nobody else)
└── 🎛 ORCHESTRATOR
    ├── #orchestrator      ← the main door: talk to Omnius (wakes/spawns on demand)
    ├── #daybook           ← quick capture → daybook: notes, tasks, voice notes (transcribed)
    ├── #fleet-status      ← bot-maintained pinned overview — mute it, don't post
    └── #alerts            ← errors + approval requests from all sessions
```

That is the **whole** initial server — deliberately minimal. Project categories appear only when projects are stamped (§3); nothing exists "in advance".

| Channel | Who answers | Behavior |
|---|---|---|
| `#orchestrator` | Omnius (root session) | any message wakes/spawns Omnius; full fleet control from the phone |
| `#daybook` | Omnius | text/voice → note or task in `daybook\` (voice notes transcribed first); personal data never leaves the orchestrator context |
| `#fleet-status` | nobody — bot-maintained | one pinned embed, updated by `/status` and every fleet mutation; treat as read-only |
| `#alerts` | the session that asked | errors, confirmations; Phase 4: Approve/Deny buttons — replying here approves destructive ops |

## 2. Conventions

- **Names:** kebab-case, lowercase (Discord renders category names uppercase on its own).
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
