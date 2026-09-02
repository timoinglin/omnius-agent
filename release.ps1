# Omnius - cut (or re-cut) the ROLLING release. One command, no ceremony.
#
#   powershell -ExecutionPolicy Bypass -File release.ps1        (or release.bat)
#
# The model (decided 2026-08-15): releases here are not versioned events, they
# are the DISTRIBUTION ARTIFACT the one-line installer downloads. main moves
# daily, so a ceremonial release goes stale the week it is cut - and a stale
# release means every new user's first hour runs on already-fixed bugs. So one
# release named `rolling` always carries the latest green build, and this
# script is the only way it moves. Named vX.Y tags stay possible for genuine
# milestones; they are decoration, not machinery.
#
# What it does, in order - each step refuses loudly rather than shipping doubt:
#   1. preflight : on main, clean tree, in sync with origin (a release must be
#                  a pushed commit or nobody can ever check out what shipped)
#   2. suites    : every test suite must pass, fresh, now - "it passed this
#                  morning" is how a broken installer ships
#   3. build     : pack.ps1 -Fresh -Yes (stages the memory seed, refuses on
#                  identifying data, sanitizes, probes the shipped installer)
#   4. publish   : force-move the `rolling` tag to HEAD, upload the new zip and
#                  its SHA256SUMS FIRST, then delete whatever else is left over
#                  (get.ps1 takes the first *.zip it sees, and a leftover dated
#                  zip from yesterday would win the glob - but deleting before
#                  uploading left a window with no zip at all)
#   5. verify    : ask the same API endpoint get.ps1 asks and prove the answer
#                  is this build, checksum included - publishing is not the same
#                  as being served
#
#   -SkipTests : emergencies only; the release notes will say tests were skipped.
param([switch]$SkipTests, [switch]$Help)

# Unknown arguments are refused - same lesson as pack.ps1 (2026-08-10): param()
# without CmdletBinding drops typos into $args and does the default thing, and
# this tool's default overwrites what strangers download.
if ($Help -or $args.Count) {
  if ($args.Count) {
    Write-Host ("[X] unknown argument: {0}" -f ($args -join ' ')) -ForegroundColor Red
    Write-Host ''
  }
  Write-Host 'release.ps1 - re-cut the rolling release from the current pushed commit'
  Write-Host ''
  Write-Host '  (no flags)  preflight, run every suite, build with pack.ps1 -Fresh,'
  Write-Host '              move the `rolling` tag and swap the zip on GitHub'
  Write-Host '  -SkipTests  skip the suites (emergencies; noted in the release body)'
  Write-Host '  -Help       this text'
  if ($args.Count) { exit 2 }
  exit 0
}

Set-Location $PSScriptRoot
# No global $ErrorActionPreference='Stop' on purpose: git and gh talk on stderr
# even when they succeed, and PS 5.1 turns redirected native stderr into
# ErrorRecords - Stop would kill the script on a successful `git fetch`. Every
# native call below is judged by $LASTEXITCODE instead, which is the only
# verdict those tools actually give.

function Fail([string]$msg, [string]$hint = '') {
  Write-Host "[X] $msg" -ForegroundColor Red
  if ($hint) { Write-Host "    $hint" -ForegroundColor Gray }
  exit 1
}

# --- 1. preflight --------------------------------------------------------------
& git rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) { Fail 'not a git repository' }
# A release is published by the instance that OWNS the repo. Every other install
# runs this same script, and on one of those it fails deep in - after building a
# zip - with a push rejection nobody asked for. Ask first, in one line.
$roleJson = & python (Join-Path $PSScriptRoot 'tools\repo_access.py') --json 2>$null
if ($LASTEXITCODE -eq 0 -and $roleJson) {
  try {
    if (-not (ConvertFrom-Json ([string]$roleJson)).canPush) {
      Fail 'this instance cannot publish releases - it does not own the remote' `
           'that is normal: your own commits are kept and replayed by !update. Nothing to fix.'
    }
  } catch { }
}
$branch = (& git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne 'main') { Fail "on branch '$branch', not main" 'the rolling release ships main only' }
if (@(& git status --porcelain).Count) {
  Fail 'working tree is not clean' 'commit or stash first - the zip is built from this tree, and an unpushed tree ships code nobody can check out'
}
& git fetch origin main *> $null
$local  = (& git rev-parse HEAD).Trim()
$remote = (& git rev-parse origin/main).Trim()
if ($local -ne $remote) {
  Fail 'main is not in sync with origin/main' 'push (or pull) first - what ships must be what is public'
}
$short = (& git rev-parse --short HEAD).Trim()
& gh auth status *> $null
if ($LASTEXITCODE -ne 0) { Fail 'gh CLI is not authenticated' 'run: gh auth login' }
Write-Host "[OK] main @ $short, clean and pushed" -ForegroundColor Green

# --- 2. suites -----------------------------------------------------------------
# All of them, every time. The watchdog suite alone is 1,200+ checks and runs in
# seconds; the others are smaller. TESTING.md calls these the release gate, and
# a gate you can wave through is not a gate.
$suites = @('tools\discord\test_watchdog.py', 'daybook\test_storage.py',
            'tools\email\test_email.py', 'tools\documents\test_documents.py',
            'tools\telegram\test_telegram.py')
if ($SkipTests) {
  Write-Host '[! ] -SkipTests: suites NOT run - the release body will say so' -ForegroundColor Yellow
} else {
  foreach ($s in $suites) {
    if (-not (Test-Path $s)) { Fail "suite missing: $s" }
    Write-Host "[..] $s" -ForegroundColor Cyan
    $out = & python $s 2>&1
    if ($LASTEXITCODE -ne 0) {
      # Print the tail, not the 1,200 passes - the failure is what matters.
      $out | Select-Object -Last 15 | ForEach-Object { Write-Host "    $_" }
      Fail "suite failed: $s" 'a red suite never ships - fix it or (emergencies) -SkipTests'
    }
  }
  Write-Host '[OK] every suite green' -ForegroundColor Green
}

# --- 3. build ------------------------------------------------------------------
# pack.ps1 -Fresh owns the hard parts: the scrubbed memory seed, the sentinel
# audit, release_sanitize.py (refuses + deletes on any leak), and the shipped-
# installer probe. If it exits 0 the zip is fit to hand to a stranger.
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'pack.ps1') -Fresh -Yes
if ($LASTEXITCODE -ne 0) { Fail 'pack.ps1 -Fresh refused - nothing published' }
$leaf  = Split-Path $PSScriptRoot -Leaf
$stamp = Get-Date -Format 'yyyy-MM-dd'
$zip   = Join-Path (Split-Path $PSScriptRoot -Parent) ("{0}-release-{1}.zip" -f $leaf, $stamp)
if (-not (Test-Path $zip)) { Fail "expected $zip - pack.ps1 wrote somewhere else?" }

# The checksum ships WITH the zip, as its own asset. get.ps1 refuses to install
# anything whose hash does not match this file, so a truncated download or a
# swapped asset stops at the stranger's machine instead of unpacking. Format is
# the classic `sha256sum` one - `<hash>  <filename>`, two spaces - so anybody
# can verify it by hand with the tool they already have.
$zipName = Split-Path $zip -Leaf
$sha     = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower()
$sums    = Join-Path (Split-Path $zip -Parent) 'SHA256SUMS'
Set-Content -LiteralPath $sums -Value ("{0}  {1}" -f $sha, $zipName) -Encoding ASCII
Write-Host ("[OK] SHA256 {0}" -f $sha) -ForegroundColor Green

# --- 4. publish ----------------------------------------------------------------
# The tag moves first, force-pushed: `rolling` is a POINTER, not history - the
# git log is the history. The release object then follows the tag.
& git tag -f rolling $local *> $null
& git push --force origin rolling *> $null
if ($LASTEXITCODE -ne 0) { Fail 'could not push the rolling tag' }

$notes = @"
Rolling build of ``$short`` - $stamp. Always the latest green ``main``; history is the [commit log](https://github.com/timoinglin/omnius-agent/commits/main).

**Install**
``````powershell
irm https://raw.githubusercontent.com/timoinglin/omnius-agent/main/get.ps1 | iex
``````

**Upgrading an existing install:** say ``!update`` in Discord (preview, then ``!update go``) - or ``git pull`` + ``!reload`` at the desk. New config keys arrive commented in ``config\*.example.ini`` - copy what you need.
"@
if ($SkipTests) { $notes = "**Note: test suites were skipped for this build (-SkipTests).**`n`n" + $notes }

& gh release view rolling *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host '[..] creating the rolling release' -ForegroundColor Cyan
  & gh release create rolling $zip $sums --title 'Omnius - rolling' --notes $notes
  if ($LASTEXITCODE -ne 0) { Fail 'gh release create failed' }
} else {
  # UPLOAD FIRST, DELETE SECOND. The old order deleted every asset and then
  # uploaded, which left a window - seconds on a good line, minutes on a bad
  # one - where the release everybody's one-liner downloads had no zip at all.
  # --clobber replaces the same-named assets (SHA256SUMS every time, the zip
  # when a second release is cut the same day), so the release always serves a
  # zip and a matching checksum.
  Write-Host '[..] uploading the new zip + SHA256SUMS' -ForegroundColor Cyan
  & gh release upload rolling $zip $sums --clobber
  if ($LASTEXITCODE -ne 0) { Fail 'gh release upload failed' 'the previous zip is still published - nothing was removed. Re-run this script.' }
  # Only NOW remove what is left over: assets whose names differ from the two
  # just uploaded. get.ps1 takes the first *.zip it sees, so yesterday's dated
  # zip must not survive next to today's.
  $keep = @($zipName, 'SHA256SUMS')
  $old = & gh release view rolling --json assets --jq '.assets[].name'
  foreach ($a in @($old)) {
    if ($a -and ($keep -notcontains $a)) { & gh release delete-asset rolling $a --yes *> $null }
  }
  & gh release edit rolling --title 'Omnius - rolling' --notes $notes *> $null
}

# --- 5. verify -----------------------------------------------------------------
# Ask what get.ps1 asks. Publishing and being served are different facts.
$rel = $null
try {
  $rel = Invoke-RestMethod 'https://api.github.com/repos/timoinglin/omnius-agent/releases/latest' `
                           -Headers @{ 'User-Agent' = 'omnius-release' } -ErrorAction Stop
} catch {
  Fail ("could not verify releases/latest: {0}" -f $_.Exception.Message) 'the upload may still have worked - check the release page by hand'
}
$asset = $rel.assets | Where-Object { $_.name -like '*.zip' } | Select-Object -First 1
if ($rel.tag_name -ne 'rolling') {
  Fail ("releases/latest serves '{0}', not rolling" -f $rel.tag_name) 'is an old release still marked latest? mark it prerelease: gh release edit <tag> --prerelease'
}
if (-not $asset -or $asset.name -ne $zipName) {
  Fail 'releases/latest does not serve the zip just built'
}
# A zip with no published checksum is a zip get.ps1 refuses to install (it fails
# closed), so an upload that dropped SHA256SUMS is a broken release, not a
# cosmetic gap.
if (-not ($rel.assets | Where-Object { $_.name -eq 'SHA256SUMS' })) {
  Fail 'releases/latest serves no SHA256SUMS asset' 'get.ps1 refuses to install without it - re-run this script'
}
$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host ''
Write-Host ("[OK] rolling release = {0} @ {1}  ({2}, {3} MB)" -f $branch, $short, $asset.name, $mb) -ForegroundColor Green
Write-Host ("     served at: {0}" -f $rel.html_url)
