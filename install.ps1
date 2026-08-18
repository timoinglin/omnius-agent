# Omnius - first-time install & environment doctor (idempotent - safe to re-run anytime)
# Run via install.bat (double-click) or:  powershell -ExecutionPolicy Bypass -File install.ps1
param(
  [switch]$CheckOnly,  # report only - never prompt, never install
  # Pretend winget is not here. The direct-download routes exist for machines
  # that have no package manager, and the only honest place to exercise them
  # used to be a Windows Sandbox - where Python's MSI takes 18 minutes against a
  # copy-on-write disk, so one test run costs an evening (2026-08-15). With this
  # switch the same path runs on real hardware at real speed. Test it in a fresh
  # LOCAL USER ACCOUNT: per-user installs are invisible to it, so the tools are
  # genuinely missing, and nothing you already rely on gets touched.
  [switch]$NoWinget
)

$ErrorActionPreference = 'Continue'
# -LiteralPath: an unzip target like "omnius [backup]" is a wildcard to Set-Location,
# which would silently leave us in the wrong directory and read no .env at all.
Set-Location -LiteralPath $PSScriptRoot

# Set-Content -Encoding utf8 writes a BOM on Windows PowerShell 5.1, and a BOM
# makes configparser reject an .ini outright (MissingSectionHeaderError) - every
# setting silently falls back to its default. .NET lets us say "no BOM".
function Write-Utf8NoBom([string]$Path, [string]$Text) {
  [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding $false))
}

# Set one key in an .ini WITHOUT ever creating a second [section].
# Prepending a "[backup]" block to a file whose template ALREADY had one gave
# configparser a DuplicateSectionError - which does not just lose that key, it
# makes the WHOLE file unreadable, so every setting silently reverts to its
# default (2026-08-11: the desk correctly reported no backup folder minutes
# after install said it had set one).
function Set-IniValue([string]$Path, [string]$Section, [string]$Key, [string]$Value) {
  $text = if (Test-Path $Path) { Get-Content $Path -Raw } else { '' }
  $line = "$Key = $Value"
  if ($text -match "(?m)^\s*\[$Section\]\s*$") {
    if ($text -match "(?m)^\s*$Key\s*=") {
      $text = $text -replace "(?m)^\s*$Key\s*=.*$", $line          # already set: replace
    } else {
      $text = $text -replace "(?m)^(\s*\[$Section\]\s*)$", "`$1`r`n$line"
    }
  } else {
    $text = "[$Section]`r`n$line`r`n`r`n" + $text
  }
  Write-Utf8NoBom $Path $text
}

function Write-Status([string]$tag, [string]$msg) {
  $colors = @{ 'OK'='Green'; 'X'='Red'; '!'='Yellow'; '..'='Cyan'; 'i'='Gray'; 'skip'='DarkGray' }
  $c = $colors[$tag]; if (-not $c) { $c = 'Gray' }
  Write-Host ("[{0}] {1}" -f $tag.PadRight(4), $msg) -ForegroundColor $c
}

function Test-Cmd([string]$name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

function Test-Python {
  # Get-Command alone is fooled by the Windows Store alias - do a functional check
  cmd /c "python -c ""print(1)"" >nul 2>nul"
  return ($LASTEXITCODE -eq 0)
}

# Where the official Claude installer puts the binary. It DOES add this to the
# user PATH in the registry, so Refresh-Path finds it - but only for a process
# that looks again, and Get-Command does not reliably re-scan inside the same
# window. On 2026-08-14 that cost a real move: the CLI installed correctly and
# the installer then declared it missing and stopped, telling him to close the
# window and start over. Checking the FILE removes the question entirely.
$script:ClaudeBin = Join-Path $env:USERPROFILE '.local\bin'
# Initialised HERE, not only where it is set. Add-ClaudeToUserPath is the only
# writer and it does not run when the CLI is already present - so on every
# normal install the later `if ($script:PathChanged)` read an unset variable,
# which is fatal the moment anything turns StrictMode on. Caught by the release
# gate, which is exactly the failure that gate exists for.
$script:PathChanged = $false

function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
              [Environment]::GetEnvironmentVariable('Path','User')
  # Prepend it ourselves when the binary is there: everything later in this
  # script shells out to `claude`, and none of it should depend on whether a
  # third-party installer's PATH edit is visible to us yet.
  if ((Test-Path (Join-Path $script:ClaudeBin 'claude.exe')) -and
      ($env:Path -notlike "*$script:ClaudeBin*")) {
    $env:Path = "$script:ClaudeBin;$env:Path"
  }
}

function Test-Claude {
  # Two ways, either is proof. Get-Command covers a custom install location;
  # the file test covers the official one when PATH has not caught up.
  if (Test-Cmd 'claude') { return $true }
  return (Test-Path (Join-Path $script:ClaudeBin 'claude.exe'))
}

function Add-ToUserPath([string]$dir, [string]$note = '') {
  # User scope, append only, idempotent - and it also fixes THIS process, so
  # everything later in the script can shell out to what we just installed
  # without asking the user to open a new window.
  if (-not (Test-Path $dir)) { return $false }
  $user = [Environment]::GetEnvironmentVariable('Path','User')
  if ($null -eq $user) { $user = '' }
  foreach ($p in ($user -split ';' | Where-Object { $_ -ne '' })) {
    if ($p.TrimEnd('\') -ieq $dir.TrimEnd('\')) {
      if ($env:Path -notlike "*$dir*") { $env:Path = "$dir;$env:Path" }
      return $true                                                # already there
    }
  }
  $new = if ($user -eq '') { $dir } else { $user.TrimEnd(';') + ';' + $dir }
  try {
    [Environment]::SetEnvironmentVariable('Path', $new, 'User')
    $script:PathChanged = $true
    $env:Path = "$dir;$env:Path"
    Write-Status 'OK' ("added {0} to your user PATH{1}" -f $dir, $note)
    return $true
  } catch {
    Write-Status '!' ("could not add {0} to PATH: {1}" -f $dir, $_.Exception.Message)
    return $false
  }
}

function Add-ClaudeToUserPath {
  # The official installer does NOT edit PATH. Its own closing note, verbatim
  # on a clean Windows 11 (2026-08-14): "Native installation exists but
  # C:\Users\<you>\.local\bin is not in your PATH. Add it by opening: System
  # Properties -> Environment Variables -> ... Then restart your terminal."
  #
  # Making him click through that dialog is exactly the kind of step this
  # installer exists to remove - and without it EVERY later piece breaks, since
  # the watchdog, the bridge and fleet_ops all resolve `claude` on PATH.
  if (-not (Test-Path (Join-Path $script:ClaudeBin 'claude.exe'))) { return }
  [void](Add-ToUserPath $script:ClaudeBin ' (the Claude installer does not)')
}

# --- installing WITHOUT winget ------------------------------------------------
# A clean Windows Sandbox has no winget at all, and winget is also absent on
# plenty of Windows 10 boxes and every machine where App Installer was removed.
# Before this, that was a dead end: three "missing - fix, then re-run" lines and
# an installer that could not install anything (2026-08-14, run in a sandbox).
# The Claude CLI already proved the alternative works - it installs from its own
# URL, no package manager involved. So every REQUIRED tool now has a direct
# route to the same place winget would have got it from:
#
#   git     the official Git for Windows installer, newest release from GitHub
#   python  python.org's own installer, per-user (no admin, and it sets PATH)
#   ffmpeg  the static build ffmpeg.org points Windows users at, unzipped
#
# Anything portable lands here rather than in the workspace: binaries must not
# travel in the backup zip, and a machine is disposable while the repo is not.
$script:OmniusBin = Join-Path $env:LOCALAPPDATA 'Omnius\bin'

function Get-Download([string]$url, [string]$name) {
  # Streamed by hand rather than with Invoke-WebRequest, for one reason: in
  # PowerShell 5.1 its progress bar redraws per chunk and turns a 100 MB
  # download into a multi-minute crawl, while switching the bar OFF leaves the
  # console completely silent for those minutes. On a fresh sandbox that is
  # indistinguishable from a hang - "i see no progress bar or anything at all"
  # (2026-08-14). Reading the stream ourselves gives real megabytes, cheaply.
  $dst = Join-Path $env:TEMP $name
  [Net.ServicePointManager]::SecurityProtocol = `
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
  $resp = $null; $in = $null; $out = $null
  try {
    $req = [Net.HttpWebRequest]::Create($url)
    $req.UserAgent = 'omnius-install'
    $req.Timeout = 60000
    $req.ReadWriteTimeout = 300000
    $resp = $req.GetResponse()
    $total = $resp.ContentLength
    $size = if ($total -gt 0) { "{0:N1} MB" -f ($total / 1MB) } else { 'unknown size' }
    Write-Status '..' ("downloading {0} ({1})" -f $name, $size)
    $in  = $resp.GetResponseStream()
    $out = [IO.File]::Create($dst)
    $buf = New-Object byte[] 262144
    $read = 0L; $lastTick = 0L
    while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
      $out.Write($buf, 0, $n)
      $read += $n
      $now = [DateTime]::UtcNow.Ticks
      if ($now - $lastTick -gt 5000000) {                    # twice a second
        $lastTick = $now
        if ($total -gt 0) {
          Write-Host ("`r       {0,5:N1} / {1:N1} MB  ({2,3:N0}%)" -f `
                      ($read / 1MB), ($total / 1MB), ($read * 100 / $total)) -NoNewline
        } else {
          Write-Host ("`r       {0,5:N1} MB" -f ($read / 1MB)) -NoNewline
        }
      }
    }
    Write-Host ("`r       {0:N1} MB downloaded            " -f ($read / 1MB))
    return $dst
  } catch {
    Write-Host ''
    Write-Status 'X' ("download failed ({0}): {1}" -f $name, $_.Exception.Message)
    return $null
  } finally {
    if ($out)  { $out.Close() }
    if ($in)   { $in.Close() }
    if ($resp) { $resp.Close() }
  }
}

function Wait-Installer($proc, [string]$label) {
  # A silent installer prints NOTHING while it works. Two minutes of that on a
  # slow machine reads exactly like a hang, and the honest fix is to show that
  # time is passing rather than to hope nobody waits.
  # Touch .Handle FIRST. `Start-Process -PassThru` without -Wait hands back an
  # object whose ExitCode reads back $null once the process is gone, because
  # nothing kept the OS handle open - and `$null -ne 0` is TRUE, so every
  # SUCCESSFUL install would have been reported as a failure and the Claude
  # installer's whole log dumped as an error. Caching the handle while the
  # process is still alive is what makes the code readable afterwards.
  # (Found by testing this helper against a real process instead of trusting
  # it, 2026-08-14. WaitForExit() alone does not fix it.)
  try { $null = $proc.Handle } catch { }
  $t0 = Get-Date
  while (-not $proc.HasExited) {
    Start-Sleep -Milliseconds 1000
    Write-Host ("`r       {0} - {1:mm\:ss} elapsed" -f $label, ((Get-Date) - $t0)) -NoNewline
  }
  $proc.WaitForExit()
  Write-Host ("`r       {0} - finished in {1:mm\:ss}          " -f $label, ((Get-Date) - $t0))
  # $null means "could not tell", never "failed" - the caller re-tests whether
  # the tool is actually there, which is the answer that matters anyway.
  try { return $proc.ExitCode } catch { return $null }
}

function Install-GitDirect {
  $rel = $null
  try {
    $rel = Invoke-RestMethod 'https://api.github.com/repos/git-for-windows/git/releases/latest' `
                             -UseBasicParsing -TimeoutSec 60 -Headers @{ 'User-Agent' = 'omnius-install' }
  } catch { Write-Status 'X' ("could not reach the Git for Windows release feed: {0}" -f $_.Exception.Message); return }
  $asset = $rel.assets | Where-Object { $_.name -match '^Git-.*-64-bit\.exe$' } | Select-Object -First 1
  if (-not $asset) { Write-Status 'X' 'no 64-bit Git installer in the latest release'; return }
  $exe = Get-Download $asset.browser_download_url $asset.name
  if (-not $exe) { return }
  # Git installs machine-wide, so Windows will raise a UAC prompt. Say so first:
  # an elevation dialog nobody expected looks like the installer misbehaving.
  Write-Status '..' 'installing Git (Windows will ask for administrator rights)'
  try {
    $p = Start-Process -FilePath $exe -PassThru `
         -ArgumentList '/VERYSILENT','/NORESTART','/NOCANCEL','/SP-','/SUPPRESSMSGBOXES'
    $code = Wait-Installer $p 'Git'
    if ($null -ne $code -and $code -ne 0) { Write-Status '!' ("the Git installer exited with code {0}" -f $code) }
  } catch { Write-Status 'X' ("could not run the Git installer: {0}" -f $_.Exception.Message) }
  Remove-Item $exe -Force -ErrorAction SilentlyContinue
}

function Get-PythonInstallerUrl {
  # Discovered, not pinned: a hard-coded version rots the day it is superseded,
  # and python.org keeps every release directory forever.
  #
  # The order is NOT "newest wins". This Python has to run faster-whisper,
  # ctranslate2, onnxruntime and playwright, and those ship compiled wheels that
  # trail a brand-new Python by months - so the newest series is the one most
  # likely to fail at `pip install`, loudly and late. 3.13 first (current, wheels
  # everywhere), 3.12 as the conservative fallback, 3.14 only if neither exists.
  # Keep this in step with the WingetId above so both routes land on the same
  # series; a machine with winget and one without should not end up different.
  #
  # The window is 6 deep per series because a series keeps getting SOURCE-only
  # security releases after its last Windows binary: 3.12 stopped shipping
  # installers at 3.12.10 and has four source-only releases stacked on top of
  # it (checked 2026-08-14). A 3-deep probe silently skipped the whole series.
  try {
    $listing = (Invoke-WebRequest 'https://www.python.org/ftp/python/' -UseBasicParsing -TimeoutSec 60).Content
  } catch { Write-Status 'X' ("could not reach python.org: {0}" -f $_.Exception.Message); return $null }
  $all = [regex]::Matches($listing, '(?<v>3\.\d+\.\d+)/') | ForEach-Object { $_.Groups['v'].Value } | Select-Object -Unique
  foreach ($series in @('3.13','3.12','3.14')) {
    $cands = @($all | Where-Object { $_ -like "$series.*" } |
               Sort-Object { [version]$_ } -Descending | Select-Object -First 6)
    foreach ($v in $cands) {
      $url = "https://www.python.org/ftp/python/$v/python-$v-amd64.exe"
      try {
        $r = Invoke-WebRequest $url -Method Head -UseBasicParsing -TimeoutSec 30
        if ($r.StatusCode -eq 200) { return $url }
      } catch { }                                   # 404 = source-only release
    }
  }
  return $null
}

function Install-PythonDirect {
  $url = Get-PythonInstallerUrl
  if (-not $url) { Write-Status 'X' 'no Windows installer found on python.org'; return }
  $exe = Get-Download $url (Split-Path $url -Leaf)
  if (-not $exe) { return }
  # InstallAllUsers=0 keeps this out of Program Files, so there is NO UAC prompt
  # and no admin requirement. PrependPath=1 is what makes `python` resolve
  # afterwards - without it the install succeeds and every later step still fails.
  #
  # InstallLauncherAllUsers=0 is the one that actually made it work. The py
  # launcher installs for ALL USERS by default even in a per-user install, and
  # that needs elevation - which `/quiet` has no way to ask for, so the
  # installer just sits there. Observed 2026-08-14 in a sandbox: downloaded
  # fine, then nothing, apparently forever.
  #
  # /passive rather than /quiet: it shows Python's own progress window, no
  # clicking required. This installer is run by a person at a keyboard, and a
  # visible bar beats a silent five minutes.
  # Trimmed to the parts this workspace actually uses, because this is by far
  # the slowest step of the install. Measured, not guessed: in a Windows Sandbox
  # it reached "Executables" at 2:09 and was still on "Development Libraries" at
  # 5:52, since every one of those thousands of small files is scanned on write.
  #
  # Dropped: the stdlib test suite, the CHM docs, tkinter (nothing here draws a
  # window - the notes app is HTTP), debug binaries, symbols, and the C headers
  # and .lib files. Include_dev is the surprising one, so: every dependency this
  # installer goes on to fetch - pywinpty, psutil, yt-dlp, pymupdf,
  # faster-whisper, playwright - ships a Windows wheel, and a wheel is a copy,
  # not a build. Headers are needed only when pip has to compile from source,
  # which none of them does. If some future package ever demands them, running
  # the same installer again and choosing Modify adds them in a minute.
  #
  # pip itself obviously stays, and so does PrependPath - without that the
  # install succeeds and every later step still fails to find `python`.
  Write-Status '..' 'installing Python (per-user, no administrator rights needed - a few minutes on a fresh machine)'
  try {
    $p = Start-Process -FilePath $exe -PassThru `
         -ArgumentList '/passive','InstallAllUsers=0','PrependPath=1', `
                       'InstallLauncherAllUsers=0','Include_pip=1','Include_dev=0', `
                       'Include_test=0','Include_doc=0','Include_tcltk=0', `
                       'Include_debug=0','Include_symbols=0'
    $code = Wait-Installer $p 'Python'
    if ($null -ne $code -and $code -ne 0) { Write-Status '!' ("the Python installer exited with code {0}" -f $code) }
  } catch { Write-Status 'X' ("could not run the Python installer: {0}" -f $_.Exception.Message) }
  Remove-Item $exe -Force -ErrorAction SilentlyContinue
}

function Install-NodeDirect {
  # Node ships a plain zip, so this needs no installer and no admin either.
  # index.json is the official release feed; the first entry carrying an `lts`
  # name is the current LTS, which is what OpenJS.NodeJS.LTS installs too.
  try {
    $rel = Invoke-RestMethod 'https://nodejs.org/dist/index.json' -UseBasicParsing -TimeoutSec 60
  } catch { Write-Status 'X' ("could not reach nodejs.org: {0}" -f $_.Exception.Message); return }
  $lts = $rel | Where-Object { $_.lts } | Select-Object -First 1
  if (-not $lts) { Write-Status 'X' 'no LTS release listed on nodejs.org'; return }
  $name = "node-$($lts.version)-win-x64"
  $zip = Get-Download "https://nodejs.org/dist/$($lts.version)/$name.zip" "$name.zip"
  if (-not $zip) { return }
  $dest = Join-Path $env:LOCALAPPDATA 'Omnius\node'
  $tmp = Join-Path $env:TEMP ('node-unpack-' + [guid]::NewGuid().ToString('N').Substring(0,8))
  try {
    # Function-scoped, and it is what makes the catch below reachable: this
    # script runs with ErrorActionPreference 'Continue', so a failing cmdlet
    # prints red and CARRIES ON. That is how a failed Move-Item was followed by
    # "[OK] Node installed" in the same breath (2026-08-15).
    $ErrorActionPreference = 'Stop'
    Write-Status '..' 'unpacking Node'
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    # Move-Item does not create intermediate directories, so the FIRST tool to
    # land in %LOCALAPPDATA%\Omnius has to make it. Node runs before ffmpeg,
    # which is the one that used to create it - so on a machine where ffmpeg
    # had not run yet, this failed every time and nowhere else.
    New-Item -ItemType Directory -Force -Path (Split-Path $dest -Parent) | Out-Null
    Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
    Move-Item (Join-Path $tmp $name) $dest -Force
    if (-not (Test-Path (Join-Path $dest 'node.exe'))) { throw "no node.exe under $dest" }
    [void](Add-ToUserPath $dest ' (Node, installed by Omnius)')
    Write-Status 'OK' ("Node {0} installed to {1}" -f $lts.version, $dest)
  } catch {
    Write-Status 'X' ("could not install Node: {0}" -f $_.Exception.Message)
  } finally {
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
  }
}

function Get-FfmpegSource {
  # SAME BUILD, FASTER HOST. gyan.dev is the site ffmpeg.org links to, but it
  # serves from one machine: in a VM it crawled at 7 MB of 106 while the rest of
  # the install waited (2026-08-15). The identical zip is published as a GitHub
  # release by the same builder, behind GitHub's CDN - which is where winget's
  # own manifest points, and why winget's ffmpeg felt instant on the same day.
  #
  # "essentials" rather than "full": it carries ffmpeg, ffprobe and ffplay,
  # which is everything whisper, /watch and the transcribe desk call. The full
  # build is 240 MB for codecs nothing here asks for.
  try {
    $rel = Invoke-RestMethod 'https://api.github.com/repos/GyanD/codexffmpeg/releases/latest' `
                             -UseBasicParsing -TimeoutSec 60 -Headers @{ 'User-Agent' = 'omnius-install' }
    $a = $rel.assets | Where-Object { $_.name -match 'essentials_build\.zip$' } | Select-Object -First 1
    if ($a) { return @{ Url = $a.browser_download_url; Name = $a.name } }
  } catch { }
  # The original URL stays as the fallback: no API, no rate limit, and always
  # the current release. Slower, but it works when GitHub's API does not.
  return @{ Url  = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
            Name = 'ffmpeg-release-essentials.zip' }
}

function Install-FfmpegDirect {
  # ffmpeg ships no installer: it is a zip of static binaries, which is why this
  # one needs no admin and no package manager at all.
  $src = Get-FfmpegSource
  $zip = Get-Download $src.Url $src.Name
  if (-not $zip) { return }
  $tmp = Join-Path $env:TEMP ('ffmpeg-unpack-' + [guid]::NewGuid().ToString('N').Substring(0,8))
  try {
    $ErrorActionPreference = 'Stop'          # see Install-NodeDirect
    Write-Status '..' 'unpacking ffmpeg'
    Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
    $bin = Get-ChildItem -Path $tmp -Recurse -Filter 'ffmpeg.exe' -File | Select-Object -First 1
    if (-not $bin) { throw 'no ffmpeg.exe inside the archive' }
    New-Item -ItemType Directory -Force -Path $script:OmniusBin | Out-Null
    # ffprobe travels with it: whisper and the transcribe desk both call it, and
    # an ffmpeg without ffprobe fails later and confusingly.
    Copy-Item (Join-Path $bin.DirectoryName '*.exe') $script:OmniusBin -Force
    if (-not (Test-Path (Join-Path $script:OmniusBin 'ffmpeg.exe'))) {
      throw "nothing landed in $script:OmniusBin"
    }
    [void](Add-ToUserPath $script:OmniusBin ' (portable tools Omnius installed)')
    Write-Status 'OK' ("ffmpeg + ffprobe installed to {0}" -f $script:OmniusBin)
  } catch {
    Write-Status 'X' ("could not install ffmpeg: {0}" -f $_.Exception.Message)
  } finally {
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
  }
}

function Test-PyImport([string]$module) {
  # -> $null when the import works, otherwise the ERROR TEXT.
  #
  # The old check was `python -c "import x" 2>nul`, which decided pass/fail
  # correctly and threw away the one line that explains a failure. On a fresh
  # Windows 10 VM (2026-08-15) pymupdf and faster-whisper both reported FAILED
  # with nothing above them - pip had actually succeeded, and the packages
  # could not LOAD. Never hide the reason for a failure you are about to report.
  $out = & cmd /c "python -c ""import $module"" 2>&1"
  if ($LASTEXITCODE -eq 0) { return $null }
  return (($out | Out-String).Trim())
}

function Install-VcRedist {
  # The Microsoft Visual C++ 2015-2022 runtime. A bare Windows does not have it,
  # and Python ships only vcruntime140.dll - not vcruntime140_1.dll, which every
  # C++ extension built with VS2019 or later needs. The symptom is exactly what
  # that VM showed: pip installs fine, then `import` dies with "DLL load failed
  # while importing ...". It hits the two packages here that ARE C++ - pymupdf
  # and ctranslate2 (under faster-whisper) - and leaves the pure-Python ones
  # working, which is why the failure looks so arbitrary.
  #
  # aka.ms/vs/17/release is Microsoft's own permanent link to the current build.
  $exe = Get-Download 'https://aka.ms/vs/17/release/vc_redist.x64.exe' 'vc_redist.x64.exe'
  if (-not $exe) { return $false }
  Write-Status '..' 'installing the Microsoft Visual C++ runtime (Windows will ask for administrator rights)'
  try {
    $p = Start-Process -FilePath $exe -PassThru -ArgumentList '/quiet','/norestart'
    $code = Wait-Installer $p 'VC++ runtime'
    # 3010 = success, reboot advised. Not an error, and not worth a reboot here:
    # the DLLs are in place the moment the installer finishes.
    if ($null -ne $code -and $code -notin 0, 3010) {
      Write-Status '!' ("the VC++ runtime installer exited with code {0}" -f $code)
      return $false
    }
    return $true
  } catch {
    Write-Status 'X' ("could not install the VC++ runtime: {0}" -f $_.Exception.Message)
    return $false
  } finally { Remove-Item $exe -Force -ErrorAction SilentlyContinue }
}

function Resolve-PyImport([string]$module, [string]$label) {
  # Import, and if it fails SAY WHY - then fix the one cause we can fix.
  # -> $true if the module ends up importable.
  $err = Test-PyImport $module
  if (-not $err) { return $true }
  $err -split "`n" | Select-Object -Last 3 | ForEach-Object {
    if ($_.Trim()) { Write-Host ("       {0}" -f $_.Trim()) }
  }
  if ($err -match 'DLL load failed' -and -not $CheckOnly) {
    Write-Status 'i' ("{0} installed, but Windows is missing the C++ runtime it needs" -f $label)
    if (Ask-YesNo 'install the Microsoft Visual C++ runtime now (25 MB, from microsoft.com)') {
      if (Install-VcRedist) {
        $err = Test-PyImport $module
        if (-not $err) { return $true }
        Write-Status '!' 'still not loading - the output above is what it says now'
      }
    }
  }
  return $false
}

function Ask-YesNo([string]$question, [switch]$DefaultNo) {
  if ($CheckOnly) { return $false }
  if ($DefaultNo) {
    $a = Read-Host ("     {0}? [y/N]" -f $question)
    return ($a -match '^[yYjJ]')
  }
  $a = Read-Host ("     {0}? [Y/n]" -f $question)
  return ($a -eq '' -or $a -match '^[yYjJ]')      # j: the installer gets run on German keyboards
}

function Ask-Install([string]$what) { return (Ask-YesNo ("install {0} now" -f $what)) }

function Test-ClaudeAuth {
  # -> $true signed in, $false signed out, $null cannot tell (older CLI without
  # `auth status`). The distinction matters: "cannot tell" must stay SILENT
  # rather than nag somebody who is perfectly signed in.
  if (-not (Test-Claude)) { return $null }
  try {
    $out = (& claude auth status 2>&1 | Out-String)
    if ($out -match '"loggedIn"\s*:\s*true')  { return $true }
    if ($out -match '"loggedIn"\s*:\s*false') { return $false }
    if ($out -match 'not logged in|signed out|no credentials') { return $false }
    return $null
  } catch { return $null }
}

Write-Host '============================================================'
Write-Host ' OMNIUS - setup & environment check'
Write-Host '============================================================'
Write-Host ''

# --- mark-of-the-web ----------------------------------------------------------
# Files unzipped from a DOWNLOAD carry a Zone.Identifier stream saying "this
# came from the internet", and under the default RemoteSigned policy PowerShell
# then refuses to run them: "no está firmado digitalmente". The .bat shims
# survive because they pass -ExecutionPolicy Bypass, so the failure only shows
# up the moment someone runs a .ps1 directly - which is exactly what a person
# does when something needs debugging (2026-08-11, on the first downloaded
# install). Clearing it once here costs nothing and removes a whole class of
# confusing failure. Unblock-File is per-file and safe: it only strips that
# stream, and files that were never blocked are untouched.
if (-not $CheckOnly) {
  $blocked = @(Get-ChildItem -Path $PSScriptRoot -Recurse -Include *.ps1, *.bat, *.psm1 `
                 -File -ErrorAction SilentlyContinue |
               Where-Object { Get-Item $_.FullName -Stream Zone.Identifier -ErrorAction SilentlyContinue })
  if ($blocked.Count -gt 0) {
    $blocked | Unblock-File -ErrorAction SilentlyContinue
    Write-Status 'OK' ("unblocked {0} script(s) - downloaded files are blocked by Windows" -f $blocked.Count)
  }
}

# --- whose account is this? ---------------------------------------------------
# A shell running as LocalSystem has no user profile: $env:USERPROFILE points at
# C:\WINDOWS\system32\config\systemprofile, and anything that writes to "your"
# home writes THERE. The Claude CLI installer is the first thing to hit it -
# `EPERM: mkdir 'C:\WINDOWS\system32\config\systemprofile\.cache'` (2026-08-15)
# - but it would not be the last: the CLI's own credentials, ~\.claude, the
# desktop shortcut and every hook path we write are all per-user. Installing
# from that account produces a fleet the actual human cannot use.
#
# This is NOT the same thing as "elevated". Running as an admin USER is fine and
# normal; Git's installer asks for exactly that. Only SYSTEM is refused.
$whoami = try { [Security.Principal.WindowsIdentity]::GetCurrent().Name } catch { '' }
if ($env:USERPROFILE -like '*\system32\config\systemprofile*' -or $whoami -eq 'NT AUTHORITY\SYSTEM') {
  Write-Status 'X' ("this shell is running as {0}, which has no user profile" -f ($whoami -replace '^$','SYSTEM'))
  Write-Status 'i' ("its home is {0}" -f $env:USERPROFILE)
  Write-Host ''
  Write-Host '  Run install.bat as YOURSELF - a normal double-click, or "Run as'
  Write-Host '  administrator" from your own account. Both are fine. What does not'
  Write-Host '  work is a SYSTEM shell (PsExec -s, a scheduled task set to SYSTEM,'
  Write-Host '  or a remote-management agent), because everything this installs -'
  Write-Host '  the Claude sign-in, ~\.claude, the desktop icon, the hook paths -'
  Write-Host '  is per-user, and you would not own any of it.'
  exit 1
}

# --- prerequisites ------------------------------------------------------------
$haveWinget = (Test-Cmd 'winget') -and (-not $NoWinget)
if ($NoWinget) {
  Write-Status 'i' '-NoWinget: ignoring winget on purpose, to exercise the direct downloads'
} elseif (-not $haveWinget) {
  Write-Status '!' 'winget not available - installing required tools directly from their official sites instead'
}

# ORDER MATTERS. Everything that can run unattended goes first - downloads,
# silent installers, a zip to unpack - so the long boring stretch needs nobody
# watching. The Claude CLI is LAST on purpose (his call, 2026-08-14): it is the
# one step that ends in a browser and a sign-in, and asking a person to be
# present is only fair once nothing else is going to interrupt them.
$tools = @(
  @{ Name='git';    Label='Git';              WingetId='Git.Git';                   Required=$true;  Url='https://git-scm.com';            Why='version control - the system runs on it'; Direct={ Install-GitDirect }; DirectFrom='github.com/git-for-windows' },
  # 3.13, not 3.12: 3.12 stopped shipping Windows installers at 3.12.10 (April
  # 2025) and only gets source-only security releases now, so pinning it means a
  # fresh install gets an ever-older Python. Kept in step with the series order
  # in Get-PythonInstallerUrl - both routes must land on the same Python.
  @{ Name='python'; Label='Python 3.10+';     WingetId='Python.Python.3.13';        Required=$true;  Url='https://python.org';             Why='daybook + watchdog + whisper'; Test={ Test-Python }; Direct={ Install-PythonDirect }; DirectFrom='python.org' },
  @{ Name='node';   Label='Node.js LTS';      WingetId='OpenJS.NodeJS.LTS';         Required=$false; Url='https://nodejs.org';             Why='remotion video rendering'; Direct={ Install-NodeDirect }; DirectFrom='nodejs.org' },
  # Promoted to REQUIRED 2026-08-06. It was optional when voice notes were the
  # only user; since then /watch, whisper and the whole tool.transcribe desk all
  # shell out to it, and every one of them dies with an unhelpful error if it is
  # absent. A missing ffmpeg is not a reduced install, it is a broken one.
  @{ Name='ffmpeg'; Label='ffmpeg';           WingetId='Gyan.FFmpeg';               Required=$true;  Url='https://ffmpeg.org';             Why='voice notes, video analysis, meeting transcription'; Direct={ Install-FfmpegDirect }; DirectFrom='gyan.dev (the build ffmpeg.org links to)'; Test={ (Test-Cmd 'ffmpeg') -or (Test-Path (Join-Path $script:OmniusBin 'ffmpeg.exe')) } },
  @{ Name='wt';     Label='Windows Terminal'; WingetId='Microsoft.WindowsTerminal'; Required=$false; Url='Microsoft Store';                Why='tabbed terminals for the fleet' },
  @{ Name='claude'; Label='Claude Code CLI';  Installer='claude'; Test={ Test-Claude }; Required=$true;  Url='https://claude.com/claude-code'; Why='the agent runtime - installed last, because signing in needs you' }
)

$missingRequired = @()
foreach ($t in $tools) {
  $present = if ($t['Test']) { & $t['Test'] } else { Test-Cmd $t.Name }
  if ($present) { Write-Status 'OK' $t.Label; continue }

  $sev = if ($t.Required) { 'X' } else { '!' }
  Write-Status $sev ("{0} missing - {1}  ({2})" -f $t.Label, $t.Why, $t.Url)

  $attempted = $false
  if ($t['Installer'] -eq 'claude') {
    if (Ask-Install $t.Label) {
      $attempted = $true
      Write-Status '..' 'running the official installer: irm https://claude.ai/install.ps1 | iex'
      # In a CHILD PROCESS, not `iex` into ours. Piping someone else's script
      # into Invoke-Expression runs it in OUR session, so anything it changes -
      # StrictMode, ErrorActionPreference, the current directory - persists for
      # the rest of our install. That is not hypothetical: on a real fresh
      # install (2026-08-11) the Claude installer succeeded, left StrictMode on,
      # and the very next line of ours died with PropertyNotFoundStrict on a
      # hashtable key that simply was not set. The install stopped there.
      # A child process cannot reach back into us.
      # Its output is CAPTURED, not streamed. That installer ends with a big
      # "Installation complete!" and a paragraph telling you to edit PATH by
      # hand - both true of itself, both wrong here: our install is only three
      # steps in, and we add that PATH entry ourselves two lines below. Printed
      # mid-run it reads as "you are done" (2026-08-14). Kept in full and shown
      # verbatim the moment anything goes wrong.
      # The call operator, NOT Start-Process: `&` starts the same child process
      # while inheriting this shell's environment verbatim, and 2>&1 captures
      # both streams. Start-Process with redirection did the job too, but it
      # sets up its own environment block and console handles, and when this
      # step failed on a real machine (2026-08-15) that difference was one more
      # thing to rule out. It takes ~15s, so nothing needs a heartbeat.
      try {
        $ctext = (& powershell -NoProfile -ExecutionPolicy Bypass -Command `
                    "Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression" 2>&1 |
                  Out-String)
        $code = $LASTEXITCODE
        if (-not (Test-Claude)) {
          Write-Status '!' ("the Claude installer exited with code {0} - its output:" -f $code)
          $ctext -split "`n" | ForEach-Object { if ($_.Trim()) { Write-Host ("       {0}" -f $_.TrimEnd()) } }
          # Name the cause when we can recognise it. "EPERM ... systemprofile"
          # is not a broken installer, it is a shell with no user profile, and
          # the fix is a different shell rather than a retry (2026-08-15).
          if ($ctext -match 'systemprofile') {
            Write-Status 'X' 'that path is the SYSTEM account''s home - this shell has no user profile'
            Write-Status 'i' 'run install.bat from your own account (elevated is fine, SYSTEM is not)'
          } elseif ($ctext -match 'EPERM|EACCES|denied') {
            Write-Status 'i' ("something blocked it from writing to {0} - antivirus, or a policy on this machine" -f $script:ClaudeBin)
          } else {
            Write-Status 'i' 'you can install it by hand from https://claude.com/claude-code, then re-run install.bat'
          }
        } else {
          $ver = [regex]::Match($ctext, 'Version:\s*(\S+)')
          Write-Status 'OK' ("Claude Code installed{0}" -f $(if ($ver.Success) { " ({0})" -f $ver.Groups[1].Value } else { '' }))
        }
      } catch { Write-Status 'X' ("installer failed: {0}" -f $_.Exception.Message) }
      Add-ClaudeToUserPath
    }
  } elseif ($t['WingetId'] -and $haveWinget) {
    if (Ask-Install $t.Label) {
      $attempted = $true
      # --source winget, always. Without it winget also queries msstore, and on
      # a clean Windows 11 (2026-08-14) that source failed TLS validation
      # (0x8a15005e) - whereupon winget found the package in two sources, called
      # it ambiguous, and installed NOTHING. Node and ffmpeg both died that way
      # in one run. We only ever want the winget source anyway; naming it skips
      # the broken one entirely instead of depending on it being reachable.
      winget install -e --id $t['WingetId'] --source winget `
                     --accept-source-agreements --accept-package-agreements
      if ($LASTEXITCODE -ne 0) {
        Write-Status '!' ("winget exited with code {0} for {1}" -f $LASTEXITCODE, $t.Label)
      }
    }
  }

  if ($attempted) { Refresh-Path }
  $present = if ($t['Test']) { & $t['Test'] } else { Test-Cmd $t.Name }

  # The direct route, when winget is absent OR when it ran and left the tool
  # missing anyway. That second case is not theoretical: on 2026-08-14 winget
  # found Node and ffmpeg in two sources, called them ambiguous and installed
  # neither. A package manager that fails is no reason for the install to fail.
  if (-not $present -and $t['Direct']) {
    if (Ask-Install ("{0} directly from {1}" -f $t.Label, $t['DirectFrom'])) {
      $attempted = $true
      try { & $t['Direct'] } catch { Write-Status 'X' ("{0}: {1}" -f $t.Label, $_.Exception.Message) }
      Refresh-Path
      $present = if ($t['Test']) { & $t['Test'] } else { Test-Cmd $t.Name }
    }
  }

  if ($present) {
    Write-Status 'OK' ("{0} ready" -f $t.Label)
  } else {
    # Say which of the two actually happened. "installed but not on PATH" was
    # printed even when the install had plainly failed a line earlier, which
    # sent him restarting a window that was never the problem (2026-08-14).
    if ($attempted) {
      Write-Status '!' ("{0}: the install did not complete - see the output just above" -f $t.Label)
    }
    if ($t.Required) { $missingRequired += $t.Label }
  }
}

if (Test-Python) {
  $v = & python -c "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
  try { if ([version]$v -lt [version]'3.10') { Write-Status '!' ("Python {0} found - daybook and tools need 3.10+" -f $v) } } catch {}
}

if ($missingRequired.Count -gt 0) {
  Write-Host ''
  Write-Status 'X' ("required and still missing: {0} - fix, then re-run install.bat" -f ($missingRequired -join ', '))
  Write-Status 'i' 'each of those can also be installed by hand from the URL above, then re-run install.bat'
  exit 1
}

# --- signing in ---------------------------------------------------------------
# The CLI installs signed OUT, and nothing in this system works signed out:
# every desk is a `claude -p` run, and a signed-out CLI fails at the first
# message with nothing in the channel to explain why. So it is worth one
# question here - asked, never forced, because it opens a browser and that is a
# moment somebody has to actually be present for.
Write-Host ''
Write-Host '--- Claude sign-in ----------------------------------------'
$authed = Test-ClaudeAuth
if ($authed -eq $true) {
  Write-Status 'OK' 'Claude CLI is signed in'
} elseif ($authed -eq $null) {
  Write-Status 'i' 'could not read the sign-in state - if desks fail to start, run: claude auth login'
} elseif ($CheckOnly) {
  Write-Status '!' 'Claude CLI is NOT signed in - run: claude auth login'
} else {
  Write-Status '!' 'Claude CLI is not signed in yet - no desk can run until it is'
  Write-Host ''
  Write-Host '     It opens a browser, you approve, and it gives you a CODE to paste'
  Write-Host '     back here. Have a minute for it now, or do it later - the rest of'
  Write-Host '     this install does not depend on it.'
  Write-Host ''
  # Defaults to NO, and that is the whole point: once `claude auth login` is
  # running there is no clean way out of it. Pressing Enter at its "Paste code
  # here" prompt does not cancel - it retries, and on 2026-08-15 somebody who
  # changed their mind got five "Invalid code" errors and a failed sign-in for
  # answering a question they had already decided against. An accidental Enter
  # must not start something you cannot stop.
  if (Ask-YesNo 'sign in now (browser + paste a code back here)' -DefaultNo) {
    try {
      & claude auth login
    } catch { Write-Status '!' ("sign-in did not complete: {0}" -f $_.Exception.Message) }
    if ((Test-ClaudeAuth) -eq $true) {
      Write-Status 'OK' 'signed in'
    } else {
      Write-Status '!' 'still signed out - finish it any time with:  claude auth login'
    }
  } else {
    Write-Status 'i' 'skipped - sign in before your first desk runs:  claude auth login'
  }
}

# --- .env ---------------------------------------------------------------------
Write-Host ''
Write-Host '--- .env -------------------------------------------------'
if (Test-Path .env) {
  Write-Status 'OK' '.env exists'
  # .env is the one file that CANNOT travel in the backup zip (secrets stay out
  # of archives), so every move ends with it hand-copied from the old machine -
  # and it carries MACHINE_NAME with it. On 2026-08-14 the new laptop spent its
  # first hour insisting it was the old one: session claims, Discord channel
  # topics and !status all wrong, and claims written under the old name then
  # read as FOREIGN, i.e. "another machine owns this desk". Nothing anywhere
  # said so. One line catches the whole class.
  $envMachine = ''
  foreach ($line in (Get-Content .env -ErrorAction SilentlyContinue)) {
    if ($line -match '^\s*MACHINE_NAME\s*=\s*(.+?)\s*$') { $envMachine = $Matches[1].Trim('"').Trim("'") }
  }
  if ($envMachine -and $envMachine -ne $env:COMPUTERNAME) {
    Write-Status '!' ("your .env says MACHINE_NAME={0}, but this PC is {1}" -f $envMachine, $env:COMPUTERNAME)
    Write-Host  ("        restored from another machine? Clear that line (or set it to {0})," -f $env:COMPUTERNAME)
    Write-Host   '        or claims, channel topics and !status will all carry the old name.'
    Write-Host   '        Claims already written under the old name look FOREIGN - prune with:'
    Write-Host   '          python tools\orchestrator\fleet_ops.py status --prune'
  }
} elseif ($CheckOnly) {
  Write-Status '!' '.env missing - would be created from .env.example'
} else {
  Copy-Item .env.example .env
  Write-Status 'OK' '.env created from .env.example - open it and paste your Discord values'
}

# --- config -------------------------------------------------------------------
# The .example.ini files ship; nothing used to copy them, so a fresh user ended
# up with no config\ at all. Most readers fall back to defaults safely, but
# email CANNOT - there is nothing to default an account to - so the gap was
# silent until someone tried to use mail. Never overwrites: an existing .ini is
# the owner's, exactly like memory\ below.
Write-Host ''
Write-Host '--- config -----------------------------------------------'
$cfgDir = Join-Path $PSScriptRoot 'config'
if (Test-Path $cfgDir) {
  # .json as well as .ini since 2026-08-14: fleet.json stopped being tracked
  # when the repo became install-only, so something has to create it - and a
  # missing one is not fatal (the watchdog falls back to built-in defaults) but
  # it does mean !model has nowhere to write.
  foreach ($ex in Get-ChildItem (Join-Path $cfgDir '*.example.*')) {
    $real = Join-Path $cfgDir ($ex.Name -replace '\.example(\.[^.]+)$', '$1')
    if (Test-Path $real) {
      Write-Status 'OK' ("config\" + (Split-Path $real -Leaf) + ' exists - left untouched')
    } elseif ($CheckOnly) {
      Write-Status '!' ("config\" + (Split-Path $real -Leaf) + ' missing - would be created from the example')
    } else {
      Copy-Item $ex.FullName $real
      Write-Status 'OK' ("config\" + (Split-Path $real -Leaf) + ' created from the example')
    }
  }
}

# --- github (updates) ---------------------------------------------------------
# A zip install used to ship frozen: no .git (a release must not carry
# history), so no remote, no `git pull`, no way to receive fixes short of
# unpacking a new zip NEXT to your instance and moving your life across. The
# public repo's history is clean by construction now, so attaching is safe -
# and everything personal (memory\, projects\, daybook notes, config\*.ini,
# .env, state\) is gitignored, so a pull can only ever move product code.
#
# The zip carries RELEASE-COMMIT (stamped by pack.ps1): the exact commit it
# was cut from. Being born THERE means `git status` starts clean instead of
# reading every file as locally modified against whatever the tip is by
# install day; the first `!update` then walks forward normally.
Write-Host ''
Write-Host '--- github (updates) -------------------------------------'
$repoUrl = 'https://github.com/timoinglin/omnius-agent.git'
if (Test-Path (Join-Path $PSScriptRoot '.git')) {
  Write-Status 'OK' 'git workspace - updates arrive with `git pull` or !update in Discord'
} elseif (-not (Test-Cmd 'git')) {
  Write-Status '!' 'git not available yet - rerun install.bat later to attach for updates'
} elseif ($CheckOnly) {
  Write-Status '!' 'not attached to GitHub - would attach, so !update / git pull work'
} else {
  Write-Status '..' 'attaching this install to GitHub (code only - your files are gitignored)'
  & git -C $PSScriptRoot init -b main *> $null
  & git -C $PSScriptRoot remote add origin $repoUrl *> $null
  & git -C $PSScriptRoot fetch --quiet origin main *> $null
  if ($LASTEXITCODE -ne 0) {
    # Offline is a normal install condition. The half-attach is harmless and
    # completes on any later rerun (init and remote add are both idempotent
    # enough: the .git\ test above short-circuits next time, and `git fetch`
    # is all that was missing).
    Write-Status '!' 'could not reach GitHub - attach incomplete; rerun install.bat online to finish'
  } else {
    $target = 'origin/main'
    $stampFile = Join-Path $PSScriptRoot 'RELEASE-COMMIT'
    if (Test-Path $stampFile) {
      $stamp = ([string](Get-Content $stampFile -TotalCount 1)).Trim()
      if ($stamp -match '^[0-9a-f]{7,40}$') {
        & git -C $PSScriptRoot cat-file -e "$stamp^{commit}" *> $null
        if ($LASTEXITCODE -eq 0) { $target = $stamp }
      }
    }
    & git -C $PSScriptRoot reset --mixed $target *> $null   # unborn main is born here; the working tree is not touched
    & git -C $PSScriptRoot branch --set-upstream-to=origin/main main *> $null
    $short = (& git -C $PSScriptRoot rev-parse --short HEAD).Trim()
    $dirty = @(& git -C $PSScriptRoot status --porcelain 2>$null | Where-Object { $_ })
    if ($dirty.Count -eq 0) {
      Write-Status 'OK' ('attached at {0} - update anytime: !update in Discord, or git pull + !reload' -f $short)
    } else {
      Write-Status 'OK' ('attached at {0} - {1} file(s) differ locally (git status names them); updates still work' -f $short, $dirty.Count)
    }
  }
}

# --- tools set ----------------------------------------------------------------
Write-Host ''
Write-Host '--- tools set --------------------------------------------'

# watch: video-analysis skill, vendored from bradautomates/claude-video (MIT)
if (Test-Path .claude\skills\watch\SKILL.md) {
  Write-Status 'OK' 'watch skill already vendored'
} elseif ($CheckOnly) {
  Write-Status '!' 'watch skill not vendored yet'
} else {
  Write-Status '..' 'vendoring watch skill from github.com/bradautomates/claude-video'
  $tmp = Join-Path $env:TEMP 'claude-video'
  if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
  cmd /c "git clone --depth 1 --quiet https://github.com/bradautomates/claude-video ""$tmp"" 2>nul" | Out-Null
  if (Test-Path (Join-Path $tmp 'skills\watch\SKILL.md')) {
    Copy-Item -Recurse -Force (Join-Path $tmp 'skills\watch') .claude\skills\watch
    Remove-Item -Recurse -Force $tmp
    Write-Status 'OK' 'watch skill vendored into .claude\skills\watch'
  } else {
    Write-Status 'X' 'could not fetch claude-video - check network, then re-run'
  }
}

# Desk-bridge dependencies. NOT optional extras: without pywinpty the bridge
# cannot own a terminal at all, and without psutil the watchdog cannot see the
# owner's own sessions and would open a second brain on a desk he is using.
foreach ($dep in @(
    @{ Name = 'pywinpty'; Import = 'winpty'; Why = 'the desk bridge (warm terminals)' },
    @{ Name = 'psutil';   Import = 'psutil'; Why = 'seeing the owner''s own sessions' },
    # The watch skill refuses to run at all without yt-dlp, even for a LOCAL
    # file it never needs to download (2026-08-03: a screen recording on disk
    # failed its preflight). pip is the portable way to get it on Windows.
    @{ Name = 'yt-dlp';   Import = 'yt_dlp'; Why = 'the watch skill (video)' },
    # Reading a PDF at all. Windows ships no poppler, so without this NOTHING
    # here can open one - not tools\documents, not even the agent's own file
    # reader. It also renders pages to images, which is the no-key way to put
    # a scan in front of the agent's eyes.
    @{ Name = 'pymupdf';  Import = 'fitz';   Why = 'reading PDFs (invoices, docs)' })) {
  # One way to test an import, everywhere: a PRESENCE probe simply ignores
  # the error text that a FAILURE report needs (Test-PyImport).
  if (-not (Test-PyImport $dep.Import)) {
    Write-Status 'OK' "$($dep.Name) already installed"
  } elseif ($CheckOnly) {
    Write-Status '!' "$($dep.Name) missing - needed for $($dep.Why)"
  } else {
    Write-Status '..' "pip install $($dep.Name)"
    cmd /c "python -m pip install --quiet $($dep.Name)"
    if (Resolve-PyImport $dep.Import $dep.Name) { Write-Status 'OK' "$($dep.Name) installed" }
    else { Write-Status 'X' "$($dep.Name) FAILED - $($dep.Why) will not work" }
  }
}

# A FRESH RELEASE ships templates\fresh\memory\ and no memory\ at all - the
# building instance's memory is its biography and never travels. Plant the seed
# on first install so the new instance boots with the product half (how the
# fleet works, the permission posture, the lessons) and none of the history.
# Never overwrites: an existing memory\ is this instance's own and is sacred.
$seedDir = Join-Path $PSScriptRoot 'templates\fresh\memory'
$memDir  = Join-Path $PSScriptRoot 'memory'
if ((Test-Path $seedDir) -and -not (Test-Path (Join-Path $memDir 'orchestrator\MEMORY.md'))) {
  if ($CheckOnly) {
    Write-Status '!' 'memory\ is empty - install.bat will seed it from the release template'
  } else {
    New-Item -ItemType Directory -Path $memDir -Force | Out-Null
    Copy-Item (Join-Path $seedDir '*') $memDir -Recurse -Force
    Write-Status 'OK' 'seeded memory\ for a fresh instance (product knowledge, no history)'
  }
} elseif (Test-Path (Join-Path $memDir 'orchestrator\MEMORY.md')) {
  Write-Status 'OK' 'memory\ already present - left untouched'
}

# Hook paths are absolute and therefore MACHINE-SPECIFIC, so they are WRITTEN
# HERE and never committed: they land in .claude\settings.local.json, which is
# gitignored and excluded from release zips. A clone or an unzipped workspace
# arrives with no hooks at all - which is the safe state, because a hook
# pointing at a path that does not exist BLOCKS every prompt typed at that desk
# (measured 2026-08-14; it is what a clone of the public repo used to do).
# This is the single most important step for a moved or freshly installed
# instance: until it runs, no desk reports its turns to the watchdog.
if ($CheckOnly) {
  cmd /c "python tools\discord\fix_hook_paths.py --check" | Out-Null
  if ($LASTEXITCODE -eq 0) { Write-Status 'OK' 'every desk is wired to this machine' }
  else { Write-Status '!' 'desks are not wired to this machine yet - re-run install.bat to fix' }
} else {
  Write-Status '..' 'wiring desk hooks to this machine'
  $fixed = cmd /c "python tools\discord\fix_hook_paths.py"
  if ($LASTEXITCODE -eq 0) {
    $line = $fixed | Select-String '^hooks:' | Select-Object -Last 1
    if ($line) { Write-Status 'OK' $line.ToString().Trim() }
    else { Write-Status 'OK' 'desk hooks written for this machine' }
  } else {
    Write-Status 'X' 'writing desk hooks FAILED - desks will not accept prompts'
  }
}

# whisper: local speech-to-text (default engine: faster-whisper)
if (-not (Test-PyImport 'faster_whisper')) {
  Write-Status 'OK' 'faster-whisper already installed'
} elseif ($CheckOnly) {
  Write-Status '!' 'faster-whisper not installed yet'
} else {
  Write-Status '..' 'pip install faster-whisper'
  cmd /c "python -m pip install --quiet faster-whisper"
  # ctranslate2 rides under this one, and it is the C++ extension most likely to
  # need the VC++ runtime - so this is usually where a bare Windows shows it.
  if (Resolve-PyImport 'faster_whisper' 'faster-whisper') {
    Write-Status 'OK' 'faster-whisper installed'
  } else {
    Write-Status 'X' 'faster-whisper install failed - audio transcription unavailable'
  }
}

# whisper model pre-warm (best-effort): download once so the first voice note is fast.
# The model cache is machine-local and does NOT travel in the zip.
if (-not $CheckOnly) {
  if (-not (Test-PyImport 'faster_whisper')) {
    Write-Status '..' 'pre-warming whisper model (first time only, ~140MB)'
    # Two warnings huggingface_hub prints on a normal Windows box, neither of
    # which the person running an installer can act on: no HF token (we make
    # anonymous downloads on purpose - no account should be needed) and no
    # symlink support (that wants Developer Mode or admin; the cache works
    # either way, just larger). Twelve lines of alarming text for "it worked".
    $env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
    $env:HF_HUB_DISABLE_TELEMETRY = '1'
    cmd /c "python tools\whisper\prewarm.py" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Status 'OK' 'whisper model cached' }
    else { Write-Status '!' 'whisper pre-warm skipped (will download on first voice note)' }
  }
}

# playwright: headless browsing (fetch pages that need JavaScript, scrape, fill
# public forms). The pip package is small; the Chromium build it drives is
# ~150MB, so that half is ASKED FOR rather than assumed - a fresh install on a
# tethered connection should not silently pull a browser.
# Deliberately NOT the tool for anything behind a login: that is the Claude
# Chrome extension, which uses the real browser and its real sessions.
$havePw = (-not (Test-PyImport 'playwright'))
if ($havePw) {
  Write-Status 'OK' 'playwright already installed'
} elseif ($CheckOnly) {
  Write-Status '!' 'playwright not installed yet'
} else {
  Write-Status '..' 'pip install playwright'
  cmd /c "python -m pip install --quiet playwright"
  $havePw = Resolve-PyImport 'playwright' 'playwright'
  if ($havePw) { Write-Status 'OK' 'playwright installed' }
  else { Write-Status 'X' 'playwright install failed - headless browsing unavailable' }
}

# The browser binary is a separate ~150MB download, cached per USER (not in the
# workspace), so it never travels in the zip and a moved install re-asks once.
# Checking the cache directory rather than the question keeps a re-run silent
# once Chromium is there - a repair must not re-ask what is already answered.
if ($havePw -and -not $CheckOnly) {
  $pwBrowser = Join-Path $env:LOCALAPPDATA 'ms-playwright'
  $haveChromium = (Test-Path $pwBrowser) -and
                  (Get-ChildItem $pwBrowser -Filter 'chromium*' -Directory -ErrorAction SilentlyContinue)
  if ($haveChromium) {
    Write-Status 'OK' 'playwright chromium already downloaded'
  } else {
    Write-Status '!' 'playwright has no browser yet (~150MB download)'
    if (Ask-Install 'the Chromium build playwright drives (~150MB)') {
      Write-Status '..' 'downloading chromium - this can take a few minutes'
      cmd /c "python -m playwright install chromium"
      if ($LASTEXITCODE -eq 0) { Write-Status 'OK' 'chromium installed' }
      else { Write-Status 'X' 'chromium download failed - rerun install.bat to retry' }
    } else {
      Write-Status 'skip' 'chromium - run: python -m playwright install chromium'
    }
  }
}

# remotion: video rendering (needs Node)
if (-not (Test-Cmd 'node')) {
  Write-Status 'skip' 'remotion - Node.js not installed'
} elseif (Test-Path tools\remotion\node_modules) {
  Write-Status 'OK' 'remotion already installed'
} elseif ($CheckOnly) {
  Write-Status '!' 'remotion not installed yet'
} else {
  Write-Status '..' 'npm install remotion into tools\remotion - this can take a while'
  Push-Location tools\remotion
  # npm ci, never `npm install <names>`: install-by-name resolves the NEWEST
  # matching versions and rewrites the tracked package.json + package-lock.json,
  # which leaves every fresh install with a dirty tree - and the dirty-tree
  # guard then blocks that user's first `!update go` (seen live 2026-08-18 on a
  # colleague's instance: remotion had drifted 4.0.508 -> 4.0.512 all by
  # itself). `npm ci` installs exactly what the shipped lockfile says and
  # writes nothing. The name-list fallback stays only for a tree that somehow
  # has no lockfile to obey.
  if (Test-Path package-lock.json) {
    cmd /c "npm ci --no-fund --no-audit"
  } else {
    if (-not (Test-Path package.json)) { cmd /c "npm init -y" | Out-Null }
    cmd /c "npm install --no-fund --no-audit remotion @remotion/cli react react-dom"
  }
  Pop-Location
  if (Test-Path tools\remotion\node_modules) {
    # node_modules existing is not the same as remotion WORKING. Newer npm
    # blocks packages' install scripts until they are approved, and esbuild -
    # which remotion bundles with - fetches its platform binary in exactly such
    # a script. Seen 2026-08-15: "added 245 packages", then a warn line naming
    # esbuild 0.28.1 and its blocked postinstall.
    # (Written without the name@version form on purpose: the release audit reads
    # that shape as an email address and refuses the build.)
    # Without that binary the packages are all present and rendering still dies.
    $esbuild = Get-ChildItem 'tools\remotion\node_modules\@esbuild' -Recurse -Filter 'esbuild.exe' `
                             -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($esbuild) {
      Write-Status 'OK' 'remotion installed'
    } else {
      Write-Status '!' 'remotion installed, but esbuild has no binary - npm blocked its install script'
      Write-Status 'i' '   video RENDERING will fail until it runs; npm printed the approve command above'
      Write-Status 'i' '   (nothing else is affected - this is remotion only)'
    }
  }
  else { Write-Status 'X' 'remotion npm install failed' }
}

# --- discord ------------------------------------------------------------------
Write-Host ''
Write-Host '--- discord ----------------------------------------------'
# One parser for the whole system: ask api.py rather than re-reading .env here.
# It validates shape too, so a wrong-but-present guild id is reported now instead
# of killing the watchdog silently later.
$checkOut = & python (Join-Path $PSScriptRoot 'tools\discord\api.py') config-check 2>&1
$discordOk = ($LASTEXITCODE -eq 0)

if ($discordOk) {
  Write-Status 'OK' 'Discord configured'
} elseif (-not (Test-Path -LiteralPath .env)) {
  # Fresh machine: .env is deliberately excluded from the portable zip, so this
  # is expected, not a fault. Say so - otherwise it reads as "the zip lost my setup".
  Write-Status 'i' 'Discord not set up yet on this machine'
  Write-Status 'i' '     .env never travels in the zip (secrets stay per-machine) - set it up once here'
} else {
  # A .env we created from the example two sections ago has all three values
  # blank. That is the EXPECTED state, not a fault list - and printing one
  # "[X] ... is empty" per key, then a verdict, then letting the setup offer
  # below say it a fourth time, is how a normal first install ends up looking
  # like something went wrong (seen twice in real logs, 2026-08-15).
  $missing = @($checkOut | Where-Object { "$_" -match 'is empty' })
  if ($missing.Count -ge 3) {
    Write-Status 'i' 'Discord not set up yet - its three values in .env are still blank'
  } else {
    # A PARTIAL config is worth spelling out: something is filled in and wrong,
    # or one key was missed. Strip the inner marker so it is not doubled.
    foreach ($l in $checkOut) {
      if ("$l".Trim()) { Write-Status '!' ("   " + ("$l" -replace '^\s*\[[^\]]*\]\s*', '')) }
    }
    Write-Status '!' 'Discord not usable - fix the values above in .env'
  }
}

if (-not $discordOk -and -not $CheckOnly) {
  powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'tools\discord\setup.ps1') -IfMissing
  # re-read: setup may have just made Discord usable. config-check, NOT check -
  # there is no `check` verb, and the typo would have left $discordOk false and
  # skipped the channel creation below without a word.
  $checkOut = & python (Join-Path $PSScriptRoot 'tools\discord\api.py') config-check 2>&1
  $discordOk = ($LASTEXITCODE -eq 0)
}

# Stamp the categories and channels HERE, rather than leaving it to whenever the
# watchdog next boots. Install must finish with a server you can actually talk
# in - "installed, now run a command" is not installed. Idempotent find-or-
# create, so it is a no-op on every later run (2026-08-11: channels deleted on
# a running instance stayed gone, and the fix was a CLI verb nobody should need
# to know).
if ($discordOk -and -not $CheckOnly) {
  Write-Status '..' 'creating categories and channels'
  $out = & python (Join-Path $PSScriptRoot 'tools\discord\api.py') ensure 2>&1
  if ($LASTEXITCODE -eq 0) {
    Write-Status 'OK' 'Discord structure in place'
  } else {
    Write-Status '!' 'could not create the channels - the watchdog will retry within a minute'
    foreach ($l in $out) { if ("$l".Trim()) { Write-Status 'i' "     $l" } }
  }
}

# --- autostart ----------------------------------------------------------------
Write-Host ''
Write-Host '--- autostart --------------------------------------------'
# A Startup shortcut can only START things. It cannot notice the watchdog dying,
# which is the failure that matters: the watchdog is the only thing listening, so
# when it goes the whole bus goes silent with nobody to say so. start-omnius.bat
# is no good as the task action either - it launches the panes and exits
# immediately, so "restart on failure" would never fire. Scheduled tasks own the
# service PROCESSES directly.
#
# The task definitions live in tools\discord\autostart.ps1, not here. They were
# duplicated in this file until 2026-08-01, which is how the tasks on the live
# machine ended up drifting from what the installer would write today. The
# installer owns only the QUESTION; autostart.ps1 owns the answer, and can be
# re-run on its own to check or repair (which is what you want after moving the
# workspace to another PC - the tasks hold absolute paths by necessity).
$autostart = Join-Path $PSScriptRoot 'tools\discord\autostart.ps1'
$startupLnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Omnius Services.lnk'

# A RUNNING service keeps the environment it was born with - editing PATH does
# nothing for it. On 2026-08-14 that cost an hour: the CLI was installed and on
# the persisted PATH, and the watchdog that had started minutes earlier could
# not see it, so every desk failed to start while the beacon stayed 3s fresh.
# If we changed PATH in this run, the services must be restarted, not merely
# checked.
if ($script:PathChanged -and -not $CheckOnly) {
  foreach ($t in @('Omnius Watchdog','Omnius Daybook','Omnius Telegram')) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
      try {
        Stop-ScheduledTask -TaskName $t -ErrorAction Stop
        Start-Sleep -Seconds 2
        Start-ScheduledTask -TaskName $t -ErrorAction Stop
        Write-Status 'OK' ("restarted '{0}' so it can see the new PATH" -f $t)
      } catch { Write-Status '!' ("could not restart '{0}': {1}" -f $t, $_.Exception.Message) }
    }
  }
}

& powershell -NoProfile -ExecutionPolicy Bypass -File $autostart -Action status | Out-Host
$autostartOk = ($LASTEXITCODE -eq 0)

if ($autostartOk) {
  Write-Status 'OK' 'autostart healthy (scheduled tasks: Omnius Watchdog, Omnius Daybook)'
  Write-Host  "     check or repair anytime:  powershell -File tools\discord\autostart.ps1 -Action status|repair"
} elseif ($CheckOnly) {
  Write-Status 'i' 'autostart needs attention - see above (run install.bat to fix)'
} else {
  Write-Host  '     A scheduled task starts a service at logon AND restarts it if it dies.'
  Write-Host  '     (A Startup shortcut can only do the first half.)'
  $a = Read-Host '     register / repair autostart for the watchdog + daybook? [y/N]'
  if ($a -match '^[yYjJ]') {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $autostart -Action repair | Out-Host
    if ($LASTEXITCODE -eq 0) {
      Write-Status 'OK' 'autostart enabled (hidden, restarts every 1 min on failure)'
      if (Test-Path $startupLnk) {
        Remove-Item $startupLnk -Force -ErrorAction SilentlyContinue
        Write-Status 'i' 'removed the older Startup shortcut - the tasks supersede it'
      }
      Write-Host '     start-omnius.bat still gives you the visible panes; if a task already'
      Write-Host '     holds the lock, that pane will say so and exit, which is correct.'
    } else {
      Write-Status 'X' 'could not enable autostart - see the messages above'
      Write-Host  '     the system still works - just start it with start-omnius.bat'
    }
  } else {
    Write-Status 'i' 'skipped - enable anytime by re-running install.bat'
  }
}

# --- desktop icon -------------------------------------------------------------
# One thing to remember instead of several. Double-clicking it starts whatever
# is not running and opens the web app - no terminal window, because the
# services are scheduled tasks and a console here would only be something to
# close by accident.
Write-Host ''
Write-Host '--- desktop icon -----------------------------------------'
$desktop  = [Environment]::GetFolderPath('Desktop')
$lnkPath  = Join-Path $desktop 'Omnius.lnk'
$launcher = Join-Path $PSScriptRoot 'tools\omnius_launcher.ps1'
$iconPath = Join-Path $PSScriptRoot 'assets\omnius.ico'
if ($CheckOnly) {
  if (Test-Path $lnkPath) { Write-Status 'OK' 'desktop shortcut present' }
  else { Write-Status '!' 'desktop shortcut missing - install.bat would create it' }
} else {
  try {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($lnkPath)
    $lnk.TargetPath       = (Get-Command powershell).Source
    $lnk.Arguments        = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`""
    $lnk.WorkingDirectory = $PSScriptRoot
    $lnk.Description      = 'Start Omnius and open the notes web app'
    if (Test-Path $iconPath) { $lnk.IconLocation = "$iconPath,0" }
    $lnk.Save()
    Write-Status 'OK' 'desktop shortcut "Omnius" created (starts services + opens the web app)'
  } catch {
    Write-Status '!' "could not create the desktop shortcut: $($_.Exception.Message)"
    Write-Host  '     not fatal - start-omnius.bat does the same job'
  }
}

# --- what is switched on ------------------------------------------------------
# The closing question a new user actually has is "did that work, and what do I
# still owe it?" - so answer it explicitly rather than leaving them to guess
# from twenty OK lines. Reads .env by NAME only; no value is ever printed.
Write-Host ''
Write-Host '--- capabilities -----------------------------------------'
$envMap = @{}
if (Test-Path .env) {
  foreach ($line in Get-Content .env) {
    if ($line -match '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
      $v = $Matches[2].Trim().Trim('"').Trim("'")
      if ($v) { $envMap[$Matches[1].ToUpper()] = $true }
    }
  }
}
function Show-Cap($label, $keys, $need, $hint) {
  $have = @($keys | Where-Object { $envMap.ContainsKey($_) })
  if ($have.Count -gt 0) { Write-Status 'OK' "$label - on" }
  elseif ($need)         { Write-Status '!' "$label - OFF. $hint" }
  else                   { Write-Status 'i' "$label - off (optional). $hint" }
}
Show-Cap 'Discord (talk to Omnius from anywhere)' @('DISCORD_BOT_TOKEN') $true 'paste DISCORD_BOT_TOKEN, DISCORD_GUILD_ID and DISCORD_OWNER_ID into .env'
Show-Cap 'Documents / OCR (Mistral)'  @('MISTRAL_API_KEY') $true  'free key at https://console.mistral.ai -> MISTRAL_API_KEY in .env'
Show-Cap 'Email'                      @($envMap.Keys | Where-Object { $_ -like 'EMAIL_*' }) $false 'add an account to config\email.ini + its password in .env'
Show-Cap 'Image / video / speech'     @('GOOGLE_API_KEY','OPENAI_API_KEY','ELEVENLABS_API_KEY') $false 'see the AI block in .env.example'
Write-Host  '     voice notes, video and meeting transcription need no key at all - they run locally'

# --- who is this instance for -----------------------------------------------
# ONLY STATED FACTS. shared\USER.md warns that a wrong entry there is worse
# than a missing one, because every desk reads it - so we ask for the two
# things a person can simply tell you (name, language) and never for working
# style, which has to be learned from corrections. The file already has a slot
# for language; this fills it on day one instead of after a week of guessing.
$userMd = Join-Path $PSScriptRoot 'memory\shared\USER.md'
$MARK   = '## Who they are (answered at install, not inferred)'
$knowsUser = (Test-Path $userMd) -and ((Get-Content $userMd -Raw) -match [regex]::Escape($MARK))
if ($knowsUser) {
  Write-Status 'OK' 'owner name and language recorded'
} elseif ($CheckOnly -or -not (Test-Path $userMd)) {
  if (-not $CheckOnly) { Write-Status 'skip' 'no memory\shared\USER.md to personalise' }
} else {
  Write-Host ''
  Write-Host '  Two quick things, so Omnius does not have to guess:' -ForegroundColor Cyan
  $who = (Read-Host '  your name (blank to skip)').Trim().Trim('"').Trim("'").Trim()
  # Default from the OS language - most people keep their machine in the
  # language they actually write in, and it saves them typing it.
  $langGuess = (Get-Culture).Parent.NativeName
  if (-not $langGuess) { $langGuess = (Get-Culture).NativeName }
  Write-Host "  Enter = $langGuess" -ForegroundColor Gray
  $lang = (Read-Host '  language you want Omnius to write to you in').Trim().Trim('"').Trim("'").Trim()
  if (-not $lang) { $lang = $langGuess }
  if ($who -or $lang) {
    $block  = "$MARK`r`n`r`n"
    if ($who)  { $block += "- **Name:** $who`r`n" }
    if ($lang) { $block += "- **Language:** $lang - write to them in $lang unless they switch.`r`n" }
    $block += "`r`n*(They typed these during setup. Everything else in this file must still be`r`n"
    $block += "LEARNED - that is why the rest starts empty.)*`r`n`r`n"
    try {
      $body = Get-Content $userMd -Raw
      # After the intro, before the first '## ' section, so it reads as context
      # rather than replacing the structure the file already has.
      $idx = $body.IndexOf("`n## ")
      if ($idx -gt 0) { $body = $body.Substring(0, $idx + 1) + $block + $body.Substring($idx + 1) }
      else { $body = $body.TrimEnd() + "`r`n`r`n" + $block }
      Write-Utf8NoBom $userMd $body
      Write-Status 'OK' ("recorded: {0}{1}" -f $(if($who){"$who, "}else{""}), $lang)
    } catch { Write-Status '!' "could not write USER.md: $_" }
    if ($lang) {
      # Also a SETTING, not only prose: the model mirrors their language on its
      # own, but Omnius generates text of its own too (heartbeats, #alerts,
      # !status). Storing it means that can follow one day; today it is at
      # least visible in !config. The plumbing itself stays English for now.
      try {
        Set-IniValue (Join-Path $PSScriptRoot 'config\omnius.ini') 'omnius' 'language' $lang
      } catch { }
    }
  } else {
    Write-Status 'skip' 'no name or language given - Omnius will learn them'
  }
}

# --- backup destination ---------------------------------------------------
# Every instance backs ITSELF up. The repo carries the system; this machine's
# notes, projects and media exist nowhere else. There is no sane default for
# "somewhere that is not this disk", so it is asked for once and complained
# about until answered - the only unset setting that is treated as a fault.
$iniPath = Join-Path $PSScriptRoot 'config\omnius.ini'
# Present is not the same as USABLE. A value that was written but points
# nowhere - a typo, a quoted path, an unplugged drive - would otherwise be
# sticky forever: install sees the key and never offers to fix it, while the
# heartbeat nags about a folder that is right there in the file.
$backupSet = ''
if (Test-Path $iniPath) {
  $m = [regex]::Match((Get-Content $iniPath -Raw), '(?m)^\s*folder\s*=\s*(.+?)\s*$')
  if ($m.Success) { $backupSet = $m.Groups[1].Value.Trim().Trim('"').Trim("'").Trim() }
}
$hasBackup = $backupSet -and (Test-Path -LiteralPath $backupSet -PathType Container)
if ($backupSet -and -not $hasBackup) {
  Write-Status '!' ("backup folder is set but unusable: {0}" -f $backupSet)
}
if ($hasBackup) {
  Write-Status 'OK' "backup folder configured -> $backupSet"
} elseif ($CheckOnly) {
  Write-Status '!' 'NO BACKUP FOLDER SET - config\omnius.ini [backup] folder'
} else {
  Write-Host ''
  Write-Host '  Where should this machine keep its backups?' -ForegroundColor Cyan
  Write-Host '  Your notes, projects and media live on this disk only - the git repo'
  Write-Host '  carries the system, not your content. Point this somewhere else:'
  Write-Host '  a synced folder (OneDrive, Dropbox, Drive), a NAS, or another drive.'
  # Offer the tenant OneDrive if there is one. NOT %OneDrive% - that is the
  # personal consumer folder, and there is no OneDriveCommercial variable;
  # probing env vars once concluded "no company cloud" with the folder in plain
  # sight. Match by folder name instead.
  $guess = Get-ChildItem $env:USERPROFILE -Directory -Filter 'OneDrive*' -EA SilentlyContinue |
           Select-Object -First 1
  $default = if ($guess) { Join-Path $guess.FullName 'omnius-backups' } else { '' }
  if ($default) { Write-Host "  Enter = $default" -ForegroundColor Gray }
  $answer = Read-Host '  backup folder (blank to skip)'
  if (-not $answer -and $default) { $answer = $default }
  # People paste paths WITH QUOTES - Explorer's "Copy as path" adds them, and it
  # is the natural thing to type for a path with spaces. Read-Host hands them to
  # us literally, so '"D:\x"' became a drive named '"D' (2026-08-11).
  $answer = $answer.Trim().Trim('"').Trim("'").Trim()
  if ($answer) {
    $ok = $false
    try {
      if (-not (Test-Path -LiteralPath $answer)) {
        # -ErrorAction Stop, or this is a NON-terminating error under our
        # 'Continue' preference: the catch never fires, and we fall straight
        # through to printing OK. That is exactly what happened - a failed
        # mkdir reported success and wrote the broken path into the config.
        New-Item -ItemType Directory -Path $answer -Force -ErrorAction Stop | Out-Null
      }
      # Trust the filesystem, not the absence of an exception.
      $ok = Test-Path -LiteralPath $answer -PathType Container
    } catch {
      Write-Status 'X' ("could not use that folder: {0}" -f $_.Exception.Message)
    }
    if ($ok) {
      Set-IniValue $iniPath 'backup' 'folder' $answer
      Write-Status 'OK' "backups -> $answer"
    } else {
      Write-Status '!' 'backup folder NOT set - Omnius will keep reminding you'
    }
  } else {
    Write-Status '!' 'skipped - Omnius will keep reminding you until it is set'
  }
}

# Where the workspace LIVES is not cosmetic. This folder becomes the instance:
# notes, projects, memory, the git repo, the desktop shortcut and every
# absolute hook path point at it, and moving it later means re-running install
# to repair those paths. Two spots are worth a word: Downloads (people empty
# it, and a zip unpacked there keeps its "-main" name) and any synced folder,
# where a sync client fights the bus over small files it writes constantly.
$here = $PSScriptRoot
$hereWarn = @()
if ($here -like "*\Downloads\*" -or $here -like "*\Downloads") {
  $hereWarn += 'it is inside your Downloads folder, which people empty'
}
if ($here -match '-main$|-master$') {
  $hereWarn += 'the "-main" name came from a GitHub zip, not from you'
}
foreach ($sync in @('OneDrive', 'Dropbox', 'Google Drive', 'iCloud')) {
  if ($here -like "*\$sync*") { $hereWarn += "$sync syncs it, and a sync client fights the bus over the small files it writes constantly" }
}
if ($hereWarn.Count -gt 0) {
  Write-Host ''
  Write-Status '!' ("this workspace lives at {0}" -f $here)
  foreach ($w in $hereWarn) { Write-Status 'i' ("   - {0}" -f $w) }
  Write-Status 'i' ("   move the folder somewhere permanent (e.g. {0}\omnius), then re-run install.bat there" -f $env:USERPROFILE)
}

Write-Host ''
Write-Host '============================================================'
Write-Host ' Done.'
Write-Host ''
Write-Host '   Double-click the Omnius icon on your desktop.'
Write-Host '   It starts everything and opens your notes at localhost:5111'
Write-Host ''
$guide = Join-Path $PSScriptRoot 'GETTING-STARTED.md'
if ($CheckOnly) {
  Write-Host '   New here? Read GETTING-STARTED.md'
} else {
  Write-Host '   New here? GETTING-STARTED.md is opening now.'
}
Write-Host '============================================================'

# Open the guide for a human who has just installed this and has no idea what to
# do next. Skipped on -CheckOnly, which is a health probe rather than an install
# and must stay silent enough to run from a script.
if (-not $CheckOnly -and (Test-Path $guide)) { Start-Process $guide }
