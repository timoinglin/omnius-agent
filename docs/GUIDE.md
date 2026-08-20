# The Owner's Manual — everything Omnius does, and how to ask for it

> The practical companion to [../README.md](../README.md) (what it is) and
> [../GETTING-STARTED.md](../GETTING-STARTED.md) (how to install it). This is the
> *using* guide: every capability, the exact way to invoke it, and what to expect
> back. Written for the person who owns the fleet — it doubles as the tour for
> anyone else reading along. Everything here is built and live as of 2026-08-15.

## 1. The mental model, once

- **One channel = one desk.** `#app` in the `📁 recipe-app` category is the
  `recipe-app.app` desk — a real Claude Code session in
  `projects\recipe-app\app\`. `#omnius` (or `#orchestrator`) is the main agent.
  A project's `#general` is relayed by the orchestrator when the project has
  several desks.
- **Two doors, one conversation.** Anything you'd type into a desk's terminal
  you can type into its channel, and vice versa — `--continue` keeps it a
  single thread. The fleet is idle-free: nothing runs and nothing spends until
  mail arrives.
- **You are the only authority.** The bot obeys exactly one Discord account.
  Guests you invite are confined to the channels you name.

## 2. Talking to desks

Just write. In any desk channel, plain text is a work order; the desk acks
first (*"👋 got it — checking the build now"*), then works, then answers in the
same channel. What you can send:

| You send | What happens |
|---|---|
| Text | The desk treats it as if typed at its keyboard |
| A **voice note** | Transcribed locally (Whisper — nothing leaves the machine), then treated as text |
| **Images** | The desk looks at them |
| **Files** | Saved to the media archive, used, and filed where they belong |
| A **video link or file** | Say *"watch this"* — frames + transcript, then ask questions about it |
| A later correction | The newer message wins; the desk says so rather than doing both |

Silence rules: heartbeats and routine checks that find nothing **say nothing**.
If a desk has mail and nothing alive to take it for ten minutes, the watchdog
pages you — you never discover a dead desk by waiting.

## 3. The instant verbs (zero tokens, work even when every desk is stuck)

Handled by the always-on watchdog itself, never by a model:

| Verb | Does |
|---|---|
| `!status` | Fleet overview: every desk, queue depths, stalls, notes freshness |
| `!kill` / `!restart [model] [effort]` | Kill / restart **this channel's** desk; `!restart sonnet low` also persists the model dial |
| `!model [reset\|<model>] [effort]` | Show or set this desk's model + effort, with provenance |
| `!stop` | Cancel this desk: kill the run, park its queued mail in `state\dropped\` (kept, not deleted) |
| `!killall` | Everything down (only accepted in `#omnius`) |
| `!cron [list\|pause\|resume\|rm\|adopt <id>]` | Routines — and the work-loop ledgers ride along in the same listing |
| `!config` | Read-only settings dump: values, sources, secrets as set/NOT-SET, guests, slash list |
| `!screen` / `!desktop <verb>` | Screenshot / desktop control of the machine (see `memory\shared\DESKTOP-CONTROL.md`) |
| `!update` / `!update go` | Self-update from the repo: preview, then **rebase your own commits onto the release** → suite (red rolls back) → reload → **the new watchdog reports back healthy, or reverts itself** |
| `!trace [id]` | One chain's, loop's or envelope's whole story — every hop, timestamps, gate holds, budget. Bare `!trace` lists recent ones |
| `!reload` | Restart the watchdog to pick up pulled code — compile-checked first, refuses rather than kill the bus |

## 4. Permissions: on by default, one exception

**Everything is allowed except `.env`.** You are alone on your own Omnius, so
the posture is not a sandbox — it is a machine acting with your authority.
Every desk carries the same profile (`.claude\settings.json`, stamped by
`tools\discord\sync_permissions.py`): 60+ tools allowed, file edits applied
without asking (`defaultMode: acceptEdits`), project MCP servers trusted, and
every installed skill reachable as a slash command.

Three things still hold:

1. **`.env` is denied** — the file holding your tokens, mail passwords and API
   keys. A desk that wants something from it has to say which key and why.
   It is a guardrail, not a cage: a desk has a shell, so `type .env` would
   work. What actually contains this machine is that only you (and the guests
   you invite) can talk to a desk at all.
2. **Irreversible or outward-facing steps are described in words first** —
   deleting broadly, force-pushing, sending mail as you, spending money —
   whatever the allow-list says.
3. **Anything genuinely unlisted still escalates** to an ok/no in your channel,
   and one `ok` teaches the whole fleet. With the list this wide it is rare.

`AskUserQuestion` is the one tool deliberately denied for a non-security
reason: it draws a menu in a terminal nobody is sitting at and blocks the turn
that would have to end for you to answer it. Denied, the desk asks in plain
text instead — which you *can* answer from a phone.

An ask nobody answers **fails closed**. Details: [PERMISSIONS.md](PERMISSIONS.md).

## 5. Projects and the fleet

Ask the orchestrator in `#omnius`, in words:

- *"create project recipe-app with app and backend"* → folder stamped from the
  template, memory seeded, Discord category + a channel per desk. Talk in
  `#app` immediately.
- *"archive recipe-app"* / *"reopen recipe-app"* → the category is archived
  (🗄), desks killed, memory finalised — and reversible.
- *"open a terminal on the backend desk"* → a visible window at that desk,
  already connected to the bus.
- **Guests:** add `[guest.<name>]` with their Discord id and allowed channels
  to `config\guests.ini` — live within a minute, no restart. Guests are
  answered in their own language and can reach nothing else.
- **Model economics per desk:** `!model sonnet low` on a docs desk, opus on the
  hard one. Defaults live in `config\fleet.json`.

## 6. Delegation — desks mail desks *(new)*

You never relay between desks anymore. Say the intent; the fleet does the
legwork:

- *"auditor: audit the project and get the findings fixed"* — the auditor
  desk-mails each finding to the desk that owns the fix, that desk fixes and
  replies on the same thread, the auditor verifies and posts **one** summary
  back to you.
- *"have the backend desk expose a /search endpoint, then tell front to use
  it"* — the orchestrator (or any desk you tell) forwards the brief as desk
  mail; each hop is **mirrored into the recipient's channel** as a compact
  `[desk mail]` line, so the fleet talking to itself is always on your screen.

What guards it, so it can't run away:

- **Depth budget** (default 3, `config\omnius.ini [delegation]`): a chain may
  travel three desks out from the one that started it. **Breadth is free** —
  one desk briefing three siblings costs nothing, because what runs away
  unattended is a chain handing off further and further, not a desk
  coordinating its own team. Replies are free at any depth, so an answer can
  always come home. Go deeper than the budget and the chain stops and asks you
  to re-instruct; a message cap (scaled to the desks involved) catches
  ping-pong.
- **The cross-project gate:** mail between desks of *one* project flows freely;
  anything crossing a project boundary (or aimed at the orchestrator or a tool
  desk) is **held** and you get one ok/no with a code. Unanswered = dropped
  after an hour. Fails closed.
- A typo'd desk name is refused out loud — it can't invent a phantom desk.

Measured live: a full four-hop chain across three desks closes in about a
minute, cold. Design: [DELEGATION.md](DELEGATION.md).

## 7. Work loops — long grinds with a leash *(new)*

For work that spans many runs: *"keep going until the tests pass — work in
steps, check in when the budget runs out."* The desk opens a **counted loop**:

- Each fired run checks the **done-condition first** (a command with an exit
  code, e.g. *"done when `pytest` exits 0"*), does one step, and re-queues
  itself.
- **Budget: 5 runs** by default (`loop_budget` in config). At the limit the
  desk must stop and report what EXISTS and ask whether to continue — **loops
  never extend themselves**; your fresh instruction opens a new one.
- `!cron` lists open loops with their run counts; a desk closes its loop the
  moment the done-command goes green.

## 8. Slash commands from Discord — on by default

Type `/goal <objective>`, `/status`, `/watch <url>` in any desk channel and that
desk runs **that skill**, with the rest of the message as its argument — no
improvisation. Nothing to switch on: since 2026-08-19 every skill passes,
including the ones that are not folders here (`/code-review`, `/simplify`,
`/run`).

To take some of that away, `config\skills.ini` NARROWS it:

```ini
[skills]
allowed = status, watch, goal   # only these
# allowed = none                # no slash commands at all
```

A slash outside a list you set delivers nothing and says so. Guests' slashes
are always plain words, whatever the file says — the gate that matters is who
is asking. Live within a minute of editing, shown in `!config`.

Skills worth allowing, all shipped: `goal` (hand over an objective — it gets
decomposed into checkable done-conditions and reported against them with
receipts), `backup` (pack the workspace + verified copy to your backup
folder), `release` (re-cut the rolling release, suites and all), `brief`
(fleet briefing: needs-you → moved today → running → open), the fleet verbs
(`new-project`, `spawn-session`, `archive-project`), and Claude Code's own
`code-review`, `simplify`, `security-review` and `run` for working a
project's diff from your phone.

## 9. Routines, reminders, schedules

Say it once, in words, to the orchestrator:

- *"check my gmail every hour on weekdays during work hours"*
- *"remind me tomorrow at 09:00 to review the PR"*
- *"every Monday 08:00, summarize last week's commits in #recipe-app → #general"*

Omnius creates the routine and echoes the parsed schedule plus the next three
fire times — read them; that's how a misparse gets caught. Routines are
machine-stamped (a restored backup never double-fires; `!cron adopt all` claims
them on a new PC), missed runs are counted and complained about **once**, and a
routine that finds nothing to say says nothing.

## 10. The daybook (notes & tasks)

- Web UI at `localhost:5111` — plain markdown files are the database; the
  **Today** page replays any day: your notes plus every commit and desk that
  worked that day.
- From Discord: write into `#daybook` — *"note: dentist moved to Tuesday"*,
  *"task: renew the domain"*. Questions *about* your notes are answered, not
  stored.
- The daily **morning briefing** arrives in `#daybook` after 07:00: open tasks
  + today's notes, short, no repeats of yesterday's unchanged items.

## 11. The tool desks

Each ships with a README in `tools\<name>\`; the fleet uses them when the task
calls for it — you can also address the desks directly:

| Ask | What happens |
|---|---|
| Forward an **email** task (one channel per account, named after the address — `you@example.com` → `#you-example`) | IMAP/SMTP and Microsoft Graph behind one contract. **Mail you asked for is sent and reported back** (a standing permission covers `mail.py send`; every send is audited to `state\logs\email.log`). **Mail the desk thought of itself — a new recipient, an unrequested follow-up, anything bulk — is drafted and shown to you first** |
| Drop a **recording** in `#transcribe` | Detached zero-token job: transcript + key frames + summary land back in the channel |
| *"read this PDF / invoice"* | Local text extraction; OCR fallback for scans (Mistral key); invoices come back as fields, checksum-validated |
| *"watch this video"* / `/watch <url>` | Download, frames, transcript (captions or local Whisper) — then ask it anything about the content |
| *"crawl these docs pages"* | Playwright for public pages. **Behind a login:** either the Claude Chrome extension on your real browser (you signed in by hand — safest, nothing scripted), or a site registered in `config\websites.ini` where `weblogin.py` signs in *for* the browser without the desk ever seeing the password — [docs/WEB.md](WEB.md) |
| A site asks for a **6-digit code** | It appears in your channel: *"`ionos` wants a 6-digit code"*. Reply with the six digits, nothing else. They're passed straight to the waiting login, used once, never stored, never transcribed — and an unanswered ask expires in 2 minutes rather than retrying into a lockout |
| *"render a video of…"* | Remotion project scaffold — programmatic video |
| `!screen`, `!desktop <verb>` | See and drive the desktop remotely — the phone-to-PC escape hatch |

## 12. Letting someone in — Discord guests, or Telegram

Two ways, for two different people.

**They have a Discord account** → `config\guests.ini`. They join your server and
you list them against an explicit set of channels — **as many as you like**,
space-separated. Mail only: control verbs, permission answers and takeover
answers stay yours. This is the easier option whenever they will accept a
Discord account.

**They don't, and shouldn't need one** → `config\telegram.ini`. They write to a
Telegram bot; their message lands in **one** channel of yours as
`**antonio** (telegram): …`; the desk answers; the answer arrives back on their
phone. Text, photos and voice notes (transcribed on the way in).

```ini
[chat.antonio]
telegram_username = antonio_h        # their @handle
discord_channel   = 141592653589793  # exactly one
visibility        = own              # own = their messages + the answers to them
                                     # all = everything in that channel
```

Two lines and a choice. **You don't name a desk** — the channel already has one
(`#web` in `my-project` *is* the `my-project.web` session), and their message
follows the same map yours does. Add `desk = …` only to override that, or for a
channel the fleet maps to nothing.

Then `TELEGRAM_BOT_TOKEN=` in `.env` (from **@BotFather**). **Nothing else to
run**: the watchdog notices the config within a minute, starts the bridge, and
restarts it if it dies or goes quiet.

One bot covers everyone — one `[chat.…]` block per person, naming the channel
they reach. **One person reaches exactly one channel**, and that is Telegram's
doing rather than a rule: their whole side is a single chat with the bot, so a
second channel would make every message they send ambiguous. Name someone twice
and only the first block is kept, with the duplicate reported in `!config`.
Somebody who needs several channels wants a Discord guest.

Two people **can** share one channel, but then both need `all`: `own` is refused
there, because a reply to one of them is an ordinary message to the other and
the promise could not be kept.

**They have to press Start first.** A Telegram bot cannot open a conversation:
send them the bot link and they tap Start. If they are in the config already,
Omnius answers *"Connected."* and says so in their channel. If they are not,
they get silence — and **you** get a line in `#alerts` with their id and the
block to paste.

What they can do: write, and be answered. What they cannot: reach any other
channel, use any `!verb` or `/skill`, or answer a permission, takeover, gate or
2FA prompt. Those are owner-only in the watchdog itself, so no setting here can
loosen them. And under `own` they never see the fleet's own traffic in that
channel — permission asks, 2FA prompts, delegation mirrors — only the answers to
what they themselves said. Full contract:
[tools/telegram/README.md](../tools/telegram/README.md).

## 13. Memory: what it knows and how to correct it

- **Shared layer** (`memory\shared\`) — read by every desk: who you are, how
  you like to work. Say *"remember: I prefer X"* to any desk and it lands here
  or in the right project.
- **Per-project memory** — the desks' working knowledge: briefs, decisions,
  interface notes siblings build against. Readable across projects on purpose.
- **Corrections:** *"forget that"*, *"that's wrong, it's actually Y"* — memory
  files are edited, not appended forever. Facts proven wrong get deleted.
- **The bus remembers conversations:** every envelope in and reply out is
  transcribed per desk (`state\transcripts\`). *"What did I tell you about the
  invoice flow last week?"* works — the desk greps its own transcript.
- Your instance's memory is **yours**: it travels in backups, never in this
  repo, and a fresh install starts from a clean seed.

## 14. Machine ops

- **Start / stop:** the desktop icon (or `start-omnius.bat`) brings everything
  up; `stop-omnius.bat` genuinely stops it (`-All` closes desks too). Services
  self-heal: back ≤60 s after a crash, back after every reboot, and no desk
  window opens at boot.
- **Update:** say **`!update`** in any channel — it fetches, shows you what's
  new, and `!update go` applies it: **rebase** → full test suite (**a red suite
  rolls the update back**) → hooks/permissions re-stamped → self-reload.
  Your files never move; everything personal is gitignored. **Changes you made
  to the code are kept**: your commits are replayed on top of the new release,
  so an instance you have customised still updates. Two other doors to the same
  path, for when Discord is not enough. The reliable one: open PowerShell **in
  your Omnius folder** and run `irm
  https://raw.githubusercontent.com/timoinglin/omnius-agent/main/update.ps1 |
  iex` — from that folder it cannot fail to find the install. Or
  `powershell -File update.ps1` there. Both repair a machine whose watchdog is
  too old or too broken to update itself ([UPDATING.md](UPDATING.md)). If your edit and the new
  release touch the same lines, the update stops, names those files, and leaves
  the instance exactly as it was. (`git pull` +
  `!reload` at the desk still works.) Zip installs attach themselves to the
  repo at install time, so this works on every instance. New config keys
  arrive commented in `config\*.example.ini` — copy what you want.
  The watchdog also checks **at startup and once a day**: new commits on
  origin get announced in `#omnius` with the `!update go` hint — announced,
  never applied, and the same news is never repeated.
- **Backups:** `pack.bat` builds one zip (memory, projects, notes, media —
  secrets excluded) next to the workspace and the heartbeat nags if the backup
  folder goes stale. **Moving machines** = unzip, `install.bat`, `!cron adopt
  all`. The instance wakes up knowing everything it knew.
- **Several machines:** each runs its own Omnius against its own Discord
  server; your phone drives them all. Every claim and `!status` names its
  machine.
- **Publishing your own build:** `release.bat` re-cuts the rolling GitHub
  release from pushed green `main` — suites, leak audit and a shipped-installer
  probe gate it; it refuses rather than ships doubt.

## 15. Omnius improves Omnius

Friction is a work order: *"Omnius, add a `!weather` command to yourself"*,
*"make the briefing shorter"*, *"stop asking about X"*. The orchestrator edits
its own code or skills, runs the 1,300+-check suite, and commits — every
self-improvement is a reviewable commit. `!reload` puts watchdog changes live.

**Your instance is yours to change, and updating will not undo it.** `!update`
**rebases** your commits onto each new release, so an Omnius you have customised
keeps its changes and still updates. If your edit and the release touch the same
lines, the update stops, names those files, and leaves the instance exactly as
it was — nothing is lost, and nothing is decided behind your back.

The one thing that differs between instances is **pushing**. Ask yours:

```
python toolsepo_access.py
```

- **`maintainer`** — this machine's git account has write access to the repo it
  updates from, so it may push. That is the instance the project is published
  from; the same person's second machine says the same thing, because the
  question is asked of git, not of a setting.
- **`user`** — everybody else, and nothing is wrong with it. Commit as much as
  you like; your work is replayed on every update. If a change is worth sharing
  upstream, send it to whoever publishes the repo rather than trying to push.

Desks read this too (`CLAUDE.md`), so a desk on a `user` instance improves the
code and commits it locally instead of trying to push and hitting a wall.

## 16. When something looks wrong

Before blaming a desk, you can **prove the whole fleet is sane** in one command — it checks, per desk, that no permission prompt can hang it, that its hooks are wired to this machine, that escalation to Discord is live, and that the watchdog's self-recovery is in place:

```
python tools\discord\desk_audit.py
```

It reports and never edits: exit 0 means every desk is green, exit 1 names the desk and the invariant it breaks.

| Symptom | Do |
|---|---|
| A desk is quiet | `!status` — it says queued/working/stalled per desk. A working desk that's merely slow announces itself after a while |
| Really stuck | `!restart` in its channel — the queue survives; the desk re-reads it fresh |
| Everything is quiet | The watchdog exits on purpose if it can't reach Discord and the task revives it — check `state\logs\watchdog.log` at the machine |
| A permission ask timed out | Re-send the instruction; the ask re-arms. Answers only count while the 🔐 ask is open |
| A run did work but said nothing | The turn-end guard posts *"changed files but reported nothing"* with the file names — the work is on disk, ask the desk to report |
| You want the paper trail | Per-desk transcripts (`state\transcripts\`), per-run logs (`state\logs\runs\`), and the chat itself — already threaded per project |

**The one rule that keeps the fleet sane:** one desk, one brain. Never point
two live sessions at the same folder — the claims system warns you if you try.
