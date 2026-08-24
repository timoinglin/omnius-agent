# Bug report — the bridge stuffs the prompt queue while a desk is mid-turn (2026-08-23)

**For:** the session that builds Omnius (`tools\bridge\desk_bridge.py`).
**Status:** root cause confirmed from logs and live symptom. **Unfixed** — the
defect is in `tools\bridge\`, which is not this desk's scope.
**Reported by:** `tool.email`, 2026-08-23, after being invoked ten times in a
row with an empty inbox.

---

## 1. Symptom

One envelope arrived in `state\inbox\tool.email\` at **15:43:59Z**. The bridge
typed `/omnius` into the live session **36 times**, at a metronomic 4-second
cadence, from **15:44:14Z to 15:46:35Z**:

```
$ grep "^2026-08-23" state/logs/bridge-tool.email.log | grep -c "typed the nudge"
36
```

Every nudge past the first was queued behind the turn already running. They then
drained one per turn, each one a full model turn that checked in, found `[]`,
and had nothing to do. **One envelope cost ~36 turns.**

The owner-visible symptom is a desk that appears to be talking to itself: the
same skill firing over and over with nothing to show for it.

---

## 2. Root cause

`may_nudge()` (`tools\bridge\desk_bridge.py:406`) gates on `turn_running()`,
which tests for `state\turns\<id>.busy`. That stamp is written by the
**UserPromptSubmit** hook — which fires when a prompt is *submitted*, not when
it is *typed*.

A session that is already mid-turn does not submit typed input. It **queues**
it. So for the whole duration of the first turn:

- the nudge has been typed and is sitting in the queue,
- no prompt has been submitted, so no hook has fired,
- `state\turns\tool.email.busy` does not exist,
- `turn_running()` returns `False`,
- `may_nudge()` sees a clear coast and types another one.

The interval comes from the two-tier cooldown at lines 80–88:

```python
NUDGE_COOLDOWN = 20.0        # after a nudge that actually STARTED a turn
NUDGE_RETRY = 4.0            # after a nudge that vanished (session not ready)
```

`pump_bus()` sets `self.nudge_took = False` immediately after typing, so until a
turn is *observed* the loop uses the 4-second retry floor. Hence 36 nudges in
141 seconds.

**The faulty inference is in the comment at line 88:** *"A nudge that produces
no turn was never seen, so retrying it quickly is right."* A nudge can produce
no observable turn precisely because it **was** seen and is queued behind live
work. The fast-retry path is armed exactly in the case where it does the most
damage.

Note this is not the same defect as the 2026-08-01 heartbeat problem, and the
busy stamp is not lying — it is accurately reporting "no turn has been
submitted." The bridge is asking a question whose answer cannot distinguish
*unread* from *queued*.

---

## 3. Why tuning the interval cannot fix it

Any fixed retry floor is a bet on how long a turn takes. A desk that reads a
mailbox, opens an attachment and writes a reply can run for minutes; a retry
window long enough to cover that is long enough to make a genuinely-missed nudge
feel broken. The two cases need to be told apart, not averaged.

---

## 4. Suggested fix — make the nudge idempotent per inbox state

Nudge once per *distinct inbox state* rather than once per elapsed interval:

- record the set of envelope filenames present when a nudge is typed;
- suppress further nudges while the inbox still holds that same set;
- re-arm when the set changes (new mail arrives, or the desk acks and clears).

This gives exactly one nudge per batch of mail, drops the `nudge_took` /
`NUDGE_RETRY` distinction entirely, and still recovers if a nudge really is
lost — because new mail changes the set. A desk that is wedged and never acks
stops getting nudged, which is correct: that is a case for an alert, not for
another 500 prompts.

---

## 5. Immediate mitigation

Pressing **Esc** in the desk's window flushes the queued prompts. There is no
server-side way to clear them — the queue lives in the CLI client.

---

## 6. Evidence

```
$ tail state/logs/bridge-tool.email.log
2026-08-23T15:46:27Z mail waiting -> typed the nudge into the live session
2026-08-23T15:46:27Z holding the nudge: cooling down
2026-08-23T15:46:31Z mail waiting -> typed the nudge into the live session
2026-08-23T15:46:31Z holding the nudge: cooling down
2026-08-23T15:46:35Z mail waiting -> typed the nudge into the live session
2026-08-23T15:46:35Z holding the nudge: cooling down
```

The envelope itself was handled correctly on the first run: read, summarised,
replied to its channel, acked at 15:48:09Z. Nothing was lost — only spent.
