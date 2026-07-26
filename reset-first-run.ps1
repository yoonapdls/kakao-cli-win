param(
  [switch]$KeepConfig
)

$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
$DataDir = Join-Path $ProjectRoot "data"
$OutputDir = Join-Path $DataDir "output"
$ConfigDir = Join-Path $DataDir "config"
$BackupRoot = Join-Path $DataDir "backup"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BackupDir = Join-Path $BackupRoot "first-run-$Stamp"

New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

$Moved = @()

function Move-IfExists {
  param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$Destination
  )
  if (Test-Path -LiteralPath $Source) {
    $Parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $Parent | Out-Null
    Move-Item -LiteralPath $Source -Destination $Destination
    $script:Moved += $Source
  }
}

if (Test-Path -LiteralPath $OutputDir) {
  $OutputItems = Get-ChildItem -LiteralPath $OutputDir -Force
  if ($OutputItems.Count -gt 0) {
    Move-IfExists -Source $OutputDir -Destination (Join-Path $BackupDir "output")
  }
}

if (-not $KeepConfig -and (Test-Path -LiteralPath $ConfigDir)) {
  foreach ($Name in @("automation_jobs.json", "summary_style.json", "config.json")) {
    Move-IfExists `
      -Source (Join-Path $ConfigDir $Name) `
      -Destination (Join-Path (Join-Path $BackupDir "config") $Name)
  }
}

foreach ($Name in @("v2_keys.json", "memory_inspection.txt", "userid_result.txt", "config.json")) {
  Move-IfExists `
    -Source (Join-Path $ProjectRoot $Name) `
    -Destination (Join-Path (Join-Path $BackupDir "root") $Name)
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null

$Readme = @"
This backup was created by reset-first-run.ps1.

Purpose:
- Move local recovered keys, decrypted SQLite outputs, and private JSON config out of the active project.
- Recreate empty data/output and data/config directories.
- Let you test the app as if it were the first run.

Restore manually:
1. Stop python backend/server.py.
2. Copy files from this backup folder back into the same relative locations.
3. Restart python backend/server.py --port 8780.

Moved:
$($Moved -join "`r`n")
"@

$Readme | Set-Content -LiteralPath (Join-Path $BackupDir "README.txt") -Encoding UTF8

Write-Host "First-run state prepared."
Write-Host "Backup:" $BackupDir
if ($Moved.Count -eq 0) {
  Write-Host "No existing local state was found to move."
}
