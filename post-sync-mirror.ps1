# post-sync-mirror.ps1
# Windows KakaoTalk Post-Sync Snapshot & VPS Mirror Runner
[CmdletBinding()]
param(
    [switch]$UploadOnly,
    [switch]$Disable,
    [switch]$Enable,
    [switch]$Status,
    [string]$VpsTarget,
    [string]$VpsDir,
    [System.Nullable[int]]$VpsUid = $null,
    [System.Nullable[int]]$VpsGid = $null,
    [string]$SshKey,
    [int]$SshPort
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python.exe"
}

# Ensure Python can resolve kwin and project packages regardless of working directory
if ($env:PYTHONPATH) {
    if ($env:PYTHONPATH -notlike "*$ProjectRoot*") {
        $env:PYTHONPATH = "$ProjectRoot;$env:PYTHONPATH"
    }
} else {
    $env:PYTHONPATH = $ProjectRoot
}

$StopMarker = Join-Path $ProjectRoot "data\config\STOP_MIRROR"

if ($Disable) {
    New-Item -ItemType Directory -Force -Path (Split-Path $StopMarker) | Out-Null
    $Stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss KST")
    Set-Content -LiteralPath $StopMarker -Value "Disabled on $Stamp" -Encoding UTF8
    Write-Host "[post-sync-mirror] Mirroring disabled (STOP marker created)."
    exit 0
}

if ($Enable) {
    if (Test-Path -LiteralPath $StopMarker) {
        Remove-Item -LiteralPath $StopMarker -Force
        Write-Host "[post-sync-mirror] Mirroring enabled (STOP marker removed)"
    } else {
        Write-Host "[post-sync-mirror] Mirroring already enabled (no STOP marker)"
    }
    exit 0
}

if ($Status) {
    Write-Host "=== Kakao Post-Sync Mirror Status ==="
    $StopActive = Test-Path -LiteralPath $StopMarker
    Write-Host "STOP Marker  : $(if ($StopActive) { 'Active' } else { 'Inactive' })"
    $StateFiles = Get-ChildItem -Path (Join-Path $ProjectRoot "data\output") -Filter "mirror_state.json" -Recurse -ErrorAction SilentlyContinue
    if ($StateFiles) {
        foreach ($sf in $StateFiles) {
            try {
                $raw = Get-Content -LiteralPath $sf.FullName -Raw | ConvertFrom-Json
                Write-Host "Mirror Status: $($raw.status)"
                if ($raw.lastAttemptKst) { Write-Host "Last Attempt : $($raw.lastAttemptKst)" }
                if ($raw.lastSuccessKst) { Write-Host "Last Success : $($raw.lastSuccessKst)" }
                if ($raw.localSnapshot -and $raw.localSnapshot.messageCount -ne $null) {
                    Write-Host "Message Count: $($raw.localSnapshot.messageCount)"
                }
                if ($raw.lastStage) { Write-Host "Last Stage   : $($raw.lastStage)" }
                if ($raw.errorCode -and $raw.status -notin @('SUCCESS', 'SNAPSHOT_CREATED')) { Write-Host "Error Code   : $($raw.errorCode)" }
            } catch {
                Write-Host "Mirror Status: UNKNOWN"
            }
        }
    } else {
        Write-Host "Mirror State : No state file found"
    }
    exit 0
}

# Parameter validation: VpsUid and VpsGid must be positive integers (> 0) if specified
if ($PSBoundParameters.ContainsKey('VpsUid')) {
    if ($null -eq $VpsUid -or $VpsUid -le 0) {
        Write-Error "[post-sync-mirror] VpsUid must be a positive integer (> 0), got: $VpsUid"
        exit 1
    }
}
if ($PSBoundParameters.ContainsKey('VpsGid')) {
    if ($null -eq $VpsGid -or $VpsGid -le 0) {
        Write-Error "[post-sync-mirror] VpsGid must be a positive integer (> 0), got: $VpsGid"
        exit 1
    }
}

if (Test-Path -LiteralPath $StopMarker) {
    Write-Host "[post-sync-mirror] STOP marker detected. Mirroring skipped."
    exit 0
}

Push-Location $ProjectRoot
try {
    # Step 1: Run sync unless UploadOnly was requested
    if (-not $UploadOnly) {
        Write-Host "[post-sync-mirror] Running kwin v2sync..."
        & $PythonExe -m kwin v2sync
        if ($LASTEXITCODE -ne 0) {
            Write-Error "[post-sync-mirror] kwin v2sync failed with exit code $LASTEXITCODE"
            exit $LASTEXITCODE
        }
    }

    # Step 2: Run snapshot & mirror
    Write-Host "[post-sync-mirror] Running kwin v2mirror..."
    $MirrorArgs = @("-m", "kwin", "v2mirror")
    if ($UploadOnly) { $MirrorArgs += "--upload-only" }
    if ($VpsTarget) { $MirrorArgs += @("--vps-target", $VpsTarget) }
    if ($VpsDir) { $MirrorArgs += @("--vps-dir", $VpsDir) }
    if ($PSBoundParameters.ContainsKey('VpsUid')) { $MirrorArgs += @("--vps-uid", "$VpsUid") }
    if ($PSBoundParameters.ContainsKey('VpsGid')) { $MirrorArgs += @("--vps-gid", "$VpsGid") }
    if ($SshKey) { $MirrorArgs += @("--ssh-key", $SshKey) }
    if ($SshPort) { $MirrorArgs += @("--ssh-port", "$SshPort") }

    & $PythonExe @MirrorArgs
    $MirrorExit = $LASTEXITCODE
    if ($MirrorExit -ne 0) {
        if (-not $env:KAKAO_CALLER_SYNC) {
            $now = Get-Date
            $logDir = Join-Path $ProjectRoot "data\logs"
            if (Test-Path -LiteralPath $logDir) {
                $diagFile = Join-Path $logDir ("mirror-diag-{0}.jsonl" -f ($now.ToString('yyyy-MM-dd')))
                $diagRecord = [ordered]@{
                    timestamp = $now.ToString('o')
                    child_exit_code = [int]$MirrorExit
                    exit_code = [int]$MirrorExit
                    fresh_state = $false
                    has_fresh_state = $false
                    lastStage = 'mirror'
                    last_stage = 'mirror'
                    stage = 'mirror'
                    errorCode = 'MIRROR_FAILED'
                    error_code = 'MIRROR_FAILED'
                }
                $diagJson = $diagRecord | ConvertTo-Json -Compress
                [System.IO.File]::AppendAllText($diagFile, $diagJson + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
            }
        }
        Write-Error "[post-sync-mirror] kwin v2mirror failed with exit code $MirrorExit"
        exit $MirrorExit
    }

    if (-not $env:KAKAO_CALLER_SYNC) {
        $now = Get-Date
        $logDir = Join-Path $ProjectRoot "data\logs"
        if (Test-Path -LiteralPath $logDir) {
            $diagFile = Join-Path $logDir ("mirror-diag-{0}.jsonl" -f ($now.ToString('yyyy-MM-dd')))
            $diagRecord = [ordered]@{
                timestamp = $now.ToString('o')
                child_exit_code = 0
                exit_code = 0
                fresh_state = $true
                has_fresh_state = $true
                lastStage = 'COMPLETE'
                last_stage = 'COMPLETE'
                stage = 'COMPLETE'
                errorCode = $null
                error_code = $null
            }
            $diagJson = $diagRecord | ConvertTo-Json -Compress
            [System.IO.File]::AppendAllText($diagFile, $diagJson + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
        }

        # Update last-sync.json if it exists
        $stateCandidate = Join-Path $ProjectRoot "data\state\last-sync.json"
        if (Test-Path -LiteralPath $stateCandidate) {
            $sf = Get-ChildItem -Path (Join-Path $ProjectRoot "data\output") -Filter "mirror_state.json" -Recurse -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if ($sf) {
                try {
                    $mstate = Get-Content -LiteralPath $sf.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                    if ($mstate -and $mstate.status -eq 'SUCCESS' -and $mstate.remoteMirror) {
                        $rec = [ordered]@{
                            status = 'SUCCESS'
                            stage = 'COMPLETE'
                            started_at = if ($mstate.lastAttemptKst) { $mstate.lastAttemptKst } else { $now.ToString('o') }
                            completed_at = $now.ToString('o')
                            elapsed_seconds = 0
                            attempts = 1
                            local = [ordered]@{
                                status = 'SUCCESS'
                                stage = 'COMPLETE'
                                error = $null
                                error_code = $null
                                messages_before = [int64]$mstate.remoteMirror.messageCount
                                messages_after = [int64]$mstate.remoteMirror.messageCount
                            }
                            mirror = [ordered]@{
                                status = 'SUCCESS'
                                stage = 'COMPLETE'
                                count = [int64]$mstate.remoteMirror.messageCount
                                messageCount = [int64]$mstate.remoteMirror.messageCount
                                minSentAtIso = $mstate.remoteMirror.minSentAtIso
                                maxSentAtIso = $mstate.remoteMirror.maxSentAtIso
                                sha256 = [string]$mstate.remoteMirror.sha256
                                sizeBytes = if ($mstate.remoteMirror.sizeBytes) { [int64]$mstate.remoteMirror.sizeBytes } elseif ($mstate.localSnapshot.sizeBytes) { [int64]$mstate.localSnapshot.sizeBytes } else { $null }
                                integrity = if ($mstate.remoteMirror.integrityCheck) { [string]$mstate.remoteMirror.integrityCheck } else { 'ok' }
                            }
                        }
                        $tmpS = "$stateCandidate.tmp"
                        [System.IO.File]::WriteAllText($tmpS, ($rec | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))
                        Move-Item -LiteralPath $tmpS -Destination $stateCandidate -Force
                    }
                } catch {}
            }
        }
    }
} finally {
    Pop-Location
}

Write-Host "[post-sync-mirror] Mirror process completed successfully."
exit 0
