# install-sync-worker.ps1
# Safe, idempotent installer for KakaoCollector sync-on-login.ps1 worker.
# Creates backups and performs atomic replacement without touching live runs.
[CmdletBinding()]
param(
    [string]$SourceScript,
    [string]$DestinationScript,
    [switch]$Status,
    [switch]$Force,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot

if (-not $SourceScript) {
    $SourceScript = Join-Path $ProjectRoot "sync-on-login.ps1"
}

if (-not $DestinationScript) {
    $DestinationScript = Join-Path $env:LOCALAPPDATA "KakaoCollector\sync-on-login.ps1"
}

$DestDir = Split-Path -Parent $DestinationScript

if ($Status) {
    Write-Host "=== KakaoCollector Sync Worker Installation Status ==="
    Write-Host "Source      : $SourceScript"
    Write-Host "Destination : $DestinationScript"
    Write-Host "Dest Exists : $(Test-Path -LiteralPath $DestinationScript)"
    $BackupStandard = "$DestinationScript.bak"
    Write-Host "Backup Found: $(Test-Path -LiteralPath $BackupStandard)"
    if (Test-Path -LiteralPath $DestinationScript) {
        $destHash = (Get-FileHash -LiteralPath $DestinationScript -Algorithm SHA256).Hash
        Write-Host "Dest SHA256 : $destHash"
    }
    if (Test-Path -LiteralPath $SourceScript) {
        $srcHash = (Get-FileHash -LiteralPath $SourceScript -Algorithm SHA256).Hash
        Write-Host "Source SHA256: $srcHash"
    }
    exit 0
}

if ($Uninstall) {
    $BackupStandard = "$DestinationScript.bak"
    if (-not (Test-Path -LiteralPath $BackupStandard)) {
        Write-Error "[install-sync-worker] Backup file not found: $BackupStandard"
        exit 1
    }
    Copy-Item -LiteralPath $BackupStandard -Destination $DestinationScript -Force
    Write-Host "[install-sync-worker] Worker restored from backup successfully."
    exit 0
}

if (-not (Test-Path -LiteralPath $SourceScript)) {
    Write-Error "[install-sync-worker] Source script not found: $SourceScript"
    exit 1
}

# Verify source PowerShell syntax
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($SourceScript, [ref]$tokens, [ref]$errors)
if ($errors -and $errors.Count -gt 0) {
    Write-Error "[install-sync-worker] Source script has syntax errors: $($errors[0].Message)"
    exit 1
}

if (-not (Test-Path -LiteralPath $DestDir)) {
    New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
}

if (Test-Path -LiteralPath $DestinationScript) {
    $srcHash = (Get-FileHash -LiteralPath $SourceScript -Algorithm SHA256).Hash
    $destHash = (Get-FileHash -LiteralPath $DestinationScript -Algorithm SHA256).Hash
    if (($srcHash -eq $destHash) -and (-not $Force)) {
        Write-Host "[install-sync-worker] Worker already up to date (idempotent)."
        exit 0
    }

    # Create standard and timestamped backups
    $BackupStandard = "$DestinationScript.bak"
    Copy-Item -LiteralPath $DestinationScript -Destination $BackupStandard -Force
    $BackupTimestamp = "$DestinationScript.bak_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
    Copy-Item -LiteralPath $DestinationScript -Destination $BackupTimestamp -Force
}

# Sibling temp + atomic replacement
$tmpPath = Join-Path $DestDir ("sync-worker-" + [guid]::NewGuid().ToString('N') + ".tmp")
Copy-Item -LiteralPath $SourceScript -Destination $tmpPath -Force

try {
    if (Test-Path -LiteralPath $DestinationScript) {
        try {
            [System.IO.File]::Replace($tmpPath, $DestinationScript, $null)
        } catch {
            Move-Item -LiteralPath $tmpPath -Destination $DestinationScript -Force
        }
    } else {
        Move-Item -LiteralPath $tmpPath -Destination $DestinationScript -Force
    }
    Write-Host "[install-sync-worker] Worker installed successfully to $DestinationScript."
} finally {
    if (Test-Path -LiteralPath $tmpPath) {
        Remove-Item -LiteralPath $tmpPath -Force -ErrorAction SilentlyContinue
    }
}
