# Updating an Omnius instance

Three ways in, one path underneath. Pick whichever you can reach.

| From | Do this |
|---|---|
| **Discord** (the normal way) | `!update` to preview, `!update go` to apply |
| **PowerShell, anywhere** | `irm https://raw.githubusercontent.com/timoinglin/omnius-agent/main/update.ps1 \| iex` |
| **At the install** | `powershell -ExecutionPolicy Bypass -File update.ps1` |

All three end up running the same `update.ps1`. Your own changes are kept, and
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
4. **Run the suite.** A failure the update *introduces* rolls everything back to
   the previous commit and re-stamps, so the machine is never left half-updated.
5. **Restart the watchdog**, because a running service keeps the code it was
   born with. (`!update go` skips this step and reloads itself instead, so the
   handshake in step 6 still applies.)
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
