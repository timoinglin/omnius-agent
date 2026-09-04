# Web work — a site registry, scripted login, and 2FA codes over Discord

> **Status: built 2026-08-17.** For an instance whose desks drive a lot of websites (Antonio's
> shape). Three pieces, one rule underneath all of them: **a Claude session never sees a password.**
> Companion to [PERMISSIONS.md](PERMISSIONS.md) (the relay this reuses) and the `playwright`/Chrome
> tooling it drives.

## The rule that shapes everything

A password in a desk's context is a password in its transcript, its logs, and its `#alerts` on the
next unrelated error. So the model orchestrates and a **tool** touches the secret — exactly the
split that lets the email desk send mail without ever holding the mailbox password. Three
consequences:

- Credentials live in `.env` (which desks are *denied from reading*), named by a key in a config
  file. The config file is just a pointer and metadata; it carries no secret.
- `tools\playwright\weblogin.py` reads that secret itself, logs the browser in, and saves a
  **reusable session** — after which the desk drives an already-authenticated page and no secret is
  touched again.
- A **one-time 2FA code** is different in kind: it is a 30-second value the owner reads off his own
  phone. Relaying it is him doing the 2FA, the desk as hands — so it travels the bus, but it is
  never delivered as mail and never transcribed.

## W1 — the site registry (`config\websites.ini`)

**Problem.** A desk driving a login needs to know the URL, which account, and where the password is
— without any of that being a secret in its context.

**Fix.** `[<site>]` blocks, read by `omnius_config.websites()`, reported by `!config`:

```ini
[ionos]
url = https://www.ionos.es
user = antonio@example.com
password_env = WEB_IONOS_PWD          ; the .env KEY, never the value
# optional selectors when a site's login form is not guessable:
# user_selector   = input#email
# pass_selector   = input#password
# submit_selector = button[type=submit]
# otp_selector    = input#otp
# success_selector = nav.account-menu   ; proves login worked
```

`password_env` names a key in the root `.env` (`WEB_IONOS_PWD=…`). `password_key` is accepted as an
alias for the same thing. The reader **fails closed** like `guests()`: a block with no `url` is
ignored and reported; a `password_env` naming a `.env` key that is empty shows `NOT SET` in
`!config`, never a value. A site with a `user` but no `password_env` is legal — it means "session
login only, I sign in by hand in the browser once" (the Chrome-extension path).

**Done when:** `websites()` parses the example, `!config` lists each site with its password as
`set`/`NOT SET`, and no tracked or shipped file ever holds a real password.

## W2 — `weblogin.py`: the tool logs in, the model never sees the secret

**Problem.** Automating a login means typing a password. The model must not be the thing typing it.

**Fix.** `python tools\playwright\weblogin.py <site>`:

1. Reads the site from the registry and the password from `.env` — in the tool, not the model.
2. Opens Chromium with the saved session state (`state\web\<site>.json`) if it exists; a valid
   session skips the login entirely.
3. If login is needed: navigates to `url`, fills `user`/`pass` (configured selectors or best-effort
   defaults), submits.
4. If a 2FA field appears: opens a **code request** (W3), waits for the owner's reply, fills it.
5. Saves the session state for reuse and prints the outcome — **never the password, never the code**.

The desk's part is one line: *"log in to ionos"* → run the tool → it returns "session saved" or
"needs your 2FA code (asked in the channel)". Selectors are best-effort; a site with an unusual form
gets `*_selector` entries in its registry block. **Honest limit:** scripted login trips bot
detection on some sites — those belong in the Chrome extension (Antonio signs in by hand once, the
session persists, the desk drives the logged-in page), where only W3 is needed.

**Two-step forms** (Zoho, Google, Microsoft) put the email on page one and the password on page
two. The tool fills the email, clicks `next_selector` (or presses Enter), waits, and only then looks
for the password field. Before 2026-09-04 it reached page two having typed nothing and reported
"still not signed in" — which is why a failure now also prints the final URL and what the page says.

**The limit that no selector fixes: corporate SSO.** If typing the email redirects to
`login.microsoftonline.com` or `accounts.google.com`, the site's own password is irrelevant — the
credential is a corporate account, the second factor is usually a *push to a phone* rather than six
digits W3 can relay, and Conditional Access commonly blocks automated browsers on principle. That
is not a bug to work around; it is the signal to make the site a **sign-in-by-hand** site (no
`password_env`, Chrome extension, session in the profile). Found the hard way on 2026-09-04: Zoho
People for a tenant federated to Entra ID.

**Done when:** with a valid saved session the tool reports logged-in without touching the password;
with a fresh session it drives the login and, on a 2FA wall, blocks on W3 rather than failing.

## W3 — 2FA codes over Discord (the relay, reused)

**Problem.** A login hits a 6-digit wall. The code is on the owner's phone, not the machine.

**Fix.** The permission relay wearing a different hat — a held **file**, never a blocked anything:

- The tool (or a desk driving the Chrome extension) writes `state\twofa\<id>.json`
  `{id, session, site, askedTs}` and polls for `state\twofa\<id>.code`.
- The watchdog posts the ask to that desk's channel: *"🔢 `<site>` wants a 6-digit code — reply
  with it."* It never echoes a code back.
- The owner replies with **exactly six digits**. `answer_twofa()` consumes it: writes the `.code`
  file, deletes the request, and **the digits are never delivered as mail and never transcribed** —
  a code is a 30-second secret and leaves no trace. Six digits collide with nothing else on the bus
  (control verbs start `!`, ok/no answers are words), so a bare code is unambiguous.
- Single use, `TWOFA_WAIT_SECONDS = 120`, **fail closed**: an unanswered ask times out and the tool
  reports "login needs your 2FA code" and stops — it never retries into an account lockout.

**Done when:** a pending 2FA ask consumes a 6-digit reply, the code reaches the tool, nothing about
it appears in the transcript, and an unanswered ask times out cleanly.

## W4 — the standing desk-stability review

Not a feature — a **check that runs**, on demand and honestly, answering Antonio's ask: *desks
reachable from Discord, with minimal (or no) permission prompts, that do not hang, are stable, and
self-recover.* `python tools\discord\desk_audit.py` verifies, per desk, the invariants that make
that true — every one already designed, this only proves it holds on this machine:

- **No prompt can hang a desk:** bare `Bash`/`PowerShell` allowed (so routine work never stops on a
  dialog), `.env` read denied, `AskUserQuestion` denied (it draws an unreachable menu).
- **Reachable + escalating:** hooks wired to this machine (`fix_hook_paths --check`), the permission
  relay registered so anything unlisted escalates to Discord instead of stalling.
- **Self-recovering:** the watchdog's failure ledger backs off and alerts rather than looping; a
  dead bridge's tree is reaped (2026-08-17); no session-side daemon to go deaf.

It prints a per-desk table and a pass/fail line, and reports what is off rather than fixing it — a
review names things; the owner decides.

**Done when:** `desk_audit.py` exits 0 with every desk green, or names exactly which desk breaks
which invariant.
