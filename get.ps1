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
  # notes and projects. Updating an install is `git pull` or a new release
  # unpacked NEXT to it, never this script over the top of it.
  if ((Test-Path $Path) -and @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue).Count) {
    Write-Host "  [X] $Path already exists and is not empty." -ForegroundColor Red
    Write-Host '      If that is an Omnius install, update it from inside instead.'
    Write-Host '      Or rerun with a different target:'
    Write-Host '      & ([scriptblock]::Create((irm <this url>))) -Path C:\some\other\folder'
    return
  }

  Write-Host "  [..] finding the latest release of $repo"
  $rel = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" `
                           -Headers @{ 'User-Agent' = 'omnius-get' }
  $asset = $rel.assets | Where-Object { $_.name -like '*.zip' } | Select-Object -First 1
  if (-not $asset) { Write-Host '  [X] the latest release has no zip asset.' -ForegroundColor Red; return }
  Write-Host ("  [OK] {0}  ({1:N1} MB, release {2})" -f $asset.name, ($asset.size / 1MB), $rel.tag_name)

  $tmp = Join-Path ([IO.Path]::GetTempPath()) ("omnius-get-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
  New-Item -ItemType Directory -Path $tmp -Force | Out-Null
  $zip = Join-Path $tmp $asset.name
  Write-Host '  [..] downloading'
  Invoke-WebRequest $asset.browser_download_url -OutFile $zip -UseBasicParsing

  Write-Host '  [..] unpacking'
  Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
  $inner = Join-Path $tmp 'omnius'
  if (-not (Test-Path (Join-Path $inner 'install.bat'))) {
    Write-Host '  [X] the archive does not look like an Omnius release (no install.bat).' -ForegroundColor Red
    return
  }
  $parent = Split-Path $Path -Parent
  if ($parent -and -not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
  Move-Item -LiteralPath $inner -Destination $Path
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
