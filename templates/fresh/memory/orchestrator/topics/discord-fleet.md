# Discord, desks and the fleet

How the bus is wired, who owns which desk, and the traps in between.
Blueprint: `docs\DISCORD.md` + `tools\discord\schema.json`.

## Architecture in one paragraph

The only always-on piece is the **watchdog** (`tools\discord\`, **Gateway websocket — pushed, not polled**, with a REST sweep every 60 s as the backstop). Claude sessions start **on demand**. Sessions never touch Discord — they use a local file bus (`state\inbox\` / `state\outbox\`), and the watchdog is the sole listener and the owner-allowlist chokepoint (`DISCORD_OWNER_ID`). Media: images read natively, audio → `tools\whisper\`, video → the `watch` skill. Registry → per-session **claim files** (`state\sessions\<id>.json`, pid liveness, self-healing after machine moves).

## Per-instance bot setup

A bot is created **per instance** and invited with either Administrator (`8`) or the minimal permission integer **`126032`**. **Message Content Intent must be on** or the watchdog sees empty messages. Default is one server per instance; a shared fleet server stays a Phase 5 candidate.
`tools\discord\setup.ps1` guides this and is called by install/start/wakeup whenever `.env` lacks Discord values; declining leaves the instance in local mode.

## Channel → desk map

Read from `watchdog.build_map()` — **not** from the schema, which is only part of the story.

| Channel | Desk |
|---|---|
| `#omnius` *or* `#orchestrator` | `orchestrator` (both names accepted deliberately) |
| `#daybook` | `daybook` |
| `#fleet-status` | `tool.fleet` |
| `#transcribe` | `tool.transcribe` |
| `#alerts` | **no session** |
| `📧 EMAIL` › `#<address>` | `tool.email` — **one channel per configured account**, derived from `config\email.ini` (the category's `channelsFrom`), named after the address with `@`/`.` flattened. TLD only on collision. |
| `📁 <project>` › `#general` | **`orchestrator`** |
| `📁 <project>` › `#<component>` | `<project>.<component>`, or unmapped + logged if the folder is missing |

- **⚠ TRAP: `!kill` in a project's `#general` stops the ORCHESTRATOR**, not the project — use the component's own channel.
- `#daybook` and `#fleet-status` have their **own desks** (2026-07-31) so notes and status never queue behind the orchestrator.
- **The door is `#omnius`** (renamed from `#orchestrator` 2026-07-31; id `<channel-id>` unchanged).
- `!killall` accepts **both** names on purpose: the watchdog maps by channel *name*, so a rename under old code would have unmapped the channel and cut the owner off.
- **Discord permits duplicate channel names** — check before any rename, or you get two rather than an error.
- **The watchdog rebuilds its channel map on a timer inside the poll loop** (`MAP_REFRESH_SECONDS`), not only at startup. Renames, additions and deletions are picked up automatically and a deleted channel does not strand the beacon.
- **⚠ But that refresh re-reads the GUILD, not the CODE.** Adding a *new* `#channel → session` branch to `build_map()` needs a **`!reload`** — a running watchdog imported the old function at startup and keeps it forever. **Symptom to recognise: a desk's replies land in `.refused` while everything about the channel looks correct** — the channel exists, the category is right, the desk writes a correct outbox file, and the watchdog renames it *"belongs to another session"* because its map still says `session=None`.
- Prefer restarting the **`Omnius Watchdog` scheduled task** over killing a pid — `service_runner.py` supervises it, so it comes back on its own, and the lock validates pid liveness so the replacement prunes a stale lock rather than exiting 3.

## Control commands — ELEVEN (the source of truth is `CONTROL_COMMANDS`)

`!status` · `!kill` · `!restart` · `!reload` · `!killall` · `!stop` · `!config` ·
`!cron` · `!model` · `!screen` · `!desktop`

Answered by the watchdog itself: **instant, no desk spawn, no tokens.**

- `!status` — every session `[on]`/`[off]` with its **model/effort**, flags session notes missing or ≥3 days old. Bare `opus/xhigh` = what that run launched on; `(parens)` = the config for its next run.
- `!kill` / `!restart` — act on **this channel's** desk (`target.session`), never on a session named as an argument. **`!restart sonnet low`** changes model/effort *and* cuts over in one command (persists, like `!model`).
- `!stop` — cancels a desk: queued mail to `state\dropped\` (kept, not deleted), state cleared, processes killed, and **survivors reported** rather than success claimed.
- `!reload` — re-execs the watchdog in place to pick up code edits; syntax-checks `watchdog.py`, `api.py`, `schedule.py` first and **refuses** rather than re-exec into code that cannot start.
- `!cron` — routines: list, `pause`/`resume`/`rm <id>`, `adopt <id|all>` after a machine move. Output is a **fenced code block**, because Discord renders no markdown tables.
- `!model` — this desk's model/effort. `!model sonnet [low]` · `!model effort low` · `!model reset`. Bare shows **what the live run is on**, what the config says, **where each value came from**, and flags when the two have diverged. Writes `desks.<id>` in `fleet.json`, so it **travels and persists**. Both are pinned at launch, so a change lands on the NEXT run — `!restart` (or `!restart sonnet low`) cuts over, keeping the conversation.
- `!config` — which capabilities have both a provider and a key.
- `!screen` / `!desktop` — desktop verbs (screenshot etc.).
- `!killall` — every session; `#omnius` only.

**This list said "five, not four" while ten existed.** Add new verbs here in the
same commit, or delete the list and point at the code.

## Opening a desk by hand

**`/spawn-session`** (skill + `fleet_ops.open_desk`, idempotent) opens a warm
bridge window. To work at a desk yourself, just run `claude` in that folder —
that IS the desk, native keyboard, no wrapper (owner's default since 2026-08-03).

- **Do not hand-roll the `claude` command** — every spawn-saga fix lives inside the helpers (`/omnius` skill name, template stub, `--add-dir <root>`, `--settings <project>`, folder-trust pre-stamp). Improvising drops them silently. Model/effort come from `fleet.json`; see `permissions.md`.
- **`--continue` only where that cwd has its OWN history** (`has_history()`, 2026-08-01, after it bit us). In a folder with no history `claude --continue` does not fail — it attaches to the most recent conversation from **somewhere else**, so a brand-new desk once resumed the *orchestrator's* conversation inside its own folder: wrong context, never claimed, looked busy throughout. A `||` fallback cannot help, because the first command succeeds at doing the wrong thing.
- **A live claim also suppresses `--continue`** (2026-08-03): a claim means a human's terminal owns that conversation, and resuming it would make the run a second writer. Such runs go fresh.

## One desk per folder, exactly one orchestrator

Decided 2026-07-31. Two orchestrators would be *"dos sesiones diferentes con memorias diferentes"* — the real objection is not concurrent writes but **two memories that silently diverge**, after which neither holds the true picture.

- **Scale by splitting DOMAINS, never by duplicating the owner.** Done twice: `#daybook` and `#fleet-status` each got a desk. Each domain has a single writer, which is why the file bus has no races.
- For a bounded one-off that would otherwise queue: an **ephemeral `claude -p` helper** that owns nothing, writes no memory, and dies. Not a second orchestrator.
- **⚠ Two sessions on one desk is invisible by design** — a claim holds one pid, so a duplicate shows in neither `state\sessions\`, `!status` nor the banner. `!kill` does not fix it: it kills the claimed pid and removes the claim, leaving the other running unclaimed. Recovery: close every window for that desk, then start exactly one.

## The bus pattern

Check in → handle → `--ack` the envelope → reply → **end the turn**. There is no
arming and no re-arming; see Liveness below for why the old wording is gone.

```
python <root>\tools\discord\inbox_watch.py <id> --once     # claim, print envelopes, exit immediately
python <root>\tools\discord\inbox_watch.py <id> --ack <envelope-id> [more]
```

- Reply by writing `state\outbox\<id>\<unix-ms>.json` = `{"text": …, "channelId": <echo the envelope's>}`. **Prefer `channelId`** — bare names are resolved among your own channels only.
- **Write that file with the `Write` tool, and never `Remove-Item` an envelope.** Both rules are permission-prompt scars from 2026-08-02: a shell-built JSON reply matches no allow rule and froze two desks for 40 minutes each, and a shell delete prompted on *every* Discord message until the owner gave up answering. `--ack` and `Write` are pre-approved everywhere.
- A session receives **every** queued envelope, oldest first — never only the latest. `--ack` each **as** it is handled, and when a later message contradicts an earlier one, the later wins: say so rather than silently doing both.
- **Whichever desk does the work owns the `--ack`.** A missed ack means the next run re-handles the envelope — harmless for a question, bad for a destructive verb (proved 2026-08-02: "delete <project>" ran twice).
- The claim records the **`claude` pid**, not the watcher's, and `session_alive()` falls back to `pid_alive(pid) or pid_alive(watcherPid)`. A dead watcher on a live session still resolves *alive*, so envelopes queue rather than spawning a second brain. **Do not "fix" that.**
- With a live claim the watchdog **delivers**; spawn-on-message only fires when no session holds the desk.

## Pinned documentation

**One pin per channel, each documenting itself** (user's design) — not one board listing every channel. `#omnius` carries the general how-to, `#daybook` explains what that channel is for, `#fleet-status` covers its own commands, and `#alerts` (added 2026-08-01) covers the approval format, the ~3-minute window, and what to do about a desk that stalled. **All four channels now document themselves.**

Rebuild by **deleting and reposting, never editing** — the emblem thumbnail is an upload and does not survive a PATCH cleanly. Embed cap is 6,000 chars total.

## Liveness

- **Beacon** (`state\watchdog\beacon.json`) is stamped only after a pass that reached **every** channel, so it means *listening*, not *running* — the alive-but-deaf failure no supervisor can catch. 20 deaf passes → log, release lock, exit(4). The banner trusts the beacon over the pid.
- **The watchdog cannot be hosted by an agent session** — it dies at turn boundaries. `start-omnius.bat` today; a Windows scheduled task now exists in the installer. Do not "fix" this by having a session babysit it.
- **Neither can inbox watchers — deleted 2026-08-01.** Same turn-boundary rule one layer up: session-side `inbox_watch` background tasks died three times in one evening, each death leaving a desk deaf or inviting a duplicate brain. Desks are now one-shot headless runs started and owned by the watchdog (lease in `state\watchdog
uns\`, pid-validated); sessions check in with `inbox_watch.py <id> --once` and **never re-arm anything**. A watcher, heartbeat, or any session-side process that must outlive a turn is the deleted bug wearing a new coat.

## The desk bridge

A desk can also be driven in a **visible terminal window** rather than only as
a headless run — the watchdog owns a ConPTY (`pywinpty`) so a human can sit at
the same live session Discord is typing into. Two doors, one conversation.

You do not need this to use Omnius over Discord; it matters when you want to
watch or take over a desk at the keyboard. The mechanics live in the code
(`tools\discord\`) and the guards are enforced there, not remembered here.

*(This section replaced ~4k characters of the pre-build research that proved
the primitive in the first place. A fresh instance needs to know the feature
exists, not the experiments that led to it.)*
