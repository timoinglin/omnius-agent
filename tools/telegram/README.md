# tools\telegram — invite one person into one channel

Someone who has no Discord account — a client, a colleague — writes to a Telegram bot.
Their message appears in **one** of your channels, attributed to them. The desk answers as
it answers anyone. The answer arrives back in Telegram.

That is the whole feature. They get no Discord account, no desk, no control verbs, and no
second channel.

```powershell
python tools\telegram\bridge.py            # run it (this is what the service runs)
python tools\telegram\bridge.py --check    # who is invited, is the token set
python tools\telegram\bridge.py --once     # one pass, then exit
```

## Contract (stable regardless of engine)

| | |
|---|---|
| **Reads** | `config\telegram.ini` (the invite list), `TELEGRAM_BOT_TOKEN` in `.env` |
| **Telegram → Discord** | posts `**<label>** (telegram): <text>` with attachments, then writes `state\inbox\<desk>\<discord-message-id>.json` |
| **Discord → Telegram** | mirrors new messages in that channel, prefixed with the author's name |
| **Writes** | `state\telegram\` (its own cursors + beacon), `media\inbox\YYYY-MM\`, `state\transcripts\<desk>\`, `state\logs\telegram.log` |
| **Never writes** | `state\outbox\` — the watchdog owns replies; the bridge mirrors the *channel*, which already contains them |
| **Exit codes** | `0` fine (including "nothing configured"), `1` a Telegram or Discord failure worth reading |

**The envelope id is the Discord message id.** Not a `tg-…` stem: `oldest_envelope` sorts the
inbox lexicographically, so a prefixed name would queue behind every Discord snowflake forever.
Sharing the id also means the Discord message, the envelope, the transcript and `!trace` are one
story.

## Why this needed no change to the watchdog

Three properties that were already true, and are worth not breaking:

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
token_env = TELEGRAM_BOT_TOKEN     # the .env KEY. The token is never in config.

[chat.antonio]
telegram_user_id = 123456789       # theirs, from @userinfobot
discord_channel  = 141592653589793 # id is safest; #name works too
desk             = my-project.web  # who answers them
visibility       = own             # own | all  (default: own)
media_out        = 0               # 1 to send real files back, not just names
```

`visibility` is the one that deserves a thought:

- **`own`** — they see their own messages and Omnius's replies. Your other traffic in that
  channel stays yours. This is the default because the safe reading of "invite someone to a
  channel" is not "give them your history".
- **`all`** — everything posted there, yours included. A shared room.

Get the token from [@BotFather](https://t.me/botfather), put it in `.env` as
`TELEGRAM_BOT_TOKEN=`, and turn **off** the bot's privacy mode only if you ever move to group
chats (v1 is one-to-one).

## Safety, none of it optional

- **The invite list fails closed.** No file, no block, a non-numeric id, a missing channel or
  desk, an unknown `visibility`, or a label that collides with a fleet sender → that entry is
  ignored and reported by `!config`. Nobody is ever admitted by a typo.
- **An unlisted Telegram id is ignored in silence.** No refusal is sent back. Telling a stranger
  "you may not write here" confirms something is here and draws them a map. The id *is* written
  to the log, which is how you add someone: have them message the bot, read the id, paste it.
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

Not a general multi-messenger abstraction. No Telegram-side commands. No group chats. No more
than one person per channel, or one channel per person. `media_out` is off, so Discord →
Telegram carries text and the *names* of attachments; flip it to `1` for the full round trip.

If a second platform is ever wanted the same file shape works — and only then is a `via:` field
on outbox replies worth adding.

## Running it

`tools\discord\autostart.ps1 -Action repair` registers the bridge as a scheduled task, next to
the watchdog and the daybook — but **only if `config\telegram.ini` exists**. Nothing is
registered for a feature nobody has configured.

```powershell
python tools\discord\autostart.ps1 -Action status   # is it alive
```

The bridge stamps `state\telegram\beacon.json` every pass, which is what the health probe reads.
