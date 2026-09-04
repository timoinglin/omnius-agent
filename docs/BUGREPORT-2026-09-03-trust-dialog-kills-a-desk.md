# Bug report — a desk on a never-opened component folder dies 9s after boot, and the nudge is what kills it (2026-09-03)

**For:** the session that builds Omnius (`tools\discord\watchdog.py`,
`tools\bridge\desk_bridge.py`).
**Status:** root cause **confirmed by reproduction**, with the CLI's own screen
captured. **Unfixed** — the defect is in the watchdog/bridge, not in any project.
**Reported by:** `orchestrator` on `LENOVO`, 2026-09-03, after the owner asked
in `#omnius`: *"why is sandbox.silver-android not responding?"*

---

## 1. Symptom

The owner wrote `hey 🙂` in `#silver-android`. Nothing ever answered. The desk
booted and died twice, **9 seconds each time**, always in the same second the
bridge typed the mail nudge:

```
13:03:36  terminal opened for sandbox.silver-android
13:03:36  #silver-android -> inbox sandbox.silver-android (run started): "hey"
13:03:37  desk claimed (session pid 61100)
13:03:46  mail waiting -> typed the nudge into the live session
13:03:46  desk closed
13:03:48  terminal for sandbox.silver-android claimed and DIED within 9s (failure #1) - backing off 300s

13:08:49  desk claimed (session pid 61508)
13:08:58  mail waiting -> typed the nudge into the live session
13:08:58  desk closed
```

The envelope stayed in `state\inbox\sandbox.silver-android\` the whole time. To
the owner this is a desk that ignores him; to the watchdog it is a crash-looping
desk that will page `#alerts` on the third strike.

## 2. Root cause — the trust dialog is on screen, and Enter answers "No, exit"

Running the bridge by hand and capturing the pty shows what the desk was
actually displaying when the nudge arrived:

```
Accessing workspace:
<root>\projects\sandbox\silver-android

Quick safety check: Is this a project you created or one you trust?
...
❯ No, exit
  Yes, I trust this folder
Enter to confirm · Esc to cancel
[bridge] mail waiting -> typed the nudge into the live session
[bridge] desk closed
```

**The default-selected option is `No, exit`.** The bridge types the nudge and
presses Enter, the dialog takes that Enter, and the CLI exits — cleanly, which is
why nothing anywhere records an error. The nudge is not landing next to a live
prompt; it is answering a modal dialog with the worst of its two options.

### Why that dialog was there at all

`watchdog.py:2463` and `fleet_ops.py:295` both pre-accept trust for the **parent**:

```python
ensure_trusted(ROOT if session == "orchestrator" else cwd.parent)
```

But a desk's cwd is the **component folder**, and Claude Code stores trust
**per directory** in `~/.claude.json`. So the folder that was pre-trusted is not
the folder the CLI opens. Verified in that file, same machine, same minute:

```
TRUSTED   <root>/projects/sandbox        <- what ensure_trusted() stamped
untrusted <root>/projects/sandbox/silver-android   <- where the desk runs
```

Existing desks are unaffected only because a human once accepted the dialog in
those folders by hand. **The bug appears on the first component folder nobody has
ever opened in person** — i.e. on every newly stamped project, which is exactly
when nobody is sitting there to click.

## 3. What is NOT the cause (each ruled out by test, not by reasoning)

- **Not the CLI, the model or the permissions.** A headless run from that exact
  cwd with that exact `--settings` answers normally: `rc=0`, 3.8s.
- **Not the project config.** `projects\sandbox\.claude\settings.json` is 2,587
  bytes against `the-campus`'s 2,589, same keys; that desk has taken nudges all
  day.
- **Not a window the owner closed.** It reproduced identically on a spawn nobody
  touched.

## 4. Suggested fix

1. **`ensure_trusted()` must stamp the folder the CLI will actually open** — the
   desk's own `cwd`, not `cwd.parent`. Stamping both is cheap and covers
   `--add-dir` roots too. This alone closes the bug.
2. **The bridge should not press Enter into a session it has never seen a prompt
   from.** A nudge before the first prompt line is indistinguishable from an
   answer to a dialog. Gate the first nudge on having observed the input box, or
   send the text without a trailing Enter until then.
3. **Log the exit code and the last screen when a desk dies inside the boot
   window.** `desk_bridge.py:590` logs a bare `desk closed`; `rc` is right there
   in the frame and never written down. Every minute of this investigation was
   spent recovering information the bridge already had.

## 5. Second defect found in the same capture (independent, non-fatal)

The bridge's reader thread dies on non-ASCII output before the desk even boots:

```
Exception in thread Thread-6 (_readerthread):
  File "...\subprocess.py", line 1614, in _readerthread
    buffer.append(fh.read())
  File "...\encodings\cp1252.py", line 23, in decode
UnicodeDecodeError: 'charmap' codec can't decode byte 0x81 in position 386
```

A `subprocess` pipe is being read with the Windows ANSI codepage instead of
UTF-8. It is swallowed as a thread exception, so whatever that pipe was
collecting is silently lost. Needs `encoding="utf-8", errors="replace"`.

## 6. How to reproduce from scratch

1. Stamp a new project with a component: `fleet_ops.py new-project probe --components thing`.
2. Confirm `C:/Users/<you>/omnius/projects/probe/thing` is **absent** from
   `~/.claude.json` `projects` (nobody has opened it).
3. Send any message to `#thing` in Discord.
4. Watch `state\logs\bridge-probe.thing.log`: claim, nudge, `desk closed`, ~9s.
5. To see the cause, run the bridge with its output captured:
   `python tools\bridge\desk_bridge.py probe.thing > capture.txt 2>&1` — the trust
   dialog is in `capture.txt` with `No, exit` selected.

## 7. Environment

- `LENOVO`, Windows 11, Python 3.14, Claude Code at `%USERPROFILE%\.local\bin\claude.EXE`
- Desk argv: `claude --add-dir <root> --settings <project>\.claude\settings.json --model opus --effort xhigh [--continue]`
- Evidence: `state\logs\watchdog.log` and `state\logs\bridge-sandbox.silver-android.log`, 2026-09-03 13:03–13:09Z.

## 8. Workaround until it is fixed

Add the component folder to `~/.claude.json` with
`"hasTrustDialogAccepted": true`, or open that folder once by hand and accept the
dialog. The desk then boots and answers normally.
