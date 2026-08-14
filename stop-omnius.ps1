# Omnius - stop everything this workspace is running.
#
#     stop-omnius.bat          services (watchdog, daybook, status banner)
#     stop-omnius.bat -All     services AND any Claude desks
#
# The counterpart to start-omnius.bat / the desktop icon. Written 2026-08-11,
# when a folder could not be deleted because services kept restarting.
#
# TWO things run, and stopping one is not stopping Omnius:
#   * scheduled tasks   - headless, pythonw + service_runner, and they RESTART
#                         within 60s by design (that is the self-heal trigger)
#   * start-omnius.bat  - the same scripts in visible terminal panes
#
# So the tasks are DISABLED, not merely stopped: stopping a task whose trigger
# fires every minute buys you less than a minute. The desktop launcher
# re-enables them, so this stays a pause rather than an uninstall.
param([switch]$All, [switch]$Quiet)

$ErrorActionPreference = 'SilentlyContinue'
$root = $PSScriptRoot
function Say($tag, $msg) {
  if ($Quiet -and $tag -eq 'i') { return }
  $c = @{ 'OK'='Green'; 'X'='Red'; '!'='Yellow'; 'i'='Gray' }[$tag]
  if (-not $c) { $c = 'Gray' }
  Write-Host ("[{0}] {1}" -f $tag.PadRight(2), $msg) -ForegroundColor $c
}

Write-Host ''
Write-Host '--- stopping Omnius ---------------------------------------'
Say 'i' "workspace: $root"

# 1. Tasks first. Kill the processes first and the scheduler just starts them
#    again while you are still reading the output.
$tasks = @(Get-ScheduledTask -TaskName 'Omnius *' -ErrorAction SilentlyContinue)
foreach ($t in $tasks) {
  Disable-ScheduledTask -TaskName $t.TaskName -ErrorAction SilentlyContinue | Out-Null
  Stop-ScheduledTask    -TaskName $t.TaskName -ErrorAction SilentlyContinue
  Say 'OK' "disabled + stopped task: $($t.TaskName)"
}
if (-not $tasks) { Say 'i' 'no Omnius scheduled tasks registered' }

# 2. Processes belonging to THIS workspace. Matched on the command line, never
#    on the image name: killing every python.exe on the machine would be a
#    spectacular way to "stop Omnius".
$rootPat = '*' + $root + '*'
$procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -like $rootPat })

$deskish = 'claude'
$services = @($procs | Where-Object { $_.Name -notlike "$deskish*" })
$desks    = @($procs | Where-Object { $_.Name -like  "$deskish*" })

foreach ($p in $services) {
  Say 'OK' ("stopped {0} (pid {1})" -f $p.Name, $p.ProcessId)
  Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
if (-not $services) { Say 'i' 'no service processes were running' }

if ($desks) {
  if ($All) {
    foreach ($p in $desks) {
      Say 'OK' ("stopped desk {0} (pid {1})" -f $p.Name, $p.ProcessId)
      Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
  } else {
    # Not killed by default: a desk mid-turn is WORK, and losing it silently is
    # worse than leaving it running. Say it plainly instead of deciding for him.
    Say '!' ("{0} Claude desk(s) still running - left alone. Use -All to close them too." -f $desks.Count)
    $desks | ForEach-Object { Say 'i' ("  pid {0}  {1}" -f $_.ProcessId, $_.Name) }
  }
}

# 3. Report SURVIVORS rather than claiming success - the same rule !stop follows.
Start-Sleep -Milliseconds 700
$left = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
          Where-Object { $_.CommandLine -like $rootPat } |
          Where-Object { $All -or $_.Name -notlike "$deskish*" })
Write-Host ''
if ($left) {
  Say 'X' 'still running:'
  $left | ForEach-Object { Say 'i' ("  pid {0}  {1}" -f $_.ProcessId, $_.Name) }
  Say 'i' 'an editor or a terminal sitting IN the folder also holds it open'
} else {
  Say 'OK' 'Omnius is stopped'
}
Say 'i' 'start again: the desktop icon, or start-omnius.bat (it re-enables the tasks)'
Write-Host ''
