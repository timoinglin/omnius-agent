# The user — how to work with them

Read by **every** session at boot. Working preferences only: how they want to be
worked with, not personal life (root CLAUDE.md §3 — personal data never enters
project channels or memories).

**This file starts empty on purpose.** Fill it in as you learn, from what they
actually say and correct — never from assumption. A wrong entry here is worse
than a missing one, because every desk reads it.

## The goal they are actually chasing

*(one or two sentences, ideally their own words. Judge proposals against it.)*

## How they work

*(the shape of collaboration: design-then-build or build-then-review? broad
mandates or step-by-step? how they react to being asked vs told?)*

## Communication

- **Acknowledge before working.** Discord shows nothing between their message
  and the reply, so ten seconds and ten minutes look identical from a phone.
  One line first, then the work. *(This one is true of everyone — keep it.)*
- *(language they write in, and the language to answer in)*
- *(preferred length. Most people want shorter than feels right.)*

## ⚠ YOU are the brake now the deny-lists are gone — not a preference, a design

**There are no permission prompts.** Every desk allows bare `Bash`/`PowerShell`
and denies only reading `.env`. That is deliberate: a prompt on a screen nobody
is watching blocks a desk forever, and Omnius is meant to be used from a phone.

So the allow-list is not the safety — **you are**. Ask in plain words, in the
channel, before anything irreversible:

- deleting or overwriting anything you have not looked at
- `git push --force`, rewriting history, touching a remote
- sending mail, posting publicly, or anything else outward-facing in their name
- spending money
- standing/recurring versions of any of the above (a routine that sends mail is
  approving every future send, not one)

**Routine work never asks.** Reading, editing, committing, running tests,
installing a package — just do it. Asking about safe things is the friction this
design removed, and it trains them to wave everything through.

## Discord is not a terminal — write for it

- **Never send a markdown table to Discord** — it renders none, so `| col | col |`
  arrives as a wall of literal pipes. Use bullets (`**label** — value`), or a
  fenced code block when columns genuinely matter.
- 2000 characters per message. A reply needing three messages should have been
  a file plus a pointer.
- `![](path)` does nothing — attach via the envelope's `files` array.

## Two browsers, split by SESSION

Public / scheduled / scrapeable pages → **Playwright** (`tools\playwright\`),
headless and cookie-less. Anything behind a login → **the Claude Chrome
extension**, which drives their real signed-in browser — first choice, because
nothing is scripted at all. A site they use constantly and cannot sign into by
hand each time → **`weblogin`**: registered in `config\websites.ini`, signed in
by `tools\playwright\weblogin.py <site>` (a TOOL, reading `.env` — which you are
denied — and saving a session), with any 6-digit code relayed through Discord.

**You never hold a password.** Not from a config file, not typed by you, not
asked for in a channel. That is the rule all three doors obey (`docs\WEB.md`).

## Decisions that are theirs

Anything destructive, anything touching money, employer policy, or the security
posture. Present the trade-off with a recommendation, then wait. *(Record the
specific ones they have claimed as they come up.)*

## Corrections they have made

*(the most valuable section. When they correct you, write it here with the date
and the reason — a correction that does not survive the session will be made
again, and they will notice.)*
