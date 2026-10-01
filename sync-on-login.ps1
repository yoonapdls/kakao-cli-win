[CmdletBinding()]
param(
    [string]$RootDirectory,
    [string]$ApiBase = 'http://127.0.0.1:8780',
    [string]$MutexName = 'Local\KakaoCollectorSync',
    [int]$MaxAttempts = 3,
    [int]$TotalDeadlineSec = 840,
    [int]$StatusTimeoutSec = 45,
    [int]$SyncTimeoutSec = 360,
    [int]$BackoffSec = 3,
    [switch]$SkipProcessCheck,
    [switch]$SkipMirror
)

$ErrorActionPreference = 'Stop'

# Enforce valid ranges and safe bounds on inputs
$MaxAttempts = [math]::Max(1, [math]::Min(3, $MaxAttempts))
$TotalDeadlineSec = [math]::Max(1, [math]::Min(840, $TotalDeadlineSec))
$StatusTimeoutSec = [math]::Max(1, [math]::Min($TotalDeadlineSec, $StatusTimeoutSec))
$SyncTimeoutSec = [math]::Max(1, [math]::Min($TotalDeadlineSec, $SyncTimeoutSec))
$BackoffSec = [math]::Max(0, [math]::Min(60, $BackoffSec))

function Write-AtomicJson {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [object]$Data
    )
    $dir = Split-Path -Parent $FilePath
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $json = $Data | ConvertTo-Json -Compress
    $tmpName = "sync-state-" + [guid]::NewGuid().ToString('N') + ".tmp"
    $tmp = Join-Path $dir $tmpName
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tmp, $json, $utf8NoBom)
    try {
        if (Test-Path -LiteralPath $FilePath) {
            try {
                [System.IO.File]::Replace($tmp, $FilePath, $null)
            } catch {
                Move-Item -LiteralPath $tmp -Destination $FilePath -Force
            }
        } else {
            Move-Item -LiteralPath $tmp -Destination $FilePath -Force
        }
        if ((-not $RootDirectory) -and $PSScriptRoot -and (-not $FilePath.StartsWith($PSScriptRoot, [System.StringComparison]::OrdinalIgnoreCase))) {
            try {
                $localRel = if ($FilePath -match '(?i)[\\/]state[\\/]([^\\/]+)$') { Join-Path $PSScriptRoot ("data\state\" + $matches[1]) } else { $null }
                if ($localRel) {
                    $ldir = Split-Path -Parent $localRel
                    if (-not (Test-Path -LiteralPath $ldir)) { New-Item -ItemType Directory -Force -Path $ldir | Out-Null }
                    [System.IO.File]::WriteAllText($localRel, $json, $utf8NoBom)
                }
            } catch {}
        }
    } finally {
        if (Test-Path -LiteralPath $tmp) {
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-SafeErrorCode {
    param(
        [object]$ErrorItem
    )
    if ($null -eq $ErrorItem) { return 'SYNC_FAILED' }

    $msg = ""
    $ex = $null
    if ($ErrorItem -is [System.Management.Automation.ErrorRecord]) {
        $ex = $ErrorItem.Exception
        $msg = [string]$ErrorItem.Exception.Message
    } elseif ($ErrorItem -is [System.Exception]) {
        $ex = $ErrorItem
        $msg = [string]$ErrorItem.Message
    } else {
        $msg = [string]$ErrorItem
    }

    $known = @(
        'BACKEND_LAUNCH_FAILED',
        'WAITING_READY',
        'POST_SYNC_NOT_READY',
        'DEADLINE_EXCEEDED',
        'MAX_RETRIES_EXCEEDED',
        'TIMEOUT',
        'CONNECTION_FAILED',
        'SERVER_BUSY',
        'HTTP_ERROR',
        'HTTP_500',
        'HTTP_502',
        'HTTP_503',
        'HTTP_504',
        'DIR_PREPARE_FAILED',
        'UPLOAD_FAILED',
        'VERIFY_FAILED',
        'VERIFY_COMMAND_FAILED',
        'DOWNLOAD_FAILED',
        'PARSE_ERROR',
        'INTEGRITY_FAILED',
        'COUNT_MISMATCH',
        'MIN_TIMESTAMP_MISMATCH',
        'MAX_TIMESTAMP_MISMATCH',
        'HASH_MISMATCH',
        'SIZE_MISMATCH',
        'CLEANUP_FAILED',
        'REPLACE_FAILED',
        'INVALID_PARAM',
        'FILE_NOT_FOUND',
        'MIRROR_FAILED',
        'SYNC_FAILED'
    )
    if ($known -contains $msg) {
        return $msg
    }
    if ($msg -eq 'CONNECT_FAILED') {
        return 'CONNECTION_FAILED'
    }

    if ($ex -is [System.Net.WebException]) {
        $webEx = [System.Net.WebException]$ex
        if ($webEx.Status -eq [System.Net.WebExceptionStatus]::Timeout) {
            return 'TIMEOUT'
        }
        if ($webEx.Status -eq [System.Net.WebExceptionStatus]::ConnectFailure) {
            return 'CONNECTION_FAILED'
        }
        if ($webEx.Response -and ($webEx.Response -is [System.Net.HttpWebResponse])) {
            $resp = [System.Net.HttpWebResponse]$webEx.Response
            $code = [int]$resp.StatusCode
            if ($code -eq 503) { return 'SERVER_BUSY' }
            if ($code -eq 504 -or $code -eq 408) { return 'TIMEOUT' }
            try {
                $stream = $resp.GetResponseStream()
                if ($stream) {
                    $reader = New-Object System.IO.StreamReader($stream)
                    $respBody = $reader.ReadToEnd()
                    if ($respBody -match 'already running|SYNC_LOCK') {
                        return 'SERVER_BUSY'
                    }
                }
            } catch {}
            if ($code -eq 500) { return 'HTTP_500' }
            if ($code -eq 502) { return 'HTTP_502' }
            if ($code -eq 503) { return 'HTTP_503' }
            if ($code -eq 504) { return 'HTTP_504' }
            return 'HTTP_ERROR'
        }
    }

    $curr = $ex
    while ($curr) {
        if ($curr -is [System.TimeoutException]) {
            return 'TIMEOUT'
        }
        $curr = $curr.InnerException
    }

    if ($msg -match '(?i)timed?\s*out|timeout|\uCD08\uACFC') {
        return 'TIMEOUT'
    }
    if ($msg -match '(?i)connect|refused|\uC5F0\uACB0|\uAC70\uBD80') {
        return 'CONNECTION_FAILED'
    }
    if ($msg -match '503|busy|running') {
        return 'SERVER_BUSY'
    }
    if ($msg -match '504') {
        return 'TIMEOUT'
    }
    if ($msg -match '500') {
        return 'HTTP_500'
    }
    if ($msg -match '502') {
        return 'HTTP_502'
    }
    if ($msg -match 'HTTP_|\b(?:4\d\d|5\d\d)\b') {
        return 'HTTP_ERROR'
    }

    return 'SYNC_FAILED'
}

$root = if ($RootDirectory) { $RootDirectory } elseif ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'KakaoCollector' } else { Join-Path $PSScriptRoot 'data' }
$stateDir = Join-Path $root 'state'
$logDir = Join-Path $root 'logs'
$dst = Join-Path $stateDir 'last-sync.json'

New-Item -ItemType Directory -Force -Path $stateDir, $logDir | Out-Null
if (Test-Path (Join-Path $root 'STOP')) { exit 0 }

$mutex = New-Object System.Threading.Mutex($false, $MutexName)
$hasMutex = $false
try {
    $hasMutex = $mutex.WaitOne(0)
} catch {
    $hasMutex = $false
}
if (-not $hasMutex) {
    if ($mutex) { $mutex.Dispose() }
    exit 0
}

$started = Get-Date
$stage = 'startup'
$attempt = 0

try {
    # 1. Immediately record IN_PROGRESS to purge any stale SUCCESS or previous failure
    $inProgressRecord = [ordered]@{
        status = 'IN_PROGRESS'
        stage = $stage
        started_at = $started.ToString('o')
        attempts = 0
    }
    Write-AtomicJson -FilePath $dst -Data $inProgressRecord

    # 2. Startup & Health check
    $api = $ApiBase
    $launched = $false
    $ready = $false
    for ($i = 0; $i -lt 20; $i++) {
        $elapsed = ((Get-Date) - $started).TotalSeconds
        if ($elapsed -ge $TotalDeadlineSec) {
            throw 'DEADLINE_EXCEEDED'
        }

        $kakao = $SkipProcessCheck -or [bool](Get-Process KakaoTalk -ErrorAction SilentlyContinue)
        $health = $null
        try {
            $hTimeout = [math]::Max(1, [int][math]::Min(5, ($TotalDeadlineSec - $elapsed)))
            $health = Invoke-RestMethod -Uri "$api/api/health" -TimeoutSec $hTimeout
        } catch {}
        if ($kakao -and $health -and $health.ok) {
            $ready = $true
            break
        }
        if (-not $health -and -not $launched) {
            $cmd = 'cmd.exe /c cd /d D:\kakao\kakao-cli-win && .venv\Scripts\python.exe backend\server.py --host 127.0.0.1 --port 8780 > NUL 2>&1'
            $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
            if ($created.ReturnValue -ne 0) { throw 'BACKEND_LAUNCH_FAILED' }
            $launched = $true
        }
        $sleepSec = [math]::Min(15, [int][math]::Max(1, ($TotalDeadlineSec - $elapsed - 1)))
        Start-Sleep -Seconds $sleepSec
    }
    if (-not $ready) { throw 'WAITING_READY' }

    # 3. Stage: sync with bounded retry loop
    $stage = 'sync'
    $syncSuccess = $false
    $lastError = $null
    $before = $null
    $after = $null
    $sw = $null

    $transientErrors = @(
        'TIMEOUT',
        'CONNECTION_FAILED',
        'SERVER_BUSY',
        'HTTP_500',
        'HTTP_502',
        'HTTP_503',
        'HTTP_504'
    )

    while ($attempt -lt $MaxAttempts) {
        $attempt++
        $now = Get-Date
        $elapsedTotal = ($now - $started).TotalSeconds
        $remainingSec = $TotalDeadlineSec - $elapsedTotal
        if ($remainingSec -le 5) {
            $lastError = 'DEADLINE_EXCEEDED'
            throw 'DEADLINE_EXCEEDED'
        }

        try {
            # GET /api/status (before)
            $tStatusBefore = [math]::Max(1, [int][math]::Min($StatusTimeoutSec, $remainingSec))
            $before = Invoke-RestMethod -Uri "$api/api/status" -TimeoutSec $tStatusBefore

            # POST /api/sync
            $elapsedTotal = ((Get-Date) - $started).TotalSeconds
            $remainingSec = $TotalDeadlineSec - $elapsedTotal
            if ($remainingSec -le 5) {
                $lastError = 'DEADLINE_EXCEEDED'
                throw 'DEADLINE_EXCEEDED'
            }
            $tSync = [math]::Max(1, [int][math]::Min($SyncTimeoutSec, $remainingSec))
            $sw = [Diagnostics.Stopwatch]::StartNew()
            $result = Invoke-RestMethod -Uri "$api/api/sync" -Method Post -TimeoutSec $tSync
            $sw.Stop()

            # GET /api/status (after)
            $elapsedTotal = ((Get-Date) - $started).TotalSeconds
            $remainingSec = $TotalDeadlineSec - $elapsedTotal
            if ($remainingSec -le 2) {
                $lastError = 'DEADLINE_EXCEEDED'
                throw 'DEADLINE_EXCEEDED'
            }
            $tStatusAfter = [math]::Max(1, [int][math]::Min($StatusTimeoutSec, $remainingSec))
            $after = Invoke-RestMethod -Uri "$api/api/status" -TimeoutSec $tStatusAfter

            if (-not $after -or -not $after.ready) {
                throw 'POST_SYNC_NOT_READY'
            }

            $syncSuccess = $true
            break
        } catch {
            $classified = Get-SafeErrorCode -ErrorItem $_
            $lastError = $classified

            if ($classified -eq 'DEADLINE_EXCEEDED') {
                throw 'DEADLINE_EXCEEDED'
            }

            # Non-transient errors (e.g. POST_SYNC_NOT_READY, BACKEND_LAUNCH_FAILED, HTTP_ERROR, SYNC_FAILED) fail immediately without retry
            if ($transientErrors -notcontains $classified) {
                throw $classified
            }

            $now = Get-Date
            $elapsedTotal = ($now - $started).TotalSeconds
            $remainingSec = $TotalDeadlineSec - $elapsedTotal

            if ($attempt -lt $MaxAttempts -and $remainingSec -gt ($BackoffSec + 5)) {
                if ($BackoffSec -gt 0) {
                    Start-Sleep -Seconds $BackoffSec
                }
                continue
            } else {
                if ($remainingSec -le ($BackoffSec + 5)) {
                    $lastError = 'DEADLINE_EXCEEDED'
                    throw 'DEADLINE_EXCEEDED'
                }
                throw $classified
            }
        }
    }

    if (-not $syncSuccess) {
        if (-not $lastError) { $lastError = 'MAX_RETRIES_EXCEEDED' }
        throw $lastError
    }

    $now = Get-Date
    $elapsedSync = if ($sw) { [math]::Round($sw.Elapsed.TotalSeconds, 2) } else { 0 }
    $localRecord = [ordered]@{
        status = 'SUCCESS'
        ready = [bool]$after.ready
        messages_before = [int64]$before.counts.messages
        messages_after = [int64]$after.counts.messages
        db_updated_kst = [string]$after.dbUpdatedKst
        elapsed_seconds = $elapsedSync
    }
    $mirrorRecord = [ordered]@{
        status = if ($SkipMirror) { 'SKIPPED' } else { 'PENDING' }
    }
    $record = [ordered]@{
        status = 'SUCCESS'
        stage = 'COMPLETE'
        started_at = $started.ToString('o')
        completed_at = $now.ToString('o')
        elapsed_seconds = $elapsedSync
        ready = [bool]$after.ready
        attempts = $attempt
        messages_before = [int64]$before.counts.messages
        messages_after = [int64]$after.counts.messages
        db_updated_kst = [string]$after.dbUpdatedKst
        local = $localRecord
        mirror = $mirrorRecord
    }
    Write-AtomicJson -FilePath $dst -Data $record

    if (-not $SkipMirror) {
        $stage = 'mirror'
# >>> kakao-post-sync-mirror >>>
# Post-sync mirror trigger: fail-closed, executes only when sync succeeded and marker is current
$__can_run_mirror = $false
$__skip_reason = $null

try {
    # Resolve marker path: check script variables or fallback to standard location
    $__marker_file = $null
    if (Test-Path variable:MarkerFile) {
        if ($MarkerFile) { $__marker_file = $MarkerFile }
    } elseif (Test-Path variable:dst) {
        if ($dst) { $__marker_file = $dst }
    } elseif (Test-Path variable:stateDir) {
        if ($stateDir) { $__marker_file = (Join-Path $stateDir "last-sync.json") }
    } elseif ($PSScriptRoot -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot "state\last-sync.json"))) {
        $__marker_file = Join-Path $PSScriptRoot "state\last-sync.json"
    } elseif ($env:LOCALAPPDATA) {
        $__marker_file = Join-Path $env:LOCALAPPDATA "KakaoCollector\state\last-sync.json"
    }

    if (-not $__marker_file -or -not (Test-Path -LiteralPath $__marker_file)) {
        $__skip_reason = "MARKER_NOT_FOUND"
    } else {
        $__raw = Get-Content -LiteralPath $__marker_file -Raw -Encoding UTF8
        $__marker = $null
        try {
            $__marker = $__raw | ConvertFrom-Json
        } catch {
            $__skip_reason = "PARSE_ERROR"
        }

        if (-not $__skip_reason) {
            if ($__marker -and ($__marker.PSObject.Properties['status'])) {
                $__status_ok = ($__marker.status -eq "SUCCESS")
                $__ready_ok = ($null -eq $__marker.ready -or [bool]$__marker.ready -eq $true)

                if (-not ($__status_ok -and $__ready_ok)) {
                    $__skip_reason = "SYNC_NOT_SUCCESS"
                } else {
                    $__is_current = $false
                    if ((Test-Path variable:started) -and ($started -is [datetime])) {
                        if ($__marker.started_at -and ($__marker.started_at -eq $started.ToString("o"))) {
                            $__is_current = $true
                        } elseif ($__marker.completed_at) {
                            try {
                                $__comp = [datetimeoffset]::Parse($__marker.completed_at)
                                $__started_dto = [datetimeoffset]$started
                                if ($__comp -ge $__started_dto.AddSeconds(-2)) {
                                    $__is_current = $true
                                }
                            } catch {}
                        }
                    } else {
                        $__freshness_ref = $null
                        if ($__marker.completed_at) {
                            try { $__freshness_ref = [datetimeoffset]::Parse($__marker.completed_at) } catch {}
                        }
                        if (-not $__freshness_ref) {
                            try {
                                $__raw_time = (Get-Item -LiteralPath $__marker_file).LastWriteTime
                                $__freshness_ref = [datetimeoffset]$__raw_time
                            } catch {}
                        }
                        if ($__freshness_ref) {
                            $__now_dto = [datetimeoffset]::Now
                            $__age = ($__now_dto - $__freshness_ref).TotalSeconds
                            if ($__age -ge -60 -and $__age -le 300) {
                                $__is_current = $true
                            }
                        }
                    }

                    if ($__is_current) {
                        $__can_run_mirror = $true
                    } else {
                        $__skip_reason = "STALE_MARKER"
                    }
                }
            } else {
                $__skip_reason = "PARSE_ERROR"
            }
        }
    }
} catch {
    $__can_run_mirror = $false
    $__skip_reason = "HOOK_EXECUTION_ERROR"
}

if ($__can_run_mirror) {
    Write-Host "[post-sync-mirror-hook] Triggering post-sync mirror (-UploadOnly)..."
    $__mirror_runner = $null
    $__runner_candidates = @()
    if (Test-Path variable:root) { $__runner_candidates += (Join-Path $root "post-sync-mirror.ps1") }
    if (Test-Path variable:PSScriptRoot) { $__runner_candidates += (Join-Path $PSScriptRoot "post-sync-mirror.ps1") }
    $__runner_candidates += "D:\kakao\kakao-cli-win\post-sync-mirror.ps1"
    foreach ($__cand in $__runner_candidates) {
        if ($__cand -and (Test-Path -LiteralPath $__cand)) {
            $__mirror_runner = $__cand
            break
        }
    }

    if ($__mirror_runner) {
        $__prev_ea = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $env:KAKAO_CALLER_SYNC = "1"
            $__mirror_output = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$__mirror_runner" -UploadOnly 2>&1
            $__mirror_exit = $LASTEXITCODE
        } finally {
            Remove-Item env:KAKAO_CALLER_SYNC -ErrorAction SilentlyContinue
            $ErrorActionPreference = $__prev_ea
        }
        $__mirror_output | ForEach-Object { Write-Host $_ }

        $__mstate = $null
        $__has_fresh_state = $false
        $__state_candidates = @()
        if (Test-Path variable:root) { $__state_candidates += (Join-Path $root "output") }
        if (Test-Path variable:PSScriptRoot) { $__state_candidates += (Join-Path $PSScriptRoot "data\output") }
        $__state_candidates += "D:\kakao\kakao-cli-win\data\output"
        foreach ($__sc in $__state_candidates) {
            if ($__sc -and (Test-Path -LiteralPath $__sc)) {
                $__sfiles = Get-ChildItem -Path $__sc -Filter "mirror_state.json" -Recurse -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending
                if ($__sfiles -and $__sfiles.Count -gt 0) {
                    $__cand_file = $__sfiles[0]
                    $__is_recent = ($__cand_file.LastWriteTime -ge $started.AddSeconds(-60))
                    if ($__is_recent) {
                        try {
                            $__parsed = Get-Content -LiteralPath $__cand_file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                            if ($__mirror_exit -ne 0) {
                                if ($__parsed -and $__parsed.status -ne 'SUCCESS' -and $__parsed.lastStage -ne 'COMPLETE') {
                                    $__mstate = $__parsed
                                    $__has_fresh_state = $true
                                    break
                                }
                            } else {
                                if ($__parsed -and ($__parsed.status -eq 'SUCCESS' -or $__parsed.status -eq 'SNAPSHOT_CREATED')) {
                                    $__mstate = $__parsed
                                    $__has_fresh_state = $true
                                    break
                                }
                            }
                        } catch {}
                    }
                }
            }
        }

        if ($__mirror_exit -ne 0) {
            $__m_stage = if ($__has_fresh_state -and $__mstate -and $__mstate.lastStage -and $__mstate.lastStage -ne 'COMPLETE') {
                [string]$__mstate.lastStage
            } else {
                'mirror'
            }
            $__cand_err = if ($__has_fresh_state -and $__mstate -and $__mstate.errorCode) {
                [string]$__mstate.errorCode
            } else {
                'MIRROR_FAILED'
            }
            $__safe_err = Get-SafeErrorCode -ErrorItem $__cand_err
            if ($__safe_err -eq 'SYNC_FAILED' -or [string]::IsNullOrEmpty($__safe_err)) {
                $__safe_err = 'MIRROR_FAILED'
            }

            $now = Get-Date
            $diagFile = Join-Path $logDir ("mirror-diag-{0}.jsonl" -f ($now.ToString('yyyy-MM-dd')))
            $diagRecord = [ordered]@{
                timestamp = $now.ToString('o')
                child_exit_code = [int]$__mirror_exit
                exit_code = [int]$__mirror_exit
                fresh_state = [bool]$__has_fresh_state
                has_fresh_state = [bool]$__has_fresh_state
                lastStage = [string]$__m_stage
                last_stage = [string]$__m_stage
                stage = [string]$__m_stage
                errorCode = [string]$__safe_err
                error_code = [string]$__safe_err
            }
            $diagJson = $diagRecord | ConvertTo-Json -Compress
            [System.IO.File]::AppendAllText($diagFile, $diagJson + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
            if ((-not $RootDirectory) -and $PSScriptRoot -and (-not $diagFile.StartsWith($PSScriptRoot, [System.StringComparison]::OrdinalIgnoreCase))) {
                try {
                    $localDiag = Join-Path $PSScriptRoot ("data\logs\" + (Split-Path -Leaf $diagFile))
                    $ldir = Split-Path -Parent $localDiag
                    if (-not (Test-Path -LiteralPath $ldir)) { New-Item -ItemType Directory -Force -Path $ldir | Out-Null }
                    [System.IO.File]::AppendAllText($localDiag, $diagJson + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
                } catch {}
            }

            $stage = $__m_stage
            throw $__safe_err
        } else {
            $now = Get-Date
            $diagFile = Join-Path $logDir ("mirror-diag-{0}.jsonl" -f ($now.ToString('yyyy-MM-dd')))
            $diagRecord = [ordered]@{
                timestamp = $now.ToString('o')
                child_exit_code = 0
                exit_code = 0
                fresh_state = [bool]$__has_fresh_state
                has_fresh_state = [bool]$__has_fresh_state
                lastStage = 'COMPLETE'
                last_stage = 'COMPLETE'
                stage = 'COMPLETE'
                errorCode = $null
                error_code = $null
            }
            $diagJson = $diagRecord | ConvertTo-Json -Compress
            [System.IO.File]::AppendAllText($diagFile, $diagJson + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
            if ((-not $RootDirectory) -and $PSScriptRoot -and (-not $diagFile.StartsWith($PSScriptRoot, [System.StringComparison]::OrdinalIgnoreCase))) {
                try {
                    $localDiag = Join-Path $PSScriptRoot ("data\logs\" + (Split-Path -Leaf $diagFile))
                    $ldir = Split-Path -Parent $localDiag
                    if (-not (Test-Path -LiteralPath $ldir)) { New-Item -ItemType Directory -Force -Path $ldir | Out-Null }
                    [System.IO.File]::AppendAllText($localDiag, $diagJson + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
                } catch {}
            }

            $elapsedTotal = [math]::Round(($now - $started).TotalSeconds, 2)
            $record.completed_at = $now.ToString('o')
            $record.elapsed_seconds = $elapsedTotal
            if ($__mstate -and $__mstate.status -eq 'SUCCESS' -and $__mstate.remoteMirror) {
                $record.mirror = [ordered]@{
                    status = 'SUCCESS'
                    stage = 'COMPLETE'
                    count = [int64]$__mstate.remoteMirror.messageCount
                    messageCount = [int64]$__mstate.remoteMirror.messageCount
                    minSentAtIso = $__mstate.remoteMirror.minSentAtIso
                    maxSentAtIso = $__mstate.remoteMirror.maxSentAtIso
                    sha256 = [string]$__mstate.remoteMirror.sha256
                    sizeBytes = if ($__mstate.remoteMirror.sizeBytes) { [int64]$__mstate.remoteMirror.sizeBytes } elseif ($__mstate.localSnapshot.sizeBytes) { [int64]$__mstate.localSnapshot.sizeBytes } else { $null }
                    integrity = if ($__mstate.remoteMirror.integrityCheck) { [string]$__mstate.remoteMirror.integrityCheck } else { 'ok' }
                }
            } elseif ($__mstate -and $__mstate.status -eq 'SNAPSHOT_CREATED') {
                $record.mirror = [ordered]@{
                    status = 'SNAPSHOT_CREATED'
                    stage = 'COMPLETE'
                    count = [int64]$__mstate.localSnapshot.messageCount
                    messageCount = [int64]$__mstate.localSnapshot.messageCount
                    minSentAtIso = $__mstate.localSnapshot.minSentAtIso
                    maxSentAtIso = $__mstate.localSnapshot.maxSentAtIso
                    sha256 = [string]$__mstate.localSnapshot.sha256
                    sizeBytes = [int64]$__mstate.localSnapshot.sizeBytes
                    integrity = if ($__mstate.localSnapshot.integrityCheck) { [string]$__mstate.localSnapshot.integrityCheck } else { 'ok' }
                }
            } else {
                $record.mirror = [ordered]@{
                    status = 'SUCCESS'
                }
            }
            Write-AtomicJson -FilePath $dst -Data $record
        }
    }
} else {
    Write-Host "[post-sync-mirror-hook] Skipped: $__skip_reason"
    if ($record -and $record.mirror -and $record.mirror.status -eq 'PENDING') {
        $record.mirror = [ordered]@{
            status = 'SKIPPED'
        }
        Write-AtomicJson -FilePath $dst -Data $record
    }
}
# <<< kakao-post-sync-mirror <<<
    }

    $logFile = Join-Path $logDir ("worker-{0}.jsonl" -f ($now.ToString('yyyy-MM-dd')))
    $logJson = $record | ConvertTo-Json -Compress
    [System.IO.File]::AppendAllText($logFile, $logJson + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
    exit 0
}
catch {
    $now = Get-Date
    $elapsedTotal = [math]::Round(($now - $started).TotalSeconds, 2)
    $safeError = Get-SafeErrorCode -ErrorItem $_

    $isMirrorErr = ($syncSuccess -eq $true -or $stage -like 'mirror*' -or $stage -like 'REMOTE_*' -or $stage -in @('INIT', 'DIR_PREPARE', 'SNAPSHOT', 'UPLOAD', 'REPLACE', 'CLEANUP'))
    if ($isMirrorErr -and ($safeError -eq 'SYNC_FAILED' -or [string]::IsNullOrEmpty($safeError))) {
        $safeError = 'MIRROR_FAILED'
    }

    $localStatus = if ($isMirrorErr) { 'SUCCESS' } else { 'FAILED' }
    $mirrorStatus = if ($isMirrorErr) { 'FAILED' } else { 'SKIPPED' }

    $record = [ordered]@{
        status = 'FAILED'
        stage = $stage
        error = $safeError
        error_code = $safeError
        started_at = $started.ToString('o')
        completed_at = $now.ToString('o')
        elapsed_seconds = $elapsedTotal
        attempts = [math]::Max(1, $attempt)
        local = [ordered]@{
            status = $localStatus
            stage = if ($localStatus -eq 'SUCCESS') { 'COMPLETE' } else { $stage }
            error = if ($localStatus -eq 'SUCCESS') { $null } else { $safeError }
            error_code = if ($localStatus -eq 'SUCCESS') { $null } else { $safeError }
            messages_before = if ($before -and $before.counts) { [int64]$before.counts.messages } else { 0 }
            messages_after = if ($after -and $after.counts) { [int64]$after.counts.messages } else { 0 }
        }
        mirror = [ordered]@{
            status = $mirrorStatus
            stage = if ($mirrorStatus -eq 'FAILED') { $stage } else { $null }
            error = if ($mirrorStatus -eq 'FAILED') { $safeError } else { $null }
            error_code = if ($mirrorStatus -eq 'FAILED') { $safeError } else { $null }
        }
    }
    Write-AtomicJson -FilePath $dst -Data $record

    $logFile = Join-Path $logDir ("worker-{0}.jsonl" -f ($now.ToString('yyyy-MM-dd')))
    $logJson = $record | ConvertTo-Json -Compress
    [System.IO.File]::AppendAllText($logFile, $logJson + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
    exit 1
}
finally {
    if ($hasMutex) {
        try { $mutex.ReleaseMutex() } catch {}
    }
    if ($mutex) {
        $mutex.Dispose()
    }
}
