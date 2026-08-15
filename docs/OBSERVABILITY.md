# Observability — one story per chain, updates that prove they landed, and a demolition derby

> **Status: O1 and O2 built 2026-08-15 (same day they were proposed); O3 remains proposed.**
> O2's live drill — a deliberately broken commit proving the auto-revert on a real machine —
> is deferred into the derby on purpose (it IS drill #11); the mechanism itself is
> suite-covered from both halves. The phase after
> delegation is deliberately not a feature phase: the fleet just grew five mechanisms
> (desk mail, gate, loops, slash, self-update), and the most valuable next work is being
> able to SEE them, trust the updater end-to-end, and then try to break all of it on
> purpose. Shaped by the 2026-08-15 outside review, whose two best findings — "build
> `!trace`" and "the updater validates before the reload but never after" — are O1 and
> O2 verbatim. Companion to [DELEGATION.md](DELEGATION.md) (the machinery this observes)
> and [RELIABILITY.md](RELIABILITY.md) (the doctrine: *a green suite is not evidence*).

**Scope rule for the whole phase: no new capabilities.** Nothing here lets a desk do
anything it cannot do today. O1 reads state that already exists, O2 verifies a verb that
already exists, O3 is a controlled attempt to break what already exists.

---

## O1 — `!trace`: the lifecycle of one chain, one loop, or one envelope, on one screen

**Problem.** A single delegated instruction now crosses up to a dozen surfaces: Discord →
watchdog → envelope → classification → ledger → gate → inbox → run → outbox → ledger →
mirror → reply. Every hop is recorded *somewhere* (thread ledgers, transcripts, run logs,
gate files, loop ledgers, `watchdog.log`) — but answering *"why didn't this do what I
asked?"* means hand-joining five files. The data has one story; nothing tells it.

**Fix.** A twelfth-plus-one control verb, zero tokens, watchdog-handled like `!status`:

- `!trace <thread-id>` — the chain's story: origin (who asked, where), every delivery in
  order with timestamps and direction (spent hop vs free reply), gate holds with their
  outcome and deadline, hop budget consumed/remaining, final state (`open | closed:
  <reason>`), and where the artifacts landed. Fenced block, never a table.
- `!trace <loop-id>` — the loop's story: opened by, budget, fires with timestamps, the
  done-condition text, closed how.
- `!trace <dm-… envelope id>` — resolves to its thread and prints that.
- bare `!trace` — the last ~10 chains/loops, one line each with state, so the id to drill
  into is always one message away. The mirror lines already print thread ids; this makes
  them worth copying.

**Mechanics** (all existing state, one enrichment): thread ledgers gain per-delivery
records — `deliveries` entries become `{id, from, to, ts, reply: bool}` instead of bare
ids (readers `.get()` everything, and old bare-string entries are read as id-only, so
mid-flight ledgers survive the upgrade). Gate records and loop ledgers already carry
their timestamps. `!trace` joins ledger + gate + loop files and never parses logs — logs
stay for humans, state stays for machines.

**Done when:** `!trace` on the pilot chain reproduces its documented story (4 deliveries,
2 spent hops, 1 free reply, closed clean) from state alone; `!trace` on a gate-timeout
chain shows held → asked-where → deadline → dropped; bare `!trace` lists both.

**Tests, named first:** trace: a chain's deliveries carry from/to/ts since D-phase · old
bare-string deliveries still render (mid-flight upgrade) · a spent hop and a free reply
are labelled differently · a gate hold appears with its outcome and deadline · a loop
trace shows fires against budget · bare !trace lists recent chains newest-first · an
unknown id says so and lists what exists · !trace is a control verb and spawns nothing.

---

## O2 — the update handshake: `!update` must prove the NEW watchdog took over

**Problem.** `!update go` validates *before* the reload — suite, compile-check — and
nothing validates *after*. A new build whose suite is green but whose live daemon cannot
talk to Discord leaves the fleet deaf, and the self-heal makes it worse: the supervisor
restarts the SAME broken code forever. Pre-reload validation without post-reload
validation is half an updater.

**Fix.** A handoff file and a birth certificate:

1. `!update go`, after the suite passes and before the re-exec, writes
   `state\watchdog\update-pending.json`: `{fromCommit, toCommit, channelId, startedAt,
   bootAttempts: 0}`.
2. Every watchdog boot increments `bootAttempts` (atomic rewrite) if the file exists.
3. When the new watchdog reaches its first proven-healthy moment — beacon written AND one
   successful Discord exchange (the startup token check that already happens) — it posts
   *"✅ update live: `<from>` → `<to>`, healthy"* to the stored channel, deletes the
   pending file, done.
4. If boot never reaches that moment, the file survives and counts: at `bootAttempts >= 3`
   (crash-looping) — or on any boot where the pending file is older than 10 minutes
   (booted, then sat deaf until the DEAF exit) — the watchdog **auto-reverts before doing
   anything else**: `git reset --hard <fromCommit>`, restamp hooks/permissions, mark the
   pending file `reverted`, re-exec once more, and the old code posts *"⛔ update
   `<to>` did not come up healthy — reverted to `<from>`"* the moment it can speak.
5. A revert that itself cannot boot is out of scope by design: `<fromCommit>` is the code
   that was running minutes earlier, and the supervisor's restart loop plus the deadman
   channel silence is the remaining (existing) signal.

The DEAF exit path (`sys.exit(4)` after consecutive unreachable sweeps) counts as
unhealthy automatically — it happens before the pending file is deleted, so the next boot
sees it aged and acts.

**Done when:** a deliberately broken commit (boots, cannot reach Discord) pushed to a
throwaway branch and applied on a test instance auto-reverts within ~2 minutes and says
so in the channel; a healthy update posts its ✅ exactly once.

**Tests, named first:** update: go writes the pending handoff before re-exec · a healthy
boot posts the ✅ once and deletes the file · bootAttempts counts every boot while
pending · the third failed boot reverts to fromCommit · an aged pending file (booted but
deaf) reverts too · revert restamps before re-exec · the ✅ and the ⛔ name both commits ·
a normal boot with no pending file does none of this.

---

## O3 — the demolition derby: break it on purpose, write down what actually happened

**Problem.** Every mechanism above was built against *imagined* failures plus the ones
2026-07-31/08-01 taught. The suite proves decisions; it cannot prove the machine. The
review's closing question is the right one: *what happens when Omnius is wrong,
interrupted, restarted, updated, duplicated, partially broken, or asked to do something
stupid?* Nobody has tried on purpose.

**Fix.** A drill campaign, run live on this machine, one drill at a time. Each drill has
a predicted outcome written BEFORE running it; afterwards the actual outcome is recorded
next to the prediction (memory topic, and promoted into suite checks where a gap is
mechanical). The opening set:

| # | Drill | Predicted (verify against reality) |
|---|---|---|
| 1 | Kill the watchdog mid-chain (between hop 1 and 2) | leases adopted on restart, chain resumes from files, no duplicate delivery (deterministic ids) |
| 2 | Corrupt a thread ledger's JSON by hand | `_load_thread` → None → threadless inference or fresh chain; capped by TTL; nothing crashes |
| 3 | Kill Discord (network off) during a pending gate ask | gate file survives, deadline holds, re-ask on reconnect, timeout still drops |
| 4 | Start a second watchdog by hand | the lock makes it exit(3) immediately |
| 5 | Hand-write a `.busy` stamp and leave it | BUSY_SILENT release after 15 min of conversation silence; desk never permanently deaf |
| 6 | `!update go` with a hand-dirtied tracked file | refuses, names the count, nothing pulled |
| 7 | Delete `state\` wholesale while running | regenerates; channel topics rebuild the map; open chains are lost and SAY so (expected loss — document it) |
| 8 | Ask a desk to delegate to itself, to a ghost desk, and 10 hops deep | three distinct refusals, all already suite-covered — confirm live |
| 9 | Two owners' messages racing one desk (rapid fire) | one run drains both in order; acks make the sequencing visible |
| 10 | Fill the loop budget, then keep saying "continue" in fresh messages | each fresh instruction opens a NEW loop with a NEW budget — confirm this is legible, not surprising |

**Done when:** every drill has prediction + observed reality recorded, every mechanical
gap found became a suite check or a fix commit, and the campaign's summary lands in
RELIABILITY.md as the second dated entry — live validation, documented where it
happened.

---

## Order and size

O1 first (a day-shaped piece; pure read-side, immediately useful every day after), O2
second (the one real engineering item; touches boot, needs the test instance), O3 last
and longest-tailed (cheap per drill, and O1 makes every drill's autopsy readable).
Explicitly out of scope for this whole phase: new envelope kinds, new verbs beyond
`!trace`, new integrations, warm desks — the fleet learns nothing new until it can be
watched, trusted to update, and has survived its own demolition.
