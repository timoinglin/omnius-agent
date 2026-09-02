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

0. **Can this instance publish at all?** — `python <root>\tools\repo_access.py`

   ```
   role: maintainer   -> continue
   role: user         -> STOP. Report, in one friendly line: this install
                         receives updates, it does not publish them. Their own
                         commits are kept and replayed by !update. Nothing is
                         broken and there is nothing to fix.
   ```

   **This step is first for a reason.** Omnius is a PUBLIC repo: exactly one
   instance owns the remote, and every other install is downstream of it —
   update-only, by design, not by misconfiguration. Step 1 used to come first
   and told the desk to `git push` when the tree was ahead — which a user's
   tree usually is, since their own commits live there. So their desk pushed,
   GitHub rejected it, and they got a raw auth error instead of the calm
   refusal `release.ps1` already has waiting for them. The script asks this
   same question in its own preflight; the skill has to ask it *before* it
   touches git, or the good manners never get a chance to speak.

1. **State check**: `git status -sb` at the root.
   - Dirty tree → **stop and ask** — committing his half-done work is never
     part of a release.
   - Clean but ahead of origin → `git push` first (you are the maintainer;
     step 0 established that). A release order includes shipping main:
     `release.ps1` refuses an unpushed tree on purpose ("a release must be a
     pushed commit"). Name the pushed commits in the report.
2. `powershell -NoProfile -File <root>\release.ps1`
   — no flags. It preflights (main, clean, pushed), runs **every suite
   `release.ps1` lists** (its own `$suites` array — today watchdog, daybook
   storage, email, documents, telegram; the list is the authority, not a count
   written here),
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
  `memory\orchestrator\topics\public-repo.md` — read it before deviating. That
  file exists only on the maintainer's instance; if it is not here, step 0 has
  already told you why, and this skill is not yours to run.
- After a successful cut, other machines pick it up with `!update`.
