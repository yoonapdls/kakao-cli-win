# install-post-sync-mirror.ps1
# Idempotent installer for Kakao Post-Sync Mirror runner hook
[CmdletBinding()]
param(
    [string]$TargetScript,
    [string]$PostSyncScript,
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot

if (-not $PostSyncScript) {
    $PostSyncScript = Join-Path $ProjectRoot "post-sync-mirror.ps1"
}

if (-not $TargetScript) {
    $Candidates = @(
        (Join-Path $ProjectRoot "sync-on-login.ps1"),
        (Join-Path $env:LOCALAPPDATA "KakaoCollector\sync-on-login.ps1")
    )
    foreach ($cand in $Candidates) {
        if ($cand -and (Test-Path -LiteralPath $cand)) {
            $TargetScript = $cand
            break
        }
    }
    if (-not $TargetScript) {
        $TargetScript = Join-Path $ProjectRoot "sync-on-login.ps1"
    }
}

$HookStartMarker = "# >>> kakao-post-sync-mirror >>>"
$HookEndMarker   = "# <<< kakao-post-sync-mirror <<<"

if ($Status) {
    Write-Host "=== Kakao Post-Sync Mirror Hook Status ==="
    if (-not (Test-Path -LiteralPath $TargetScript)) {
        Write-Host "Target Script : Not found"
        exit 0
    }
    $Content = Get-Content -LiteralPath $TargetScript -Raw
    $IsInstalled = $Content -match [regex]::Escape($HookStartMarker)
    Write-Host "Hook Installed: $IsInstalled"
    $BackupStandard = "$TargetScript.bak"
    Write-Host "Backup Exists : $(Test-Path -LiteralPath $BackupStandard)"
    exit 0
}

if (-not (Test-Path -LiteralPath $TargetScript)) {
    Write-Error "Target script not found: $TargetScript"
    exit 1
}

$Content = Get-Content -LiteralPath $TargetScript -Raw
$IsInstalled = $Content -match [regex]::Escape($HookStartMarker)

if ($Uninstall) {
    if (-not $IsInstalled) {
        Write-Host "[install-post-sync-mirror] Hook is not installed. Nothing to uninstall."
        exit 0
    }
    $BackupTimestamp = "$TargetScript.bak_pre_uninstall_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
    Copy-Item -LiteralPath $TargetScript -Destination $BackupTimestamp -Force

    $Regex = "(?s)\r?\n?" + [regex]::Escape($HookStartMarker) + ".*?" + [regex]::Escape($HookEndMarker) + "\r?\n?"
    $Cleaned = [regex]::Replace($Content, $Regex, "`r`n")
    Set-Content -LiteralPath $TargetScript -Value $Cleaned.TrimEnd() -Encoding UTF8
    Write-Host "[install-post-sync-mirror] Hook uninstalled successfully."
    exit 0
}

if ($IsInstalled -and (-not $Force)) {
    Write-Host "[install-post-sync-mirror] Hook is already installed (idempotent)."
    exit 0
}

# If force reinstalling, clean previous hook first
if ($IsInstalled -and $Force) {
    $Regex = "(?s)\r?\n?" + [regex]::Escape($HookStartMarker) + ".*?" + [regex]::Escape($HookEndMarker) + "\r?\n?"
    $Content = [regex]::Replace($Content, $Regex, "`r`n")
}

# Create backup before modifying
$BackupStandard = "$TargetScript.bak"
Copy-Item -LiteralPath $TargetScript -Destination $BackupStandard -Force
$BackupTimestamp = "$TargetScript.bak_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
Copy-Item -LiteralPath $TargetScript -Destination $BackupTimestamp -Force

$HookBlock = @"
$HookStartMarker
# Post-sync mirror trigger: fail-closed, executes only when sync succeeded and marker is current
`$__can_run_mirror = `$false
`$__skip_reason = `$null

try {
    # Resolve marker path: check script variables or fallback to standard location
    `$__marker_file = `$null
    if (Test-Path variable:MarkerFile) {
        if (`$MarkerFile) { `$__marker_file = `$MarkerFile }
    } elseif (Test-Path variable:dst) {
        if (`$dst) { `$__marker_file = `$dst }
    } elseif (Test-Path variable:stateDir) {
        if (`$stateDir) { `$__marker_file = (Join-Path `$stateDir "last-sync.json") }
    } elseif (`$PSScriptRoot -and (Test-Path -LiteralPath (Join-Path `$PSScriptRoot "state\last-sync.json"))) {
        `$__marker_file = Join-Path `$PSScriptRoot "state\last-sync.json"
    } elseif (`$env:LOCALAPPDATA) {
        `$__marker_file = Join-Path `$env:LOCALAPPDATA "KakaoCollector\state\last-sync.json"
    }

    if (-not `$__marker_file -or -not (Test-Path -LiteralPath `$__marker_file)) {
        `$__skip_reason = "MARKER_NOT_FOUND"
    } else {
        `$__raw = Get-Content -LiteralPath `$__marker_file -Raw -Encoding UTF8
        `$__marker = `$null
        try {
            `$__marker = `$__raw | ConvertFrom-Json
        } catch {
            `$__skip_reason = "PARSE_ERROR"
        }

        if (-not `$__skip_reason) {
            if (`$__marker -and (`$__marker.PSObject.Properties['status'])) {
                `$__status_ok = (`$__marker.status -eq "SUCCESS")
                `$__ready_ok = (`$null -eq `$__marker.ready -or [bool]`$__marker.ready -eq `$true)

                if (-not (`$__status_ok -and `$__ready_ok)) {
                    `$__skip_reason = "SYNC_NOT_SUCCESS"
                } else {
                    `$__is_current = `$false
                    if ((Test-Path variable:started) -and (`$started -is [datetime])) {
                        if (`$__marker.started_at -and (`$__marker.started_at -eq `$started.ToString("o"))) {
                            `$__is_current = `$true
                        } elseif (`$__marker.completed_at) {
                            try {
                                `$__comp = [datetimeoffset]::Parse(`$__marker.completed_at)
                                `$__started_dto = [datetimeoffset]`$started
                                if (`$__comp -ge `$__started_dto.AddSeconds(-2)) {
                                    `$__is_current = `$true
                                }
                            } catch {}
                        }
                    } else {
                        `$__freshness_ref = `$null
                        if (`$__marker.completed_at) {
                            try { `$__freshness_ref = [datetimeoffset]::Parse(`$__marker.completed_at) } catch {}
                        }
                        if (-not `$__freshness_ref) {
                            try {
                                `$__raw_time = (Get-Item -LiteralPath `$__marker_file).LastWriteTime
                                `$__freshness_ref = [datetimeoffset]`$__raw_time
                            } catch {}
                        }
                        if (`$__freshness_ref) {
                            `$__now_dto = [datetimeoffset]::Now
                            `$__age = (`$__now_dto - `$__freshness_ref).TotalSeconds
                            if (`$__age -ge -60 -and `$__age -le 300) {
                                `$__is_current = `$true
                            }
                        }
                    }

                    if (`$__is_current) {
                        `$__can_run_mirror = `$true
                    } else {
                        `$__skip_reason = "STALE_MARKER"
                    }
                }
            } else {
                `$__skip_reason = "PARSE_ERROR"
            }
        }
    }
} catch {
    `$__can_run_mirror = `$false
    `$__skip_reason = "HOOK_EXECUTION_ERROR"
}

if (`$__can_run_mirror) {
    Write-Host "[post-sync-mirror-hook] Triggering post-sync mirror (-UploadOnly)..."
    `$__mirror_runner = "$PostSyncScript"
    if (Test-Path -LiteralPath `$__mirror_runner) {
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "`$__mirror_runner" -UploadOnly
    }
} else {
    Write-Host "[post-sync-mirror-hook] Skipped: `$__skip_reason"
}
$HookEndMarker
"@

# Locate insertion point: after sync success marker or at end of file
$Lines = $Content -split "\r?\n"
$InsertIndex = -1

for ($i = 0; $i -lt $Lines.Count; $i++) {
    $line = $Lines[$i]
    if ($line -match '(?i)Move-Item.*(?:\$dst|last-sync)' -or `
        $line -match '(?i)(Set-Content|Out-File|New-Item).*last-sync' -or `
        $line -match '(?i)(Set-Content|Out-File|New-Item).*marker' -or `
        $line -match '(?i)\$SyncStatus\s*=\s*[''"]SUCCESS[''"]' -or `
        $line -match '(?i)\$SyncSuccess\s*=\s*\$true' -or `
        $line -match '(?i)Write-Host.*(?:sync|collector).*(?:success|complete)') {
        $InsertIndex = $i
    }
}

if ($InsertIndex -ge 0) {
    $NewLines = @()
    for ($i = 0; $i -le $InsertIndex; $i++) {
        $NewLines += $Lines[$i]
    }
    $NewLines += ""
    $NewLines += $HookBlock
    for ($i = $InsertIndex + 1; $i -lt $Lines.Count; $i++) {
        $NewLines += $Lines[$i]
    }
    $FinalContent = $NewLines -join "`r`n"
} else {
    $FinalContent = $Content.TrimEnd() + "`r`n`r`n" + $HookBlock
}

Set-Content -LiteralPath $TargetScript -Value $FinalContent -Encoding UTF8
Write-Host "[install-post-sync-mirror] Hook installed successfully."
exit 0
