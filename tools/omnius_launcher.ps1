# Omnius desktop launcher - the one thing he has to remember.
#
# Double-click the desktop icon and this makes sure the system is up, then opens
# the web app. Deliberately does NOT open a terminal: the watchdog and daybook
# are scheduled tasks that survive logoff, and a console window here would just
# be something to close (or worse, something to close ACCIDENTALLY, which used
# to take the fleet down with it).
#
#     powershell -WindowStyle Hidden -File tools\omnius_launcher.ps1 [-NoBrowser]
#
# Idempotent and safe to run repeatedly - starting a running task is a no-op.
param([switch]$NoBrowser)

$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot

# Port comes from config, so a changed port does not silently open the wrong URL.
# omnius_config has no CLI, so import it - and keep 5111 as the fallback, which
# is the same default the reader itself uses.
$port = 5111
try {
    $py = "import sys; sys.path.insert(0, r'$root\tools'); import omnius_config as c; " +
          "print(c.get_int(c.load('notes', r'$root\daybook\config.ini'), 'notes', 'port', 'PORT', 5111))"
    $out = & python -c $py 2>$null
    if ("$out" -match '(\d{2,5})') { $port = [int]$Matches[1] }
} catch { }

# Scheduled task first - it survives logoff and restarts a dead service. But a
# MISSING task used to mean this launcher started nothing at all, silently, and
# then opened a browser at a port with no server: the icon looked broken on a
# machine where everything else was fine (2026-08-11, task registration had
# failed on a fresh install). install.ps1 even says "the system still works -
# just start it with start-omnius.bat" in that state, so the icon must agree.
$runner = Join-Path $PSScriptRoot 'service_runner.py'
$pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
$services = @(
    @{ Task = 'Omnius Watchdog'; Script = 'tools\discord\watchdog.py'; Match = 'watchdog.py' },
    @{ Task = 'Omnius Daybook';  Script = 'daybook\app.py';            Match = 'app.py' }
)
foreach ($svc in $services) {
    $t = Get-ScheduledTask -TaskName $svc.Task -ErrorAction SilentlyContinue
    if ($null -ne $t) {
        # stop-omnius.bat DISABLES the tasks (stopping one whose trigger fires
        # every minute achieves nothing). Re-enable before starting, or the icon
        # silently does nothing after a stop - which would make stopping Omnius
        # a one-way door.
        if ($t.State -eq 'Disabled') {
            Enable-ScheduledTask -TaskName $svc.Task -ErrorAction SilentlyContinue | Out-Null
            $t = Get-ScheduledTask -TaskName $svc.Task -ErrorAction SilentlyContinue
        }
        if ($t.State -ne 'Running') { Start-ScheduledTask -TaskName $svc.Task -ErrorAction SilentlyContinue }
        continue
    }
    # No task: start it ourselves, exactly as the task would have. Check first
    # so a second double-click does not stack a duplicate service.
    if (-not $pyw) { continue }
    $running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
               Where-Object { $_.CommandLine -like "*$($svc.Match)*" }
    if ($running) { continue }
    Start-Process -FilePath $pyw -WindowStyle Hidden `
        -ArgumentList ('"{0}" "{1}"' -f $runner, (Join-Path $root $svc.Script)) `
        -ErrorAction SilentlyContinue
}

if ($NoBrowser) { return }

# Wait for the daybook to actually answer before opening a browser at it - a
# cold start takes a second or two, and a browser that lands on "can't reach
# this page" reads as broken even though nothing is.
$url = "http://localhost:$port/"
for ($i = 0; $i -lt 20; $i++) {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $c.Connect('127.0.0.1', $port)
        if ($c.Connected) { $c.Close(); break }
    } catch { Start-Sleep -Milliseconds 400 }
}
Start-Process $url
