# Delegation — desks that mail desks, loops that end, verbs that travel

> **Status: proposed 2026-08-15, awaiting go. Nothing here is built yet.** This is the design for the
> next capability phase: desk-to-desk envelopes over the existing file bus, budgeted work loops, and
> owner slash-commands passed through Discord. Companion to [ARCHITECTURE.md](ARCHITECTURE.md) (the
> bus this extends) and [RELIABILITY.md](RELIABILITY.md) (whose doctrine — *documentation is not a
> mechanism* — this document tries to honour by naming its tests before its code).

**Goal (owner):** *"one session gives the result to another session (or itself) and then loops over
to continue working on something until all is 100% solved"* — plus fan-out workflows (one desk
triages, another fixes, the results notify the rest) and the ability to fire a skill from a Discord
message. Four asks, one primitive: **an envelope that can name a desk instead of a channel.**
Workflows are just envelopes in sequence; a loop is an envelope to yourself with a counter; a slash
command is an envelope with a verb stamped on it. The engine below stays generic — *which* desk
forwards *what* to *whom* lives in each project's `CLAUDE.md` and memory, never in the watchdog.

## Why now, and what already exists

Delegation is not new here — it is just manual and uncounted:

- The orchestrator already hand-writes briefs into other desks' inboxes (`from: "omnius"`,
  `.claude\skills\omnius\SKILL.md` §2) and the watchdog starts the target within seconds.
- A desk whose work spans runs already queues its own continuation
  (`schedule.py add --in 2m --to <own id>`), capped only by prose: *"never queue a continuation
  more than twice in a row."*
- The auditor desk in the shipped demo already "sends each finding back to its owner's channel to
  fix" — by asking the **human** to relay it.

Every one of those is an invariant enforced by an agent remembering to behave. This phase moves
them into the transport, where refusals are mechanical, budgets are counted, and every hop is
visible in Discord.

---

## D0 — The load-bearing decision: the watchdog routes, senders never touch foreign inboxes

**Problem.** The bus has exactly one safe desk-to-desk path today — a raw `Write` into
`state\inbox\<target>\` — and it has no validation, no visibility, no atomicity and no loop
protection. A typo'd target manufactures a phantom desk that the watchdog retries every 3 s forever
(the `_unrunnable` incident, `watchdog.py:1930-1937`). And a desk can never post into a sibling's
channel (`resolve_outbox_target` refuses cross-session posts by design), so hand-delegation is
invisible in Discord — the opposite of ARCHITECTURE.md §3.4's promise that *"delegation is always
watchable."*

**Fix.** Desk mail is an **outbox file with a `to` field**. The sender writes into its *own* outbox
exactly as it does for a Discord reply; the watchdog's flush loop recognises `to`, validates the
target, applies hop and gate policy, delivers into the target's inbox atomically, transcribes both
halves, posts a compact **visible copy** into the recipient's channel, and starts the target's run —
all in the same ~3 s tick (`flush_outboxes` runs immediately before `ensure_runners` in the main
loop). Inboxes keep a single writer. One gate validates every target. The mirror line satisfies
"watchable" without opening any new channel-write path for desks.

Direct inbox writes stay *mechanically* possible (they are file writes; nothing can prevent them)
and become *doctrinally* deprecated: the orchestrator's skill is rewritten to the `to:` form in the
same change, and stragglers still classify safely (D2).

**Done when:** a desk-mail outbox file lands in the target inbox, the mirror line appears in the
recipient's channel, and a typo'd target produces a `.refused` rename plus one explanatory line in
the sender's channel — with **no inbox folder created**.

---

## D1 — Envelope v2 and the routing algorithm

**Problem.** The v1 envelope (`{id, from, channel, channelId, category, ts, text, files}`) has no
way to say *to whom*, *in reply to what*, *on whose behalf*, or *how many hops are left*. Readers
`.get()` every field, so the shape can grow without breaking anything — but nothing defines what
grows.

**Fix — what the sender writes** (its own outbox, with the `Write` tool, like every reply):

| field | required | meaning |
|---|---|---|
| `to` | yes | target session id — `orchestrator`, `daybook`, `tool.<name>`, `<project>.<component>`. Presence of `to` is the discriminator: the desk-mail branch fires before channel resolution ever sees the file. |
| `text` | yes | the brief or the reply, verbatim, markdown fine |
| `files` | no | absolute paths, same semantics as Discord replies (archived to `media\sent\`) |
| `thread` | echo | chain id — echo it from the incoming envelope exactly as you echo `channelId`. Omit when starting a chain. |
| `replyTo` | no | id of the envelope this answers. Advisory — context and transcript joinery, the engine does not depend on it. |
| `origin` | starter only | `{"channelId": "...", "from": "owner"}` — the human context that started the chain. Only the chain starter sets it (it just drained that human envelope); later hops never do. |

**Fix — what the watchdog delivers** into `state\inbox\<target>\` (`.part` → `.replace()`, the
atomic idiom from `tools\transcribe\run.py` — mandatory for every new inbox writer, and
`write_envelope()` itself gets the same one-line hardening while we are in the area):

```json
{ "id": "dm-linkbox.auditor-1755250000123",
  "from": "linkbox.auditor",
  "channel": null, "channelId": null, "category": null,
  "ts": "<iso>",
  "text": "finding 3: /api/links answers a bad id with a traceback - fix and add a test",
  "files": [],
  "kind": "desk",
  "thread": "t-1755250000123-linkbox.auditor",
  "origin": { "channelId": "C-auditor", "from": "owner", "session": "linkbox.auditor" },
  "hops": 2,
  "replyTo": null,
  "slash": null }
```

- `id` is **deterministic**: `dm-<sender>-<outbox filename stem>`. A crash between inbox-write and
  outbox-unlink redelivers to the *same* filename instead of duplicating.
- `from` is the bare sender session id — the shape ARCHITECTURE.md §3.4 already documents as legal.
  `from: "omnius"` remains a legacy alias for the orchestrator's hand-written envelopes.
- `kind: "desk"` marks v2 desk mail; absent = v1 human/system mail. v1 consumers are untouched.
- `channelId` stays `null` — desk mail has no Discord arrival channel, and the backlog notifier
  already skips null-channel envelopes. The human channel travels in `origin`, explicitly.
- `hops` is informational (hops remaining *after* this delivery, so a desk can say "1 hop left"
  when sub-delegating). The **authoritative** count lives in the thread ledger — a desk cannot
  refill its own budget by editing a field it merely echoes.

**The thread ledger** — `state\watchdog\threads\<thread-id>.json`, atomic replace, one per chain:

```json
{ "id": "t-1755250000123-linkbox.auditor",
  "origin": {"channelId": "C-auditor", "from": "owner", "session": "linkbox.auditor"},
  "hopsLeft": 2,
  "deliveries": ["dm-linkbox.auditor-1755250000123"],
  "edges": [["linkbox.auditor", "linkbox.back"]],
  "lastDeliveredTo": "linkbox.back",
  "startedAt": "<iso>", "lastAt": "<iso>",
  "closed": null }
```

`closed` ∈ `null | "hops" | "storm" | "gate-denied" | "gate-timeout" | "expired"`. A tick sweeper
deletes ledgers closed or idle for more than 48 h (logging one breadcrumb line when it expires a
chain that never finished).

**The routing order**, in the flush loop's file iteration, immediately after the JSON parse and
before `resolve_outbox_target`:

1. **Shape + reserved.** Legal target shapes mirror the check-in's own rule (`inbox_watch.py`):
   `orchestrator | daybook | tool.<name> | <project>.<component>`, kebab-case, one dot at most.
   Reserved sender names as targets are refused by name for a crisp message.
2. **Self-address → refused.** Doctrine: a session ignores envelopes with its own origin.
   Self-continuation belongs to the schedule (D5); the refusal says so.
3. **Existence.** `cwd_for(to)` must be a real folder. **The registry is the filesystem**; a miss
   is refused and no inbox folder is ever created — this closes the phantom-desk class at its only
   remaining entry point.
4. **Thread resolution.** Echoed `thread` (unknown ids are not resurrected) → else inferred (the
   most recently active open ledger whose `lastDeliveredTo` is this sender — glues a forgetful
   reply to its chain) → else a new ledger with `hopsLeft = hop_ttl`. A closed ledger refuses.
5. **Dedupe.** `dm-<sender>-<stem>` already in the ledger's `deliveries` → the outbox file is
   deleted silently (the delivery already happened; this is the restart window).
6. **Storm backstop.** `deliveries ≥ hop_ttl × 4` → close `"storm"`, refuse. Catches every
   pathological shape, including the reply ping-pong that free replies would otherwise permit.
7. **Hop accounting.** A **reply** — reversing an edge already recorded in the ledger — is
   **free**, so an A→B→C chain can always unwind to its starter. Only **new** edges spend budget;
   a new edge at `hopsLeft 0` closes the chain (`"hops"`), refuses the file, and posts a checkpoint
   line to the origin channel so the human can re-instruct (fresh mail = fresh chain, fresh TTL).
8. **Cross-project gate** (D4). Held mail leaves the outbox entirely.
9. **Deliver.** Atomic inbox write → ledger update → transcribe both halves (sender `out`,
   recipient `in` — the paper trail must not go dark just because no channel was involved) →
   unlink the outbox file → stamp the sender's `.last-posted` (the Stop hook's silence announcer
   reads it; **a desk that only delegated is not a silent desk**) → post the visible copy → 
   `ensure_runner(target)`.

**Refusal**, uniformly: rename the file `.refused` (the existing outbox idiom), log, and post one
watchdog-voice line to the sender's primary channel — `✗ could not deliver desk mail to
'<target>' — <reason>`. No run is woken for a refusal; the message is `api.send_message`, not an
envelope, so refusals can never loop.

**The visible copy** — the ARCHITECTURE.md §3.4 promise, kept by the transport:

```
[desk mail] linkbox.auditor -> linkbox.back  ·  t-…auditor  ·  2 hops left
> finding 3: /api/links answers a bad id with a traceback - fix and add a…
```

posted in the **recipient's** channel, preview capped at ~200 chars through `api.redact()` (full
text lives in both transcripts — a fleet that narrates whole briefs into chat recreates the noise
the no-narration rule exists to prevent). No recipient channel → sender's channel with a `->`
marker; neither → log only. The mirror is best-effort *after* delivery: Discord being down delays
visibility, never delivery.

**Done when:** the routing tests in D9 pass — including "a desk with no folder is refused — no
phantom inbox is ever created" and "delivery stamps the sender's `.last-posted`".

---

## D2 — Sender classification: fleet mail is not a person waiting

**Problem.** `is_human_sender()` is an exclusion list against three names (`omnius`, `heartbeat`,
`schedule`). Any other `from` counts as a **person waiting** — it can pop a terminal window
(`fleet.json` defaults `window: "terminal"` for human mail) and page the owner through the
deaf-desk alarm. ARCHITECTURE.md §3.4 says a session id is a legal origin tag; the code punishes
anyone using one. The trap is already live: `from: "transcribe-job"` counts as a person today.

**Fix.** A positive predicate, with `is_human_sender` as its negation:

- `is_fleet_sender(who)` = `who ∈ SYSTEM_SENDERS`, **or** `who` ends in `-job` (tool job handoffs),
  **or** `who` is a legal desk id whose folder exists (the same registry check routing uses, behind
  a small TTL cache so the per-tick envelope scans stay cheap).

Consequences, consumer by consumer: desk mail never opens a terminal window (delegated work runs
headless — that is the design); desk mail never trips the deaf-desk pager (*the fleet talking to
itself going unanswered is a log line, not a reason to interrupt him* — the rule the pager already
follows for system mail); the backlog notifier needs no change (null `channelId` already skips).

**Guest labels must never collide with desk ids.** `guests()` gains three fail-closed rejections in
the style of its existing four: a label containing a dot, a label matching a real desk id, a label
ending in `-job` — each ignored and reported through `problems()` / `!config`.

**Done when:** the classification tests in D9 pass, including "a `-job` sender is fleet mail too".

---

## D3 — The reply path: chains unwind, and the desk that talked to the human answers the human

**Problem.** When desk B finishes work desk A asked for, where does the answer go? A bare `{text}`
outbox reply falls through to B's own channel — visible, but the human's question dies unanswered
in A's channel, and A never learns the work finished.

**Fix.** Three rules, each the smallest that closes the gap:

1. **Desk mail is answered with desk mail**: `{to: <envelope.from>, thread: <echoed>, replyTo:
   <envelope.id>, text}`. No own-channel post alongside it — the visible copies already narrate
   every hop, and the no-narration rule extends naturally: *desk envelope in → desk envelope out.*
2. **Replies are hop-free** (D1 step 7), so unwinding can never be starved by the TTL. The
   per-thread deliveries cap bounds total traffic instead.
3. **The chain terminates at its starter.** The starter is the one desk that legitimately owns the
   origin conversation — the owner asked in *its* channel — so its final summary is an ordinary
   `{channelId: origin.channelId, text}` reply through the existing, unmodified channel-resolution
   path. No new foreign-channel write opens; the scoped-writes invariant survives intact. Chains
   started by schedule or heartbeat (no `origin`) simply end in the starter's own channel or in
   silence, per the existing quiet rules.

**Done when:** in the sandbox, an A→B chain ends with B's reply delivered to A and A's summary
posted to the origin channel — and nothing posted anywhere by B directly.

---

## D4 — The cross-project gate: intra-project is free, everything else asks first

**Problem.** Inside one project, desks delegating to each other is the whole point. Across
projects — or at the orchestrator, or at tool desks — an unasked envelope is one desk conscripting
another's context on nobody's authority. The owner's rule (2026-08-15): cross-project delegation
requires his ok, fail closed.

**Fix.** `free_pair(sender, to)` is true iff the sender is the orchestrator (delegating downward is
its job; today's hand-path is already ungated), or both are project desks of the **same** project.
Everything else — project→project, project→orchestrator, anything↔tool, anything↔daybook — holds
the mail and asks. Ambiguity fails closed.

The hold reuses the permission relay's *interaction* (ok/no words, 6-char codes) but **holds a file
instead of blocking a hook** — nothing anywhere waits in-process:

- The outbox file's content **moves** to `state\gate\<gate-id>.json` (deliberately *not* under
  `state\inbox\` — every folder there is treated as a desk) with the original filename stem kept,
  so the eventual envelope id is unchanged.
- The ask posts where the human is — origin channel, else the sender's, else `#alerts`:

  ```
  [cross-project] linkbox.auditor wants to mail demo-crm.web
  > preview of the text…
  reply ok to deliver or no to drop  ·  code a1b2c3
  ```

- `answer_gate(text)` sits in the owner-only block of `handle_message`, after the permission
  answerer. Precedence kills the bare-"ok" collision: a bare ok/no reaches the gate **only when no
  permission asks are pending and exactly one gate is** — otherwise the gate needs `ok <code>`. A
  blocked hook always outranks a parked envelope.
- `ok` → deliver the held mail through the normal path with the gate pre-approved; `no` → the gate
  file is renamed `.refused`, the thread ledger notes `"gate-denied"`, one line says so. Silence →
  a per-tick sweeper refuses anything older than `GATE_WAIT_SECONDS` (3600 — a fixed constant
  until real use argues for a knob) exactly as a `no`, reason "no answer within 60m". A late `ok`
  matches nothing and gets the existing "nothing is waiting" reply.
- **Restart-safe by construction**: the files are the whole state; codes are stored, so an answer
  typed before a watchdog restart still matches after it; on boot the sweeper re-posts any pending
  ask once (stamped, so it never spams per tick) with the **original** deadline.

**Done when:** the gate tests in D9 pass, including "silence past the deadline refuses — fail
closed" and "a pending ask survives a restart with the same code".

---

## D5 — Loops: the continuation pattern, counted

**Problem.** "Loop until 100 % solved" is where fleets burn money politely. The sanctioned
continuation (`schedule.py add --in … --to <self>`) is capped by prose — *never more than twice in
a row* — which is an honor system, and honor systems are the failure class this whole repo exists
to retire. Meanwhile a self-addressed desk-mail envelope is doctrinally ignored (*a session ignores
envelopes with its own origin*), so loops must ride the schedule, whose envelopes are already
system mail: no windows, no paging.

**Fix.** Make the continuation first-class and counted. Everything self-continuing is budgeted;
nothing is counted by honor system. The prose cap retires.

- **A self-addressed `add` requires `--loop`** (`auto` mints `loop-<desk>-<ms>` and prints
  `run 1/5`; a named id continues that loop). Third-party adds — reminders, routines to other
  desks — are unchanged.
- **`--max`** defaults to config `loop_budget` (5) and may only *lower* it; asking for more exits 2
  and points at `config\omnius.ini`.
- **`--channel <id>`** is stored on the job and copied into the fired envelope's `channelId` —
  closing the "scheduled envelopes have no channel to echo" gap for loops that must eventually
  answer a human.
- **`--to` is validated on every `add`**, loop or not: legal shape + real folder, else exit 2 with
  the id grammar in the message. This closes the schedule-side phantom-desk writer.
- **Loop ledger** `state\watchdog\loops\<id>.json`: `{id, session, max, fired, channelId, closed}`.
  The CLI opens and closes; the watchdog increments `fired` at delivery; both write atomically.
- **Budget enforced twice.** At **add-time** (primary — it reaches the desk inside its own run): a
  re-add past budget exits 2 with the checkpoint instruction — *report what EXISTS to the owner and
  ask whether to continue; a fresh owner instruction opens a NEW loop.* At **fire-time** (the belt,
  for hand-edited jobs): over-budget jobs are skipped, dropped, and one channel line says so.
  **Loops never self-extend** — only fresh owner mail restarts work, and that opens a new loop.
- **The done-condition is checkable, not felt.** The loop text carries it — *"Done when
  `<command>` exits 0"* — and each fired run checks it **first**: green → `loop close <id>` + final
  report (to `--channel` if set); not green → one step of work, one re-`add`, end the turn.
- `loop list` joins `!cron`'s read-only output; closed or week-idle ledgers are swept.

**Done when:** the loop tests in D9 pass — including "the add past budget is refused with the
checkpoint instruction" and "a typo cannot invent a desk".

---

## D6 — Slash pass-through: verbs travel, judgment stays in the desk

**Problem.** From Discord the owner can send prose and eleven `!` control verbs — but not
`/status`, `/watch`, or any skill a desk actually has. The `!` family is deliberately judgment-free
("speed, not judgment"); skills are the opposite. Passing them through must not let arbitrary
mail — or any desk — trigger arbitrary skills.

**Fix.** Owner mail only, closed list, stamped by the transport:

- In `handle_message`'s owner-only block — after control verbs, permission answers and takeover
  answers, immediately before the envelope is written — text starting `/name` is checked against
  `config\skills.ini`. **Allowed** → the envelope is delivered with `"slash": "<name>"` stamped and
  the text verbatim (transcript honesty). **Refused** → *nothing is delivered* (delivering it as
  plain text would make the desk improvise the verb — worse than refusing) and the watchdog answers
  in-channel: `"/deploy" is not on the pass-through list (config\skills.ini) — nothing was
  delivered. Send it without the slash to say it in words.`
- `/omnius` is an always-allowed **no-op alias**: delivered as plain mail with no stamp — the run
  already *is* `-p "/omnius"`, and stamping it would recurse the skill into itself.
- Guests' slashes are plain text, never passed through. Desk mail structurally **cannot** carry
  `slash` (different code path writes it) — capability escalation between desks is impossible by
  construction, not by review.
- The skill side (one new rule in `/omnius` §3): *`slash` set → the watchdog validated an owner
  `/<name>`; invoke that skill now with the Skill tool, passing the text after the first token as
  its argument; its outcome is your reply. If the skill does not exist on this desk, say so in your
  reply.* Skills are per-folder, so existence is desk-local; the in-reply error is the honest
  surface.
- **`config\skills.ini`** (+ shipped `skills.example.ini`) is an authorisation list and fails
  closed like `guests.ini`: missing file or empty list = nothing passes; labels validated
  `[a-z0-9_-]+`; unreadable → empty, reported via `problems()`; surfaced in `!config` as
  `pass-through skills: (none)`. Deliberately **not** `sync_permissions.ALLOW` — that list is
  harness tool permissions, a different layer with a different blast radius.

**Done when:** the slash tests in D9 pass, including "an unlisted /skill is refused in-channel and
nothing is delivered" and "an empty config passes nothing".

---

## D7 — Config plumbing

One new section, three keys, each with a SPEC row (the one-table rule: a setting absent from SPEC
is invisible to `!config` and the validators):

```ini
# config\omnius.ini
[delegation]
# hop_ttl = 3                    ; forward hops a chain may spend (replies are free)
# loop_budget = 5                ; scheduled continuation runs before a loop must checkpoint
# cross_project_requires_ok = 1  ; hold cross-project desk mail for an ok in Discord (fail closed)
```

Env overrides: `OMNIUS_HOP_TTL`, `OMNIUS_LOOP_BUDGET`, `OMNIUS_CROSS_PROJECT_OK` — env beats file
beats default, as everywhere. `config\skills.example.ini` ships (the `.gitignore` already
un-ignores `config/*.example.ini`); `config\README.md` gains its table row. New state dirs
(`state\gate\`, `state\watchdog\threads\`, `state\watchdog\loops\`) join the suite's sandbox
redirect block — the block's own comment warns that a path added late writes into real state.

---

## D8 — Failure modes

| # | Failure | Handling | Told where |
|---|---|---|---|
| 1 | Typo'd / non-existent target | `.refused`; **no inbox folder ever created** | sender's channel |
| 2 | Reserved name or illegal shape as target | `.refused`, distinct reason | sender's channel |
| 3 | Self-addressed desk mail | `.refused`; points at `schedule.py --loop` | sender's channel |
| 4 | New edge with hops exhausted | chain closed `"hops"`; checkpoint so the human can re-instruct | origin channel |
| 5 | Delivery storm (≥ `hop_ttl × 4` per thread) | chain closed `"storm"`; refusal | origin/sender channel |
| 6 | Target exists but runs cannot start | envelope waits; the existing `_unrunnable` backoff + owner alert covers it; desk mail never trips the deaf-desk pager | target's channel |
| 7 | Gate unanswered | fail closed after 60 m: held file `.refused`; sender desk not woken (its honest last word was "queued") | the ask's channel |
| 8 | `ok` after gate timeout | matches nothing; existing "nothing is waiting" reply | same channel |
| 9 | Loop budget exhausted | add-time refusal → desk checkpoints with state + question; fire-time belt skips, drops the job, says so | loop `--channel`, else desk's channel |
| 10 | Loop target folder vanished later | fire-time re-validation skips, drops, alerts | desk's channel / `#alerts` |
| 11 | Watchdog restart mid-chain | every stage is a file: outbox reprocessed, gate re-asked (same code, original deadline), inbox picked up, ledgers persist; deterministic `dm-` ids + ledger dedupe make the write-then-crash window redeliver-safe | nobody — it resumes |
| 12 | Two chains converge on one desk | envelopes queue in one inbox; one run drains both; replies go out per-thread. **Deadlock is impossible because nothing blocks** — "A waits for B" is not a process state, A's run *ended*; the only waiting objects are ledgers, which idle-expire | n/a |
| 13 | Discord down at mirror/notice time | delivery already happened locally; mirror logged, never retried in a storm; transcripts hold both halves | log |
| 14 | Reply omits `thread` | glued to the chain that last delivered to that sender; genuinely ambiguous → new chain, bounded by rows 4–5 | n/a |
| 15 | Slash names a skill absent on that desk | the run's Skill tool errors; the desk says so in its reply | the asking channel |
| 16 | Guest sends `/anything` | plain text, no stamp, desk treats as words | n/a — guests get silence by doctrine |

---

## D9 — The tests, named before the code

The suite idiom applies: return-token assertions, silence where silence is the feature,
`.refused`/`.bad` renames, no side effects outside the sandbox; pure-function tests plus
end-to-end sandbox tests, the way the permission relay is covered from both sides.

`== desk mail: envelope v2 ==` — a 'to' outbox file becomes a v2 inbox envelope, atomically · the
id is deterministic — redelivery overwrites, never duplicates · v1 readers ignore the new fields.

`== desk mail routing ==` (beside `== outbox routing ==`, reusing its mapping fixture) —
intra-project mail lands in the sibling's inbox · the visible copy posts in the RECIPIENT's
channel · delivery stamps the sender's `.last-posted` (silence announcer) · both halves reach the
bus transcript · a malformed id is refused and renamed `.refused` · a reserved sender name as
target is refused · a desk with no folder is refused — no phantom inbox is ever created ·
self-address is refused — continuation is the schedule's job · refusal tells the sender's channel
in one line · delivery kicks `ensure_runner` on the target · a foreign-channel envelope is renamed
`.refused` (closes a today-untested path).

`== hops and threads ==` — a fresh chain gets `hop_ttl` from config · each forward hop decrements
the thread ledger · a reply along a recorded edge is free · an exhausted chain is refused and the
owner sees a checkpoint · a threadless reply is glued to the thread that woke the sender · the
deliveries backstop stops a ping-pong storm · the ledger survives a watchdog restart.

`== system mail never opens a window ==` (extended) — a desk id in `from` is not a person — no
window opens · desk mail never trips the deaf-desk pager · a `-job` sender is fleet mail too ·
guests: a label with a dot is refused at config load · guests: a label matching a desk id is
refused at config load.

`== cross-project gate ==` — same-project mail passes free · orchestrator mail is never gated ·
cross-project mail is HELD, not delivered · `ok` delivers the held envelope · `no` drops it and
says so · silence past the deadline refuses — fail closed · a pending ask survives a restart with
the same code · a bare `ok` never answers a gate while a permission ask is pending.

`== schedule: loops ==` — a self-addressed add without `--loop` is refused · `--loop auto` mints a
ledger and prints run 1/N · the add past budget is refused with the checkpoint instruction ·
fire-time belt: a hand-edited job past budget checkpoints instead of firing · `close` ends the loop
and clears its pending job · `--to` is validated against real desks — a typo cannot invent one ·
`--channel` rides into the fired envelope.

`== slash pass-through ==` — an allow-listed /skill stamps the envelope's `slash` field, text
verbatim · an unlisted /skill is refused in-channel and nothing is delivered · a guest's slash is
plain text — never passed through · `/omnius` is a no-op alias for plain mail · an empty config
passes nothing (closed by default) · the `[delegation]` keys appear in `!config` with their
sources.

---

## The worked example — linkbox audits itself

The shipped demo (`templates\demo-project\`: `back`, `front`, and a read-only `auditor`) already
describes this flow with a human in the middle: *"send each finding back to its owner's channel to
fix."* With desk mail, the same play runs itself — and every hop is on the screen:

1. Owner, in `#auditor`: *"audit the project and get the findings fixed."*
2. `linkbox.auditor` audits, writes `memory\audits\<date>.md`, then one desk-mail envelope per
   finding: `{to: "linkbox.back", origin: {channelId: "C-auditor", from: "owner"}, text:
   "finding 3: /api/links answers a bad id with a traceback — fix and add a test. Done when the
   audit script passes."}`. `#back` shows `[desk mail] linkbox.auditor -> linkbox.back · 2 hops
   left` with the preview.
3. `linkbox.back` fixes, runs its checks, replies with desk mail on the same thread. `#auditor`
   shows the mirror of the reply.
4. The auditor re-checks the finding, and — as chain starter — posts the human-facing close in
   `#auditor`: *"finding 3 fixed and verified; audit file updated."*

Same primitive, other shapes: a triage desk forwarding a bug report to the desk that owns the fix
(one hop); the fixing desk fanning the changelog out to its siblings (N envelopes, N mirrors); a
desk grinding a migration in a budgeted loop, checkpointing at run 5 with what exists. The engine
never learns what a "changelog" is. The fleet does.

---

## What changes in existing documents when this is built (Phase B, not before)

- **Root `CLAUDE.md` §5** — the no-envelope-no-post rule gains its desk-mail clause. Draft text:
  *"Desk mail counts: an envelope with `kind: \"desk\"` is mail like any other — answer it with
  desk mail back to its sender (`to`), never with a channel post; only the desk holding the
  chain's `origin` speaks to the human, once, at the end."*
- **`ARCHITECTURE.md` §3.4** — the origin-tag sentence becomes: *"…or a session id — **fleet
  mail, not a person**; the person-waiting guards consult the desk registry."* Same change sweeps
  the stale watcher-model paragraphs (the §3.5 background-task description and the `/omnius`
  step list) that line 57's banner already supersedes.
- **`.claude\skills\omnius\SKILL.md`** — §2 hand-delegation rewritten to the `to:` form; §3 sender
  table gains the desk-mail row and the `slash` rule; §3b (new) carries the reply discipline; the
  two-continuation prose cap is replaced by the `--loop` workflow.
- **`docs\PERMISSIONS.md`** — one paragraph: the gate reuses the ok/no interaction but holds a
  file, never a hook; bare-ok precedence.
- **`README.md`** — delegation joins the capability list once it is real. Until then this document
  is linked from the reading order only.

## Rollout

| Phase | Ships | Proof |
|---|---|---|
| **B** | D1 + D2 + D3 + D4: routing branch, ledger, classification, gate — and the full `== desk mail ==` / `== hops ==` / gate test sections | suite green; live: one auditor→back finding delivered, mirrored, replied, closed on a stamped demo project |
| **C** | D6 slash pass-through + `config\skills.ini` | suite green; live: `/status` from a phone reaches a desk, an unlisted verb is refused in-channel |
| **D** | D5 loops (`--loop`, budgets, `loop close/list`, `!cron` section) | suite green; live: a 3-run loop closes on its done-command; a budget-5 loop checkpoints instead of running a 6th time |
| **E** | pilot on a real project + latency measurement of a full chain | the measured numbers decide whether warm desks (the bridge) get promoted into the delegation path as a Phase F |

Each phase lands as one commit with its tests; nothing merges on prose alone. *Documentation is
not a mechanism — including this document.*
