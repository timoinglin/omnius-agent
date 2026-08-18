<#
.SYNOPSIS
  Owns the Task Scheduler autostart for the Omnius always-on services.

.DESCRIPTION
  ARCHITECTURE.md Phase 4, "autostart hardening". The services must come back
  after a reboot with nobody in the room, and they must come back after they
  DIE, which a Startup shortcut cannot do.

  One scheduled task per service, because on 2026-08-01 only the watchdog had
  one: the owner rebooted, Discord worked, the notes web was down, and nothing
  said so. If a thing is expected to be up after a reboot it gets its own task.

  Each task is:
    trigger    at logon (20s delay) AND repeating every 5 minutes forever
    action     pythonw.exe tools\service_runner.py <script>   (no console window)
    settings   restart on failure every 1 min, no execution time limit,
               MultipleInstances=IgnoreNew, runs on battery
    principal  the current user, interactive, no elevation

  The repeating trigger is the self-healing half. Restart-on-failure only fires
  when a run ends non-zero, so a service that exits cleanly (or a task that was
  stopped by hand) would stay down until the next logon. The repeat brings it
  back within 5 minutes. It is safe to fire while the service is up:
  MultipleInstances=IgnoreNew drops the duplicate, and the watchdog's own
  single-instance lock (acquire_lock) drops it again. To stop a service for
  longer than 5 minutes, use -Action uninstall or Disable-ScheduledTask.

  NON-INTERACTIVE by design - install.ps1 owns the "shall I?" prompt. This
  script only ever does what -Action says.

.PARAMETER Action
  status     (default) report what is registered and whether it is actually
             alive; exit code 0 = healthy, 1 = something needs repair.
  install    register anything missing, leave healthy tasks alone.
  repair     re-register whatever has drifted (moved workspace, upgraded
             Python, old console-window action) and start it. Also installs.
  uninstall  remove both tasks.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\discord\autostart.ps1 -Action status
#>
[CmdletBinding()]
param(
  [ValidateSet('status', 'install', 'repair', 'uninstall')]
  [string]$Action = 'status',
  [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
# Paths are resolved from this script's own location, never hardcoded: the
# workspace travels in a zip and lands on a different machine under a different
# user. The registered task necessarily holds absolute paths - that is what
# -Action status checks, and what -Action repair fixes after a move.
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Runner = Join-Path $Root 'tools\service_runner.py'

$Services = @(
  @{ Name   = 'Omnius Watchdog'
     Script = 'tools\discord\watchdog.py'
     Desc   = 'Omnius Discord watchdog: starts at logon, restarts if it dies.'
     Live   = 'beacon' },
  @{ Name   = 'Omnius Daybook'
     Script = 'daybook\app.py'
     Desc   = 'Omnius daybook notes server (localhost:5111): starts at logon, restarts if it dies.'
     Live   = 'port:5111' }
)

# The Telegram bridge is deliberately NOT in this list. It is a child of the
# watchdog (ensure_telegram_bridge), which starts it within a minute of
# config\telegram.ini appearing and restarts it if it dies or goes silent.
# A third scheduled task would mean inviting someone required running a command
# on the machine - and this fleet is driven from a phone. It is still REPORTED
# below, because "supervised by something else" is not the same as "healthy".

# The desk is NOT a service. It is a window the owner sits in - warm Claude
# that Discord can also type into (tools\bridge\desk_bridge.py, 2026-08-02).
# So it gets its own registration: at-logon only, no 1-minute self-heal, and
# NO pythonw - a hidden desk would defeat the entire point of building it.
# Reopening it after a manual close is the owner's call, not the scheduler's.
# WHO the tasks run as. NOT "$env:USERDOMAIN\$env:USERNAME": on a real fresh
# install (2026-08-11) that produced an account Windows could not translate -
# "No mapping between account names and security IDs was done", HRESULT
# 0x80070534 - and BOTH services failed to register, so nothing survived a
# reboot. Env vars are a guess at the identity; this asks Windows for the one
# we are actually running as, which by definition resolves. The SID is the
# last-resort form, because "name -> SID" is precisely the step that failed.
function Get-TaskUserId {
  try {
    $me = [Security.Principal.WindowsIdentity]::GetCurrent()
    if ($me.Name) { return $me.Name }
    return $me.User.Value
  } catch {
    return "$env:USERDOMAIN\$env:USERNAME"
  }
}

$DeskTaskName = 'Omnius Desk (orchestrator)'

function Say($tag, $msg) {
  if ($Quiet -and $tag -eq 'OK') { return }
  $color = switch ($tag) { 'OK' { 'Green' } 'X' { 'Red' } '!' { 'Yellow' } default { 'Gray' } }
  Write-Host ("  [{0}] {1}" -f $tag, $msg) -ForegroundColor $color
}

function Get-PythonW {
  # pythonw.exe sits next to python.exe. Without it the task would run a console
  # app and put a window on the desktop at every logon.
  $py = (Get-Command python -ErrorAction SilentlyContinue).Source
  if (-not $py) { throw 'python is not on PATH - cannot register the autostart tasks' }
  $pyw = Join-Path (Split-Path $py -Parent) 'pythonw.exe'
  if (Test-Path $pyw) { return $pyw }
  Say '!' 'pythonw.exe not found next to python.exe - the service will show a console window'
  return $py
}

function Get-ExpectedAction($svc, $pyw) {
  # The script path is passed RELATIVE to the root; service_runner.py resolves it
  # against its own location, so the argument stays readable and root-agnostic.
  @{ Execute   = $pyw
     Arguments = ('"{0}" "{1}"' -f $Runner, $svc.Script)
     WorkDir   = Split-Path (Join-Path $Root $svc.Script) -Parent }
}

function Register-OmniusService($svc, $pyw) {
  $want = Get-ExpectedAction $svc $pyw
  $action = New-ScheduledTaskAction -Execute $want.Execute -Argument $want.Arguments `
              -WorkingDirectory $want.WorkDir

  # TWO triggers, and the second one is the one that actually keeps this alive.
  #
  # Measured on 2026-08-01 rather than assumed: with only an at-logon trigger and
  # RestartCount 999 / RestartInterval 1 min, killing the watchdog left it dead.
  # The task went to State=Ready, LastTaskResult=0x1, NextRunTime empty, and it
  # never came back. "If the task fails, restart every 1 minute" does not treat an
  # action exiting non-zero as a task failure, and a logon trigger's repetition
  # only runs inside the window that logon opened - a task registered after logon
  # has no window at all. The setting that everyone (including this installer,
  # since it was written) assumes is the safety net is not one.
  #
  # A plain time trigger with an indefinite repetition has none of that
  # subtlety: it has a real NextRunTime, it fires every minute forever, and
  # MultipleInstances=IgnoreNew makes it free while the service is healthy - the
  # scheduler does not even start a second instance. So worst-case downtime is
  # ~60s, from any cause: crash, kill, clean exit, or a stopped task.
  $logon = New-ScheduledTaskTrigger -AtLogOn -User (Get-TaskUserId)
  $logon.Delay = 'PT20S'     # let the network and the user profile settle first
  $triggers = @($logon)

  $probe = New-ScheduledTaskTrigger -Once -At (Get-Date).Date   # midnight today: already past
  try {
    # Repetition built directly: New-ScheduledTaskTrigger -RepetitionDuration
    # ([TimeSpan]::MaxValue) is the usual idiom but throws on some builds.
    # Omitting Duration means "indefinitely", which is what a daemon wants.
    $probe.Repetition = New-CimInstance -ClassName MSFT_TaskRepetitionPattern `
        -Namespace 'Root/Microsoft/Windows/TaskScheduler' -ClientOnly `
        -Property @{ Interval = 'PT1M'; StopAtDurationEnd = $false }
    $triggers += $probe
  } catch {
    Say '!' ("could not attach the 1-minute self-heal trigger ({0}) - the service will only start at logon" -f $_.Exception.Message)
  }
  $trigger = $triggers

  # ExecutionTimeLimit 0 = never kill it for running too long: these are daemons.
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
                -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
                -MultipleInstances IgnoreNew
  $principal = New-ScheduledTaskPrincipal -UserId (Get-TaskUserId) `
                 -LogonType Interactive -RunLevel Limited

  # Register-ScheduledTask -Force replaces an existing definition in place, so
  # install and repair are the same call - find-or-create, idempotent (§5.1).
  Register-ScheduledTask -TaskName $svc.Name -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description $svc.Desc -Force | Out-Null
}

function Get-Drift($svc, $task, $pyw) {
  # Everything that can silently rot between machines. Each entry is a reason to
  # re-register; an empty list means the task on disk matches what we would write.
  $drift = @()
  $want = Get-ExpectedAction $svc $pyw
  $have = $task.Actions[0]
  if ($have.Execute -ne $want.Execute) {
    $drift += if ($have.Execute -like '*pythonw.exe') { 'python moved' } else { 'shows a console window' }
  }
  if ($have.Arguments -ne $want.Arguments) { $drift += 'points at a different workspace/script' }
  if (-not (Test-Path $have.Execute))     { $drift += 'python is gone' }
  $argPath = ($have.Arguments -split '"' | Where-Object { $_ -match '\.py$' } | Select-Object -First 1)
  if ($argPath -and -not (Test-Path $argPath)) { $drift += 'script path no longer exists' }
  # Specifically a TIME trigger with a repetition - a repeating LOGON trigger
  # looks identical here and does not survive a kill (see Register-OmniusService).
  if (-not ($task.Triggers | Where-Object {
        $_.Repetition.Interval -and $_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger' })) {
    $drift += 'no self-heal trigger'
  }
  if ($task.Settings.RestartCount -lt 1) { $drift += 'no restart-on-failure' }
  return $drift
}

function Test-Live($svc) {
  # Registered and Running is not the same as working. Ask the service itself.
  switch -Wildcard ($svc.Live) {
    'beacon' {
      # beacon.json is stamped only after a fully successful poll pass - it says
      # the watchdog is LISTENING, where lock.json only says a process exists.
      $b = Join-Path $Root 'state\watchdog\beacon.json'
      if (-not (Test-Path $b)) { return @{ ok = $false; note = 'no beacon yet' } }
      $j = Get-Content $b -Raw | ConvertFrom-Json
      $at = ([datetime]$j.at).ToUniversalTime()
      $age = [int]((Get-Date).ToUniversalTime() - $at).TotalSeconds
      if ($age -ge 120) { return @{ ok = $false; note = "beacon ${age}s old" } }
      # Fresh is not the same as able to work. On 2026-08-14 the beacon was 3
      # seconds old, the gateway connected and 14 channels mapped, while the
      # service could not resolve the `claude` CLI at all - so for an hour every
      # desk failed to start and this said "healthy". A green status that hides
      # a dead fleet is worse than no status. The watchdog now publishes what it
      # can actually see; null means it can spawn nothing.
      $hasClaude = $true
      if ($j.PSObject.Properties.Name -contains 'claude') { $hasClaude = [bool]$j.claude }
      if (-not $hasClaude) {
        return @{ ok = $false
                  note = "beacon ${age}s old but the service cannot find the claude CLI - NO desk can start. Install Claude Code, then restart this task (a running service keeps the PATH it was born with), or set [fleet] claude_path in config\omnius.ini" }
      }
      return @{ ok = $true; note = "beacon ${age}s old" }
    }
    'file:*' {
      # file:<relative path>:<max age in seconds> - the generic form of the
      # beacon check, for services that stamp their own proof of life. Running
      # is not working: the bridge can be up with a revoked token forever.
      $parts = $svc.Live.Split(':')
      $f = Join-Path $Root $parts[1]
      $maxAge = if ($parts.Count -gt 2) { [int]$parts[2] } else { 180 }
      if (-not (Test-Path $f)) { return @{ ok = $false; note = 'no beacon yet' } }
      try {
        $j = Get-Content $f -Raw | ConvertFrom-Json
        $age = [int]((Get-Date).ToUniversalTime() - ([datetime]$j.at).ToUniversalTime()).TotalSeconds
      } catch { return @{ ok = $false; note = 'beacon unreadable' } }
      if ($age -ge $maxAge) { return @{ ok = $false; note = "beacon ${age}s old" } }
      return @{ ok = $true; note = "beacon ${age}s old" }
    }
    'port:*' {
      $port = [int]($svc.Live -replace '^port:', '')
      $ok = (Test-NetConnection -ComputerName '127.0.0.1' -Port $port -InformationLevel Quiet -WarningAction SilentlyContinue)
      return @{ ok = $ok; note = "localhost:$port $(if ($ok) { 'answering' } else { 'not answering' })" }
    }
  }
  return @{ ok = $true; note = '' }
}

# --- actions ------------------------------------------------------------------

Write-Host ''
Write-Host ("--- omnius autostart ({0}) ---" -f $Action)
Write-Host ("    workspace: {0}" -f $Root)

function Register-OmniusDesk {
  # wt.exe is a WindowsApps execution alias, so it is resolved through the
  # user's PATH at run time rather than hardcoded - the workspace travels.
  $bat = Join-Path $Root 'wakeup-omnius.bat'
  $action = New-ScheduledTaskAction -Execute 'cmd.exe' `
              -Argument ('/c "' + $bat + '" orchestrator') -WorkingDirectory $Root
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User (Get-TaskUserId)
  $trigger.Delay = 'PT40S'      # after the watchdog (PT20S), so the bus is up first
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries -StartWhenAvailable `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
  $principal = New-ScheduledTaskPrincipal -UserId (Get-TaskUserId) `
                 -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $DeskTaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description 'Opens the warm Omnius desk window at logon (Discord types into it).' | Out-Null
}

if ($Action -eq 'uninstall') {
  if (Get-ScheduledTask -TaskName $DeskTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $DeskTaskName -Confirm:$false
    Say 'OK' "removed '$DeskTaskName'"
  }
  # 'Omnius Telegram' left $Services when the watchdog took the bridge over, so
  # the loop below can no longer see it - and uninstall exits before the
  # legacy-cleanup block further down. Named here or it survives an uninstall.
  if (Get-ScheduledTask -TaskName 'Omnius Telegram' -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName 'Omnius Telegram' -Confirm:$false
    Say 'OK' "removed 'Omnius Telegram' (a task from an earlier release)"
  }
  foreach ($svc in $Services) {
    if (Get-ScheduledTask -TaskName $svc.Name -ErrorAction SilentlyContinue) {
      Unregister-ScheduledTask -TaskName $svc.Name -Confirm:$false
      Say 'OK' "removed '$($svc.Name)' - it will not start at the next logon"
    } else {
      Say 'i' "'$($svc.Name)' was not registered"
    }
  }
  Write-Host '    the services still start by hand with start-omnius.bat'
  exit 0
}

$pyw = Get-PythonW
$problems = 0

# Hook commands must be ABSOLUTE paths, and they are machine-specific, so this
# runs before anything starts. It writes them into each desk's gitignored
# .claude\settings.local.json - a workspace cloned or unzipped from another PC
# arrives with none, and a hook pointing at a path that is not there BLOCKS the
# prompt, so every desk would come up unable to accept /omnius (2026-08-02: a
# morning of window loops and frozen desks, twice).
$fixer = Join-Path $Root 'tools\discord\fix_hook_paths.py'
if (Test-Path $fixer) {
  if ($Action -eq 'status') {
    & python $fixer --check | Out-Null
    if ($LASTEXITCODE -ne 0) { Say '!' 'desk hooks need writing - run -Action repair'; $problems++ }
    else { Say 'OK' 'desk hooks wired to this machine' }
  } else {
    & python $fixer | ForEach-Object { if ($_ -match '^hooks:') { Say 'OK' $_.Trim() } }
  }
}

# Same reasoning one step further: a desk missing an allow-list entry stops and
# ASKS, and over Discord that is a question on a screen nobody is watching. The
# list is kept in one place and stamped onto every desk here, because kept by
# hand it drifted silently - projects\ is gitignored, so a widening committed to
# the repo never reached a single project desk (2026-08-13).
$perms = Join-Path $Root 'tools\discord\sync_permissions.py'
if (Test-Path $perms) {
  if ($Action -eq 'status') {
    & python $perms --check | Out-Null
    if ($LASTEXITCODE -ne 0) { Say '!' 'some desks are short of the allow-list - run -Action repair'; $problems++ }
    else { Say 'OK' 'every desk carries the full allow-list' }
  } else {
    & python $perms | ForEach-Object { if ($_ -match '^\s*\+') { Say 'OK' $_.Trim() } }
  }
}

foreach ($svc in $Services) {
  $task = Get-ScheduledTask -TaskName $svc.Name -ErrorAction SilentlyContinue

  if (-not $task) {
    if ($Action -eq 'status') {
      Say 'X' "'$($svc.Name)' is NOT registered - it will not survive a reboot"
      $problems++
      continue
    }
    Register-OmniusService $svc $pyw
    Start-ScheduledTask -TaskName $svc.Name
    Say 'OK' "registered '$($svc.Name)' and started it"
    $task = Get-ScheduledTask -TaskName $svc.Name
  } else {
    $drift = @(Get-Drift $svc $task $pyw)
    if ($drift.Count -gt 0) {
      if ($Action -eq 'repair') {
        # Stop first: -Force replaces the DEFINITION, but a task instance that is
        # already running keeps running under the old one, and MultipleInstances
        # =IgnoreNew would then swallow our Start. Repair has to actually swap the
        # process, otherwise "repaired" means "next reboot, maybe".
        if ($task.State -eq 'Running') { Stop-ScheduledTask -TaskName $svc.Name }
        Register-OmniusService $svc $pyw
        Start-ScheduledTask -TaskName $svc.Name
        Say 'OK' ("repaired '{0}' ({1}) and restarted it" -f $svc.Name, ($drift -join '; '))
        $task = Get-ScheduledTask -TaskName $svc.Name
      } else {
        Say '!' ("'{0}' needs repair: {1}" -f $svc.Name, ($drift -join '; '))
        Write-Host  "        fix with:  powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\autostart.ps1`" -Action repair"
        $problems++
      }
    }
  }

  $info = $task | Get-ScheduledTaskInfo
  # 0x800710E0 is what MultipleInstances=IgnoreNew returns when the 1-minute
  # self-heal trigger fires while the service is already up. That is the HEALTHY
  # steady state - it happens every minute, forever - so showing it as a raw
  # error code would make every status check look half-broken.
  # LastTaskResult is a UInt32 here (2147946720), not the signed -2147020576 the
  # same code is usually written as - matching only the signed form silently
  # never fires. Both are listed so this survives a build that returns Int32.
  $last = switch ($info.LastTaskResult) {
            267009      { 'running' }
            0           { 'ok' }
            2147946720  { 'skipped (already running - self-heal trigger doing its job)' }
            -2147020576 { 'skipped (already running - self-heal trigger doing its job)' }
            default     { '0x{0:X}' -f $_ }
          }
  Say 'i' ("{0}: state={1} lastRun={2} result={3}" -f $svc.Name, $task.State, $info.LastRunTime, $last)

  $live = Test-Live $svc
  if ($live.ok) {
    Say 'OK' ("{0} is alive ({1})" -f $svc.Name, $live.note)
  } else {
    # Registered but not answering. During install/repair the service was only
    # just started, so give it a fair chance before calling it a failure.
    # POLL, do not sleep-once: a single 8s wait was too short for a genuinely
    # cold start on a fresh install (2026-08-11) - no .pyc caches, first Discord
    # REST round trip, then a gateway connect - and reported a working watchdog
    # as broken. Polling reports a fast start immediately and still waits out a
    # slow one.
    if ($Action -ne 'status') {
      # RUNTIME drift, which Get-Drift cannot see: it compares the task
      # DEFINITION on disk, and on 2026-08-14 that was perfectly correct all
      # along - the fault was the environment the running process was born
      # with, and repair was a no-op. A service that is answering but reports a
      # capability it does not have needs restarting, not waiting for.
      if ($live.note -like '*cannot find the claude CLI*') {
        Say '!' ("{0}: restarting it - its environment predates the CLI install" -f $svc.Name)
        try {
          Stop-ScheduledTask -TaskName $svc.Name -ErrorAction Stop
          Start-Sleep -Seconds 2
          Start-ScheduledTask -TaskName $svc.Name -ErrorAction Stop
        } catch { Say '!' ("could not restart {0}: {1}" -f $svc.Name, $_.Exception.Message) }
      }
      $deadline = (Get-Date).AddSeconds(45)
      while (-not $live.ok -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        $live = Test-Live $svc
      }
    }
    if ($live.ok) {
      Say 'OK' ("{0} is alive ({1})" -f $svc.Name, $live.note)
    } else {
      Say 'X' ("{0} is registered but NOT answering ({1}) - see state\logs\" -f $svc.Name, $live.note)
      $problems++
    }
  }
}

# --- desks are NOT started at boot ---------------------------------------------
# Owner's model, 2026-08-03: a fresh boot brings up the watchdog and the notes
# web and NOTHING else. At the desk he starts `claude` himself; from Discord,
# the first message opens that desk's bridge. A logon task that opened the
# orchestrator existed briefly and was wrong in both directions - it put a
# window in front of him when he wanted to choose, and it was the wrong desk
# whenever he wrote to a different channel.
$deskTask = Get-ScheduledTask -TaskName $DeskTaskName -ErrorAction SilentlyContinue
if ($deskTask) {
  if ($Action -eq 'status') {
    Say '!' "'$DeskTaskName' is registered but desks must not open at boot - run -Action repair"
    $problems++
  } else {
    Unregister-ScheduledTask -TaskName $DeskTaskName -Confirm:$false
    Say 'OK' "removed '$DeskTaskName' - desks open on demand, never at boot"
  }
} else {
  Say 'OK' 'no desk opens at boot (watchdog + daybook only, by design)'
}

# The telegram bridge has no task of its own (see $Services): the watchdog
# starts and restarts it. Report it anyway - a supervised process that is not
# actually running looks identical to one nobody ever configured.
$legacyTg = Get-ScheduledTask -TaskName 'Omnius Telegram' -ErrorAction SilentlyContinue
if ($legacyTg) {
  # Shipped for one release (2026-08-18) before the bridge became a child of the
  # watchdog. Harmless - the bridge's own lock keeps a second one from starting -
  # but two supervisors for one process is a thing nobody should have to reason
  # about at 2am, so repair removes it.
  if ($Action -eq 'status') {
    Say '!' "'Omnius Telegram' is registered but the watchdog owns the bridge now - run -Action repair to remove it"
    $problems++
  } else {
    Unregister-ScheduledTask -TaskName 'Omnius Telegram' -Confirm:$false
    Say 'OK' "removed 'Omnius Telegram' - the watchdog starts the bridge itself"
  }
}

if (Test-Path (Join-Path $Root 'config\telegram.ini')) {
  $tg = Test-Live @{ Live = 'file:state\telegram\beacon.json:900' }
  $bf = Join-Path $Root 'state\telegram\beacon.json'
  $tgState = 'unknown'
  if (Test-Path $bf) {
    try { $tgState = (Get-Content $bf -Raw | ConvertFrom-Json).state } catch { }
  }
  if (-not $tg.ok) {
    Say '!' "telegram bridge: $($tg.note). The watchdog starts it within a minute of config\telegram.ini appearing - if this persists, see state\logs\telegram.out.log"
    $problems++
  } elseif ($tgState -eq 'failing') {
    # Fresh timestamp, useless service. A bridge whose every call fails keeps
    # stamping - so freshness alone reported a revoked token as healthy.
    Say 'X' 'telegram bridge is RUNNING BUT FAILING - every pass is erroring (revoked token? no network?). See state\logs\telegram.log'
    $problems++
  } elseif ($tgState -eq 'idle') {
    Say '!' 'telegram bridge is idle: config\telegram.ini exists but there is no TELEGRAM_BOT_TOKEN in .env, or no usable [chat.*] block. Run: python tools\telegram\bridge.py --check'
    $problems++
  } else {
    Say 'OK' "telegram bridge is alive ($($tg.note)) - started by the watchdog, no task of its own"
  }
}

Write-Host ''
if ($problems -eq 0) {
  Say 'OK' 'autostart healthy - services and the desk window come back after a reboot'
  exit 0
}
Say 'X' "$problems problem(s) above"
exit 1
