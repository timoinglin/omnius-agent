# Updating an Omnius instance

**The one that always works:** open PowerShell **in your Omnius folder** and run

```powershell
cd C:\path\to\omnius
irm https://raw.githubusercontent.com/timoinglin/omnius-agent/main/update.ps1 | iex
```

Proven on a live instance 2026-08-19. From that folder the script cannot fail to
find the install, which is the one thing auto-discovery can get wrong: a
watchdog started by hand leaves the scheduled task's working directory empty.

Four doors, one path underneath. Pick whichever you can reach.

| From | Do this |
|---|---|
| **Discord** (the normal way) | `!update` to preview, `!update go` to apply |
| **Discord, when the watchdog is too old for `!update`** | tell a desk: *"run `git pull --rebase origin main`, then `powershell -ExecutionPolicy Bypass -File update.ps1`"* — then `!reload` |
| **PowerShell** (most reliable) | `cd` to the Omnius folder, then `irm https://raw.githubusercontent.com/timoinglin/omnius-agent/main/update.ps1 \| iex` |
| **At the install** | `powershell -ExecutionPolicy Bypass -File update.ps1` |

All of them end up running the same `update.ps1`. Your own changes are kept, and
nothing personal moves — `.env`, `config\`, `memory\`, `projects\`, your notes
and `state\` are gitignored and never touched.

## What it does, in order

1. **Find the install** — the `-Path` you gave, or the folder the script sits
   in, or the registered `Omnius Watchdog` task, or the current directory.
2. **Fetch and rebase.** Your commits are replayed on top of the release;
   uncommitted work rides an autostash. Local changes *survive* an update
   instead of blocking it.
3. **Stamp hooks and permissions** — `fix_hook_paths.py`, `sync_permissions.py`.
   This happens **before** the tests, because new code raises the bar (a wider
   allow-list, a new hook) and these idempotent stamps are what meet it.
4. **Run the suite — twice.** Once BEFORE the pull, to learn what is already red
   on this machine, and once after. Only a failure the update *introduces* rolls
   everything back (and re-stamps, so the machine is never left half-updated). A
   check that was already failing is this instance's housekeeping — an
   over-budget memory file, a desk short of the allow-list — and blaming the
   release for it reverted perfectly good updates until 2026-08-19.
5. **Restart the watchdog**, because a running service keeps the code it was
   born with — and the *live* one, which is not always the scheduled task. A
   watchdog started by hand holds the lock while the task sits Ready, so
   restarting only the task launches a second one that exits on that lock:
   updated on disk, still running the old code, with nothing saying so. The
   process holding the lock is stopped, the task started, and a fresh beacon
   waited for rather than assumed. Two cases skip it and say so: `!update go` (it reloads itself, so
   the handshake below still applies), and **a desk running the script**, which
   is detected by the `OMNIUS_SESSION` the watchdog stamps into every run.
   Restarting from inside a desk would kill the process printing the result -
   the update would finish and Discord would simply go quiet. Type `!reload`
   after that one.
6. **The handshake** (Discord path): the new watchdog must report back healthy —
   one that crash-loops or sits deaf reverts to the previous commit on its own
   and says so.

## Why there is a script at all

The update logic used to live only inside the watchdog. That is a bootstrap
trap: when the logic is wrong, **every** instance is stranded at once and the
fix cannot reach the machines that need it — including ones their owner only
talks to through Discord. It happened twice on 2026-08-19.

So `!update go` now **fetches `update.ps1` from origin first and runs that**.
The logic doing the work is always the newest published version, never the copy
this process happened to start with. A bug in the updater costs one bad update
instead of every future one.

The `irm` one-liner is the same file, reached without needing the instance to
work at all. That is the escape hatch: it repairs a machine whose watchdog is
too old, too broken, or simply not running.

**A desk cannot run that one-liner, and should not be asked to.** Claude Code's
own safety classifier blocks downloading code and executing it in one gesture,
and a desk that worked around it — fetching to disk, then running the file —
would be defeating a fence rather than respecting it. A desk asked to do this
will say so and stop, which is correct. Give a desk the two local commands in
the table instead (`git pull`, then the file), or run the one-liner yourself.

## When something stops it

Every refusal leaves the instance exactly as it was and says why.

**"your local changes and the new release edit the same lines"** — you changed a
file the release also changed. Nothing is lost: your version is still there and
you are still on the old commit. Take the incoming version of a file with
`git checkout -- <file>`, or fold your change into it, then update again.

**"the new code failed its own suite — rolled back"** — the release is at fault,
not your machine. Say so; a published commit should never do this.

**"not attached to GitHub"** — a zip install that never ran `install.bat`. Run
it once (it attaches without touching your files), then update.

**Anything else, or the watchdog is dead** — use the `irm` one-liner. It needs
nothing from the instance except git, Python and the folder itself.

## Flags

| Flag | Meaning |
|---|---|
| `-Path <folder>` | Which install to update, when it cannot be found automatically |
| `-NoRestart` | Update but leave the watchdog alone (what `!update go` passes) |
| `-NoTests` | Skip the suite. Emergencies only — the suite is the only thing that catches a bad release |
| `-Quiet` | Only the result lines |

With parameters through the one-liner:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/timoinglin/omnius-agent/main/update.ps1))) -Path D:\omnius
```

## What updating never does

- Touch `.env`, `config\`, `memory\`, `projects\`, `daybook\notes\`, `media\` or
  `state\` — all gitignored, all yours.
- Push anything. Almost every instance is a read-only clone of the public repo;
  `python tools\repo_access.py` says which yours is.
- Force anything through. Every stop leaves the instance runnable on the commit
  it was already on.
