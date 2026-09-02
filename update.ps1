# Omnius updater - the one that can always reach a stuck instance.
#
#   irm https://raw.githubusercontent.com/timoinglin/omnius-agent/main/update.ps1 | iex
#   powershell -ExecutionPolicy Bypass -File update.ps1          (at the install)
#   & ([scriptblock]::Create((irm <url>))) -Path D:\omnius -NoRestart
#
# WHY THIS EXISTS. The update logic used to live only inside the watchdog, so a
# mistake in it stranded every instance at once - twice on 2026-08-19 - and the
# fix could not reach machines their owners only talk to through Discord. A
# script fetched from the repo does not have that problem: it is never the
# broken copy. `!update go` now fetches THIS FILE first and runs it, so the
# logic that updates you is always the newest one, not the one already in
# memory.
#
# WHAT IT DOES, in order, refusing loudly rather than half-doing:
#   1. find the install (parameter, or the folder it sits in, or the registered
#      scheduled task, or the current directory)
#   2. fetch, then REBASE local work onto the release (autostash) - your commits
#      and edits survive; a real conflict stops and puts everything back
#   3. stamp hooks and permissions BEFORE judging - new code raises the bar and
#      these are what meet it
#   4. run the suite; NEW failures roll the whole thing back and re-stamp
#   5. restart the watchdog so the new code is actually running
#
# It is safe to run repeatedly, and safe to run while Omnius is running.
#
# DESIGN NOTE (from get.ps1, learned 2026-08-11): this is `iex`'d into the
# user's own session, so it must leave nothing behind - no Set-StrictMode, no
# session-level $ErrorActionPreference, no cd. Everything lives in one function
# with local preferences.
param(
  [string]$Path,
  [switch]$NoRestart,      # used by the watchdog: it reloads itself afterwards
  [switch]$NoTests,        # emergencies only; the release notes say so
  [switch]$Quiet
)

function Update-Omnius {
  param([string]$Path, [bool]$NoRestart, [bool]$NoTests, [bool]$Quiet)
  $ErrorActionPreference = 'Continue'      # function-local, never the session's
  $ProgressPreference    = 'SilentlyContinue'

  function Say([string]$tag, [string]$msg, [string]$colour = 'Gray') {
    if (-not $Quiet -or $tag -in @('X', 'OK')) {
      Write-Host ("  [{0}] {1}" -f $tag, $msg) -ForegroundColor $colour
    }
  }

  Write-Host ''
  Write-Host '  OMNIUS - update' -ForegroundColor Cyan
  Write-Host ''

  # --- 1. find the install ----------------------------------------------------
  # Four ways, because the three that can fail each fail differently: the
  # parameter (explicit), the folder this file sits in (a local run), the
  # registered task (an `irm` run on a machine that installed normally), and
  # the current directory (someone standing in it).
  # AN EXPLICIT -Path IS OBEYED OR REFUSED, never quietly replaced. Falling
  # through to the other candidates meant `-Path D:\typo` updated whatever
  # install the script happened to find instead - a different machine's fleet,
  # silently, while printing a cheerful OK. Found by tools\update_drills.ps1
  # 2026-08-19, which is the entire reason that file exists.
  if ($Path) {
    if (-not (Test-Path (Join-Path $Path 'tools\discord\watchdog.py'))) {
      Say 'X' "could not find an Omnius install at $Path" 'Red'
      Write-Host '      -Path must name the folder that holds tools\discord\watchdog.py.'
      return 1
    }
    $Path = (Resolve-Path $Path).Path
  }
  $root = $null
  foreach ($cand in @(
      $Path,
      $(if ($PSScriptRoot) { $PSScriptRoot } else { $null }),
      $(try {
          $t = Get-ScheduledTask -TaskName 'Omnius Watchdog' -ErrorAction Stop
          $wd = ($t.Actions | Select-Object -First 1).WorkingDirectory
          if ($wd) { $wd } else {
            $a = ($t.Actions | Select-Object -First 1).Arguments
            if ($a -match '"([^"]*)\\tools\\discord\\watchdog\.py"') { $Matches[1] }
          }
        } catch { $null }),
      (Get-Location).Path)) {
    if (-not $cand) { continue }
    $cand = $cand.Trim('"')
    if (Test-Path (Join-Path $cand 'tools\discord\watchdog.py')) { $root = (Resolve-Path $cand).Path; break }
  }
  if (-not $root) {
    Say 'X' 'could not find an Omnius install.' 'Red'
    Write-Host '      Run it from the install folder, or name it:'
    Write-Host '      & ([scriptblock]::Create((irm <this url>))) -Path C:\path\to\omnius'
    return 1
  }
  Say 'OK' "install: $root" 'Green'

  foreach ($cmd in @('git', 'python')) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
      Say 'X' "$cmd is not on PATH - install it (or rerun install.bat) and try again" 'Red'
      return 1
    }
  }
  if (-not (Test-Path (Join-Path $root '.git'))) {
    Say 'X' 'this install is not attached to GitHub - run install.bat once, then update' 'Red'
    return 1
  }

  # --- 1b. BASELINE: what is already red, before we touch anything ------------
  # This machine's own housekeeping is not the release's fault. A memory file
  # over budget, a desk short of the allow-list - those fail on the CURRENT code
  # too, and judging the update by the raw exit code blames the release for them
  # and reverts a perfectly good update. Found on the owner's own machine
  # 2026-08-19, where "memory budget: topics/discord-fleet.md <= 13,000" would
  # have rolled back every release until someone noticed.
  # A REBASE LEFT HALF-DONE blocks everything below with a git error nobody
  # outside a terminal can read ("cannot pull with rebase: you have unstaged
  # changes" / "a rebase is in progress"). It happens for ordinary reasons - the
  # machine slept, the window was closed, the watchdog was restarted mid-update.
  # Clearing it is safe: --abort is exactly "put the tree back the way it was".
  foreach ($d in @('.git\rebase-merge', '.git\rebase-apply')) {
    if (Test-Path (Join-Path $root $d)) {
      Say '!' 'a previous update was interrupted and left a rebase half-finished' 'Yellow'
      & git -C $root rebase --abort 2>&1 | Out-Null
      Say 'OK' 'that rebase was aborted - your tree is back as it was; carrying on' 'Green'
      break
    }
  }

  $baseFails = @()
  if (-not $NoTests) {
    Say '..' 'baseline: running the suite on the CURRENT code first'
    $baseRes = Get-SuiteResult $root
    if (-not $baseRes.Ran) {
      Say 'X' 'the test suite could not RUN on the current code - nothing was pulled' 'Red'
      Write-Host '      There is no baseline to judge an update against, so this stops here.'
      $baseRes.Err -split "`n" | ForEach-Object { Write-Host "      $_" }
      Write-Host '      Run: python tools\discord\test_watchdog.py'
      return 1
    }
    $baseFails = $baseRes.Fails
    if ($baseFails.Count) {
      Say '!' "$($baseFails.Count) check(s) already failing here - noted, not held against the update" 'Yellow'
      $baseFails | Select-Object -First 3 | ForEach-Object { Write-Host "      $_" }
    }
  }

  # --- 2. fetch and rebase ----------------------------------------------------
  $before = (& git -C $root rev-parse --short HEAD 2>$null)
  Say '..' "at $before - fetching"
  & git -C $root fetch --quiet origin main 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) { Say 'X' 'could not reach GitHub' 'Red'; return 1 }

  $behind = (& git -C $root rev-list --count HEAD..origin/main 2>$null)
  if ("$behind".Trim() -eq '0') {
    Say 'OK' "already current at $before - nothing to do" 'Green'
    if (-not $NoRestart) { Restart-Watchdog $root }
    return 0
  }
  Say '..' "$("$behind".Trim()) commit(s) behind - rebasing your work onto them"

  # Anything of yours is replayed on top; uncommitted work rides the autostash.
  $stashBefore = (& git -C $root rev-parse -q --verify refs/stash 2>$null)
  $out = & git -C $root -c rebase.autoStash=true pull --rebase origin main 2>&1
  $pullRc = $LASTEXITCODE
  # An exit code describes the rebase, NOT the tree it left: when the rebase
  # lands but restoring the autostash conflicts, git exits 0 with merge markers
  # in the files. Ask the tree.
  $conflicted = @(& git -C $root diff --name-only --diff-filter=U 2>$null | Where-Object { $_ })
  if ($pullRc -ne 0 -or $conflicted.Count) {
    & git -C $root rebase --abort 2>&1 | Out-Null
    & git -C $root checkout -- . 2>&1 | Out-Null
    & git -C $root reset --hard $before 2>&1 | Out-Null
    $stashAfter = (& git -C $root rev-parse -q --verify refs/stash 2>$null)
    $kept = $null
    if ($stashAfter -and $stashAfter -ne $stashBefore) {
      & git -C $root stash pop 2>&1 | Out-Null
      # "Nothing was lost" has to be TRUE. If the pop failed the work is still in
      # the stash - name it, rather than print a reassurance the tree does not
      # support.
      if ($LASTEXITCODE -ne 0) { $kept = $stashAfter }
    }
    Say 'X' 'your local changes and the new release edit the same lines' 'Red'
    if ($conflicted.Count) { $conflicted | ForEach-Object { Write-Host "      $_" } }
    if ($kept) {
      Write-Host "      Your uncommitted work is SAFE IN THE STASH ($($kept.Substring(0, [Math]::Min(12, $kept.Length))))" -ForegroundColor Yellow
      Write-Host '      but could not be re-applied. Recover it with:  git stash list'
      Write-Host '      then  git stash pop'
    }
    Write-Host "      Still on $before, with your version intact."
    Write-Host '      Somebody has to choose which version wins. From Discord you do not'
    Write-Host '      need a shell for it: say in the channel "take the new version of'
    Write-Host '      <file>" (or "of all of them") and the desk runs it, then `!update go`.'
    Write-Host '      If your change was deliberate, say "fold my change into the new one".'
    Write-Host '      At a terminal it is `git checkout -- <file>`, then run this again.'
    return 1
  }
  $after = (& git -C $root rev-parse --short HEAD 2>$null)
  Say 'OK' "pulled $before -> $after" 'Green'

  # --- 3. stamp BEFORE judging -----------------------------------------------
  # New code raises the bar (a wider allow-list, a new hook) and these
  # idempotent stamps are what meet it. Judged first, every such release looked
  # like the update had broken something and rolled itself back.
  Stamp-Machine $root

  # --- 4. the suite decides ---------------------------------------------------
  if ($NoTests) {
    Say '!' 'suite skipped (-NoTests)' 'Yellow'
  } else {
    Say '..' 'running the test suite on the new code'
    $postRes = Get-SuiteResult $root
    $postFails = $postRes.Fails
    # ONLY the delta. A check that was already red before the pull is this
    # machine's housekeeping; a check the update BREAKS is the release's fault
    # and the only thing worth reverting for. A suite that did not RUN at all is
    # the third case, and it is a hard failure: no evidence is not good news.
    $introduced = @($postFails | Where-Object { $baseFails -notcontains $_ })
    if ($introduced.Count -or -not $postRes.Ran) {
      $kept = Undo-Pull $root $before
      Stamp-Machine $root        # restore stamps to match the restored code
      if (-not $postRes.Ran) {
        Say 'X' "the test suite could not RUN on the new code - rolled back to $before" 'Red'
        Write-Host '      A crash is not a pass, so the update was reverted.'
        $postRes.Err -split "`n" | ForEach-Object { Write-Host "      $_" }
      } else {
        Say 'X' "the update BROKE $($introduced.Count) check(s) that were green - rolled back to $before" 'Red'
        $introduced | Select-Object -First 5 | ForEach-Object { Write-Host "      $_" }
      }
      if ($kept) {
        Write-Host "      Your uncommitted work is SAFE IN THE STASH ($($kept.Substring(0, [Math]::Min(12, $kept.Length))))" -ForegroundColor Yellow
        Write-Host '      but could not be re-applied. Recover it with:  git stash list'
        Write-Host '      then  git stash pop'
      }
      Write-Host '      Nothing was reloaded. Report this - a released commit should never do it.'
      return 1
    }
    if ($postFails.Count) {
      Say 'OK' "no NEW failures ($($postFails.Count) pre-existing local one(s) ride along)" 'Green'
    } else {
      Say 'OK' 'suite green' 'Green'
    }
  }

  # --- 5. run the new code ----------------------------------------------------
  if ($NoRestart) {
    Say 'OK' "updated to $after - caller will reload" 'Green'
  } else {
    Restart-Watchdog $root
    Say 'OK' "updated $before -> $after and restarted" 'Green'
  }
  return 0
}

function Get-SuiteResult([string]$root) {
  # -> @{ Ran; Fails; Err }. The NAMES of failing checks let before and after be
  # compared: the exit code alone cannot tell "this machine has an untidy memory
  # file" from "the release is broken", and those need opposite reactions.
  #
  # RAN IS THE OTHER HALF. Reading only [FAIL] lines made a suite that never got
  # off the ground - a bad import in the new code, no output at all - look
  # exactly like a green one: no failing names, verdict green, and the update
  # reloaded onto code nothing had tested. A crash is not a pass.
  $out = & python (Join-Path $root 'tools\discord\test_watchdog.py') 2>&1
  $rc = $LASTEXITCODE
  $lines = @($out | ForEach-Object { $_.ToString() } | Where-Object { $_.Trim() })
  # ANCHORED to the start of the line, like watchdog.py's parser. Matching
  # "[FAIL]" anywhere turned a PASSING check whose NAME mentions "[FAIL]" (the
  # gate's own regression test, 2026-09-02) into an "introduced failure", and the
  # baseline drill rolled a good release back for it.
  $fails = @($lines | Select-String -Pattern '^\s*\[FAIL\]' | ForEach-Object {
    ($_.ToString() -replace '^\s*\[FAIL\]\s*', '') -split '\s{2,}' | Select-Object -First 1
  })
  $ran = ($lines.Count -gt 0) -and -not ($rc -ne 0 -and $fails.Count -eq 0)
  return @{ Ran = $ran; Fails = $fails
            Err = (($lines | Select-Object -Last 6) -join "`n") }
}

function Undo-Pull([string]$root, [string]$before) {
  # Roll the tree back to $before WITHOUT eating uncommitted work.
  #
  # `git reset --hard` after the pull is a demolition: --autostash has already
  # replayed their uncommitted edits on top of the new code, so resetting there
  # deletes files that existed on the disk before the update was ever asked for.
  # Park them, reset, put them back - and if putting them back fails, they stay
  # in the stash and the caller says where. -> the stash ref if the work is
  # still parked, $null if the tree is whole.
  $stashBefore = (& git -C $root rev-parse -q --verify refs/stash 2>$null)
  & git -C $root stash push -u -m 'omnius-update-rollback' 2>&1 | Out-Null
  $stashAfter = (& git -C $root rev-parse -q --verify refs/stash 2>$null)
  $parked = ($stashAfter -and $stashAfter -ne $stashBefore)
  & git -C $root reset --hard $before 2>&1 | Out-Null
  if ($parked) {
    & git -C $root stash pop 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { return $stashAfter }
  }
  return $null
}

function Stamp-Machine([string]$root) {
  foreach ($tool in @('fix_hook_paths.py', 'sync_permissions.py')) {
    $p = Join-Path $root "tools\discord\$tool"
    if (Test-Path $p) { & python $p 2>&1 | Out-Null }
  }
}

function Restart-Watchdog([string]$root) {
  # RUNNING INSIDE A DESK? Then restarting is the one thing not to do. A desk is
  # a child of the watchdog, so stopping that task can kill the very process
  # printing this - the update would be complete and Discord would simply go
  # quiet, which looks exactly like a hang. The watchdog stamps OMNIUS_SESSION
  # into every run it starts, so this is knowable rather than guessable, and the
  # plain one-liner stays safe to paste into a channel.
  if ($env:OMNIUS_SESSION -or $env:OMNIUS_RUN_ID) {
    Write-Host ("  [OK] updated - now type !reload in Discord to run the new code " +
                "(not restarting from inside the $($env:OMNIUS_SESSION) desk: " +
                "that would kill this run mid-sentence)") -ForegroundColor Green
    return
  }
  # A running service keeps the code it was born with, so an update nobody
  # restarts is an update nobody got.
  #
  # THE LIVE WATCHDOG IS NOT ALWAYS THE TASK. It can be a plain process someone
  # started by hand (start-omnius.bat, a terminal) while the task sits Ready -
  # exactly the case on the owner's machine, 2026-08-19, pid 6668 since 09:42.
  # Restarting only the task then launches a second watchdog, which finds the
  # first one's lock and exits: updated on disk, still running the old code, and
  # nothing says so. So the process holding the lock is what has to go.
  $lock = Join-Path $root 'state\watchdog\lock.json'
  $livePid = $null
  if (Test-Path $lock) {
    try { $livePid = [int](Get-Content $lock -Raw | ConvertFrom-Json).pid } catch { }
  }
  $task = Get-ScheduledTask -TaskName 'Omnius Watchdog' -ErrorAction SilentlyContinue
  if ($task) { try { Stop-ScheduledTask -TaskName 'Omnius Watchdog' -ErrorAction SilentlyContinue } catch { } }
  if ($livePid) {
    $p = Get-Process -Id $livePid -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -match 'python') {
      Stop-Process -Id $livePid -Force -ErrorAction SilentlyContinue
      Write-Host "  [OK] stopped the running watchdog (pid $livePid)" -ForegroundColor Green
    }
  }
  Start-Sleep -Seconds 2
  if ($task) {
    try {
      Start-ScheduledTask -TaskName 'Omnius Watchdog' -ErrorAction Stop
      # VERIFY, do not assume: a fresh beacon is the watchdog saying it is
      # listening, and a start that silently did nothing looks identical here.
      $beacon = Join-Path $root 'state\watchdog\beacon.json'
      $deadline = (Get-Date).AddSeconds(45)
      while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        if (Test-Path $beacon) {
          try {
            $at = ([datetime](Get-Content $beacon -Raw | ConvertFrom-Json).at).ToUniversalTime()
            if (((Get-Date).ToUniversalTime() - $at).TotalSeconds -lt 60) {
              Write-Host '  [OK] watchdog restarted and listening on the new code' -ForegroundColor Green
              return
            }
          } catch { }
        }
      }
      Write-Host '  [!] watchdog task started but has not reported in yet - check state\logs\' -ForegroundColor Yellow
      return
    } catch { }
  }
  Write-Host '  [!] no Omnius Watchdog task here - start it yourself (start-omnius.bat)' -ForegroundColor Yellow
}

$rc = Update-Omnius -Path $Path -NoRestart:$NoRestart -NoTests:$NoTests -Quiet:$Quiet
Write-Host ''
# `exit` ONLY when this ran as a file. The headline use is
# `irm <url> | iex`, which runs in the user's own session - and `exit` there
# closes their PowerShell window, taking the output they were about to read
# with it. Same reasoning as get.ps1's "leave no trace in the session".
if ($PSCommandPath) { exit $rc }
