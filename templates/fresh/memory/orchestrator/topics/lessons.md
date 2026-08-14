# Lessons — things that cost real time once

Read this before "fixing" anything that looks wrong. Several entries exist specifically to stop a future session undoing a deliberate choice.

## THE ONE STRUCTURAL FLAW (distilled 2026-07-31 from four separate failures)

In one afternoon: (1) a session sat frozen on a permission dialog while every health signal read healthy; (2) an inbox watcher died with a closed tab and the session went deaf with no signal; (3) two sessions ended up on one desk because `spawn_session()` was called without the occupancy guard; (4) the user waited five minutes in silence and reasonably assumed a crash.

**They are all the same failure: too many invariants depend on an agent *remembering* to do something** — re-arm the watcher, acknowledge receipt, check the desk is free, delete the handled envelope. That is discipline, not architecture. **All four were already written down, and were broken the same day anyway. Documentation is not a mechanism.**

**So the next leap is not more features — it is making the system enforce its own invariants:** a watcher whose survival does not depend on the session relaunching it; a spawn that *cannot* duplicate a desk (guard inside `spawn_session`, not its callers — **done 2026-07-31**); a `!status` that distinguishes *alive* from *listening*; envelope deletion tied to handling rather than left as a step.

## The acknowledge rule cannot cover being ASLEEP (2026-08-01)

The owner waited **13 minutes** and sent *"Holaaa???"*. The ack rule in `/omnius` was followed — the previous voice note *was* acknowledged. The gap was different: the watcher caught the envelope instantly and exited to wake the session, and **the wake itself took 13 minutes to arrive**. There was nobody awake to ack.

**From a phone, "asleep", "hung", "stuck on a dialog" and "busy" are identical** — and in every one of them the session is the thing that cannot report. So the notice must come from the **watchdog**, the only always-on piece. Built the same day: an envelope undrained for 90 s gets one message in its own channel. Deliberately **cause-agnostic** — undrained means not-being-handled, whatever the reason — which makes it strictly more general than the permission-only stall marker built the night before.

**Generalise the lesson:** any invariant that depends on the blocked component reporting its own blockage is not an invariant. Put the check in something that cannot be blocked by the same cause.

## Verification lessons

- **The offline suite is not evidence.** Six commits touched `watchdog.py` before it was ever run; the first live run exposed `transcript.py` dying on emoji under cp1252 — *the same bug class `watchdog.log()` had already fixed*, reintroduced because the assertion only covered `status_banner`. **Any new tool that prints Discord text must reconfigure stdout.**
- **A recorded limitation is not evidence either.** A 2026-07-25 note said a background `inbox_watch.py` dies at turn boundaries. Disproved live 2026-07-31 — it survives and re-invokes the session. **Re-test when the memory disagrees with the harness.**
- **Entry counts and file sizes prove nothing about an archive.** `git gc` packed ~700 loose objects into 2 packfiles and an archive fell 837 → 144 entries with identical history. **Verify by extracting and reading `git log`.**
- **A successful push says nothing about *what* was pushed.** Read the tree back: `gh api repos/<o>/<r>/git/trees/main?recursive=1` and assert the gitignored things are absent.
- **A drained outbox and a posted message do not prove an attachment landed.** Query the message's `attachments` array.
- **`gh release create` returning a URL does not mean the upload finished.** Check `state=uploaded`.
- **`Register-ScheduledTask` piped to `Out-Null` swallows nothing but tells you nothing either.** An inline `$env:USERDOMAIN\$env:USERNAME` mangled through bash → `powershell -Command` produced an unresolvable principal (**HRESULT 0x80070534**) while the script cheerfully printed "registered". **Write PowerShell that needs escaping to a .ps1 file**, and read the task back before claiming it exists.
- **Never report a session healthy from claim data alone** — cross-check `#alerts` and the watchdog log for an unanswered `-perm` request.
- **When verifying a spawn, compare `startedAt`/pid against what you saw before** — never merely that a claim file exists. A poll of `if claim.exists(): break` matches the *pre-existing* claim instantly.
- **Check the account's repo list before concluding a project has no remote.** `git remote -v` being empty means *this clone* has no remote, not that none exists — `<your-remote>/omnius` had existed since 2026-07-27.

## Scope-of-exclusion lessons

- **`.gitignore` has no say over `pack.ps1`'s archive, and the archive is the *wider* exposure** — it becomes a GitHub release asset and a cloud copy. A Firebase admin key was correctly gitignored by `*-sa.json` and **would still have shipped in a release**. Git being right is not enough.
- **An exclusion list and a tracked-file list are different things — only the intersection is actually excluded.** A tar exclusion cannot remove anything already in git history, because the archive bundles `.git\`. Tracking `daybook\notes\` silently voided `-Work`'s entire promise within the hour.
- **A redaction filter that covers one channel says nothing about the others.** The outbox redacts token shapes in *text* but does **not** scan **attachments** — files post byte-for-byte. Check before attaching.
- **Over-broad exclusions cause silent loss.** `--exclude=state` matched at any depth and was deleting `projects/*/src/state/` (Redux/Zustand/XState) from the zip. A bare `*.key` is deliberately **not** excluded for the same reason. Anchor workspace excludes to the archive root, and **name what was excluded** — a file that vanishes without a word is indistinguishable from one that was never there.

## Windows lessons

- **`os.kill(pid, 0)` is not a safe liveness probe — it can terminate.** Always ctypes `OpenProcess`.
- **`atexit` and `signal` handlers do not run on a hard `TerminateProcess`** (taskkill /F, closed console, most supervisors), and `SIGTERM` is largely fictional on Windows. A killed watchdog **does** strand `state\watchdog\lock.json`. The lock is not the safety mechanism — **`acquire_lock`'s pid-liveness check is.** Never write logic assuming the lock disappears on exit.
- **cp1252 will kill a tool that prints emoji.** Our own schema uses 📁/🎛/🗄. Reconfigure stdout in every tool that prints Discord text.
- **`mimetypes.guess_type` reads the Windows registry** and rejects valid PNGs where `.png` maps to `image/x-png`. Sniff magic bytes.
- **PowerShell `>` and `Out-File` write UTF-16.** Blind utf-8 parsing made `.env` keys vanish.

## Bug classes seen more than once

- **Config values that are wrong but non-empty pass naive checks and kill the service later.** A bad guild id passed four independent `.env` parsers and killed the watchdog in the one unlogged gap in `main()`. Hence: `api.py` is the single parser, `config_problems()` validates *shape*, and every new setting **falls back** rather than being passed through (invalid `--effort`, unknown `permissionMode`, corrupt `fleet.json`).
- **Durable-before-side-effect.** `!reload` re-execed before the message cursor was persisted, so the next process re-read the same `!reload` and looped forever, ~3 s per cycle. Persist state *before* acting on the thing that can end the process.
- **One bad item must not abort a pass.** An unreachable channel used to starve every later channel *and* skip `flush_outboxes` — deaf and mute from one bad channel.

## Parsing and API hardening (why the code looks the way it does)

- **`.env` splits on ASCII line breaks only.** A `U+2028` line separator inside a pasted value made Python and PowerShell disagree on how many lines the file had — the same file, two different parses. Do not "simplify" the splitting.
- **`api()` caps `retry_after` at 60 s and raises beyond it.** Discord returns **hour-scale** rate limits on some endpoints (avatar changes among them), and the watchdog is single-threaded — it would have blocked for hours, alive and completely silent.
- **`last_ids.json` is written only when a cursor actually moved.** Writing every poll was ~10.5M rewrites/year of a file that changes only when a message arrives. The 2026-07-31 durable-before-side-effect fix preserves this — it writes on advance, not on every pass.

## Working-with-the-user lessons

- **Acknowledge before working.** A terminal shows thinking and tool calls; **Discord shows nothing at all** between the message and the reply. Ten quiet seconds and ten quiet minutes look identical from a phone, so silence reads as a crash. Send a one-line "got it, checking X" *first*.
- **Judge every bus change by how it feels on a phone, away from the desk** — not by how it reads in a terminal.
- **The rule is not about task size.** §3 (delegate, don't implement) was broken by running `npm install` in a project "because it was small".
- **A parked component does not need a live desk.** Spawn desks for *active* work only.
- **The user talks to project sessions directly in their channels.** Don't push envelopes into their inboxes unprompted — that creates two sources of truth for what a desk is doing.

## Security notes

- An audit subagent once copied the live `~/.claude/.credentials.json` into scratch dirs to bootstrap nested `claude` instances. Byte-identical, never left the machine, deleted. **Explicitly forbid copying credentials in future audit prompts.**
- **GitHub keeps leaked secrets indexed even after deletion** — the pre-push secret audit has to happen *before* the first push to any new remote, not after.
- Free-form notes are where a wifi password or onboarding PIN lands. **Scan before the first push of any note-like content.** Keep the user's existing discipline: reference credentials *by file*, never paste them.

## 2026-08-01 — "is the window gone?" cannot be answered by counting processes

`CREATE_NO_WINDOW` still **allocates a console** for the child; it only stops it
being *displayed*. So `conhost.exe` and `powershell.exe` keep appearing in
`Win32_Process` exactly as before, and a check that counts them reports the bug
is still live when it is fixed. Nearly filed a false "still broken" on that.

Ask the question you actually mean: poll `Get-Process | Where MainWindowHandle
-ne 0` and look for a **visible** window. Same shape as the stall-marker lesson —
measure the symptom the owner experiences, not a proxy that correlates with it.

Related: the watchdog runs under `pythonw`, which has no console of its own, so
**every** console child it starts becomes a window. That is a property of the
service, not of any one call site, which is why the test is an ast walk over the
source rather than a note telling the next author to remember.

## A signal needs a mechanical clear, or it becomes a liar (2026-08-01)

The banner showed `[!!] STALLED orchestrator` for hours over a dialog answered
long before. `permission_relay` wrote `state\permissions\<id>.stalled`, but every
clear that existed (an answer arriving, `kill_session`, `spawn_session`) had to
be *caused* by someone. The ordinary ending — the desk is answered at its own
keyboard and carries on — cleared nothing, so the marker outlived the dialog by
construction. Fixed in `turn_end_hook`: a turn that ended is proof no dialog is
pending, because a blocked session never reaches the Stop hook.

**The rule: do not add a state file, marker or alarm unless something clears it
without a human remembering to.** And the day's real lesson — the owner spent it
watching "the same problems" while detection layers were stacked on top of an
indicator that was already lying. **Check the top-level surface the owner
actually looks at before building anything underneath it.**

## `wt --help` is a GUI dialog, not stdout (2026-08-02)

Checking whether Windows Terminal supports a flag, the obvious `wt --help`
printed nothing useful and popped a **modal Help window on the owner's
desktop** — twice, while he was working. `wt` writes no help to stdout; it
opens a dialog, and it does the same for **any invalid argument**.

- To learn a `wt` flag, read the docs — never probe the binary.
- Relevant beyond tidiness: a malformed `wt` invocation in `open_tab` would
  show that dialog *instead of opening the tab*, so the desk would never
  claim, and before the tab-failure backoff existed it would have popped a
  dialog every 150 s forever. Bounded now, but the shape is worth remembering:
  **a GUI-on-error tool inside an automated loop is a focus-stealing bomb.**
