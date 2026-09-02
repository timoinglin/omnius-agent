# Omnius one-line installer.
#
#   irm https://raw.githubusercontent.com/timoinglin/omnius-agent/main/get.ps1 | iex
#
# Downloads the latest release zip, unpacks it, and hands over to the guided
# installer inside (install.bat). Power users can call it with parameters:
#
#   & ([scriptblock]::Create((irm <url>))) -Path D:\omnius -NoInstall
#
# DESIGN NOTES, learned the hard way on 2026-08-11: this script is `iex`'d into
# the USER'S session, so it must leave no trace there - no Set-StrictMode, no
# $ErrorActionPreference at script scope, no cd. A third-party installer once
# left StrictMode on in our session and the next unrelated line died on it.
# Everything below therefore lives inside one function with local preferences.
param(
  [string]$Path = (Join-Path $env:USERPROFILE 'omnius'),
  [switch]$NoInstall
)

function Install-Omnius {
  param([string]$Path, [bool]$NoInstall)
  $ErrorActionPreference = 'Stop'          # function-local, not the session's
  $ProgressPreference    = 'SilentlyContinue'

  $repo = 'timoinglin/omnius-agent'
  Write-Host ''
  Write-Host '  OMNIUS - one-line install' -ForegroundColor Cyan
  Write-Host ''

  if ($PSVersionTable.PSVersion.Major -lt 5) {
    Write-Host '  [X] PowerShell 5+ required.' -ForegroundColor Red; return
  }
  # PS 5.1 defaults to TLS 1.0 and GitHub refuses it. Additive, standard.
  [Net.ServicePointManager]::SecurityProtocol = `
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

  # Never clobber. An existing folder is somebody's instance - their memory,
  # notes and projects. Updating an install is `!update` in Discord or
  # `git pull` at the desk (installs attach to the repo since 2026-08-15),
  # never this script over the top of it.
  if ((Test-Path $Path) -and @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue).Count) {
    Write-Host "  [X] $Path already exists and is not empty." -ForegroundColor Red
    Write-Host '      If that is an Omnius install, update it from inside instead.'
    Write-Host '      Or rerun with a different target:'
    Write-Host '      & ([scriptblock]::Create((irm <this url>))) -Path C:\some\other\folder'
    return
  }

  Write-Host "  [..] finding the latest release of $repo"
  # A repo with no published release answers 404 here, and with
  # $ErrorActionPreference='Stop' that reaches the user as a raw web exception
  # on the one command the README tells everybody to run. Say what happened and
  # what to do instead - cloning is a complete install, just without the zip.
  $rel = $null
  try {
    $rel = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" `
                             -Headers @{ 'User-Agent' = 'omnius-get' }
  } catch {
    $code = $null
    try { $code = [int]$_.Exception.Response.StatusCode } catch { }
    if ($code -eq 404) {
      Write-Host '  [X] no release published yet.' -ForegroundColor Red
      Write-Host '      Install from source instead - same thing, minus the zip:'
      Write-Host ("      git clone https://github.com/{0}.git `"{1}`"" -f $repo, $Path)
      Write-Host ("      cd `"{0}`" ; .\install.bat" -f $Path)
    } else {
      Write-Host ("  [X] could not reach GitHub: {0}" -f $_.Exception.Message) -ForegroundColor Red
    }
    return
  }
  $asset = $rel.assets | Where-Object { $_.name -like '*.zip' } | Select-Object -First 1
  if (-not $asset) {
    Write-Host ("  [X] release {0} has no zip asset - re-cut it with release.bat, or clone instead." -f $rel.tag_name) -ForegroundColor Red
    return
  }
  # Every release cut since 2026-09-02 publishes a SHA256SUMS asset next to the
  # zip (release.ps1). No checksum = no install: unpacking an unverified archive
  # onto a stranger's machine is exactly the thing this file must never do, and
  # "probably fine" is not a verdict an installer gets to reach on its own.
  $sumAsset = $rel.assets | Where-Object { $_.name -eq 'SHA256SUMS' } | Select-Object -First 1
  if (-not $sumAsset) {
    Write-Host ("  [X] release {0} publishes no checksum (SHA256SUMS)." -f $rel.tag_name) -ForegroundColor Red
    Write-Host '      This installer will not unpack a download it cannot verify.'
    Write-Host '      Ask the author to re-cut the release, or install from source:'
    Write-Host ("      git clone https://github.com/{0}.git `"{1}`"" -f $repo, $Path)
    return
  }
  Write-Host ("  [OK] {0}  ({1:N1} MB, release {2})" -f $asset.name, ($asset.size / 1MB), $rel.tag_name)

  $tmp = Join-Path ([IO.Path]::GetTempPath()) ("omnius-get-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
  New-Item -ItemType Directory -Path $tmp -Force | Out-Null
  $zip = Join-Path $tmp $asset.name
  Write-Host '  [..] downloading'
  # -TimeoutSec so a stalled connection ends in a sentence instead of hanging
  # the console forever, and try/catch so a dropped line prints one actionable
  # line rather than a web-exception stack trace on the command the README
  # tells everybody to run.
  try {
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip -UseBasicParsing -TimeoutSec 600
  } catch {
    Write-Host ("  [X] the download failed: {0}" -f $_.Exception.Message) -ForegroundColor Red
    Write-Host '      Check your connection and run the same command again.'
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    return
  }
  # A truncated download is the common failure and it is silent: unpacking would
  # report a corrupt-file exception nobody can act on. GitHub tells us how
  # many bytes it should be, so compare before touching the hash.
  $got = (Get-Item -LiteralPath $zip).Length
  if ($asset.size -and $got -ne $asset.size) {
    Write-Host ("  [X] the download is incomplete ({0:N0} of {1:N0} bytes)." -f $got, $asset.size) -ForegroundColor Red
    Write-Host '      Nothing was installed. Run the same command again.'
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    return
  }

  Write-Host '  [..] verifying the checksum'
  $sumFile = Join-Path $tmp 'SHA256SUMS'
  try {
    Invoke-WebRequest $sumAsset.browser_download_url -OutFile $sumFile -UseBasicParsing -TimeoutSec 60
  } catch {
    Write-Host ("  [X] could not download the checksum: {0}" -f $_.Exception.Message) -ForegroundColor Red
    Write-Host '      Nothing was installed. Run the same command again.'
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    return
  }
  # Format: `<hash>  <filename>`, one line per asset. Match on the zip's name so
  # a multi-line file keeps working if a release ever ships more than one.
  $want = $null
  foreach ($line in (Get-Content -LiteralPath $sumFile)) {
    if ($line -match '^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$') {
      if ($matches[2] -eq $asset.name) { $want = $matches[1].ToLower() }
    }
  }
  if (-not $want) {
    Write-Host ("  [X] SHA256SUMS does not list {0} - the release is inconsistent." -f $asset.name) -ForegroundColor Red
    Write-Host '      Do not install it. Tell the author.'
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    return
  }
  $have = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower()
  if ($have -ne $want) {
    Write-Host '  [X] the download did not match the published checksum - re-run;' -ForegroundColor Red
    Write-Host '      if it keeps failing, do not install and tell the author.' -ForegroundColor Red
    Write-Host ("      expected {0}" -f $want) -ForegroundColor Gray
    Write-Host ("      got      {0}" -f $have) -ForegroundColor Gray
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    return
  }
  Write-Host '  [OK] checksum matches'

  Write-Host '  [..] unpacking'
  Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
  # The root folder's NAME is not the contract - install.bat inside it is.
  # v0.1.0 shipped omnius\, v0.1.1 ships omnius-agent\ (pack.ps1 names it after
  # the repo), and a hardcoded 'omnius' here made this script refuse the very
  # release it had just downloaded (2026-08-15 - caught by running the
  # one-liner end to end before telling anyone else to).
  $inner = Get-ChildItem -LiteralPath $tmp -Directory |
           Where-Object { Test-Path (Join-Path $_.FullName 'install.bat') } |
           Select-Object -First 1
  if (-not $inner) {
    Write-Host '  [X] the archive does not look like an Omnius release (no install.bat).' -ForegroundColor Red
    return
  }
  $parent = Split-Path $Path -Parent
  if ($parent -and -not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
  Move-Item -LiteralPath $inner.FullName -Destination $Path
  Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
  Write-Host "  [OK] unpacked to $Path"

  if ($NoInstall) {
    Write-Host "  done. Next: run install.bat inside $Path"
    return
  }
  Write-Host ''
  # In a pipeline like `irm | iex` there may be no interactive stdin. A prompt
  # that cannot be answered must fail SAFE: stop before installing, say what to
  # run - never assume yes on somebody's machine.
  $go = ''
  try { $go = Read-Host '  run the guided setup now? [Y/n]' }
  catch { Write-Host "  (no interactive console) Next: run install.bat inside $Path"; return }
  if ($go -match '^[nN]') {
    Write-Host "  ok. Next: run install.bat inside $Path"
    return
  }
  # Same console, so the installer's own prompts (name, language, backup
  # folder, Discord values) work exactly as if it had been double-clicked.
  Push-Location $Path
  try { & cmd /c .\install.bat } finally { Pop-Location }
}

Install-Omnius -Path $Path -NoInstall:$NoInstall
