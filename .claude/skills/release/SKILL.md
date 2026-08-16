---
name: release
description: Re-cut the rolling release - push main if needed, then release.ps1 (preflight, all suites, fresh zip, move the rolling tag, upload, verify). Run only when the user explicitly asks for a release or to ship/re-cut.
---

# /release — move `rolling` to the current green main

**Outward by nature** — it publishes code and a zip to GitHub. The owner's
explicit ask (this skill being invoked) is the authorisation; nothing here
asks again for the steps the release inherently contains. Anything BEYOND
those steps keeps its own brakes.

## Steps

1. **State check first**: `git status -sb` at the root.
   - Dirty tree → **stop and ask** — committing his half-done work is never
     part of a release.
   - Clean but ahead of origin → `git push` first. A release order includes
     shipping main: `release.ps1` refuses an unpushed tree on purpose
     ("a release must be a pushed commit"). Name the pushed commits in the
     report.
2. `powershell -NoProfile -File <root>\release.ps1`
   — no flags. It preflights (main, clean, pushed), runs **all four suites**,
   builds `pack.ps1 -Fresh -Yes`, force-moves the `rolling` tag, replaces the
   release assets, and verifies `releases/latest` actually serves the new
   zip. `-SkipTests` exists and is not yours to use — a release that skipped
   its suites is a regression with a version number.
3. Relay the script's own receipts: old → new commit, zip size, and the
   verified `releases/latest` line. On failure, name the stage that failed
   and stop — no retry loops, no tag surgery by hand; `release.ps1` is the
   only tool that moves the tag.

## Notes

- The rolling model (one release, re-cut, no changelog) is documented in
  `memory\orchestrator\topics\public-repo.md` — read it before deviating.
- After a successful cut, other machines pick it up with `!update`.
