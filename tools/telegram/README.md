# tools\telegram — invite people who have no Discord account

Someone who has no Discord account — a client, a colleague — writes to a Telegram bot.
Their message appears in **one** of your channels, attributed to them. The desk answers as
it answers anyone. The answer arrives back in Telegram.

That is the whole feature. One bot serves everyone you invite; each of them reaches exactly
one channel, and gets no Discord account, no desk and no control verbs.

```powershell
python tools\telegram\bridge.py            # run it (the watchdog does this for you)
python tools\telegram\bridge.py --check    # who is invited, is the token set
python tools\telegram\bridge.py --once     # one pass, then exit
```

## Contract (stable regardless of engine)

| | |
|---|---|
| **Reads** | `config\telegram.ini` (the invite list), `TELEGRAM_BOT_TOKEN` in `.env` |
| **Telegram → Discord** | posts `**<label>** (telegram): <text>` with attachments, then writes `state\inbox\<desk>\<discord-message-id>.json` |
| **Discord → Telegram** | mirrors new messages in that channel, prefixed with the author's name |
| **Writes** | `state\telegram\` (cursors, beacon, and the watchdog-facing copy of the lock), `media\inbox\YYYY-MM\`, `media\sent\YYYY-MM\` (only with `media_out = 1`), `state\transcripts\<desk>\`, `state\logs\telegram.log`, and `%LOCALAPPDATA%\omnius\telegram-<hash>.lock` — the one-bridge-per-token lock, machine-wide by design |
| **Never writes** | `state\outbox\` — the watchdog owns replies; the bridge mirrors the *channel*, which already contains them |
| **Exit codes** | Only `--once` and `--check` exit at all; the service form runs until stopped. `0` covers everything ordinary — nothing configured, a second bridge on standby, even a pass whose Telegram calls all failed (that shows in the log and in the beacon's `state`, never in an exit code). `1` is a `--once` run that could not reach Discord, or a fatal Telegram error |

**The envelope id is the Discord message id.** Not a `tg-…` stem: `oldest_envelope` sorts the
inbox lexicographically, so a prefixed name would queue behind every Discord snowflake forever.
Sharing the id also means the Discord message, the envelope, the transcript and `!trace` are one
story.

## Why message delivery needed no change to the watchdog

The watchdog does own the bridge's *lifecycle* (see "Running it"). What needed no change is the
**delivery path**, thanks to three properties that were already true and are worth not breaking:

- **A message posted with the bot token is skipped at the watchdog's first branch**
  (`author.bot` → `skip-bot`), before any envelope or run. So the bridge can post into Discord
  and write the envelope itself, with no double delivery.
- **A file dropped in `state\inbox\<desk>\` starts that desk within ~3 s** (`ensure_runners`).
  No signal, no import, no API.
- **Desks decide who they are talking to from the envelope's `from` label**, not from a Discord
  account (`is_human_sender`). Anything that is not a fleet tag is already treated as a guest —
  so an invited Telegram user gets correct guest treatment without a line of new logic, and
  `config\guests.ini` stays what it is: the gate for people who *do* have Discord accounts.

## Config

Copy `config\telegram.example.ini` → `config\telegram.ini`. With no file, the bridge idles and
nobody is invited.

```ini
[telegram]
token_env = TELEGRAM_BOT_TOKEN      # the .env KEY. The token is never in config.

[chat.antonio]
telegram_username = antonio_h       # their @handle - or telegram_user_id = 123456789
discord_channel   = 141592653589793 # id is safest; #name works too
visibility        = own             # own | all  (default: own)
media_out         = 0               # 1 to send real files back, not just names
# desk = my-project.web             # only to OVERRIDE the channel's own desk
```

**Invite by handle, not by id.** Nobody knows their own Telegram user id, and a bot cannot look
one up: the Bot API resolves `@name` for public channels and supergroups only, never for a
private user — an id reaches a bot in a message or not at all. So the bridge takes the handle,
and **pins the numeric id behind it** the first time that person writes. After that the handle
is irrelevant: if they change it, or somebody else registers the one they dropped, the invite
stays with the person who was meant.

If they have no handle, invite nobody and let them write once: an unlisted sender is answered
with silence, but the **owner** is told in `#alerts` — id, handle, and the config block to
paste. Reading `state\logs\` to set up an invite was the least friendly step in the system.

**There is no desk to name.** Every channel already belongs to one — `#web` inside the
`my-project` category *is* the `my-project.web` session — and the bridge resolves it through
`watchdog.build_map`, the same map every message the owner types goes through (imported, not
reimplemented: two copies of a mapping is how they drift). `desk =` exists only to override
that pairing, or for a channel the fleet maps to nothing. A channel that answers to no desk
relays **nothing** and says so in the log, rather than posting a message that looks handled
while nothing was ever going to answer it.

`visibility` is the one that deserves a thought:

- **`own`** — their own messages, and the desk's answers **to them**: the replies that follow
  their message until somebody else speaks. Not everything the bot posts — a desk channel also
  carries permission asks, 2FA prompts and delegation mirrors, and those are fleet business.
  This is the default, because the safe reading of "invite someone to a channel" is not "give
  them your history".
- **`all`** — everything posted there, yours included. A shared room.

Get the token from [@BotFather](https://t.me/botfather), put it in `.env` as
`TELEGRAM_BOT_TOKEN=`, and turn **off** the bot's privacy mode only if you ever move to group
chats (v1 is one-to-one).

## Safety, none of it optional

- **The invite list fails closed.** No file, no block, a malformed id or handle, a missing
  channel, an unknown `visibility`, a duplicate person, `own` in a shared room, or a label that
  collides with a fleet sender → that entry is ignored and reported by `!config`. Nobody is
  ever admitted by a typo.
- **An unlisted sender is ignored in silence.** No refusal reaches them: telling a stranger
  "you may not write here" confirms something is here and draws them a map. The *owner* is told
  in `#alerts` instead — once per id, capped, without quoting their message, and with the
  handle stripped to `[a-z0-9_]` so nothing in it can mention anyone.
- **They cannot use control verbs, slash commands, or answer any prompt.** `!status`, `!kill`,
  `/goal`, permission asks, takeover asks, gate answers, 2FA codes — every one of those lives
  inside `if sender == "owner":` in the watchdog. This bridge could not grant them if it tried.
- **One person, one channel.** There is no wildcard and no second channel per person.
- **The token never reaches a log line** — every line is filtered through the same pattern that
  matches a bot token, and Discord posts go through `api.redact` like everything else.
- **The first mirror pass starts from now**, never from the channel's history. Being invited
  should not dump your last month into someone's phone.
- **A failed mirror retries in order** rather than advancing past it, and after three passes it
  gives up on that one message **and says so in the log**. A dropped message nobody is told about
  is the worst of both designs.

## What this is NOT (v1)

Not a general multi-messenger abstraction. No Telegram-side commands (beyond `/start`). No
Telegram group chats. No second channel for one person. `media_out` is off, so Discord →
Telegram carries text and the *names* of attachments; flip it to `1` for the full round trip.

If a second platform is ever wanted the same file shape works — and only then is a `via:` field
on outbox replies worth adding.

## How many people, how many bots

**One bot serves everybody.** Add a `[chat.<label>]` block per person, each naming its own
channel. Nothing else is required — the channel supplies the desk.

**One person reaches one channel.** Named in two blocks, only the first (alphabetically, by
label) is kept; the other is ignored and reported by `!config`, so a person cannot quietly
end up in a room you did not mean.

**Two people can share one channel**, but only with `visibility = all` for both. `own` is
refused there and said out loud, because it cannot be kept: the desk's reply to one of them is
an ordinary bot message to the other, indistinguishable from a reply to anyone. A privacy
promise that the code cannot honour is refused rather than quietly broken.

## Running it — nothing to run

The **watchdog starts it**. Write `config\telegram.ini` and within a minute the bridge is up;
it is restarted if it dies, and restarted if it stops stamping its beacon for 15 minutes. A
revoked token is a *different* failure, handled differently: the process keeps stamping, with
`state: failing`, so nothing restarts it — `autostart.ps1 -Action status` reports it as RUNNING
BUT FAILING, because a restart would not fix a token. There is no
scheduled task to register: inviting someone would then mean running a command on the machine,
and this fleet is driven from a phone.

```powershell
powershell -File tools\discord\autostart.ps1 -Action status   # reports the bridge too
```

It also refuses to run twice. Telegram allows a single `getUpdates` consumer per bot, so a
second poller would split the mail between them; the bridge takes a pid-validated lock and a
second copy goes to **standby** — alive, polling nothing, stamping `state: standby` — so it
takes over the moment the first one stops. It deliberately does not exit: to the watchdog an
exit is indistinguishable from a crash, and would earn a fresh copy every minute.
