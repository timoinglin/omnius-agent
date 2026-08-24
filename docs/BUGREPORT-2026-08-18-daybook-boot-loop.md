# Bug report — the `daybook` desk boot-loop (2026-08-18)

**For:** the session that builds Omnius.
**Status:** root cause confirmed by live reproduction. A **stop-gap is in
place** for `daybook` only. The underlying defect is fleet-wide and unfixed.
**Reported by:** orchestrator, 2026-08-18, after the owner asked "que pasa con
daybook?".

---

## 1. Symptom

The `daybook` desk opened a terminal window and died ~4 seconds later, over and
over, from **04:02:08Z to 05:02:59Z** — **112 launches, 110 closes** before the
stop-gap landed.

```
$ grep -c "starting desk daybook" state/logs/bridge-daybook.log
112
```

Owner-visible consequence: his `ping` to `#daybook` at **04:52:18Z** sat
undelivered in `state\inbox\daybook\` for **11 minutes**. No alert fired.

---

## 2. Root cause

`claude` is launched with `--continue` in a folder where the CLI does **not**
consider anything resumable. In an interactive TTY that is **fatal**: the CLI
prints one line and exits.

Reproduced live by spawning the desk's exact argv in a ConPTY:

```
exec <claude.EXE> --add-dir <root>
     --settings <root>\.claude\settings.json
     --model opus --effort xhigh --continue

captured from the pty:
    No conversation found to continue
*** EOF after 9.2s
```

The bridge treats the pty ending as the desk ending
(`tools/bridge/desk_bridge.py:528` logs `desk closed`), the watchdog sees a
desk with no live claim and mail waiting, and opens another terminal. Cadence:
one window every ~6s, indefinitely.

### 2.1 Why the gate allowed `--continue`

`tools/discord/watchdog.py:178`

```python
def has_history(cwd):
    """True when `claude --continue` has something of ITS OWN to resume here."""
    try:
        d = history_dir_for(cwd)
        return d.is_dir() and any(d.iterdir())
    except OSError:
        return False
```

The docstring states the intended contract. The implementation tests only that
the transcript **folder exists and is non-empty**. Those are not the same
predicate, and on 2026-08-18 they disagreed:

- the CLI's history folder for that cwd (`~/.claude/projects/<slugified-cwd>/`)
  contained `92ae1624-….jsonl` — 392 KB, 145 lines, **all valid JSON**, `cwd`
  recorded correctly as the daybook folder, last written 2026-08-17 07:59.
- `has_history()` → `True`. The CLI → `No conversation found to continue`.

The transcript was written by CLI **2.1.232**; the machine now runs **2.1.234**.
That version boundary is the most likely reason the CLI no longer offers it,
but **the exact CLI-side rule is not the point** — the point is that
`has_history()` predicts the CLI's answer by proxy and the proxy is wrong.
Any future change to how the CLI indexes conversations breaks it again.

### 2.2 The failure is asymmetric — and that hid it

Same flags, two outcomes:

- **Headless** (`-p`): exit 0, printed `ok`, and silently **started a new
  session** instead of resuming. Verified directly. No error, no signal.
- **Interactive** (ConPTY/TTY): prints `No conversation found to continue` and
  **exits immediately**.

So `has_history()` has been wrong on the headless path too — it just never
looked wrong, because `-p` degrades to a fresh session. Only the desk path
turns it into a crash loop.

### 2.3 All three launch paths share the defect

- `tools/discord/watchdog.py:2143` — `start_run()`, headless runs
- `tools/bridge/desk_bridge.py:194` — the bridge (**this is the crash loop**)
- `tools/orchestrator/fleet_ops.py:318` — `open_desk()`, i.e. `/spawn-session`

All three read the same `has_history()`. A desk opened by hand via
`/spawn-session` on a folder in this state opens a window that closes
instantly — same defect, without the loop, and it would read to the owner as
"the desk won't start".

**Blast radius:** any desk whose transcript folder is non-empty but not
resumable — after a CLI upgrade, after moving the workspace to another machine,
after a transcript is trimmed or archived. The daybook folder is not special.

---

## 3. Second defect — the tab-loop guard never counted this

`tools/discord/watchdog.py:1128 run_active()`, terminal-lease branch:

```python
if lease.get("mode") == "terminal":
    c = read_claim(session)
    if c and same machine and pid_alive(c["pid"]):
        (RUNS / f"{session}.json").unlink(missing_ok=True)   # booted: claim governs
        return False
    ...
    if age < TAB_GRACE_SECONDS:
        return True
    # only HERE does the failure ledger increment
    _run_failures[session] = fails
    _run_backoff[session] = time.time() + RUN_BACKOFF_SECONDS
```

The ledger increments **only when a tab never claims within
`TAB_GRACE_SECONDS` (150)**. In this failure the tab *did* claim every single
time — `desk_bridge.claim_desk()` runs on a thread at startup and succeeds —
and *then* died 4s later. Every iteration therefore took the "booted: claim
governs now" branch, which deletes the lease and returns `False` **without
touching the counter**.

Result: `_run_failures["daybook"]` reached **1**, from one unrelated earlier
pass. `RUN_FAILURES_BEFORE_ALERT = 3` (line 91) was never reached.

**A desk was dead for an hour, restarted 112 times, and the owner was never
told.** The guard is written against "the window never connects"; it has no
concept of "the window connects and dies immediately", which is
indistinguishable from a healthy desk to every check it performs.

---

## 4. Stop-gap currently in place

`config/fleet.json`, `desks.daybook`:

```json
"daybook": { "resume": "fresh", "_why_fresh": "STOP-GAP 2026-08-18 …" }
```

This drops `--continue` for that one desk. Verified after the change:

- last exec line carries **no** `--continue`
- desk claimed at 05:02:59Z and was still alive 70s later (previously: 4s)
- it drained the stuck `ping` and posted its reply at 05:03:34Z
- launch count froze at 112

**This is a patch on one desk, not a fix.** Every other desk still has the
loaded gun, and the missing alert is untouched. Remove the `_why_fresh` block
when the real fix lands — `daybook` should go back to resuming its transcript.

---

## 5. Proposed fix

**(a) Make `has_history()` answer the question it claims to answer.**
Folder-non-empty is not "resumable". At minimum require a `.jsonl` containing
real conversation turns; better, stop predicting the CLI's decision at all and
move to (b), keeping `has_history()` only as a cheap negative check.

**(b) Never let a refused `--continue` kill a desk** — the durable half.
In `desk_bridge`, if the child exits within a few seconds of boot **and**
`No conversation found to continue` appeared in its output, relaunch once
without `--continue` and log the downgrade. That survives any future change in
how the CLI decides what is resumable, which (a) alone does not.

**(c) Teach the tab-loop guard about fast death.**
Count "claimed, then the claim's pid was gone in under N seconds" as a failure
in the same ledger as "never claimed". Without this, the next variant of this
bug is equally silent. Suggested: if a desk's claim pid dies < 30s after the
lease was cleared, increment `_run_failures` and apply the backoff.

### Acceptance criteria

- `python tools\discord\test_watchdog.py` exits 0 (these suites are unittest
  scripts run directly — `release.ps1` runs all five that way, not via pytest,
  which is not installed on this machine).
- A test asserts `has_history()` is **False** for a folder whose only `.jsonl`
  holds no resumable conversation.
- A test asserts the bridge relaunches without `--continue` after a child exit
  carrying `No conversation found to continue`.
- A test asserts a claim that dies within seconds of boot increments the
  failure ledger and reaches the alert threshold.
- `config/fleet.json` `desks.daybook` stop-gap removed, and the desk boots and
  stays up with `resume` back on `transcript`.

---

## 6. Evidence trail

- `state/logs/bridge-daybook.log` — 112 starts / 110 closes, exec line per try
- `state/logs/watchdog.log` — `terminal opened for daybook` every ~6s;
  `04:56:28Z terminal for daybook never claimed within 150s (failure #1)`;
  `04:57:55Z pruned stale claim daybook (pid 29080 is gone)`
- ConPTY reproduction capturing `No conversation found to continue` verbatim
- the CLI history folder for the daybook cwd — the 2.1.232 transcript that
  `has_history()` accepted and the CLI refused
