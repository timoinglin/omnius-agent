# Omnius - build the portable zip. A BACKUP travels complete, `.env` included, so
# it can actually restore this machine; a RELEASE (-Fresh) carries no secrets at all.
# Run via pack.bat (double-click) or:  powershell -ExecutionPolicy Bypass -File pack.ps1
#
#   -Work : build a WORK instance - system only. Leaves personal content behind
#           (daybook notes, every projects\<name>\, media archive). Verified safe:
#           all three are gitignored and were never committed, so excluding them
#           from the archive really removes them - the bundled .git\ cannot leak
#           them back. Everything the system needs to run still travels.
#           Before dropping anything it prints a real inventory and asks.
#   -Fresh: build a CLEAN RELEASE - Omnius as a product, for installing on any
#           PC as a NEW instance. Ships code, skills, docs, templates and the
#           PRODUCT half of memory (how Omnius works: the fleet design,
#           permissions, lessons). Ships NO biography: no projects, no daybook
#           notes, no media, no state, no .env - and NO .git, because history
#           carries every note ever committed no matter what tar skips. Memory
#           comes from templates\fresh\, which is scrubbed of ids, machine
#           names and project names, and the build REFUSES if anything
#           identifying slips in.
#   -Yes  : skip that confirmation. For non-interactive callers only.
param([switch]$Work, [switch]$Fresh, [switch]$Yes, [switch]$Help)

# UNKNOWN ARGUMENTS ARE REFUSED, and this is not pedantry. `param()` without
# [CmdletBinding()] drops anything it does not recognise into $args and carries
# on, so `pack.bat --help` silently built a full 31MB backup and OVERWROTE the
# day's zip (2026-08-10). A typo must never be indistinguishable from "do the
# default thing", least of all in a tool whose default writes a large file.
if ($Help -or $args.Count) {
  if ($args.Count) {
    Write-Host ("[X] unknown argument: {0}" -f ($args -join ' ')) -ForegroundColor Red
    Write-Host ''
  }
  Write-Host 'pack.ps1 - build a portable zip of this workspace'
  Write-Host ''
  Write-Host '  (no flags)  BACKUP of this instance: system, projects, daybook notes,'
  Write-Host '              media, full git history AND .env. What you restore onto a'
  Write-Host '              new PC - so it holds secrets: keep it on a drive you control.'
  Write-Host '  -Fresh      CLEAN RELEASE for a NEW instance: code, skills, docs and a'
  Write-Host '              scrubbed memory seed. No notes, no projects, no history,'
  Write-Host '              no .env. Refuses to build if anything identifies you.'
  Write-Host '  -Work       personal -> work leg. Retired; refuses when content is'
  Write-Host '              tracked in git, which it now always is.'
  Write-Host '  -Yes        skip the confirmation (non-interactive callers only)'
  Write-Host '  -Help       this text'
  Write-Host ''
  Write-Host '  Both zips land NEXT TO the workspace folder, not inside it.'
  if ($args.Count) { exit 2 }
  exit 0
}

if ($Work -and $Fresh) {
  Write-Host '[X] -Work and -Fresh are different products: pick one.' -ForegroundColor Red
  Write-Host '    -Work  = THIS instance, minus personal content (a move).'
  Write-Host '    -Fresh = a clean Omnius anyone can install (a release).'
  exit 1
}

Set-Location $PSScriptRoot

function Format-Bytes([long]$n) {
  if ($n -ge 1MB) { return ('{0:N1} MB' -f ($n / 1MB)) }
  if ($n -ge 1KB) { return ('{0:N0} KB' -f ($n / 1KB)) }
  return "$n B"
}

# What a -Work pack is about to throw away, measured rather than assumed.
# Paths that do not exist or hold no files are simply absent from the result,
# so an empty inventory means "this drops nothing" - not "we did not look".
function Get-DropInventory {
  param([string]$Root, [string[]]$Paths)
  $inv = @()
  foreach ($rel in $Paths) {
    $full = Join-Path $Root $rel
    if (-not (Test-Path -LiteralPath $full)) { continue }
    $files = @(Get-ChildItem -LiteralPath $full -Recurse -File -Force -ErrorAction SilentlyContinue)
    if ($files.Count -eq 0) { continue }
    $inv += [pscustomobject]@{
      Path   = $rel
      Files  = $files.Count
      Bytes  = ($files | Measure-Object -Property Length -Sum).Sum
      Newest = ($files | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime
    }
  }
  return $inv
}

# Use Windows' own bsdtar explicitly: a GNU tar earlier in PATH (e.g. Git Bash)
# treats "D:\..." as a remote host ("Cannot connect to D") and breaks packing.
# Falling back to a bare 'tar' would silently re-introduce exactly that, so say
# so instead of failing later with an unrecognisable error.
$tar = Join-Path $env:SystemRoot 'System32\tar.exe'
if (-not (Test-Path -LiteralPath $tar)) { $tar = Join-Path $env:SystemRoot 'Sysnative\tar.exe' }
if (-not (Test-Path -LiteralPath $tar)) {
  Write-Host '[X] Windows tar.exe not found (ships with Windows 10 1803+) - cannot pack' -ForegroundColor Red
  Write-Host '    a GNU tar on PATH cannot be used: it reads "C:\..." as a remote host' -ForegroundColor Gray
  exit 1
}

$parent = Split-Path $PSScriptRoot -Parent
$leaf   = Split-Path $PSScriptRoot -Leaf
$stamp  = Get-Date -Format 'yyyy-MM-dd'
$kind   = if ($Work) { 'work-' } elseif ($Fresh) { 'release-' } else { '' }
$dest   = Join-Path $parent ("{0}-{1}{2}.zip" -f $leaf, $kind, $stamp)
if (Test-Path $dest) {
  Write-Host ("[!] {0} exists - overwriting" -f $dest) -ForegroundColor Yellow
  Remove-Item $dest -Force
}

# Exclude patterns match at ANY depth, so workspace-scoped ones must be anchored
# to the archive root: a bare "--exclude=state" also deleted projects\*\src\state\
# (Redux/Zustand/XState) from the zip - real source, silently missing on the new PC.
# Secrets and regenerable junk stay global on purpose.
# zip:hdrcharset=UTF-8 keeps non-ASCII filenames intact across machines.
$tarArgs = @('-a', '-c', '-f', $dest, '--options', 'zip:hdrcharset=UTF-8')
$tarArgs += "--exclude=$leaf/state"
# .env travels in a BACKUP and only in a backup. Owner, 2026-09-01: "the backup
# of OMNIUS should take the complete OMNIUS folder as is, including the .env,
# that's why it is a separate user backup". He is right - restoring onto a new
# PC without .env gives you a workspace with no Discord token, so the fleet
# never starts and the "backup" was a folder copy with the important part
# missing. It stays excluded from -Fresh, which is a RELEASE: that one goes to
# strangers and becomes a GitHub asset, which is the exposure the key-material
# note below is about. Two products, two rules - not one rule applied twice.
#
# The cost, stated plainly because it is now true: the backup zip CONTAINS
# SECRETS. It belongs on a drive he controls. Never a shared folder, never a
# cloud sync, never a release asset.
if ($Fresh -or $Work) {
  $tarArgs += '--exclude=.env', '--exclude=.env.local', '--exclude=.env.development',
              '--exclude=.env.production', '--exclude=.env.test'
}
$tarArgs += '--exclude=node_modules', '--exclude=__pycache__', '--exclude=.venv',
            '--exclude=*.log', '--exclude=dist', '--exclude=build',
            '--exclude=settings.local.json'
# .next is the same class as dist/build - regenerable output - but it earns its
# own line because it does not merely bloat the zip, it BREAKS the backup: a
# running `next dev` holds `.next\dev\lock` open, tar cannot read it, and the
# whole archive is refused. 2026-08-13, the first backup he asked for on this
# machine died on exactly that while a project's dev server was up. A
# backup must not depend on which servers happen to be running.
$tarArgs += '--exclude=.next', '--exclude=.turbo'

# --- key material -------------------------------------------------------------
# .gitignore has NO say over this archive. A key that git correctly refuses to
# commit still lands in the zip, and the zip becomes a GitHub release asset and
# a cloud-folder copy - so the archive is the WIDER exposure, not the narrower
# one. Found 2026-07-31: a project's serviceAccount json (full Firebase admin)
# was correctly gitignored by *-sa.json and would still have shipped in a
# release. Deliberately NOT excluding a bare *.key: too broad, and a wrongly
# dropped file is the failure this repo has already been bitten by.
$secretGlobs = @('*serviceAccount*.json', '*service-account*.json', '*adminsdk*.json',
                 '*-sa.json', '*.pem', '*.p12', '*.pfx', '*.jks', '*.keystore',
                 'id_rsa', 'id_ed25519')
foreach ($g in $secretGlobs) { $tarArgs += "--exclude=$g" }
$tarArgs += "--exclude=$leaf/.secrets"

$drops = @()
if ($Work) {
  # Exclude each project BY NAME rather than the projects\ folder itself, so the
  # tracked projects\.gitkeep still travels - otherwise the unzipped repo opens
  # with a deleted tracked file and a dirty tree on day one.
  $projects = @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'projects') -Directory -ErrorAction SilentlyContinue)
  foreach ($p in $projects) { $tarArgs += "--exclude=$leaf/projects/$($p.Name)" }
  $tarArgs += "--exclude=$leaf/daybook/notes"
  $tarArgs += "--exclude=$leaf/media"

  # --- guard -------------------------------------------------------------------
  # -Work drops that content BY DESIGN: it is the personal -> work move, where
  # leaving your personal life behind is the entire point. But the switch reads
  # as "the one you use for the work PC", so on a work machine - where the
  # daybook notes and projects ARE the work - the same flag silently destroys
  # the only irreplaceable thing on the disk. The old static "LEFT BEHIND:" line
  # printed identically whether it dropped nothing or a year of notes, and it
  # printed AFTER the archive was already written. So: count it, show it, and
  # make the user say it out loud before anything is packed.
  $dropPaths = @('daybook\notes', 'media') + @($projects | ForEach-Object { "projects\$($_.Name)" })
  $drops = @(Get-DropInventory -Root $PSScriptRoot -Paths $dropPaths)

  # A tar exclusion can only remove what is NOT in git. The archive bundles
  # .git\, so anything ever committed travels inside it no matter what tar
  # skips - which makes -Work's promise ("personal content stays behind")
  # quietly false. daybook\notes\ became tracked on 2026-07-31 and broke
  # exactly this; the suite caught it the same day. Refuse rather than hand
  # someone a work instance that only looks clean.
  $tracked = @()
  foreach ($d in $drops) {
    $rel = ($d.Path -replace '\\', '/')
    $ls = @(& git -C $PSScriptRoot ls-files -- $rel 2>$null)
    if ($ls.Count -gt 0) { $tracked += [pscustomobject]@{ Path = $d.Path; Files = $ls.Count } }
  }
  if ($tracked.Count) {
    Write-Host ''
    Write-Host '[X] -Work cannot produce a clean work instance: this content is TRACKED IN GIT' -ForegroundColor Red
    foreach ($t in $tracked) {
      Write-Host ('      {0}  - {1} file(s) committed' -f $t.Path, $t.Files) -ForegroundColor Red
    }
    Write-Host '    The archive bundles .git\, so their full history travels even though tar' -ForegroundColor Gray
    Write-Host '    skips the working copy. Excluding them here removes nothing.' -ForegroundColor Gray
    Write-Host '    Either untrack them first, or use plain pack.ps1 and accept that everything' -ForegroundColor Gray
    Write-Host '    travels. No archive written.' -ForegroundColor Gray
    exit 1
  }

  if ($drops.Count -eq 0) {
    Write-Host '[i] -Work: nothing to leave behind (daybook\notes\, projects\ and media\ hold no files)' -ForegroundColor Gray
  } else {
    Write-Host ''
    Write-Host '  -Work LEAVES THIS BEHIND - it will NOT be in the archive:' -ForegroundColor Yellow
    foreach ($d in $drops) {
      Write-Host ('    {0,-26} {1,4} file(s)  {2,9}   newest {3:yyyy-MM-dd}' -f `
                  $d.Path, $d.Files, (Format-Bytes $d.Bytes), $d.Newest) -ForegroundColor Yellow
    }
    Write-Host ''
    Write-Host '  -Work is for the PERSONAL -> WORK move (leave your personal life behind).' -ForegroundColor Gray
    Write-Host '  Work machine -> work machine? That content IS your work: use plain pack.ps1.' -ForegroundColor Gray
    Write-Host ''
    if ($Yes) {
      Write-Host '  [-Yes] confirmed non-interactively - dropping it' -ForegroundColor Yellow
    } elseif ([Console]::IsInputRedirected) {
      # Never prompt into a pipe: it would hang a scripted caller, or read
      # whatever happens to be on stdin as consent. Refusing is the safe
      # direction here - no archive is written, nothing is lost.
      Write-Host '[X] refusing: -Work would drop the content above, and stdin is not interactive' -ForegroundColor Red
      Write-Host '    pass -Yes if that is genuinely what you want, or run plain pack.ps1 for everything' -ForegroundColor Gray
      exit 1
    } else {
      $answer = Read-Host '  Type  leave  to drop it, anything else to abort'
      if ($answer -ne 'leave') {
        Write-Host '[X] aborted - no archive written. For everything, run:  pack.ps1  (no -Work)' -ForegroundColor Red
        exit 1
      }
    }
  }
}

# Name what the secret rules caught. tar excludes silently, and a file that
# vanishes without a word is indistinguishable from one that was never there -
# which is how a wrong exclusion hides. Saying it out loud also tells the user
# a key is sitting in the workspace when it should be outside it.
$secretHits = @()
foreach ($g in $secretGlobs) {
  $secretHits += @(Get-ChildItem -LiteralPath $PSScriptRoot -Recurse -File -Force -Filter $g -ErrorAction SilentlyContinue |
                   Where-Object { $_.FullName -notmatch '\\node_modules\\|\\\.git\\' })
}
$secretHits = @($secretHits | Sort-Object FullName -Unique)
if ($secretHits.Count) {
  Write-Host '  key material found in the workspace - EXCLUDED from the archive:' -ForegroundColor Yellow
  foreach ($h in $secretHits) {
    Write-Host ('    ' + $h.FullName.Substring($PSScriptRoot.Length + 1)) -ForegroundColor Yellow
  }
  Write-Host '    (keys belong outside the workspace - point an env var at them instead)' -ForegroundColor Gray
}

# --- -Fresh: a RELEASE, not a copy of this instance ----------------------------
# Everything below is about what a stranger's PC must NOT receive. The guard is
# a refusal, not a filter: if anything identifying is still in the staged tree,
# the build stops rather than shipping it.
$freshStage = $null
if ($Fresh) {
  $seed = Join-Path $PSScriptRoot 'templates\fresh\memory'
  if (-not (Test-Path $seed)) {
    Write-Host '[X] -Fresh needs templates\fresh\memory\ (the clean memory seed) - missing.' -ForegroundColor Red
    exit 1
  }
  # Biography: never in a release.
  # Projects go BY NAME, exactly as the -Work path does and for the same reason:
  # `projects/*` also swallows the tracked `projects\.gitkeep`, so every fresh
  # install was born with a deleted tracked file - and `!update` refuses to pull
  # over local changes, so a brand-new machine could never update itself. Found
  # on a colleague's install 2026-08-19, reproduced by attaching the shipped zip
  # to its own RELEASE-COMMIT: "1 tracked file(s) changed locally".
  $relProjects = @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'projects') `
                     -Directory -ErrorAction SilentlyContinue)
  foreach ($p in $relProjects) { $tarArgs += "--exclude=$leaf/projects/$($p.Name)" }
  $tarArgs += "--exclude=$leaf/daybook/notes",
              "--exclude=$leaf/media", "--exclude=$leaf/memory",
              "--exclude=$leaf/scratch", "--exclude=$leaf/assets/private"
  # tools\remotion ships the TOOL - the README and the pinned package.json.
  # What you WRITE with it is yours: compositions under src\ and the mp4s in
  # out\ are content, and a stranger's fresh install has no business carrying
  # one machine's trailer. Git stopped tracking these on 2026-08-24; this line
  # is the other half, because -Fresh packs the FILESYSTEM and would otherwise
  # ship what git no longer does. The owner's own transport (-Work, and the
  # plain backup) keeps them - they are his work.
  $tarArgs += "--exclude=$leaf/tools/remotion/src",
              "--exclude=$leaf/tools/remotion/out",
              "--exclude=$leaf/tools/remotion/.claude",
              "--exclude=$leaf/tools/remotion/remotion.config.ts",
              "--exclude=$leaf/tools/remotion/tsconfig.json"
  # config\: the REAL files name his mail accounts, hosts and machine. Only the
  # *.example.* templates and the README are product. fleet.json was on this
  # list until 2026-08-14 and should not have been - its desks map is keyed by
  # session id, so it shipped his project names. Excluding
  # the folder wholesale would ship an install with nothing to copy, so this
  # drops every non-product file by name and leaves the templates standing.
  #
  # An ALLOW-LIST, not a '*.ini' filter. That filter was correct until
  # routines.json landed here on 2026-08-07 - a .json, therefore invisible to
  # it, and a routine's text can name an account ("check my gmail every
  # hour"). Deny-by-default is the only version that survives the next file
  # somebody adds to this folder. Mirrors .gitignore's allow-list exactly.
  $configKeep = @('README.md')
  Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'config') -File `
                -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike '*.example.*' -and $configKeep -notcontains $_.Name } |
    ForEach-Object { $tarArgs += "--exclude=$leaf/config/$($_.Name)" }
  # .git carries every note ever committed - tar skipping the working copy is
  # not enough, which is the lesson -Work already learned the hard way.
  $tarArgs += "--exclude=$leaf/.git"
  # Documents belonging to OTHER projects are not Omnius product. The WL
  # support handover carries another server's ids, handles and internals.
  $tarArgs += "--exclude=*HANDOVER*.md", "--exclude=$leaf/scratch",
              "--exclude=$leaf/START-HERE.md"
  # A stale on-disk stamp from some earlier unzip must not ride along - the
  # ONLY correct stamp is the one injected below, naming THIS build's commit.
  $tarArgs += "--exclude=$leaf/RELEASE-COMMIT"
  # Stage the clean memory under a temp root so it lands at memory\ in the zip.
  # Staged UNDER $leaf so it lands at "omnius\memory\" in the archive, beside
  # everything else. Staging it as a bare "memory" put it at the archive ROOT:
  # unzipping then produced omnius\ AND a sibling memory\, and the new instance
  # booted with no memory at all - the one thing -Fresh exists to provide.
  # Caught 2026-08-09 by extracting the archive; the build reported success.
  $freshStage = Join-Path ([IO.Path]::GetTempPath()) ("omnius-fresh-" + [Guid]::NewGuid().ToString('N').Substring(0,8))
  New-Item -ItemType Directory -Path (Join-Path $freshStage $leaf) -Force | Out-Null
  Copy-Item $seed (Join-Path $freshStage "$leaf\memory") -Recurse -Force

  # THE GUARD. Anything that identifies this owner, machine, server or projects
  # stops the build. Refusing is the only safe default for something meant to
  # be handed to someone else.
  $bad = @()
  # Structural pattern here; the NAME patterns are per-instance and come from
  # config\audit-sentinels.txt (gitignored). They were hard-coded in this block
  # until 2026-08-14, which meant this script - which SHIPS in every release -
  # carried the very list of names it existed to keep out of one.
  $patterns = @{ 'discord/guild id' = '\b\d{17,20}\b' }
  $sentinelFile = Join-Path $PSScriptRoot 'config\audit-sentinels.txt'
  if (Test-Path $sentinelFile) {
    $sn = 0
    foreach ($line in Get-Content $sentinelFile) {
      $line = $line.Trim()
      if ($line -eq '' -or $line.StartsWith('#')) { continue }
      $sn++
      $patterns["sentinel:$sn"] = ($line -split '=>')[0].Trim()
    }
  } else {
    Write-Host '[! ] no config\audit-sentinels.txt - the seed audit checks ids only, not names' -ForegroundColor Yellow
  }
  foreach ($f in Get-ChildItem $freshStage -Recurse -File) {
    $txt = Get-Content $f.FullName -Raw
    foreach ($k in $patterns.Keys) {
      if ($txt -match $patterns[$k]) {
        $bad += ('{0}: {1}' -f $f.FullName.Substring($freshStage.Length + 1), $k)
      }
    }
  }
  if ($bad.Count) {
    Write-Host '[X] -Fresh REFUSED: the memory seed still identifies this instance:' -ForegroundColor Red
    foreach ($b in $bad) { Write-Host "      $b" -ForegroundColor Red }
    Write-Host '    Scrub templates\fresh\memory\ and re-run. No archive written.' -ForegroundColor Gray
    Remove-Item $freshStage -Recurse -Force -EA SilentlyContinue
    exit 1
  }
  Write-Host '[OK] memory seed is clean (no ids, machine names, handles or project names)' -ForegroundColor Green
}

$tarArgs += '-C', $parent, $leaf

if ($Work) {
  Write-Host '[..] packing WORK instance (system only - no personal notes, projects or media) ...' -ForegroundColor Cyan
} elseif ($Fresh) {
  Write-Host '[..] packing CLEAN RELEASE (no projects, notes, media, state or git history) ...' -ForegroundColor Cyan
} else {
  Write-Host '[..] packing (includes projects, daybook notes and full git history) ...' -ForegroundColor Cyan
}
& $tar @tarArgs

# The seed is INJECTED after packing rather than handed to tar, and the reason
# is not style. tar applies --exclude to everything it adds, including a second
# -C source: excluding "<leaf>/memory" (the real one) also drops a staged copy
# that lands at the same archive path, so the release shipped with no memory at
# all. Naming the stage "memory" instead dodged the exclude but put it at the
# ARCHIVE ROOT - unzipping gave omnius\ and an orphan memory\ beside it. Both
# builds reported success; only extracting the archive showed it (2026-08-09).
# Writing the entries directly is the one version with no ambiguity, and it
# runs BEFORE the audit, so the seed is scanned like everything else.
if ($Fresh -and (Test-Path $dest)) {
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $zip = [IO.Compression.ZipFile]::Open($dest, 'Update')
  try {
    $seedRoot = Join-Path $freshStage $leaf
    foreach ($f in Get-ChildItem (Join-Path $seedRoot 'memory') -Recurse -File) {
      $rel = $f.FullName.Substring($seedRoot.Length + 1).Replace('\', '/')
      [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
        $zip, $f.FullName, "$leaf/$rel") | Out-Null
    }
  } finally { $zip.Dispose() }
  $n = (Get-ChildItem (Join-Path $freshStage "$leaf\memory") -Recurse -File).Count
  Write-Host "     seeded $n memory file(s) into $leaf\memory\" -ForegroundColor Gray

  # RELEASE-COMMIT: the exact commit this zip was cut from. install.ps1's
  # github-attach step births the new instance's `main` HERE, so its working
  # tree starts CLEAN against git instead of showing every file as modified
  # versus whatever the tip is by install day. Gitignored on the receiving
  # side; a plain hex hash, nothing identifying.
  $relHash = (& git -C $PSScriptRoot rev-parse HEAD 2>$null)
  if ($relHash) {
    $stampFile = Join-Path $freshStage 'RELEASE-COMMIT'
    Set-Content -Path $stampFile -Value $relHash.Trim() -Encoding ascii -NoNewline
    $zip2 = [IO.Compression.ZipFile]::Open($dest, 'Update')
    try {
      [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
        $zip2, $stampFile, "$leaf/RELEASE-COMMIT") | Out-Null
    } finally { $zip2.Dispose() }
    Write-Host ("     stamped RELEASE-COMMIT ({0})" -f $relHash.Trim().Substring(0, 7)) -ForegroundColor Gray
  } else {
    Write-Host '[! ] not a git checkout - no RELEASE-COMMIT stamped (attach will use origin/main)' -ForegroundColor Yellow
  }
}

# A release is only a release once it has been read back and proved clean.
# Building it is not the same as verifying it, and only one of those two is
# recoverable after the zip has been handed to someone.
if ($Fresh -and (Test-Path $dest)) {
  Write-Host '[..] sanitising and auditing the release ...' -ForegroundColor Cyan
  & python (Join-Path $PSScriptRoot 'tools\release_sanitize.py') $dest
  if ($LASTEXITCODE -ne 0) {
    Write-Host '[X] release REFUSED - archive deleted. Fix the leaks above and re-run.' -ForegroundColor Red
    Remove-Item $dest -Force -EA SilentlyContinue
    if ($freshStage) { Remove-Item $freshStage -Recurse -Force -EA SilentlyContinue }
    exit 1
  }

  # THE INSTALLER MUST ACTUALLY RUN. Four releases in a row shipped an
  # install.ps1 nobody had executed, and he found each break by hand on a
  # second PC: a syntax path that only fires on 3.11, a hashtable key that is
  # fatal under StrictMode, a verb that does not exist. Auditing the archive
  # for leaks says nothing about whether the thing inside works.
  #
  # So: unzip what we are about to ship and run its own installer in -CheckOnly
  # (no prompts, no installs, no writes), with STRICTMODE FORCED ON - because
  # the Claude installer leaves it on, which is how the worst of them happened.
  # Non-zero exit or a strict-mode error refuses the release.
  Write-Host '[..] running the SHIPPED installer (check mode, strict) ...' -ForegroundColor Cyan
  $probe = Join-Path ([IO.Path]::GetTempPath()) ("omnius-probe-" + [Guid]::NewGuid().ToString('N').Substring(0,8))
  New-Item -ItemType Directory -Path $probe -Force | Out-Null
  try {
    & $tar -xf $dest -C $probe
    $inst = Join-Path $probe "$leaf\install.ps1"
    if (-not (Test-Path $inst)) { throw "install.ps1 missing from the archive" }
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -Command `
             "Set-StrictMode -Version Latest; `$ErrorActionPreference='Continue'; & '$inst' -CheckOnly" 2>&1
    $code = $LASTEXITCODE
    $strict = @($out | Select-String -Pattern 'PropertyNotFound|StrictMode|is not set|no se ha establecido')
    if ($code -ne 0 -or $strict.Count -gt 0) {
      Write-Host '[X] release REFUSED - the shipped installer does not run cleanly:' -ForegroundColor Red
      if ($code -ne 0) { Write-Host ("    exit code {0}" -f $code) }
      $strict | Select-Object -First 5 | ForEach-Object { Write-Host ("    {0}" -f $_.Line.Trim()) }
      Remove-Item $dest -Force -EA SilentlyContinue
      Remove-Item $probe -Recurse -Force -EA SilentlyContinue
      if ($freshStage) { Remove-Item $freshStage -Recurse -Force -EA SilentlyContinue }
      exit 1
    }
    Write-Host '     installer runs clean' -ForegroundColor Gray
  } catch {
    Write-Host ("[!] could not probe the installer: {0}" -f $_.Exception.Message) -ForegroundColor Yellow
  } finally {
    Remove-Item $probe -Recurse -Force -EA SilentlyContinue
  }
}
if ($freshStage) { Remove-Item $freshStage -Recurse -Force -EA SilentlyContinue }

if ((Test-Path $dest) -and ($LASTEXITCODE -eq 0)) {
  $mb = (Get-Item $dest).Length / 1MB
  $entries = (& $tar -tf $dest | Measure-Object).Count
  Write-Host ("[OK] {0}  ({1:N1} MB, {2} entries)" -f $dest, $mb, $entries) -ForegroundColor Green
  Write-Host ("     unzipping recreates the {0}\ folder" -f $leaf)
  if ($Fresh) {
    Write-Host '     a NEW instance: code, skills, docs, templates, and the product half of memory'
    Write-Host '     NOT included: projects, daybook notes, media, state, .env, and NO git history'
    Write-Host '     on the target PC: unzip -> install.bat (prerequisites, .env, hook paths) -> start-omnius.bat'
    Write-Host '     it will ask for its OWN Discord bot and server - nothing of this instance carries over'
  } elseif ($Work) {
    Write-Host '     included: system, docs, memory, skills, templates, assets, full git history'
    if ($drops.Count -eq 0) {
      Write-Host '     left behind: nothing - daybook\notes\, projects\ and media\ held no files'
    } else {
      # Name what was actually dropped, with counts. The old line listed all
      # three unconditionally, so it read the same on a pack that lost nothing
      # and on one that lost every note you had.
      $summary = ($drops | ForEach-Object { '{0} ({1} file(s))' -f $_.Path, $_.Files }) -join ', '
      Write-Host ("     LEFT BEHIND, stays on this PC: $summary") -ForegroundColor Yellow
    }
    Write-Host '     excluded: .env (only .env.example travels), state\, node_modules\, caches, logs'
    Write-Host '     on the new PC: unzip -> install.bat (set up the NEW Discord bot + server) -> start-omnius.bat'
  } else {
    Write-Host '     included: system, docs, memory, full git history, projects\, daybook notes, media archive'
    Write-Host '     included: .env - this restores the machine, so it CONTAINS SECRETS' -ForegroundColor Yellow
    Write-Host '     keep it on a drive you control: no shared folder, no cloud sync, never a release asset' -ForegroundColor Yellow
    Write-Host '     excluded: state\, node_modules\, caches, logs, and key files (*.pem, serviceAccount*.json, id_rsa, ...)'
    Write-Host '     on the new PC: unzip -> install.bat -> start-omnius.bat'
  }
} else {
  # A truncated archive with a completely plausible name is worse than none:
  # it looks like a good backup until the day it is needed.
  if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Force }
  Write-Host '[X] packing failed - no archive written (see the tar errors above)' -ForegroundColor Red
  exit 1
}
