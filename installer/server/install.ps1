<#
PawPoller Server — one-shot installer for Windows (runs as a scheduled task at startup).

    irm https://raw.githubusercontent.com/knaughtykat01-prog/pawpoller-public/main/installer/server/install.ps1 | iex

Run from an elevated PowerShell (the task runs as SYSTEM so it starts before anyone signs in).
Layout: <root>\releases\<version>\PawPoller-Server.exe with <root>\current as a junction the
task launches; the server updates itself from then on and keeps the previous release.

Knobs (set before running): $env:VERSION, $env:ARCHIVE (offline zip), $env:PAWPOLLER_SERVER_ROOT
(default C:\ProgramData\PawPoller-Server), $env:PAWPOLLER_APPDATA_DIR (default <root>\data),
$env:PAWPOLLER_PORT (8420), $env:PAWPOLLER_BIND (127.0.0.1).
#>
$ErrorActionPreference = 'Stop'
$Repo = 'knaughtykat01-prog/pawpoller-public'
$Root = if ($env:PAWPOLLER_SERVER_ROOT) { $env:PAWPOLLER_SERVER_ROOT } else { Join-Path $env:ProgramData 'PawPoller-Server' }
$Data = if ($env:PAWPOLLER_APPDATA_DIR) { $env:PAWPOLLER_APPDATA_DIR } else { Join-Path $Root 'data' }
$Port = if ($env:PAWPOLLER_PORT) { $env:PAWPOLLER_PORT } else { '8420' }
$Bind = if ($env:PAWPOLLER_BIND) { $env:PAWPOLLER_BIND } else { '127.0.0.1' }
$TaskName = 'PawPoller Server'
$Tag = if ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -eq 'Arm64') { 'windows-arm64' } else { 'windows-x64' }

function Say($m) { Write-Host "==> $m" -ForegroundColor Green }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw 'Run this from an elevated (Administrator) PowerShell.' }

# ── 1. which version ─────────────────────────────────────────────────────────
$Tmp = Join-Path $env:TEMP ("pawpoller-server-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Tmp | Out-Null
try {
  if ($env:ARCHIVE) {
    $Archive = $env:ARCHIVE
    if (-not (Test-Path $Archive)) { throw "ARCHIVE not found: $Archive" }
    $Version = if ($env:VERSION) { $env:VERSION } else { ([IO.Path]::GetFileName($Archive) -replace '^PawPoller-Server-([^-]+)-.*$', '$1') }
  } else {
    $Version = $env:VERSION
    if (-not $Version) {
      Say 'Looking up the latest release'
      $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ 'User-Agent' = 'PawPoller-Server-installer' }
      $Version = ($rel.tag_name -replace '^v', '')
    }
    $Asset = "PawPoller-Server-$Version-$Tag.zip"
    $Url = "https://github.com/$Repo/releases/download/v$Version/$Asset"
    $Archive = Join-Path $Tmp $Asset
    Say "Downloading $Asset"
    Invoke-WebRequest -Uri $Url -OutFile $Archive
    Invoke-WebRequest -Uri "$Url.sha256" -OutFile "$Archive.sha256"
    Say 'Verifying checksum'
    $expected = ((Get-Content "$Archive.sha256" -Raw) -split '\s+' | Where-Object { $_ -match '^[0-9a-fA-F]{64}$' } | Select-Object -First 1).ToLower()
    $actual = (Get-FileHash -Algorithm SHA256 $Archive).Hash.ToLower()
    if ($expected -ne $actual) { throw "checksum mismatch: expected $expected got $actual" }
  }

  # ── 2. unpack into releases\<version>, point current at it ─────────────────
  Say "Installing $Version under $Root"
  $Releases = Join-Path $Root 'releases'
  New-Item -ItemType Directory -Force -Path $Releases, $Data | Out-Null
  $Stage = Join-Path $Releases "$Version.staging"
  if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
  Expand-Archive -Path $Archive -DestinationPath $Stage -Force
  $entries = Get-ChildItem $Stage
  if ($entries.Count -eq 1 -and $entries[0].PSIsContainer) {
    Get-ChildItem $entries[0].FullName | Move-Item -Destination $Stage -Force
    Remove-Item $entries[0].FullName -Force
  }
  if (-not (Test-Path (Join-Path $Stage 'PawPoller-Server.exe'))) { throw 'PawPoller-Server.exe not found in the archive' }
  $Final = Join-Path $Releases $Version
  if (Test-Path $Final) { Remove-Item -Recurse -Force $Final }
  Rename-Item $Stage $Final
  $Current = Join-Path $Root 'current'
  if (Test-Path $Current) { cmd /c rmdir "$Current" | Out-Null }      # a junction: rmdir removes the link only
  cmd /c mklink /J "$Current" "$Final" | Out-Null

  # ── 3. launcher + scheduled task ─────────────────────────────────────────
  $Run = Join-Path $Root 'run.cmd'
  if (-not (Test-Path $Run)) {
    Say "Writing $Run"
    @"
@echo off
rem PawPoller Server launcher — edit the variables, then: schtasks /end /tn "$TaskName" & schtasks /run /tn "$TaskName"
set PAWPOLLER_APPDATA_DIR=$Data
set PAWPOLLER_SERVER_ROOT=$Root
set PAWPOLLER_SERVER_MANAGED=1
set PAWPOLLER_AUTO_BACKUP=1
set PAWPOLLER_AUTO_BACKUP_DIR=$Data\backups
cd /d "$Root\current"
"$Root\current\PawPoller-Server.exe" --host $Bind --port $Port
"@ | Set-Content -Path $Run -Encoding ASCII
  }
  $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>PawPoller Server — starts at boot, restarts if it stops (exit 75 = self-update).</Description></RegistrationInfo>
  <Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <Enabled>true</Enabled><Hidden>true</Hidden>
  </Settings>
  <Actions Context="Author"><Exec><Command>$Run</Command><WorkingDirectory>$Root</WorkingDirectory></Exec></Actions>
</Task>
"@
  $xmlPath = Join-Path $Tmp 'task.xml'
  $xml | Set-Content -Path $xmlPath -Encoding Unicode
  Say "Registering the scheduled task '$TaskName'"
  schtasks /end /tn "$TaskName" 2>$null | Out-Null
  schtasks /create /tn "$TaskName" /xml "$xmlPath" /f | Out-Null
  schtasks /run /tn "$TaskName" | Out-Null

  # ── 4. health ────────────────────────────────────────────────────────────
  Say 'Waiting for the server'
  for ($i = 0; $i -lt 40; $i++) {
    try {
      $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
      if ($h.status -eq 'ok') {
        Write-Host ''
        Say "PawPoller Server $Version is running."
        Write-Host "    Dashboard (this machine):  http://127.0.0.1:$Port"
        Write-Host "    From your other devices:   install Tailscale on both machines, then: tailscale serve --bg $Port"
        Write-Host "    Data:                      $Data"
        Write-Host "    Releases:                  $Releases   (it updates itself; the previous one is kept)"
        Write-Host "    Task:                      Task Scheduler -> '$TaskName'"
        exit 0
      }
    } catch { }
    Start-Sleep -Seconds 1
  }
  throw "the server did not answer on port $Port within 40 s — check Task Scheduler -> '$TaskName' -> History"
} finally {
  Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}
