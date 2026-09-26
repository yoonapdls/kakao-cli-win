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
    if (-not (Test-Path -LiteralPath `$__mirror_runner)) {
        `$__runner_candidates = @()
        if (Test-Path variable:root) { `$__runner_candidates += (Join-Path `$root "post-sync-mirror.ps1") }
        if (Test-Path variable:PSScriptRoot) { `$__runner_candidates += (Join-Path `$PSScriptRoot "post-sync-mirror.ps1") }
        `$__runner_candidates += "D:\kakao\kakao-cli-win\post-sync-mirror.ps1"
        foreach (`$__cand in `$__runner_candidates) {
            if (`$__cand -and (Test-Path -LiteralPath `$__cand)) {
                `$__mirror_runner = `$__cand
                break
            }
        }
    }

    if (`$__mirror_runner -and (Test-Path -LiteralPath `$__mirror_runner)) {
        `$__mirror_output = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "`$__mirror_runner" -UploadOnly 2>&1
        `$__mirror_exit = `$LASTEXITCODE
        `$__mirror_output | ForEach-Object { Write-Host `$_ }

        `$__mstate = `$null
        `$__has_fresh_state = `$false
        `$__state_candidates = @()
        if (Test-Path variable:root) { `$__state_candidates += (Join-Path `$root "output") }
        if (Test-Path variable:PSScriptRoot) { `$__state_candidates += (Join-Path `$PSScriptRoot "data\output") }
        `$__state_candidates += "D:\kakao\kakao-cli-win\data\output"
        foreach (`$__sc in `$__state_candidates) {
            if (`$__sc -and (Test-Path -LiteralPath `$__sc)) {
                `$__sfiles = Get-ChildItem -Path `$__sc -Filter "mirror_state.json" -Recurse -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending
                if (`$__sfiles -and `$__sfiles.Count -gt 0) {
                    `$__cand_file = `$__sfiles[0]
                    `$__is_recent = if (Test-Path variable:started) { `$__cand_file.LastWriteTime -ge `$started.AddSeconds(-60) } else { `$true }
                    if (`$__is_recent) {
                        try {
                            `$__parsed = Get-Content -LiteralPath `$__cand_file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                            if (`$__mirror_exit -ne 0) {
                                if (`$__parsed -and `$__parsed.status -ne 'SUCCESS' -and `$__parsed.lastStage -ne 'COMPLETE') {
                                    `$__mstate = `$__parsed
                                    `$__has_fresh_state = `$true
                                    break
                                }
                            } else {
                                if (`$__parsed -and (`$__parsed.status -eq 'SUCCESS' -or `$__parsed.status -eq 'SNAPSHOT_CREATED')) {
                                    `$__mstate = `$__parsed
                                    `$__has_fresh_state = `$true
                                    break
                                }
                            }
                        } catch {}
                    }
                }
            }
        }

        if (`$__mirror_exit -ne 0) {
            `$__m_stage = if (`$__has_fresh_state -and `$__mstate -and `$__mstate.lastStage -and `$__mstate.lastStage -ne 'COMPLETE') {
                [string]`$__mstate.lastStage
            } else {
                'mirror'
            }
            `$__cand_err = if (`$__has_fresh_state -and `$__mstate -and `$__mstate.errorCode) {
                [string]`$__mstate.errorCode
            } else {
                'MIRROR_FAILED'
            }
            `$__safe_err = if (Test-Path function:Get-SafeErrorCode) { Get-SafeErrorCode -ErrorItem `$__cand_err } else { `$__cand_err }
            if (`$__safe_err -eq 'SYNC_FAILED' -or [string]::IsNullOrEmpty(`$__safe_err)) {
                `$__safe_err = 'MIRROR_FAILED'
            }

            `$now = Get-Date
            `$__diag_dir = if (Test-Path variable:logDir) { `$logDir } elseif (Test-Path variable:root) { Join-Path `$root "logs" } elseif (Test-Path variable:PSScriptRoot) { Join-Path `$PSScriptRoot "logs" } else { Join-Path `$env:LOCALAPPDATA "KakaoCollector\logs" }
            if (`$__diag_dir -and (Test-Path -LiteralPath `$__diag_dir)) {
                `$__diagFile = Join-Path `$__diag_dir ("mirror-diag-{0}.jsonl" -f (`$now.ToString('yyyy-MM-dd')))
                `$__diagRecord = [ordered]@{
                    timestamp = `$now.ToString('o')
                    child_exit_code = [int]`$__mirror_exit
                    exit_code = [int]`$__mirror_exit
                    fresh_state = [bool]`$__has_fresh_state
                    has_fresh_state = [bool]`$__has_fresh_state
                    lastStage = [string]`$__m_stage
                    last_stage = [string]`$__m_stage
                    stage = [string]`$__m_stage
                    errorCode = [string]`$__safe_err
                    error_code = [string]`$__safe_err
                }
                `$__diagJson = `$__diagRecord | ConvertTo-Json -Compress
                [System.IO.File]::AppendAllText(`$__diagFile, `$__diagJson + "`r`n", (New-Object System.Text.UTF8Encoding(`$false)))
            }

            `$elapsedTotal = if (Test-Path variable:started) { [math]::Round((`$now - `$started).TotalSeconds, 2) } else { 0 }
            if (Test-Path variable:record) {
                `$record.status = 'FAILED'
                `$record.stage = `$__m_stage
                `$record.error = `$__safe_err
                `$record.error_code = `$__safe_err
                `$record.completed_at = `$now.ToString('o')
                `$record.elapsed_seconds = `$elapsedTotal
                if (-not `$record.local) {
                    `$record.local = [ordered]@{
                        status = 'SUCCESS'
                        stage = 'COMPLETE'
                        error = `$null
                        error_code = `$null
                    }
                } else {
                    `$record.local.status = 'SUCCESS'
                    `$record.local.stage = 'COMPLETE'
                    `$record.local.error = `$null
                    `$record.local.error_code = `$null
                }
                `$record.mirror = [ordered]@{
                    status = 'FAILED'
                    stage = `$__m_stage
                    error = `$__safe_err
                    error_code = `$__safe_err
                }
                if (Test-Path function:Write-AtomicJson) {
                    Write-AtomicJson -FilePath `$__marker_file -Data `$record
                } else {
                    `$__tmp_m = "`$__marker_file.tmp"
                    Set-Content -LiteralPath `$__tmp_m -Value (`$record | ConvertTo-Json -Compress) -Encoding UTF8
                    Move-Item -LiteralPath `$__tmp_m -Destination `$__marker_file -Force
                }
            }
            if (Test-Path variable:logFile) {
                `$logJson = `$record | ConvertTo-Json -Compress
                [System.IO.File]::AppendAllText(`$logFile, `$logJson + "`r`n", (New-Object System.Text.UTF8Encoding(`$false)))
            }
            exit 1
        } else {
            `$now = Get-Date
            `$__diag_dir = if (Test-Path variable:logDir) { `$logDir } elseif (Test-Path variable:root) { Join-Path `$root "logs" } elseif (Test-Path variable:PSScriptRoot) { Join-Path `$PSScriptRoot "logs" } else { Join-Path `$env:LOCALAPPDATA "KakaoCollector\logs" }
            if (`$__diag_dir -and (Test-Path -LiteralPath `$__diag_dir)) {
                `$__diagFile = Join-Path `$__diag_dir ("mirror-diag-{0}.jsonl" -f (`$now.ToString('yyyy-MM-dd')))
                `$__diagRecord = [ordered]@{
                    timestamp = `$now.ToString('o')
                    child_exit_code = 0
                    exit_code = 0
                    fresh_state = [bool]`$__has_fresh_state
                    has_fresh_state = [bool]`$__has_fresh_state
                    lastStage = 'COMPLETE'
                    last_stage = 'COMPLETE'
                    stage = 'COMPLETE'
                    errorCode = `$null
                    error_code = `$null
                }
                `$__diagJson = `$__diagRecord | ConvertTo-Json -Compress
                [System.IO.File]::AppendAllText(`$__diagFile, `$__diagJson + "`r`n", (New-Object System.Text.UTF8Encoding(`$false)))
            }
            `$elapsedTotal = if (Test-Path variable:started) { [math]::Round((`$now - `$started).TotalSeconds, 2) } else { 0 }
            if (Test-Path variable:record) {
                `$record.completed_at = `$now.ToString('o')
                `$record.elapsed_seconds = `$elapsedTotal
                if (`$__mstate -and `$__mstate.status -eq 'SUCCESS' -and `$__mstate.remoteMirror) {
                    `$record.mirror = [ordered]@{
                        status = 'SUCCESS'
                        stage = 'COMPLETE'
                        count = [int64]`$__mstate.remoteMirror.messageCount
                        messageCount = [int64]`$__mstate.remoteMirror.messageCount
                        minSentAtIso = `$__mstate.remoteMirror.minSentAtIso
                        maxSentAtIso = `$__mstate.remoteMirror.maxSentAtIso
                        sha256 = [string]`$__mstate.remoteMirror.sha256
                        sizeBytes = if (`$__mstate.remoteMirror.sizeBytes) { [int64]`$__mstate.remoteMirror.sizeBytes } elseif (`$__mstate.localSnapshot.sizeBytes) { [int64]`$__mstate.localSnapshot.sizeBytes } else { `$null }
                        integrity = if (`$__mstate.remoteMirror.integrityCheck) { [string]`$__mstate.remoteMirror.integrityCheck } else { 'ok' }
                    }
                } elseif (`$__mstate -and `$__mstate.status -eq 'SNAPSHOT_CREATED') {
                    `$record.mirror = [ordered]@{
                        status = 'SNAPSHOT_CREATED'
                        stage = 'COMPLETE'
                        count = [int64]`$__mstate.localSnapshot.messageCount
                        messageCount = [int64]`$__mstate.localSnapshot.messageCount
                        minSentAtIso = `$__mstate.localSnapshot.minSentAtIso
                        maxSentAtIso = `$__mstate.localSnapshot.maxSentAtIso
                        sha256 = [string]`$__mstate.localSnapshot.sha256
                        sizeBytes = [int64]`$__mstate.localSnapshot.sizeBytes
                        integrity = if (`$__mstate.localSnapshot.integrityCheck) { [string]`$__mstate.localSnapshot.integrityCheck } else { 'ok' }
                    }
                } else {
                    `$record.mirror = [ordered]@{
                        status = 'SUCCESS'
                    }
                }
                if (Test-Path function:Write-AtomicJson) {
                    Write-AtomicJson -FilePath `$__marker_file -Data `$record
                } else {
                    `$__tmp_m = "`$__marker_file.tmp"
                    Set-Content -LiteralPath `$__tmp_m -Value (`$record | ConvertTo-Json -Compress) -Encoding UTF8
                    Move-Item -LiteralPath `$__tmp_m -Destination `$__marker_file -Force
                }
            }
        }
    }
} else {
    Write-Host "[post-sync-mirror-hook] Skipped: `$__skip_reason"
    if ((Test-Path variable:record) -and `$record -and `$record.mirror -and `$record.mirror.status -eq 'PENDING') {
        `$record.mirror = [ordered]@{
            status = 'SKIPPED'
        }
        if (Test-Path function:Write-AtomicJson) {
            Write-AtomicJson -FilePath `$__marker_file -Data `$record
        } else {
            `$__tmp_m = "`$__marker_file.tmp"
            Set-Content -LiteralPath `$__tmp_m -Value (`$record | ConvertTo-Json -Compress) -Encoding UTF8
            Move-Item -LiteralPath `$__tmp_m -Destination `$__marker_file -Force
        }
    }
}
$HookEndMarker
"@

# Locate insertion point: after sync success marker or at end of file
$Lines = $Content -split "\r?\n"
$InsertIndex = -1

for ($i = 0; $i -lt $Lines.Count; $i++) {
    $line = $Lines[$i]
    if ($line -match '(?i)(?:Write-AtomicJson|Move-Item).*(?:\$dst|last-sync)' -or `
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
