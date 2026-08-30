# Slash commands from Discord — what travels, what cannot, what to send instead

Owner asked on 2026-08-29: *"look which commands we should add to discord chat
options, e.g. we already have /goal. but i want to have the /btw, /design, and
more useful ones."* This is the answer, and the reason each line falls where it
does. Mechanism: `docs\DELEGATION.md` D6.

## The gate is already open — nothing needs "adding"

There is no enrolment list to join. `config\skills.ini` **narrows** the
pass-through and does not normally exist; with it absent, **every** `/word` in
owner mail is stamped and delivered (D6, opened 2026-08-19). The desk then does
what `/omnius` §3 tells it: *invoke that skill now with the Skill tool.*

So what decides whether `/x` works is not the transport. It is one question:

> **Is `x` a SKILL, or a thing the terminal draws?**

A skill is a folder of instructions — a headless run can follow it. A terminal
command is UI code in the CLI — a headless run has no UI to drive, and no
amount of config changes that.

## Works from Discord today

**Ours — travel in this repo, so both PCs have them after `!update`:**

- `/omnius` — no-op alias for plain mail (D6); never stamped, never recurses.
- `/status` · `/brief` — fleet state, phone-shaped.
- `/new-project` · `/spawn-session` · `/archive-project` — the fleet verbs.
- `/goal` — hand over an objective, not a task. **Shadows** Claude Code's own
  `/goal`; ours wins because `.claude\skills\` is searched first, and ours is
  the one he means.
- `/backup` · `/release` — `/release` pushes, so it is maintainer-only and says
  so itself.
- `/watch` — a video URL or path, answered as content.
- `/btw` — a side question that does not become the desk's work. Ours, because
  the built-in is terminal-only; it does the half that survives the trip.

**Claude Code's own skills — these ship with the CLI, not with this repo, so
they exist on a PC only if its CLI is current. `!update` does not install them.**

- `/design` — the one he asked for. Publishes an Artifact and answers with a
  **URL**, which is the right shape for a phone: he opens it, edits the design
  by hand, saves. Genuinely useful remote.
- `/code-review` · `/security-review` · `/simplify` — text in, text out.
  `/code-review ultra` is billed and user-triggered; a desk cannot launch it.
- `/run` — launches the project's app **on the machine**. Fine when he is at
  the PC, pointless when he is not; a desk should say which it thinks this is.
- `/dataviz` · `/artifact-design` · `/artifact-capabilities` — guidance a desk
  loads while building something. Sending one alone accomplishes nothing.
- `/init` · `/update-config` · `/claude-api` · `/workflow-authoring` ·
  `/fewer-permission-prompts` · `/keybindings-help` · `/claude-in-chrome` —
  they work; they are rarely what a phone message means.

**Two that work but should not be used from Discord:**

- `/loop` — runs a prompt *while the session stays open*. A headless run ends
  at the end of its turn, so the loop dies with it. Use the counted work loop
  (`schedule.py --loop`, D5) — same idea, survives the run, has a budget.
- `/schedule` — creates **cloud** agents, a separate system that does not know
  about desks, inboxes or channels. Routines belong to `schedule.py` and
  `!cron`, which fire into this fleet.

## Cannot work from Discord, and why

All of these are the terminal drawing something. A desk invoking them can only
report that it cannot:

`/btw`\* · `/focus` · `/context` · `/usage` · `/cost` · `/rewind` · `/clear` ·
`/compact` · `/resume` · `/branch` · `/fork` · `/config` · `/settings` ·
`/theme` · `/color` · `/diff` · `/copy` · `/export` · `/keybindings` ·
`/permissions` · `/hooks` · `/memory` · `/doctor` · `/debug` · `/login` ·
`/logout` · `/exit` · `/ide` · `/desktop` · `/teleport` · `/remote-control` ·
`/agents` · `/plugin` · `/mcp` · `/add-dir` · `/cd` · `/model` · `/effort` ·
`/fast` · `/autocompact` · `/tasks` · `/insights` · `/powerup` · `/mobile` ·
`/radio` · `/web` · `/passes` · `/upgrade` · `/bug` · `/feedback`

\* the built-in. `/btw` from Discord reaches **our** skill instead, which is the
point of writing one.

**Where a `!` verb already does the useful part** — prefer these; the watchdog
answers them itself, with no desk spawned and no model call:

- `/model` → **`!model`** · `/status` → **`!status`** · `/config` → **`!config`**
- `/tasks`, `/rewind` → **`!trace`** (what a run actually did)
- `/clear`, `/exit` → **`!kill`** / **`!restart`** / **`!stop`**
- scheduling → **`!cron`** · updating → **`!update`** · screen → **`!screen`**

Asked for one that cannot travel, a desk should answer with the `!` verb or
skill that does the useful half — not with a failure. That rule lives in
`.claude\skills\btw\SKILL.md`, because that is the skill most likely to be
holding the question when it comes up.

## Adding one later

Write it as a skill in `.claude\skills\<name>\SKILL.md` and it is reachable
from Discord the moment it lands on the desk — no watchdog change, no config.
That is the whole extension mechanism, and it is why this document lists
judgement rather than plumbing.
