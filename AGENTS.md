# Kakao CLI Windows - Project Guidelines & Invariants

## Domain Invariants

1. **Room Classification Invariant**:
   - `rooms.type`: Preserves the raw KakaoTalk room type string verbatim.
   - `rooms.isOpenChat`: `1` for open chats (`OM`, `OD`), `0` for normal chats (`MultiChat`, `DirectChat`, `PlusChat`, `MemoChat`), `NULL` for unknown or null type.
   - `rooms.roomCategory`:
     - `OM` -> `open_group`
     - `OD` -> `open_direct`
     - `MultiChat` -> `normal_group`
     - `DirectChat` -> `normal_direct`
     - `PlusChat` -> `channel`
     - `MemoChat` -> `memo`
     - other / NULL -> `unknown`
   - APIs `/api/rooms` and `/api/rooms/{chatId}` must expose both `isOpenChat` and `roomCategory`.

2. **Message Reply Relationship Invariant**:
   - Reply relationship and thread metadata must be preserved in `messages`:
     - `threadId`: Parent message logId for threads (0 normalized to NULL).
     - `threadScope`: Scope integer from supplement JSON.
     - `prevLogId`: Sequential predecessor pointer in chat.
     - `referer`: Client routing / entry point flag.
     - `attachmentSrcLogId`: Quoted message source logId from `attachment.src_logId`.
     - `replyToLogId`: Normalized quote/reply target logId.
     - `supplementJson`, `attachmentJson`: Verbatim stored JSON strings.

3. **Data Integrity & Security**:
   - Consolidated database `messages_v2.sqlite` migrations must never mutate decrypted source databases (`v2_decrypted/*.sqlite`).
   - Sensitive payloads (chat message text, usernames, room titles, API keys) must never be logged or output in verification reports. Only de-identified counts and schema metadata are permitted.

4. **Mirror & Backup Pipeline Invariants**:
   - `StateMarker`: Atomic persistence via sibling temp file and `os.replace`. On transition to `SUCCESS` (or `SNAPSHOT_CREATED`), purge failure fields (`errorCode`, `lastErrorKst`, `error`) and record `lastStage=COMPLETE`. On new execution start (`IN_PROGRESS`), purge stale failure fields (`errorCode`, `lastErrorKst`, `error`) and reset `lastStage` to `INIT`.
   - `ProcessLock`: Atomic file lock via `os.O_CREAT | os.O_EXCL` with safe stale PID checking and break mechanism.
   - Remote Verification:
     - Remote SSH commands must produce no stdout to prevent remote SSH channel hangs.
     - Remote SSH Command Wrapper & Channel Closure: All remote SSH commands must be wrapped with POSIX-safe wrapper `sh -c <shlex.quote(command + "; rc=$?; exit $rc")>` to enforce explicit process exit (closing SSH channels even on complex commands with redirected/closed output) while strictly preserving the original command exit code.
     - Remote verification: Uses host `python3` standard library one-shot script (`file:...mode=ro`, `sqlite3`, `hashlib`, `json`) without external dependencies or `sqlite3` CLI binary. Writes integrity (`PRAGMA integrity_check`), message count, min/max sentAtIso, and SHA-256 hash with mode 600 to an unpredictable temporary JSON meta file in the private remote directory. All stdout and stderr are redirected to `/dev/null` (`> /dev/null 2>&1`) to prevent SSH channel hangs, strictly preserving the original exit code.
     - Remote verification failure: Remote `python3` absence (exit 127) or database error exits immediately non-zero, triggering `VERIFY_COMMAND_FAILED`, preserving `current`, and cleaning up `partial` and `meta`.
     - Meta file is downloaded via scp and strictly parsed locally, comparing all three message metrics (`count`, `minSentAtIso`, `maxSentAtIso`) and SHA-256 hash before atomic `mv`.
     - Local Windows temporary meta file is immediately deleted. Remote meta file cleanup is consolidated directly into the remote replace command on success (preventing separate SSH network roundtrip timeouts), and is cleaned up with stdout-free `rm -f` alongside partial/gz files on all failure paths (zero residue).
     - Partial file cleanup (`rm -f .partial`) on ANY verification, parse, timeout, download, cleanup, or transfer failure.
     - Cleanup failure must never cause `current` replacement.
     - Large DB Verification Timeout: Large databases (~400MB / 398MB+) require significant I/O for remote `PRAGMA integrity_check`, count/min-max aggregation, and SHA-256 calculation; `verify` stage timeout is conservatively set to 600s to avoid premature `REMOTE_VERIFY/TIMEOUT` while retaining existing limits for other stages (prepare 30s, upload 300s, download 60s, replace/cleanup 30s).
   - Information Security: Prohibit exposing file paths, remote host target, user IDs, or raw error messages in stdout, state markers, or return dictionaries. Store only sanitized `stage` and `errorCode`.
   - Post-Sync Hook Invariant: Post-sync trigger must be fail-closed (require valid `%LOCALAPPDATA%\KakaoCollector\state\last-sync.json` with explicit `status: SUCCESS` matching current execution or within freshness window). If marker is missing, malformed, failed, or stale, skip without executing mirror. When triggered, invoke runner exactly once with `-UploadOnly` to prevent redundant `v2sync` loops.
   - Remote Ownership, Permissions & Rollback Invariant:
     - `remote_dir` (`current` directory) must be owned by approved runtime UID:GID (default `10000:10000`, or resolved from CLI/env/config `KAKAO_VPS_UID`/`KAKAO_VPS_GID`, validated as positive integer `> 0`) with mode `700`.
     - `remote_current` (`messages_v2.sqlite`) and `remote_partial` must be owned by the resolved UID:GID with mode `600`.
     - Pre-replace verification: Both directory (`700`) and partial file (`600`) ownership and permissions must be verified via `stat -c '%u:%g:%a'` prior to atomic replacement.
     - Fail-closed replacement and rollback: Replace stage must mv existing `current` to rollback backup before placing new DB; if chown, chmod, or stat verification fails after move, new DB is deleted, previous `current` is restored from rollback backup, and process exits non-zero (`REPLACE_FAILED`).
     - Archive preservation: Recursive `chown -R` is strictly prohibited to guarantee existing archives' owner, mode, and contents remain immutable.
   - Gzip Transfer & Verification Pipeline Invariant:
     - SQLite read-only snapshot (`LOCAL_SNAPSHOT`) is locally compressed with standard gzip level 6 (`LOCAL_COMPRESS`) to sibling `.gz`.
     - Remote upload targets `.partial.gz`. On remote, decompresses into `.partial` candidate with UID:GID and mode `600`, and cleans up `.partial.gz` via `rm -f`.
     - Decompressed remote candidate SHA-256 is strictly compared against the uncompressed local snapshot SHA-256 in `REMOTE_VERIFY` before atomic replacement.
     - On success and on ANY failure, local `.gz` temp files, remote `.partial.gz`, and extracted candidate `.partial` are cleaned up with stdout-free `rm -f`. Current DB and source live DB remain immutable.
   - Monotonic Deadline Budget Invariant:
     - Entire mirror execution is bounded by a single monotonic deadline (`total_deadline_sec`, clamped `1..840` seconds, default 840s).
     - All stages (`LOCAL_SNAPSHOT`, `LOCAL_COMPRESS`, `REMOTE_PREPARE`, `REMOTE_UPLOAD`, `REMOTE_DECOMPRESS`, `REMOTE_VERIFY`, `REMOTE_REPLACE`, `REMOTE_CHECK`) share the remaining monotonic deadline budget without stacking stage timeouts exceeding the overall budget.
   - Mirror State Marker Local/Mirror Separation Invariant:
     - On failure, `mirror_state.json` cleanly separates local snapshot/collection success (`local: {status: SUCCESS, messageCount: ...}`) from mirror failure (`mirror: {status: FAILED, stage: ..., errorCode: ...}`).
     - Strictly prohibits exposing raw message payloads, room titles, user names, passwords, or absolute filesystem paths.

5. **Login Sync Worker & Deadline Invariants**:
   - `StateMarker Lifecycle`: At the start of execution, write `IN_PROGRESS` atomically (`last-sync.json`) via sibling temporary file and `[System.IO.File]::Replace` (or `Move-Item -Force`) to immediately purge stale `SUCCESS`. On terminal failure, overwrite `last-sync.json` atomically with `FAILED`.
   - `Sanitized Error Allowlisting`: `error` and `error_code` fields in `last-sync.json` and `worker-*.jsonl` must strictly record allowlisted safe tokens (`TIMEOUT`, `CONNECTION_FAILED`, `SERVER_BUSY`, `HTTP_ERROR`, `HTTP_500`, `HTTP_502`, `HTTP_503`, `HTTP_504`, `WAITING_READY`, `BACKEND_LAUNCH_FAILED`, `POST_SYNC_NOT_READY`, `DEADLINE_EXCEEDED`, `MAX_RETRIES_EXCEEDED`, `SYNC_FAILED`). Unknown HTTP status codes outside explicit status tokens must normalize to `HTTP_ERROR`. Never leak raw localized exception strings (e.g. "작업 시간을 초과했습니다"), URLs, file paths, chat IDs, or usernames.
   - `Watcher Schema Compatibility`: `last-sync.json` must expose `status`, `stage`, `started_at`, `completed_at` (ISO timestamp dedupe key for VPS watcher), `elapsed_seconds`, `attempts`, and for failures `error`/`error_code`. On `SUCCESS`, expose `ready`, `messages_before`, `messages_after`, `db_updated_kst`.
   - `Bounded Retries & Budget-Aware Deadlines`: In `stage=sync`, retry transient `/api/status` and `POST /api/sync` errors up to 3 attempts with short backoff under v2sync idempotency and server non-blocking lock. The `-MaxAttempts` parameter is strictly clamped to `1..3`. The total execution deadline `-TotalDeadlineSec` is strictly bounded to `1..840` seconds (< 15 min scheduled task limit, default 14 min / 840s). Request timeouts (`StatusTimeoutSec`, `SyncTimeoutSec`) and backoff (`BackoffSec`) are normalized to safe bounded ranges. Only explicitly allowlisted transient errors (`TIMEOUT`, `CONNECTION_FAILED`, `SERVER_BUSY`, `HTTP_500`, `HTTP_502`, `HTTP_503`, `HTTP_504`) are retried; non-transient errors (`BACKEND_LAUNCH_FAILED`, `POST_SYNC_NOT_READY`, `HTTP_ERROR`, `SYNC_FAILED`, `DEADLINE_EXCEEDED`) fail immediately without retry.
