---
name: backup
description: Build the personal backup zip of the whole workspace with pack.ps1 and copy it to the configured backup folder. Run when the user asks for a backup, or before risky workspace-wide changes.
---

# /backup — the workspace in one zip, copied off-drive

Root verb: it packs the WHOLE workspace (memory, config, daybook, projects —
`state\`, `.env` and caches excluded by design), so run it against the
workspace root whatever desk received the ask.

## Steps

1. `powershell -NoProfile -File <root>\pack.ps1 -Yes`
   — **always `-Yes`**: the plain run can stop on a Read-Host confirmation,
   and a headless run freezes on a prompt nobody can see. The zip lands next
   to the workspace as `omnius-<date>.zip`; the script prints the path.
2. Destination: read it, never hard-code it —
   `python -c "import sys; sys.path.insert(0, r'<root>\tools'); import omnius_config; print(omnius_config.backup_folder())"`
   Empty → no folder is configured: report the zip path, say the copy was
   skipped and that `config\omnius.ini` `[backup] folder` sets it. Done.
3. Copy the zip there (`Copy-Item <zip> <folder>`), then **verify the copy**:
   destination file exists and its length equals the source's. A copy you did
   not verify is a backup that may not exist (docs\RELIABILITY.md).
4. Reply with receipts: zip name, size in MB, where the copy landed, and how
   long the pack took. On failure, the failing step and its actual error —
   never "backup done" on a pack that errored.

## Never

- Never use `-Fresh` here — that builds the RELEASE seed (personal data
  stripped), which is the opposite of a backup. `/release` owns that flag.
- Never delete older zips on your own; rotation is the owner's call.
