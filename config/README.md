# config\ — everything you can change, in one place

Omnius reads its settings from this folder. **You never have to edit anything
here**: every key has a working default, and a missing file behaves exactly
like an untouched one. Change something only when you want it different.

## The three rules

**1 · Precedence: environment variable → this folder → built-in default.**

So a one-off still wins over the file, without editing anything:

```
set NOTES_HOST=0.0.0.0 && python daybook\app.py
```

**2 · Secrets do NOT live here.** Passwords, API keys and tokens live in the
root `.env`, which is gitignored and never leaves this machine. A config file
names the `.env` key that holds the credential — it never holds the value:

```ini
[account.work]
user        = someone@example.com
# The password is NOT here. This names the .env key that holds it.
password_env = EMAIL_WORK_PASSWORD
```

That split is what lets this folder be readable, diffable and copied between
machines while credentials stay in one place.

**3 · A broken file can never take Omnius down.** Anything unreadable or
misspelled is *reported* and then ignored, and the default is used. Ask
`!config` in Discord to see what is actually in effect and why.

**4 · Never indent a setting.** In INI, a line starting with a space *continues
the value above it* — so

```ini
visibility = own
 media_out = 0      ← one stray space
```

is a single setting whose value is `own\nmedia_out = 0`, not two. That cost a
real invite on 2026-08-19: the value became invalid, the entry was refused, and
nothing in the file looked wrong. A continuation shaped like `key = value` is
now put back where it belongs **and named in `!config`** — but keep settings
flush left and it never arises.

## What is in here

| File | What it configures |
|---|---|
| `omnius.ini` | machine label, heartbeat interval, Discord gateway on/off, desk-mail delegation budgets |
| `notes.ini` | the notes web server — folder, port, bind address |
| `fleet.json` | which model and effort each desk runs with |
| `guests.ini` | people who are **not you** and may write to one desk (see `guests.example.ini`) |
| `skills.ini` | slash commands your Discord mail may fire on a desk (see `skills.example.ini`) — closed until you fill it |
| `websites.ini` | sites your desks log into: url, user, and the **name** of the `.env` key holding the password (see `websites.example.ini` and `docs\WEB.md`) — never the password itself |
| `email.ini` | mail accounts (see `email.example.ini`) — IMAP/SMTP **and** Microsoft Graph; one Discord channel per account |
| `telegram.ini` | people with **no Discord account** invited into exactly one channel through a Telegram bot (see `telegram.example.ini` and `tools\telegram\README.md`) — no file, nobody invited |

Files ending in `.example.ini` are templates that ship with Omnius. Copy one to
the name without `.example` and edit it. Your real files stay out of git, so
your addresses and hostnames are never published — but they DO travel in your
personal backup zip, alongside `.env` and `media\`.

## Seeing what is in effect

```
python tools\omnius_config.py
```

or, from Discord, `!config`. Both print every setting, its value, and **where
that value came from** — env, file, or default. Secrets are shown only as
`set` / `NOT SET`; the values are never printed anywhere.
