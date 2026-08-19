# Drills for update.ps1 - break the updater on purpose, in disposable clones.
#
#   powershell -ExecutionPolicy Bypass -File tools\update_drills.ps1
#   ... -Only conflict           run one drill by name
#   ... -KeepTemp                leave the clones behind for inspection
#
# WHY. The updater is the one component that, when wrong, cannot fix itself -
# every instance is stranded at once and the fix cannot reach them. That
# happened twice on 2026-08-19, and both times the flaw was invisible on a
# healthy machine and obvious on a real one: a pre-existing failure, a desk
# short of the allow-list, a watchdog that was not the scheduled task.
#
# So this does not check that update.ps1 is well written. It builds instances
# that are ALREADY in trouble, runs the real script at them, and asserts what
# survived. Every drill here is a shape that actually happened or that would
# strand somebody if it did.
#
# Cheap by design: only the two drills about the test-gate run the suite (it
# takes ~40s a pass, twice per run); the rest pass -NoTests, because what they
# are testing is git behaviour and refusals, not the suite.
param([string]$Only, [switch]$KeepTemp)

$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$updater = Join-Path $repo 'update.ps1'
$base = 'HEAD~4'          # far enough back that a drill has real work to do
$script:pass = 0
$script:fail = 0
$temps = @()

function Check([string]$label, [bool]$ok, [string]$hint = '') {
  if ($ok) { $script:pass++; Write-Host "  [PASS] $label" -ForegroundColor Green }
  else { $script:fail++; Write-Host "  [FAIL] $label  $hint" -ForegroundColor Red }
}

function New-Instance([string]$at) {
  # A disposable clone of THIS repo, rewound - the closest thing to somebody
  # else's machine that does not need somebody else's machine.
  $dir = Join-Path ([IO.Path]::GetTempPath()) ("omnius-drill-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
  & git clone --quiet $repo $dir 2>&1 | Out-Null
  & git -C $dir checkout --quiet $at 2>&1 | Out-Null
  & git -C $dir checkout --quiet -B main 2>&1 | Out-Null
  & git -C $dir remote set-url origin $repo 2>&1 | Out-Null
  & git -C $dir config user.email 'drill@example.invalid' 2>&1 | Out-Null
  & git -C $dir config user.name 'drill' 2>&1 | Out-Null
  $script:temps += $dir
  return $dir
}

function Run-Update([string]$dir, [switch]$WithTests, [hashtable]$env = @{}) {
  $args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $updater,
            '-Path', $dir, '-NoRestart')
  if (-not $WithTests) { $args += '-NoTests' }
  $old = @{}
  foreach ($k in $env.Keys) { $old[$k] = [Environment]::GetEnvironmentVariable($k); [Environment]::SetEnvironmentVariable($k, $env[$k]) }
  try {
    $out = & powershell @args 2>&1
    return @{ rc = $LASTEXITCODE; out = ($out -join "`n") }
  } finally {
    foreach ($k in $env.Keys) { [Environment]::SetEnvironmentVariable($k, $old[$k]) }
  }
}

function Head([string]$dir) { (& git -C $dir rev-parse --short HEAD 2>$null) }
function Want([string]$name) { -not $Only -or $Only -eq $name }

Write-Host ''
Write-Host '--- update.ps1 drills ------------------------------------' -ForegroundColor Cyan
$target = (& git -C $repo rev-parse --short HEAD)
Write-Host "    updating clones of $repo from $base up to $target"
Write-Host ''

# 1. THE ORDINARY CASE. If this ever fails, nothing else matters.
if (Want 'plain') {
  Write-Host '== plain: a clean instance, behind =='
  $d = New-Instance $base
  $before = Head $d
  $r = Run-Update $d
  Check 'a behind-but-clean instance updates' ($r.rc -eq 0 -and (Head $d) -ne $before) $r.out
  Check '...and lands exactly on the published commit' ((Head $d) -eq $target)
  Check '...and says what it did' ($r.out -match 'pulled')
}

# 2. RUNNING IT AGAIN MUST BE BORING. An updater that is not idempotent is one
#    nobody dares run twice.
if (Want 'idempotent') {
  Write-Host '== idempotent: already current =='
  $d = New-Instance 'HEAD'
  $r = Run-Update $d
  Check 'an up-to-date instance is a no-op, not an error' ($r.rc -eq 0 -and $r.out -match 'already current') $r.out
}

# 3. LOCAL WORK SURVIVES. The reason ff-only was abandoned: every instance but
#    one is somebody's own copy and they change things.
if (Want 'localwork') {
  Write-Host '== local work: a commit and an uncommitted edit, both harmless =='
  $d = New-Instance $base
  Set-Content (Join-Path $d 'MY-NOTES.md') "mine`n" -Encoding utf8
  & git -C $d add -A 2>&1 | Out-Null
  & git -C $d commit --quiet -m 'local: my own note' 2>&1 | Out-Null
  Set-Content (Join-Path $d 'MY-SCRATCH.txt') "work in progress`n" -Encoding utf8
  $r = Run-Update $d
  Check 'the update goes through with local work present' ($r.rc -eq 0) $r.out
  Check '...their commit is replayed on top, not dropped' (
    [bool](((& git -C $d log --oneline -3) -join "`n") -match 'local: my own note'))
  Check '...their committed file is still there' (Test-Path (Join-Path $d 'MY-NOTES.md'))
  Check '...and the uncommitted one too' (Test-Path (Join-Path $d 'MY-SCRATCH.txt'))
  # HEAD is THEIR commit now, sitting on top of the release - that is what
  # rebasing means. The release has to be an ancestor, not the tip.
  & git -C $d merge-base --is-ancestor $target HEAD 2>&1 | Out-Null
  Check '...and the release is underneath it' ($LASTEXITCODE -eq 0)
}

# 4. A REAL CONFLICT MUST STOP, AND PUT EVERYTHING BACK. The failure mode to
#    fear is a half-merged tree that then gets reloaded.
if (Want 'conflict') {
  Write-Host '== conflict: their edit and the release touch the same lines =='
  $d = New-Instance $base
  $victim = Join-Path $d 'README.md'
  Set-Content $victim "COMPLETELY DIFFERENT README`n" -Encoding utf8
  & git -C $d add -A 2>&1 | Out-Null
  & git -C $d commit --quiet -m 'local: rewrote the readme' 2>&1 | Out-Null
  $before = Head $d
  $r = Run-Update $d
  Check 'a conflicting local commit stops the update' ($r.rc -ne 0) $r.out
  Check '...and says which file' ($r.out -match 'README')
  Check '...the instance is left on the commit it was on' ((Head $d) -eq $before)
  Check '...their version is intact' ((Get-Content $victim -Raw) -match 'COMPLETELY DIFFERENT')
  Check '...and no merge markers were left behind' (
    -not ((Get-Content $victim -Raw) -match '<<<<<<<'))
  Check '...no rebase left in progress' (
    -not (Test-Path (Join-Path $d '.git\rebase-merge')) -and
    -not (Test-Path (Join-Path $d '.git\rebase-apply')))
}

# 5. THE AUTOSTASH CONFLICT. git exits 0 here while leaving markers in the
#    file - the case that would have reloaded the fleet onto a broken tree.
if (Want 'autostash') {
  Write-Host '== autostash: an UNCOMMITTED edit collides with the release =='
  $d = New-Instance $base
  $victim = Join-Path $d 'README.md'
  Set-Content $victim "my uncommitted rewrite`n" -Encoding utf8
  $before = Head $d
  $r = Run-Update $d
  Check 'an uncommitted collision stops the update too' ($r.rc -ne 0) $r.out
  Check '...the instance is unchanged' ((Head $d) -eq $before)
  Check '...their uncommitted text is still in the file' (
    (Get-Content $victim -Raw) -match 'my uncommitted rewrite')
  Check '...and the tree carries no merge markers' (
    -not ((Get-Content $victim -Raw) -match '<<<<<<<'))
}

# 6. THIS MACHINE'S OWN UNTIDINESS IS NOT THE RELEASE'S FAULT. The bug that
#    stranded the owner: a memory file over budget failed the suite, and the
#    updater reverted a perfectly good release for it.
if (Want 'baseline') {
  Write-Host '== baseline: a check that was ALREADY red (runs the suite) =='
  $d = New-Instance $base
  $topic = Join-Path $d 'memory\orchestrator\topics\discord-fleet.md'
  New-Item -ItemType Directory -Force (Split-Path $topic) | Out-Null
  Set-Content $topic ("# planted`n" + ('x' * 17000)) -Encoding utf8
  $r = Run-Update $d -WithTests
  Check 'a pre-existing failure does NOT revert the update' ($r.rc -eq 0) $r.out
  Check '...it is named as the baseline instead' ($r.out -match 'already failing')
  Check '...and the update lands' ((Head $d) -eq $target)
}

# 7. A RELEASE THAT REALLY IS BROKEN MUST BE REVERTED. The other half: the gate
#    has to still bite when the new code is the problem.
if (Want 'rollback') {
  Write-Host '== rollback: the incoming commit breaks the suite (runs the suite) =='
  $bare = Join-Path ([IO.Path]::GetTempPath()) ("omnius-badrelease-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
  $script:temps += $bare
  & git clone --quiet --bare $repo $bare 2>&1 | Out-Null
  $stage = Join-Path ([IO.Path]::GetTempPath()) ("omnius-stage-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
  $script:temps += $stage
  & git clone --quiet $bare $stage 2>&1 | Out-Null
  & git -C $stage config user.email 'drill@example.invalid' 2>&1 | Out-Null
  & git -C $stage config user.name 'drill' 2>&1 | Out-Null
  # Break something the suite genuinely asserts, rather than appending a failing
  # line to the suite file: everything after its sys.exit() never runs, so the
  # first attempt planted a check that could not fail. Removing Artifact from
  # the shared allow-list trips "Artifact is allowed - a desk must not need
  # permission to publish its answer", which is a real check about real code.
  $perms = Join-Path $stage 'tools\discord\sync_permissions.py'
  (Get-Content $perms -Raw).Replace('"Artifact", "SendUserFile"', '"SendUserFile"') |
    Set-Content $perms -Encoding utf8 -NoNewline
  & git -C $stage commit --quiet -am 'a release that breaks its own suite' 2>&1 | Out-Null
  & git -C $stage push --quiet origin HEAD:refs/heads/main 2>&1 | Out-Null

  $d = New-Instance $base
  & git -C $d remote set-url origin $bare 2>&1 | Out-Null
  $before = Head $d
  $r = Run-Update $d -WithTests
  Check 'a release that breaks the suite is rolled back' ($r.rc -ne 0) $r.out
  Check '...and the instance is back on the commit it had' ((Head $d) -eq $before)
  Check '...and it names the check that broke' ($r.out -match 'Artifact is allowed')
}

# 8. RUN FROM A DESK. Restarting the watchdog from inside one of its own
#    children kills the process printing the answer.
if (Want 'desk') {
  Write-Host '== desk: run from inside a desk of the instance itself =='
  $d = New-Instance 'HEAD'
  $r = Run-Update $d -env @{ 'OMNIUS_SESSION' = 'orchestrator' }
  Check 'a desk run refuses to restart the watchdog' ($r.rc -eq 0 -and $r.out -notmatch 'watchdog restarted') $r.out
}

# 9. REFUSALS THAT MUST BE LEGIBLE, not stack traces.
if (Want 'refusals') {
  Write-Host '== refusals: not an install, and not attached =='
  $empty = Join-Path ([IO.Path]::GetTempPath()) ("omnius-empty-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
  New-Item -ItemType Directory -Force $empty | Out-Null
  $script:temps += $empty
  $r = Run-Update $empty
  Check 'a folder that is not an Omnius install is refused clearly' (
    $r.rc -ne 0 -and $r.out -match 'could not find an Omnius install') $r.out

  $d = New-Instance 'HEAD'
  Remove-Item (Join-Path $d '.git') -Recurse -Force
  $r = Run-Update $d
  Check 'an install with no .git says how to attach it' (
    $r.rc -ne 0 -and $r.out -match 'not attached to GitHub') $r.out
}

Write-Host ''
if ($script:fail) {
  Write-Host "==== $($script:pass) passed, $($script:fail) FAILED ====" -ForegroundColor Red
} else {
  Write-Host "==== $($script:pass) passed, 0 failed ====" -ForegroundColor Green
}
if ($KeepTemp) {
  Write-Host ''
  Write-Host '  kept:'; $temps | ForEach-Object { Write-Host "    $_" }
} else {
  foreach ($t in $temps) { Remove-Item $t -Recurse -Force -ErrorAction SilentlyContinue }
}
Write-Host ''
if ($PSCommandPath) { exit ($(if ($script:fail) { 1 } else { 0 })) }
