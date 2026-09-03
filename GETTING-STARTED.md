<p align="center">
  <img src="assets/omnius.png" alt="Omnius" width="180">
</p>

# Getting started with Omnius

<!-- Keep each "## " section able to stand on its own - most people read
     exactly one of them. This file stays ONE file: it is what install.ps1
     opens, what GitHub shows, and the only copy. (Until 2026-08-15 the web
     app rendered it as a tabbed Guide page; that tab is now a link here.) -->

## Start here

**You talk to it in Discord. It works on your PC.** That is the whole idea.

Ask it something from your phone on the train, and a Claude session wakes up on
your computer, does the work in your actual files, and answers you back in the
chat. Close the app; it carries on. Nothing runs — and nothing is billed — while
you are not asking.

There is also a small **web app** on your own machine for notes, statistics and
settings. Two doors, one system.

Read the tabs in order for a first install. Afterwards, each one stands alone.

*(Already have Omnius running and just moving to a new PC? You want
[START-HERE.md](START-HERE.md) instead — it is the short path.)*

## What you need

Set aside about 15 minutes for accounts. Only the first two are required.

| | What | Cost | Why |
|---|---|---|---|
| **1** | **Claude Code** — signed in | your Claude plan | This *is* the agent. Nothing works without it. |
| **2** | **A Discord bot** — token + server ID + your user ID | free | How you talk to Omnius from anywhere. |
| **3** | **A Mistral API key** | free tier is plenty | Reading PDFs and scanned invoices. |
| **4** | **An email address + app password** | free | Only if you want Omnius handling mail. |
| **5** | Google / OpenAI / other keys | varies | Optional. Image, video and speech generation. |

**You can skip 3, 4 and 5 and add them later.** Every capability stays switched
off until it has both a provider and a key, and turning one on never requires
touching code — just `config\` and `.env`.

### Creating the Discord bot, step by step (~5 minutes)

The bot is how you talk to Omnius from anywhere. You create it once, in your
browser, and it ends with three values pasted into `.env`. The guided setup
walks this same list with you — and then **verifies every value live against
Discord**, including the one checkbox almost everyone misses.

1. **Create the app.** Go to <https://discord.com/developers/applications> →
   **New Application** → give it a name. The name is what will answer you in
   chat — *Omnius* works fine.

2. **Make it private.** Open the **Bot** tab → switch **Public Bot** *off*.
   Only you should be able to add this bot anywhere.

3. **Let it read messages.** Same tab, under **Privileged Gateway Intents** →
   switch **Message Content Intent** *on* → then **press Save Changes** in the
   green bar at the bottom. Leave the page without pressing it and the switch
   silently resets — and without this intent, Discord hands the bot every
   message *empty*: no text, no attachments, and nothing tells you why. It is
   the single most common setup failure, which is why setup tests it with a
   real connection at the end.

4. **Copy the token.** Still on the **Bot** tab → **Reset Token** → copy the
   long string somewhere safe for a minute. That is `DISCORD_BOT_TOKEN`. Treat
   it like a password — whoever holds it *is* your bot.

5. **Build the invite link.** **OAuth2 → URL Generator** → tick the `bot`
   scope → under *Bot Permissions* tick these eight: **Manage Channels**,
   **View Channels**, **Send Messages**, **Read Message History**,
   **Embed Links**, **Attach Files**, **Add Reactions**, **Manage Messages**.
   That is the whole set Omnius uses — least privilege is the security posture
   here, and the bot should model it. (Shortcut: the generated URL ends in
   `permissions=…`; the eight above are `permissions=126032`. Administrator
   works too on a private server, but don't start there — details in
   [docs/DISCORD.md](docs/DISCORD.md) §4.) Copy the URL from the bottom of the
   page.

   Why each: *Manage Channels* stamps the categories and channels · *Send /
   Read / View* carry the mail · **Embed Links** is the `#fleet-status` board ·
   **Attach Files** sends screenshots and reports · **Add Reactions** is the 👀
   receipt on every message · *Manage Messages* pins and tidies. Skip Embed
   Links or Add Reactions and the fleet still runs, but the board and the
   receipts fail quietly against a locked-down `@everyone`.

6. **Create the server.** In the normal Discord app: **+** (Add a Server) →
   *Create My Own*. Private, just for you — no invite links, no other members.
   This is where Omnius' channels will live.

7. **Connect the bot to it.** Open the URL from step 5 in your browser, pick
   the new server, **Authorize**. The bot appears in the member list —
   offline, which is right: nothing is running on your PC yet.

8. **Copy the two IDs.** In Discord: **User Settings → Advanced → Developer
   Mode on** (this adds the right-click entries). Right-click the server's
   name → **Copy Server ID** → that is `DISCORD_GUILD_ID`. Right-click your
   own name in any chat → **Copy User ID** → that is `DISCORD_OWNER_ID`.

Paste the three values into `.env`, save, and setup takes it from there — it
checks the token, the server *and* the message-content intent against Discord
live, then creates the channels for you.

> The owner ID matters more than it looks: the bot obeys **only** that user,
> plus anyone you invite yourself — `config\guests.ini` for people with a
> Discord account, `config\telegram.ini` for people without one. Everyone
> else, people and bots alike, is dropped before any agent ever sees them. And
> an invited person can only send mail: control verbs, permission answers and
> takeover answers stay yours. That, plus a private server, is the security
> model.

### Getting the Mistral key (2 minutes)

<https://console.mistral.ai> → sign up → **API Keys** → create one. Free tier
covers ordinary use comfortably.

## Your Claude plan

Worth understanding, because it is the one thing that actually costs money.

**Omnius has no AI of its own.** It is plumbing: Discord in, files and tools in
the middle, Discord out. The thinking is **Claude Code**, signed in with your
Claude account, running on your machine. Your plan is what pays for it — there
is no separate Omnius bill and no API key to buy for the agent itself.

**Nothing spends while you are quiet.** The only always-on piece is the
watchdog, a small Python process listening to Discord. It uses no model at all.
A Claude session starts when a message arrives, does the work, and ends. An idle
Omnius costs nothing.

**What that means in practice:**

- A heavier plan buys **more and longer sessions**, not extra features.
- Big jobs are the expensive ones — reading a two-hour transcript costs far more
  than fifty short questions. That is exactly why transcription runs on its own
  desk, so a long job never eats the session you are talking to.
- **One desk, one job.** Each Discord channel is a separate session with its own
  memory, so a stuck or expensive job is contained to that channel.
- Model and effort are set once in `config\fleet.json` (currently Opus, xhigh
  effort, everywhere). Lower them there if you want cheaper, shallower runs.

**Why not an API key instead?** Measured on 2026-08-07 rather than guessed: the
same real usage, priced at API rates, came to roughly **$9,000 a month** against
€100–200 for the plan. Not because the rates are high, but because **63% of an
agentic bill is re-reading context** — a desk re-sends its whole conversation
every turn, which is the worst possible shape for pay-per-token and the best
possible fit for a flat fee.

The practical consequence: **more people means a plan each**, not a shared key.
A shared key has no per-user cap, so one person's big refactor spends everyone's
month. The decision, the numbers and how to re-measure them live in
`memory\orchestrator\topics\claude-cost.md`.

## Install

Double-click **`install.bat`**, or:

```
powershell -ExecutionPolicy Bypass -File install.ps1
```

It is **idempotent** — safe to run again any time, and the right way to repair a
half-finished setup. It will:

- check and offer to install **Python**, **Node**, **ffmpeg**, **Windows Terminal** (via `winget`)
- install the Python packages: Discord bus, terminal bridge, local speech-to-text, PDF reading
- install **Playwright** and offer its browser (~150MB, asked for — see *Browsing*)
- vendor the **`/watch`** video skill and set up **Remotion** for video rendering
- create **`.env`** and **`config\*.ini`** from their examples — *never overwriting yours*
- ask **what you want to call it** — Omnius, Jarvis, Maikel, anything. That name becomes its own channel and how it signs off (the folder and the commands stay `omnius`); press Enter to keep Omnius
- write each desk's hooks for **this** machine *(the single most important step on a clone or a moved install — until it runs, no desk reports its turns)*
- create the Discord channels
- register the **watchdog** as an auto-starting background task
- put an **Omnius** shortcut on your desktop
- open this guide

Then paste your values into **`.env`**, save, and run install again — it will
confirm what is on and what is still off.

## Every day

Install puts **Omnius** on your desktop. Double-click it any time to start the
watchdog if it is not running and open the web app. That is the only thing you
have to remember.

Then, from anywhere:

- **"What did I do last week?"** in `#daybook`.
- **Drop a voice note** while walking — transcribed on your machine and filed.
- **Send a meeting recording** to `#transcribe`, get a summary you can question.
- **"Any invoices in my mail this month?"** in your email channel.
- **"Start a project for X"** in `#omnius` — folders and channels appear together.
- **"Check my mail every hour on weekdays"** — see *Routines*.

Typed commands, answered instantly with no session started:

| | |
|---|---|
| `!status` | what is running |
| `!config` | what is configured |
| `!cron` | your routines and work loops |
| `!model` | this desk's model and effort — and where they came from |
| `!stop` | cancel this channel's desk (its queued mail is kept, not deleted) |
| `!trace` | what the fleet did — one delegation chain's whole story |
| `!update` | fetch what's new from the repo; `!update go` applies it safely |
| `!reload` | pick up changed code |

### Making a desk cheaper or sharper

Everything runs on Opus at xhigh effort, which is right for coding and overkill
for a desk that just files notes. In that desk's own channel:

```
!model                 what it is running on NOW, what the next run gets,
                       and where each value came from
!model sonnet low      set it (persists, and travels with your workspace)
!model effort max      change only the effort
!model reset           back to the default
!model sonnet low now  set it AND cut over immediately
!model now             cut over to the setting already recorded
!restart sonnet low    the same thing from the other end
```

Model and effort are fixed for the life of a Claude process, so a plain
`!model` lands on that desk's **next** run and says so. `now` is what saves you
the follow-up `!restart`.

`!status` shows the whole fleet's models at a glance — a bare `opus/xhigh` is
what that desk's run actually launched on, `(in parens)` is the config waiting
for its next one.

A model is fixed for the life of a run, so a change lands on that desk's **next**
one — the reply tells you which, and `!restart` cuts over immediately.

## Discord

Each channel is a different worker with its own memory. Talking in a channel
*is* talking to that worker.

**`#omnius`** — the main door. General questions, anything you are not sure
where to put, and running the fleet. This is the one you will use most.
*(Named after your agent: if you called it Jarvis at install, this is*
*`#jarvis`. Rename any channel in Discord whenever you like — nothing*
*breaks, because each channel is matched by its id and not by its name.)*

**`#daybook`** — quick capture. Notes, tasks, voice notes *(spoken in any
language — they are transcribed on your machine, not in the cloud)*. Anything
you drop here lands in the web app.

**`#transcribe`** — send a recording, or the path to one. You get back a full
transcript, screenshots of the moments the screen changed, and a written summary
you can then ask questions about. A two-hour meeting takes about 25 minutes and
never blocks anything else.

**`#fleet-status`** — "is everything running?" in plain language.

**`#alerts`** — problems that need you. Quiet when all is well.

**`📧 EMAIL`** — one channel per mail account, named after the address. Read,
search, summarise, draft. It always shows you a draft before anything is sent.

**`📁 <project>`** — one category per project you create, one channel per part
of it. Ask for a project and Omnius builds the folders and the channels together.

### Two things worth knowing

**It answers where you asked.** Replies come back in the channel you wrote in —
you never have to go somewhere else to find the answer.

**It asks before anything irreversible.** Routine work just happens, with no
permission prompts. But deleting things, force-pushing, or sending mail in your
name gets a plain-language question first, and a recommendation.

## The web app

<http://localhost:5111> — your private notebook and control panel. **Local
only**: it binds to `127.0.0.1`, so nothing outside your machine can reach it.
There is no login because there is no way in.

| Page | What it is for |
|---|---|
| **Today** | Quick capture, your open tasks — and **any day, replayed**: step back to a date and see what you wrote *and what the fleet did*: every commit across your projects, every desk that worked, with times. "What did I do on Tuesday?" is one click. |
| **Notes** | Everything you have captured, searchable, by month. Plain Markdown files in `daybook\notes\` — readable without this app, forever. |
| **Write** | The full composer — markdown, attachments, tasks. |
| **Settings** | Every setting, and **where its value came from** — the file, an environment variable, or a built-in default. Read-only on purpose. The guide lives here as a link. |

Notes are **append-only**: nothing you write is ever silently rewritten.

## Browsing

Omnius has **three** ways to use the web, and which one it picks is about
**sessions**, not difficulty.

**Playwright — headless, no login.** A real Chromium with no screen and no
cookies. Public pages, documentation, scraping, price checks, pages that are
empty until JavaScript builds them, screenshots. It is clean every time and runs
unattended, so it is the one a routine can use at 03:00 while you sleep.

**The Claude Chrome extension — logged in, your browser.** Anything behind a
sign-in: your dashboards, Zoho, the bank, a webmail UI, an admin panel. It
drives the browser you are already signed into, so the session stays yours and
**no password ever reaches this workspace**.

**`weblogin` — logged in, unattended.** For sites you use constantly and cannot
sign into by hand every time (the reason it exists: a fleet you drive from a
phone). Register the site in `config\websites.ini` with the **name** of a `.env`
key, and `tools\playwright\weblogin.py <site>` signs the browser in and saves the
session. A 6-digit 2FA code is asked for in your Discord channel and used once.

> The rule under all three, and it is not negotiable: **a desk never sees a
> password.** `weblogin` is a tool, not a session — it reads the secret from
> `.env` (which desks are denied from reading), types it, and hands back a saved
> session; the desk works from there with no secret involved. That is why the
> extension is still the first choice where you can use it: nothing is scripted
> at all. What no path allows is a password in a config file, in a prompt, or in
> a session's context. See [docs/WEB.md](docs/WEB.md).

Playwright's browser is a ~150MB download that install **asks** about rather
than assuming. Missing it later is not fatal:

```
python -m playwright install chromium
```

Details and the command-line tool: [`tools\playwright\README.md`](tools/playwright/README.md).

## Routines

Recurring work, created by **talking**. In `#omnius`:

> *"check my mail every hour on weekdays during work hours"*

Omnius picks the right worker, writes the job, and replies with what it
understood — the schedule, the channel it will wake, and **the next three times
it will run**. Read that reply: a wrong schedule otherwise fails silently, at a
time you are not watching.

Manage them with **`!cron`** from anywhere — instant, no session started:

| | |
|---|---|
| `!cron` | list everything |
| `!cron pause <id>` / `resume <id>` | stop and restart without losing the job |
| `!cron rm <id>` | delete it |
| `!cron adopt <id>` | claim a job created on your other PC |

**A routine that finds nothing says nothing.** An hourly check reporting
"nothing new" nine times a day would teach you to ignore the channel, so silence
is the designed behaviour — you hear from it when something actually needs you.

Two things worth knowing:

- Routines **travel** with your workspace, and each is stamped with the machine
  that created it. Restore a backup on a second PC and it will *not* double-fire;
  `!cron adopt` moves them over when you actually mean to.
- If the PC is asleep when a routine is due, it is skipped rather than fired
  late. After three misses in a row it says so once in `#alerts` — it never
  reschedules behind your back.

## What each key switches on

| Key | Without it | With it |
|---|---|---|
| **Mistral** | PDFs with real text still read fine. | Scanned documents and photographed invoices become text, with the fields pulled out. |
| **Email** | — | Reading, searching and drafting mail from your phone. |
| **Google / other** | — | Image generation, video generation, speech. |
| **Telegram** | — | Invite someone who has **no Discord account** into one of your channels: they write to a bot, a desk answers, the answer reaches their phone. Two lines in `config\telegram.ini` — [tools/telegram/README.md](tools/telegram/README.md). |

Check what is currently on: the **Settings** page in the web app, or type
**`!config`** in Discord.

## If something is wrong

**Run `install.bat` again.** It is a repair tool as much as an installer, and it
fixes the most common problem by itself (paths pointing at an old machine).

| Symptom | Cause |
|---|---|
| Bot silent in Discord | **Message Content Intent** is off, or `DISCORD_OWNER_ID` is not your ID. |
| Web app will not open | Watchdog is not running — double-click the desktop icon. |
| A desk never answers | Run `install.bat`; almost always stale hook paths. |
| "not configured" | A key is missing. Check **Settings**, or `!config`. |
| A code change seems ignored | The watchdog loads its code at startup — `!reload`. |
| Headless browsing fails | The browser was skipped at install: `python -m playwright install chromium`. |

Deeper detail: [`docs\ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how it works,
[`docs\NEW-INSTANCE.md`](docs/NEW-INSTANCE.md) for the full runbook.

## Where things live

```
daybook\notes\      your notes — plain Markdown, yours forever
media\              recordings, transcripts, attachments (never sent to git)
config\             settings, in commented .ini files — including your routines
.env                secrets — the ONLY place credentials live
memory\             what Omnius has learned about your work
```

**`.env` never leaves the machine.** It is excluded from git and from backups by
design, and Claude sessions are explicitly denied reading it. Config files name
the *key* that holds a secret, never the secret itself — so `config\` stays safe
to read, diff and share.
