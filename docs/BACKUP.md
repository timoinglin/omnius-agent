# Backup & restore

Your Omnius folder holds things no command can rebuild: your memory, your
projects, your daybook notes, your Discord history, your `.env`. This is how you
copy all of it, and how you get it back.

> **New machine, fresh Omnius, nothing to restore?** That is a different
> document — `docs\NEW-INSTANCE.md`. This one is about *your* instance surviving.

---

## Make one

```
pack.bat
```

Or `powershell -ExecutionPolicy Bypass -File pack.ps1`. It writes
`omnius-YYYY-MM-DD.zip` **next to** the workspace folder, not inside it.

Or ask for it in Discord — `/backup` packs it *and* copies it to the folder in
`config\omnius.ini` `[backup] folder`, then verifies the copy actually opens.

### What is in it

**The whole folder, as it stands.** That is the rule, and it is deliberate:

- `memory\` — everything Omnius knows
- `projects\` — all of them, including each project's own `.git\`
- `daybook\notes\` — your personal notes
- `media\` — the archive of everything sent and received
- `state\` — claims, inboxes, and **your Discord conversation history**
  (`state\transcripts\`)
- `config\` — including `routines.json`, your scheduled jobs
- `.git\` — the full history of the workspace repo
- **`.env`** — your Discord token and every other secret
- **key files** — `*.pem`, `serviceAccount*.json`, `id_rsa`, `.secrets\`

### What is left out

Only what a command rebuilds. Nothing else:

| Skipped | Comes back from |
|---|---|
| `node_modules\` | `npm ci` / `npm install` |
| `.next\`, `.turbo\` | the next build |
| `__pycache__\`, `.venv\` | Python, on first run |
| `dist\`, `build\` | the next build |
| `*.log` | yesterday's noise |
| `settings.local.json` | `install.bat` writes this machine's hook paths |

On this instance that is ~1.8 GB skipped against a ~475 MB zip — the
rebuildables are four times the size of everything real.

`.next\` earns its place on that list twice over: a running `next dev` holds
`.next\dev\lock` open, tar cannot read it, and **the whole archive is refused**.
A backup must not depend on which dev servers happen to be running.

---

## ⚠ This zip contains your secrets

`.env`, saved browser sessions under `state\web\`, and any key file in the tree.
That is what makes it a *restore* rather than a folder copy — and it is why:

- keep it on a drive **you** control
- never a shared folder, never cloud sync, **never a GitHub release asset**
- the public release (`pack.ps1 -Fresh`) is a different product and carries none
  of it — it refuses to build if anything identifies you

---

## Restore it

Onto a fresh PC, or over a dead install. **In this order:**

```
1. unzip           into the PARENT folder (unzipping recreates omnius\)
2. install.bat     prerequisites + the parts that were left out
3. start-omnius.bat
```

### Do NOT install first and copy the backup over

It is the natural instinct and it makes more work, not less. A clean install
creates its own `.env`, walks you through registering a **new Discord bot**, and
lays down its own `.git\` — all of which your backup then overwrites. Two `.git\`
folders overwriting each other is exactly where a restore goes wrong.

The backup is already a complete Omnius. `install.bat` only adds what is missing.

### `install.bat` never overwrites your `.env`

Worth stating plainly, because it is the thing people are afraid of:

```
if (Test-Path .env) { OK '.env exists' }     # touches nothing
else                { Copy .env.example .env }
```

It only *creates* one when there is none. It does **read** it, for one check —
see `MACHINE_NAME` below.

### What `install.bat` rebuilds, and what it does not

**It does:** Python packages (whisper, playwright, pymupdf …), the `watch`
skill, `tools\remotion\node_modules` (via `npm ci`, exact shipped lockfile), and
this machine's hook paths into each desk's `settings.local.json`.

**It does not:** a **project's** own `node_modules\` or `.next\`. It only knows
about `tools\remotion`. Run `npm install` in a project the first time you work
on it again. One command per project — not a broken restore, but not automatic.

---

## Three things that bite on a new machine

**1. `MACHINE_NAME` still says the old PC.** Your `.env` travelled, and it
carries that line. Every desk claim then looks *foreign* — "another machine owns
this desk" — and nothing says why. `install.bat` warns you now. Fix the line (or
delete it), then:

```
python tools\orchestrator\fleet_ops.py status --prune
```

**2. Your routines do not fire.** They travelled (`config\routines.json` is in
the zip) but each is stamped with the old machine's name. Once, in Discord:

```
!cron adopt all
```

**3. Renamed Discord channels come back unmapped.** Channel→desk pins live in
`state\watchdog\channels.json`, which now travels — so this only bites if you
restore onto a **different Discord server**. Rename the channel back to re-pin
it, then rename it freely again.

---

## Verify a backup before you need it

A truncated zip with a plausible name is worse than no zip: it looks fine until
the day it matters. `pack.ps1` deletes a failed archive rather than leaving one,
and `/backup` re-verifies the copy at the destination. To check one by hand:

```
tar -tf ..\omnius-2026-09-01.zip | find /c "/"      REM entry count
tar -tf ..\omnius-2026-09-01.zip | find "omnius/.env"
```

An entry count in the thousands and a `.env` line means it can restore you.

---

## The other two products

`pack.ps1` builds three different things. Only the first is a backup.

- **`pack.ps1`** — backup. Everything. Restores *this* machine.
- **`pack.ps1 -Work`** — the personal→work move. System only: leaves
  `daybook\notes\`, every project and `media\` behind. Refuses if that content is
  tracked in git, because the bundled `.git\` would carry it anyway.
- **`pack.ps1 -Fresh`** — the public release. Product only, scrubbed memory
  seed, no history, no secrets. Refuses to build if anything identifies you.
