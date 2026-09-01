# System status — read this first

Updated: *(fresh instance — nothing has happened yet)*. **Current state and open work only.** Everything durable lives in `topics\` — open those as needed, not by default (root CLAUDE.md §2).

> Keep this file small. When something here stops being *current*, move it into a topic file rather than letting this grow. The suite enforces a 9,000-character budget, and it exists because this file once reached 65 KB.

## Where we are

- **Fresh instance.** The Discord layer, the bus, desks, the bridge and the daybook are all built and travel with the code — see `topics\discord-fleet.md` for how they work and `docs\ARCHITECTURE.md` for why.
- **Nothing is proven on THIS machine yet.** First things worth doing, in order: `install.bat` (prerequisites, `.env`, hook paths, and what to call this agent — `config\omnius.ini` `[omnius] name`; only the address changes, the folder and the `/omnius` skill keep theirs), `start-omnius.bat`, then write a message in the agent's own channel (`#omnius` unless it was named something else) and confirm a desk answers.
- **Suites:** `tools\discord\test_watchdog.py`, `daybook\test_storage.py`, `tools\email\test_email.py`, `tools\documents\test_documents.py`, `tools\telegram\test_telegram.py` — all five should pass before you trust anything else. They print their own counts.
- **Shared tools that ship with this install** (`tools\<name>\`, each with a README — read it, don't guess): `whisper` (speech→text, local), `documents` (PDF text + OCR), `transcribe` (recordings → transcript + frames + summary, its own desk), `email` (IMAP/SMTP **and** Microsoft Graph), `playwright` (headless browsing + `weblogin`), `remotion` (video rendering), `telegram` (invite people who have no Discord account - one bot, one channel each — `config\telegram.ini`, off until you write it). **Behind a login: the Claude Chrome extension first (nothing scripted), or `weblogin.py` for a site registered in `config\websites.ini` — the TOOL reads the password from `.env`, never a desk. 6-digit codes are relayed through Discord. `docs\WEB.md`.**

## Repos

- **This install receives Omnius; it does not publish it.** Omnius is a public
  repo, and exactly one instance owns that remote. Yours is almost certainly not
  it — that is the normal, designed state, not a misconfiguration to fix. **Ask
  git, never guess:** `python tools\repo_access.py` answers `maintainer` or
  `user` by running `git push --dry-run`, and it never prompts.
- **You may commit here as much as you like.** `!update` **rebases your commits
  onto each release**, so local work survives updates instead of blocking them.
  What you may not do is push, and a rejection there is a wall, not a bug to
  solve. If a change is worth sharing upstream, say so to the owner rather than
  fighting the remote. (`/release` refuses on a `user` instance before it
  touches git at all.)
- *(record your own project repos here, and whether they are private, once
  decided — those are yours and have nothing to do with the above)*

## Fleet

- **One orchestrator, ever.** One desk per folder. Scale by splitting domains, never by duplicating the owner.
- Desks that exist on a fresh install: `orchestrator`, `daybook`, `tool.fleet`, `tool.transcribe`, `tool.email` *(add projects as they are created)*.
- **A desk is a series of one-shot headless runs owned by the watchdog** — not a terminal that stays armed. One run per desk at a time, `--continue` for continuity. There are no session-side inbox watchers and no claim heartbeats; both were deleted 2026-08-01 after they died at turn boundaries and invited duplicate orchestrators. **Never re-arm anything session-side.**
- Run defaults live in **`config\fleet.json`**: Opus 5 / xhigh, and no `--permission-mode`.
- **⚠ Permission prompts are OFF by design.** Every desk allows bare `Bash`/`PowerShell` and denies only reading `.env`. That is deliberate: a prompt on a screen nobody is watching blocks a desk forever. **So the model is the brake** — ask the owner in plain words before anything irreversible (deleting, force-pushing, sending mail in their name, spending money). Routine work must NOT ask. See `topics\permissions.md` and `shared\USER.md`.

## Standing rules

**How to work with this owner lives in [`memory\shared\USER.md`](../shared/USER.md)** — fill it in as you learn, and keep it in *shared* so every desk reads it.

## Open work, in the order it should be taken

1. Get `install.bat` clean on this machine (Python 3.10+, git, Claude Code; `pywinpty` and `psutil` are required, not optional).
2. Discord setup (`tools\discord\setup.ps1`) — bot, server, `.env`.
3. Prove the loop end to end: message in the agent's own channel (`#omnius` unless install was told another name) → a desk opens → it answers.
4. Reboot once and confirm the watchdog and daybook come back on their own, and that **no desk window opens at boot**.

## Loose ends worth remembering

*(nothing yet — this is where facts that are true of THIS machine go: paths, hardware, the owner's decisions)*
