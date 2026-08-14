# Discord setup check & guided configuration (interactive - called by launchers/installer).
#   -IfMissing : exit silently (0) when configured; otherwise offer the guided setup.
# Exit codes: 0 = configured, 2 = not configured (user skipped).
param([switch]$IfMissing, [switch]$Quiet)

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Set-Location (Join-Path $PSScriptRoot '..\..')

# NOTE: tools\discord\api.py owns .env parsing for the whole system. This file
# only falls back to its own reader when Python is unavailable - keep the two in
# step, or a launcher will disagree with the service it launches.
function Read-EnvFile {
  $vals = @{}
  # -Encoding UTF8: PS 5.1 defaults to ANSI, so one accented char or an NBSP
  # pasted with an id used to mangle the line and hide the key.
  # -LiteralPath: a workspace folder like "omnius [backup]" is a wildcard otherwise.
  if (Test-Path -LiteralPath .env) {
    foreach ($line in (Get-Content -LiteralPath .env -Encoding UTF8)) {
      if ($line -match '^\s*(?:export\s+)?([A-Za-z0-9_]+)\s*=\s*(.*)$') {
        $val = $Matches[2].Trim()
        if ($val.Length -ge 2 -and $val[0] -eq $val[-1] -and ($val[0] -eq '"' -or $val[0] -eq "'")) {
          $val = $val.Substring(1, $val.Length - 2)          # "123..." -> 123...
        } elseif ($val -match '^(.*?)\s+#.*$') {
          $val = $Matches[1].Trim()                          # trailing inline comment
        }
        $vals[$Matches[1].ToUpper()] = $val
      }
    }
  }
  return $vals
}

function Test-Configured {
  # Authoritative path: the same parser the watchdog uses, so "configured" here
  # can never mean "not configured" thirty seconds later in the service.
  $check = Join-Path $PSScriptRoot 'api.py'
  if (Test-Path -LiteralPath $check) {
    & python $check config-check 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { return $true }
    if ($LASTEXITCODE -eq 2) { return $false }
    # any other code = python missing/broken -> fall through to the local reader
  }
  $v = Read-EnvFile
  return [bool]($v['DISCORD_BOT_TOKEN'] -and $v['DISCORD_GUILD_ID'] -and $v['DISCORD_OWNER_ID'])
}

if (Test-Configured) { exit 0 }

if ($Quiet) {
  Write-Host '[i] Discord not configured - local mode (run install.bat for the guided setup)' -ForegroundColor Gray
  exit 2
}

Write-Host ''
Write-Host '[!] Discord is not set up for this instance (values missing in .env)' -ForegroundColor Yellow
$a = Read-Host '    set it up now? [y/N]'
if ($a -notmatch '^[yYjJ]') {
  Write-Host '[i] skipping - Omnius runs in local mode; the setup offer returns on the next start' -ForegroundColor Gray
  exit 2
}

Write-Host ''
Write-Host '--- Discord setup (full details: docs\DISCORD.md) -------------' -ForegroundColor Cyan
Write-Host ' 1. Create the app + bot:  https://discord.com/developers/applications'
Write-Host '    - New Application -> Bot -> Reset Token -> copy it'
Write-Host '    - Disable "Public Bot", enable "Message Content Intent"'
Write-Host ' 2. Create your PRIVATE server (no invites) in the Discord client'
Write-Host ' 3. Invite the bot: OAuth2 -> URL Generator -> scope "bot" -> permissions:'
Write-Host '    Administrator (simple) or the minimal set - see docs\DISCORD.md section 4'
Write-Host ' 4. Fill these in .env:  DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, DISCORD_OWNER_ID'
Write-Host '    (IDs: Settings -> Advanced -> Developer Mode on, then right-click -> Copy ID)'
Write-Host ''
Start-Process 'https://discord.com/developers/applications'
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
Start-Process notepad .env

while ($true) {
  $r = Read-Host 'press Enter when .env is saved (or type skip)'
  if ($r -match '^[sS]') { Write-Host '[i] skipped - local mode for now' -ForegroundColor Gray; exit 2 }
  if (-not (Test-Configured)) { Write-Host '[!] still missing values in .env' -ForegroundColor Yellow; continue }

  # verify token + server against the Discord API
  $v = Read-EnvFile
  try {
    $h = @{ Authorization = ('Bot {0}' -f $v['DISCORD_BOT_TOKEN']) }
    $me     = Invoke-RestMethod -Uri 'https://discord.com/api/v10/users/@me' -Headers $h
    $guilds = Invoke-RestMethod -Uri 'https://discord.com/api/v10/users/@me/guilds' -Headers $h
    Write-Host ('[OK] token valid - bot: {0}' -f $me.username) -ForegroundColor Green
    $inGuild = $guilds | Where-Object { $_.id -eq $v['DISCORD_GUILD_ID'] }
    if ($inGuild) {
      Write-Host ('[OK] bot is in your server: {0}' -f $inGuild.name) -ForegroundColor Green
      Write-Host '[OK] Discord is set up - the watchdog can go live in Phase 2' -ForegroundColor Green
      exit 0
    }
    Write-Host '[!] bot is NOT in the server with that DISCORD_GUILD_ID - check the invite and the ID' -ForegroundColor Yellow
  } catch {
    Write-Host ('[X] Discord API check failed: {0}' -f $_.Exception.Message) -ForegroundColor Red
    Write-Host '    check the token, save .env, then Enter to re-check'
  }
}
