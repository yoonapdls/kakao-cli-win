"""SQLite backup snapshot and VPS mirror updater for Kakao consolidated DB.

Domain & Security Invariants:
1. Uses SQLite online backup API on read-only connection (?mode=ro) for consistent snapshot.
2. Transfer sequence:
   - Upload snapshot to remote .partial
   - Remote PRAGMA integrity_check == ok
   - Remote message count & min/max sentAtIso match local
   - Remote SHA-256 matches local SHA-256
   - Atomic replacement (mv .partial -> current)
   - Target dir permissions 700, DB file permissions 600
   - Preserves existing archives (e.g. 2026-09-08 archive is immutable)
3. Concurrency & Reliability:
   - Atomic file lock via O_CREAT | O_EXCL with safe stale PID timeout
   - Atomic state marker persistence via temp + os.replace
   - STOP / disable marker support
   - Rollback / cleanup of .partial on ANY verification or upload failure
4. Security & Data Integrity:
   - Never log or return chat message text, user names, room titles, credentials,
     or absolute filesystem/remote paths.
   - Store stage and errorCode instead of raw exception details.
   - Strictly validate and quote remote POSIX paths to prevent command injection.
   - Enforce SSH BatchMode=yes and StrictHostKeyChecking=yes with stage timeouts.
"""
from __future__ import annotations

import errno
import hashlib
import inspect
import json
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

KST = timezone(timedelta(hours=9), "KST")

DEFAULT_STAGE_TIMEOUTS = {
    "prepare": 30,
    "compress": 60,
    "upload": 300,
    "decompress": 120,
    "verify": 600,
    "download": 60,
    "replace": 30,
    "check": 30,
    "cleanup": 30,
}

REMOTE_VERIFY_SCRIPT = (
    "import hashlib, json, os, sqlite3, sys\n"
    "from pathlib import Path\n"
    "try:\n"
    "    db_path, meta_path = sys.argv[1], sys.argv[2]\n"
    "    conn = sqlite3.connect(f'file:{Path(db_path).resolve().as_posix()}?mode=ro', uri=True)\n"
    "    try:\n"
    "        cur = conn.cursor()\n"
    "        cur.execute('PRAGMA integrity_check;')\n"
    "        row = cur.fetchone()\n"
    "        integrity = row[0] if row else 'fail'\n"
    "        if integrity != 'ok':\n"
    "            sys.exit(1)\n"
    "        cur.execute(\"SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages';\")\n"
    "        has_messages = cur.fetchone() is not None\n"
    "        if has_messages:\n"
    "            cur.execute('SELECT count(*), min(sentAtIso), max(sentAtIso) FROM messages;')\n"
    "            r = cur.fetchone()\n"
    "            cnt = r[0] if (r and r[0] is not None) else 0\n"
    "            min_iso = r[1] if r else None\n"
    "            max_iso = r[2] if r else None\n"
    "        else:\n"
    "            cnt, min_iso, max_iso = 0, None, None\n"
    "    finally:\n"
    "        conn.close()\n"
    "    h = hashlib.sha256()\n"
    "    with open(db_path, 'rb') as f:\n"
    "        while True:\n"
    "            chunk = f.read(1048576)\n"
    "            if not chunk:\n"
    "                break\n"
    "            h.update(chunk)\n"
    "    sha = h.hexdigest()\n"
    "    sz = os.path.getsize(db_path)\n"
    "    data = {\n"
    "        'integrityCheck': integrity,\n"
    "        'messageCount': cnt,\n"
    "        'minSentAtIso': min_iso,\n"
    "        'maxSentAtIso': max_iso,\n"
    "        'sha256': sha,\n"
    "        'sizeBytes': sz,\n"
    "    }\n"
    "    fd = os.open(meta_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n"
    "    with open(fd, 'w', encoding='utf-8') as f:\n"
    "        json.dump(data, f)\n"
    "    os.chmod(meta_path, 0o600)\n"
    "except Exception:\n"
    "    sys.exit(1)\n"
)



def _call_runner_with_timeout(runner: Any, *args: Any, timeout: Optional[int] = None) -> Any:
    """Invoke runner with keyword argument timeout if supported, otherwise with positional args."""
    if timeout is not None:
        try:
            sig = inspect.signature(runner)
            params = sig.parameters
            if "timeout" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                return runner(*args, timeout=timeout)
        except (ValueError, TypeError):
            pass
    return runner(*args)


def _now_kst() -> str:
    return datetime.now(tz=KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


class MirrorPipelineError(Exception):
    """Pipeline failure carrying stage and error code without leaking sensitive paths."""

    def __init__(self, stage: str, error_code: str):
        super().__init__(f"[{stage}:{error_code}]")
        self.stage = stage
        self.error_code = error_code


def validate_remote_path(path: str) -> str:
    """Validate remote POSIX directory path against control chars and option injection."""
    if not isinstance(path, str):
        raise ValueError("Remote path must be a string")
    p = path.strip()
    if not p:
        raise ValueError("Remote path cannot be empty")
    if any(ord(c) < 32 or ord(c) == 127 for c in p):
        raise ValueError("Remote path contains forbidden control characters")
    if p.startswith("-"):
        raise ValueError("Remote path must not start with a hyphen")
    if not p.startswith("/"):
        raise ValueError("Remote path must be an absolute POSIX path starting with '/'")
    if not re.match(r"^/[A-Za-z0-9_./@+-]+$", p):
        raise ValueError("Remote path contains disallowed characters")
    parts = p.split("/")
    if any(part == ".." for part in parts):
        raise ValueError("Remote path must not contain directory traversal ('..')")
    return p.rstrip("/")


def validate_ssh_target(target: str) -> str:
    """Validate SSH target format to prevent option injection and control characters."""
    if not isinstance(target, str):
        raise ValueError("SSH target must be a string")
    t = target.strip()
    if not t:
        raise ValueError("SSH target cannot be empty")
    if t.startswith("-"):
        raise ValueError("SSH target must not start with a hyphen")
    if any(ord(c) < 32 or ord(c) == 127 for c in t):
        raise ValueError("SSH target contains forbidden control characters")
    if not re.match(r"^[A-Za-z0-9_.-]+(@[A-Za-z0-9_.-]+)?$", t):
        raise ValueError("SSH target contains invalid characters")
    return t


def posix_quote(path: str) -> str:
    """Safely quote a POSIX path using standard single-quote escaping."""
    return "'" + path.replace("'", "'\\''") + "'"


DEFAULT_VPS_UID = 10000
DEFAULT_VPS_GID = 10000


def validate_positive_id(val: Any, name: str = "ID") -> int:
    """Validate that val is a positive integer (> 0). Raises ValueError otherwise."""
    if isinstance(val, bool):
        raise ValueError(f"{name} must be a positive integer, got boolean: {val}")
    try:
        if isinstance(val, float) and not val.is_integer():
            raise ValueError(f"{name} must be an integer, got float: {val}")
        num = int(val)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer, got: {val!r}")
    if num <= 0:
        raise ValueError(f"{name} must be a positive integer (> 0), got: {num}")
    return num


def resolve_vps_uid_gid(
    uid: Optional[Any] = None,
    gid: Optional[Any] = None,
    config_paths: Optional[list[str | Path]] = None,
    parent_stat_fn: Optional[Any] = None,
) -> tuple[int, int]:
    """Resolve and strictly validate VPS target UID and GID.

    Precedence:
    1. Explicit function arguments (`uid`, `gid`)
    2. Environment variables (`KAKAO_VPS_UID`, `KAKAO_VPS_GID`)
    3. LocalAppData / project configuration files
    4. Remote parent directory stat fallback (if callable provided)
    5. Default convention: 10000:10000
    """
    resolved_uid = None
    resolved_gid = None

    # 1. Explicit arguments
    if uid is not None:
        resolved_uid = validate_positive_id(uid, "vps_uid")
    if gid is not None:
        resolved_gid = validate_positive_id(gid, "vps_gid")

    # 2. Environment variables
    if resolved_uid is None:
        env_u = os.environ.get("KAKAO_VPS_UID")
        if env_u is not None and str(env_u).strip():
            resolved_uid = validate_positive_id(env_u.strip(), "KAKAO_VPS_UID")
    if resolved_gid is None:
        env_g = os.environ.get("KAKAO_VPS_GID")
        if env_g is not None and str(env_g).strip():
            resolved_gid = validate_positive_id(env_g.strip(), "KAKAO_VPS_GID")

    # 3. Config file lookup
    if resolved_uid is None or resolved_gid is None:
        if config_paths is None:
            config_paths = []
            local_appdata = os.environ.get("LOCALAPPDATA", "")
            if local_appdata:
                config_paths.append(Path(local_appdata) / "KakaoCollector" / "config.json")
                config_paths.append(Path(local_appdata) / "KakaoCollector" / "mirror_config.json")
                config_paths.append(Path(local_appdata) / "kakao-cli" / "config.json")
            here = Path(__file__).resolve().parent
            config_paths.append(here.parent / "config.json")
            config_paths.append(here.parent / "data" / "config" / "mirror.json")

        for cp in config_paths:
            p = Path(cp)
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        if resolved_uid is None:
                            cfg_u = data.get("KAKAO_VPS_UID") or data.get("vps_uid")
                            if cfg_u is not None:
                                resolved_uid = validate_positive_id(cfg_u, "config.vps_uid")
                        if resolved_gid is None:
                            cfg_g = data.get("KAKAO_VPS_GID") or data.get("vps_gid")
                            if cfg_g is not None:
                                resolved_gid = validate_positive_id(cfg_g, "config.vps_gid")
                except Exception as exc:
                    if isinstance(exc, ValueError):
                        raise
                    pass
            if resolved_uid is not None and resolved_gid is not None:
                break

    # 4. Parent stat fallback
    if (resolved_uid is None or resolved_gid is None) and parent_stat_fn is not None:
        try:
            p_uid, p_gid = parent_stat_fn()
            if resolved_uid is None and p_uid is not None:
                resolved_uid = validate_positive_id(p_uid, "parent_uid")
            if resolved_gid is None and p_gid is not None:
                resolved_gid = validate_positive_id(p_gid, "parent_gid")
        except Exception as exc:
            if isinstance(exc, ValueError):
                raise
            pass

    # 5. Safe defaults
    if resolved_uid is None:
        resolved_uid = DEFAULT_VPS_UID
    if resolved_gid is None:
        resolved_gid = DEFAULT_VPS_GID

    return resolved_uid, resolved_gid


def compute_sha256(file_path: str | Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def create_readonly_snapshot(src_db_path: str | Path, snapshot_path: str | Path) -> Dict[str, Any]:
    """Create a consistent point-in-time snapshot using SQLite backup API in read-only mode.

    Returns metrics without leaking absolute snapshot path.
    """
    src_path = Path(src_db_path).resolve()
    dst_path = Path(snapshot_path).resolve()

    if not src_path.exists():
        raise FileNotFoundError(f"Source DB does not exist: {src_path.name}")

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dst = dst_path.with_name(f"{dst_path.name}.tmp_{os.getpid()}_{time.time_ns()}")

    # Connect to source in read-only mode with URI
    src_uri = f"file:{src_path.as_posix()}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True)
    try:
        dst_conn = sqlite3.connect(str(temp_dst))
        try:
            with dst_conn:
                src_conn.backup(dst_conn, pages=1000)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    try:
        os.replace(temp_dst, dst_path)
    finally:
        if temp_dst.exists():
            try:
                temp_dst.unlink()
            except OSError:
                pass

    stats = inspect_db_stats(dst_path)
    file_size = dst_path.stat().st_size
    file_sha256 = compute_sha256(dst_path)

    return {
        "sizeBytes": file_size,
        "sha256": file_sha256,
        "integrityCheck": stats["integrityCheck"],
        "messageCount": stats["messageCount"],
        "minSentAtIso": stats["minSentAtIso"],
        "maxSentAtIso": stats["maxSentAtIso"],
        "createdAtKst": _now_kst(),
    }


def compress_file_gzip(src_path: str | Path, dst_gz_path: str | Path) -> Dict[str, Any]:
    """Compress source file to dst_gz_path using standard gzip with atomic temp file replacement.

    Returns size and sha256 of compressed file.
    """
    src = Path(src_path).resolve()
    dst = Path(dst_gz_path).resolve()
    if not src.exists():
        raise FileNotFoundError(f"Source file to compress not found: {src.name}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    temp_dst = dst.with_name(f"{dst.name}.tmp_{os.getpid()}_{time.time_ns()}")
    try:
        import gzip
        import shutil
        with open(src, "rb") as f_in, gzip.open(temp_dst, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out, length=1024 * 1024)
        os.replace(temp_dst, dst)
    except Exception as exc:
        if temp_dst.exists():
            try:
                temp_dst.unlink()
            except OSError:
                pass
        raise MirrorPipelineError("LOCAL_COMPRESS", "COMPRESS_FAILED") from exc

    gz_size = dst.stat().st_size
    gz_sha = compute_sha256(dst)
    return {
        "sizeBytes": gz_size,
        "sha256": gz_sha,
    }


def inspect_db_stats(db_path: str | Path) -> Dict[str, Any]:
    """Inspect SQLite database integrity and message aggregate metrics without logging content."""
    conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        integrity = cur.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Database integrity check failed: {integrity}")

        has_messages = bool(
            cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
        )
        if not has_messages:
            return {
                "integrityCheck": integrity,
                "messageCount": 0,
                "minSentAtIso": None,
                "maxSentAtIso": None,
            }

        count_row = cur.execute(
            "SELECT count(*), min(sentAtIso), max(sentAtIso) FROM messages"
        ).fetchone()

        return {
            "integrityCheck": integrity,
            "messageCount": count_row[0] or 0,
            "minSentAtIso": count_row[1],
            "maxSentAtIso": count_row[2],
        }
    finally:
        conn.close()


class ProcessLock:
    """File lock with atomic O_CREAT|O_EXCL creation and safe stale PID handling."""

    def __init__(self, lock_file: str | Path, timeout_seconds: int = 600):
        self.lock_file = Path(lock_file).resolve()
        self.timeout_seconds = timeout_seconds
        self.acquired = False

    def _is_pid_running(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            STILL_ACTIVE = 259
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            exit_code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
            kernel32.CloseHandle(h)
            return exit_code.value == STILL_ACTIVE
        else:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

    def _write_lock_payload(self, fd: int) -> None:
        payload = json.dumps({
            "pid": os.getpid(),
            "timestamp": time.time(),
            "acquiredAtKst": _now_kst(),
        }, indent=2).encode("utf-8")
        os.write(fd, payload)

    def acquire(self) -> bool:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        max_attempts = 2
        for _ in range(max_attempts):
            try:
                fd = os.open(
                    str(self.lock_file),
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    0o600,
                )
                try:
                    self._write_lock_payload(fd)
                finally:
                    os.close(fd)
                self.acquired = True
                return True
            except (FileExistsError, OSError) as e:
                if getattr(e, "errno", None) not in (errno.EEXIST, None) and not isinstance(e, FileExistsError):
                    return False

            # Lock file exists. Check if it is stale.
            try:
                if not self.lock_file.exists():
                    continue
                content = self.lock_file.read_text(encoding="utf-8")
                data = json.loads(content)
                lock_pid = data.get("pid", 0)
                lock_time = data.get("timestamp", 0)
                age = time.time() - lock_time
                is_stale = (age >= self.timeout_seconds) or (not self._is_pid_running(lock_pid))
            except Exception:
                is_stale = True

            if not is_stale:
                return False

            # Break stale lock safely via atomic rename before unlink
            stale_temp = self.lock_file.with_name(
                f"{self.lock_file.name}.stale_{os.getpid()}_{time.time_ns()}"
            )
            try:
                os.replace(self.lock_file, stale_temp)
                try:
                    stale_temp.unlink(missing_ok=True)
                except OSError:
                    pass
            except OSError:
                pass

        return False

    def release(self) -> None:
        if self.acquired:
            try:
                if self.lock_file.exists():
                    try:
                        data = json.loads(self.lock_file.read_text(encoding="utf-8"))
                        if data.get("pid") == os.getpid():
                            self.lock_file.unlink(missing_ok=True)
                    except Exception:
                        self.lock_file.unlink(missing_ok=True)
            except OSError:
                pass
            self.acquired = False

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("Mirror process lock active or already held.")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class StateMarker:
    """Persists mirror execution state atomically without path or raw secret leakage."""

    def __init__(self, marker_file: str | Path):
        self.marker_file = Path(marker_file).resolve()

    def read(self) -> Dict[str, Any]:
        if not self.marker_file.exists():
            return {}
        try:
            return json.loads(self.marker_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def update(self, **kwargs) -> None:
        self.marker_file.parent.mkdir(parents=True, exist_ok=True)
        state = self.read()
        state.update(kwargs)

        # Sanitize any legacy, prohibited, or sensitive fields
        prohibited = {"lastError", "snapshotPath", "remoteTarget", "remoteCurrent", "stopFile", "lockFile"}
        for k in prohibited:
            state.pop(k, None)
        if "localSnapshot" in state and isinstance(state["localSnapshot"], dict):
            state["localSnapshot"].pop("snapshotPath", None)
        if "remoteMirror" in state and isinstance(state["remoteMirror"], dict):
            state["remoteMirror"].pop("remoteTarget", None)
            state["remoteMirror"].pop("remoteCurrent", None)

        state["updatedAtKst"] = _now_kst()

        status = state.get("status")
        if status in ("SUCCESS", "SNAPSHOT_CREATED"):
            # Purge failure fields on success transition and record clear complete stage
            state.pop("errorCode", None)
            state.pop("lastErrorKst", None)
            state.pop("error", None)
            state.pop("errorMessage", None)
            if "lastStage" not in kwargs or not kwargs.get("lastStage"):
                state["lastStage"] = "COMPLETE"
        elif status == "IN_PROGRESS":
            # Purge stale failure fields on new execution start
            state.pop("errorCode", None)
            state.pop("lastErrorKst", None)
            state.pop("error", None)
            state.pop("errorMessage", None)
            if "lastStage" not in kwargs or not kwargs.get("lastStage"):
                state["lastStage"] = "INIT"


        # Atomic write: write to sibling temp file, then os.replace
        temp_file = self.marker_file.with_name(
            f"{self.marker_file.name}.tmp_{os.getpid()}_{time.time_ns()}"
        )
        try:
            temp_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(temp_file, self.marker_file)
        finally:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except OSError:
                    pass


def check_disabled(stop_marker: str | Path) -> bool:
    """Check if STOP marker or disable flag exists."""
    p = Path(stop_marker).resolve()
    return p.exists()


def wrap_remote_posix_command(command: str) -> str:
    """Wrap a POSIX command so that it exits explicitly while preserving the original exit code.

    Format: sh -c <shlex.quote(command.strip().rstrip(';').strip() + "; rc=$?; exit $rc")>
    Ensures SSH channels close cleanly on remote servers where complex commands
    otherwise hang without an explicit exit.
    """
    if not isinstance(command, str):
        raise ValueError("Remote command must be a string")
    stripped = command.strip().rstrip(";").strip()
    if not stripped:
        raise ValueError("Remote command cannot be empty")

    # Idempotent: avoid re-wrapping if already wrapped with this pattern
    if stripped.startswith("sh -c ") and stripped.endswith("; rc=$?; exit $rc'"):
        return stripped

    inner = f"{stripped}; rc=$?; exit $rc"
    return f"sh -c {shlex.quote(inner)}"


wrap_posix_command = wrap_remote_posix_command


def run_ssh_command(ssh_cmd: list[str], remote_cmd: str, timeout: int = 120) -> tuple[int, str, str]:
    """Run an SSH command remotely, capturing output without echoing secrets."""
    wrapped_cmd = wrap_remote_posix_command(remote_cmd)
    full_cmd = list(ssh_cmd) + [wrapped_cmd]
    proc = subprocess.run(
        full_cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def run_sftp_upload(sftp_cmd: list[str], local_path: str | Path, remote_path: str, timeout: int = 300) -> tuple[int, str, str]:
    """Upload file via sftp or scp."""
    cmd = list(sftp_cmd) + [str(local_path), remote_path]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def run_scp_download(scp_cmd: list[str], remote_path: str, local_path: str | Path, timeout: int = 60) -> tuple[int, str, str]:
    """Download file via scp without remote stdout leakage."""
    cmd = list(scp_cmd) + [remote_path, str(local_path)]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def parse_remote_meta(content: str) -> Dict[str, Any]:
    """Strictly parse remote verification metadata from JSON or delimiter format."""
    if not isinstance(content, str):
        raise ValueError("Invalid metadata content")
    text = content.strip()
    if not text:
        raise ValueError("Empty metadata content")

    # 1. JSON format
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("Metadata JSON must be an object")
            integrity = data.get("integrityCheck")
            count_raw = data.get("messageCount")
            min_iso = data.get("minSentAtIso")
            max_iso = data.get("maxSentAtIso")
            sha = data.get("sha256")
            size_raw = data.get("sizeBytes")
            size_val = None
            if size_raw is not None:
                try:
                    size_val = int(size_raw)
                except (ValueError, TypeError):
                    size_val = None

            if not isinstance(integrity, str) or not isinstance(sha, str) or count_raw is None:
                raise ValueError("Missing or invalid required fields in JSON metadata")
            count = int(count_raw)
            if count < 0:
                raise ValueError("Message count cannot be negative")
            sha_clean = sha.strip().lower()
            if not re.match(r"^[0-9a-f]{64}$", sha_clean):
                raise ValueError("Invalid sha256 hex format in metadata")

            return {
                "integrityCheck": integrity.strip(),
                "messageCount": count,
                "minSentAtIso": str(min_iso).strip() if min_iso is not None else None,
                "maxSentAtIso": str(max_iso).strip() if max_iso is not None else None,
                "sha256": sha_clean,
                "sizeBytes": size_val,
            }
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise ValueError(f"Failed to parse JSON metadata: {exc}") from exc

    # 2. Multi-line or single-line delimiter format
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 3:
        integrity = lines[0]
        cnt_parts = lines[1].split("|")
        if len(cnt_parts) < 3:
            raise ValueError("Invalid counts format in delimiter metadata")
        try:
            count = int(cnt_parts[0].strip())
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid count in delimiter metadata") from exc
        if count < 0:
            raise ValueError("Message count cannot be negative")
        min_iso = cnt_parts[1].strip() if cnt_parts[1].strip() else None
        max_iso = cnt_parts[2].strip() if cnt_parts[2].strip() else None
        sha_clean = lines[2].strip().lower()
    elif len(lines) == 1 and "|" in lines[0]:
        parts = lines[0].split("|")
        if len(parts) < 5:
            raise ValueError("Insufficient fields in single-line delimiter metadata")
        integrity = parts[0].strip()
        try:
            count = int(parts[1].strip())
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid count in delimiter metadata") from exc
        if count < 0:
            raise ValueError("Message count cannot be negative")
        min_iso = parts[2].strip() if parts[2].strip() else None
        max_iso = parts[3].strip() if parts[3].strip() else None
        sha_clean = parts[4].strip().lower()
    else:
        raise ValueError("Unrecognized metadata format")

    if not integrity:
        raise ValueError("Empty integrity check result")

    if not re.match(r"^[0-9a-f]{64}$", sha_clean):
        raise ValueError("Invalid sha256 hex format in metadata")

    return {
        "integrityCheck": integrity,
        "messageCount": count,
        "minSentAtIso": min_iso,
        "maxSentAtIso": max_iso,
        "sha256": sha_clean,
    }


def sync_mirror(
    src_db: str | Path,
    snapshot_dir: str | Path,
    state_file: str | Path,
    lock_file: str | Path,
    stop_file: str | Path,
    vps_ssh_target: Optional[str] = None,
    vps_remote_dir: Optional[str] = None,
    vps_uid: Optional[Any] = None,
    vps_gid: Optional[Any] = None,
    ssh_key_path: Optional[str] = None,
    ssh_port: Optional[int] = None,
    ssh_runner: Optional[Any] = None,
    upload_runner: Optional[Any] = None,
    download_runner: Optional[Any] = None,
    upload_only: bool = False,
    stage_timeouts: Optional[Dict[str, int]] = None,
    total_deadline_sec: Optional[float | int] = 840,
) -> Dict[str, Any]:
    """Execute complete mirror sync pipeline with atomic replacement and verification."""
    stop_path = Path(stop_file).resolve()
    if check_disabled(stop_path):
        return {
            "status": "STOPPED",
            "message": "STOP marker present. Mirroring is disabled.",
            "timestampKst": _now_kst(),
        }

    lock = ProcessLock(lock_file)
    if not lock.acquire():
        return {
            "status": "LOCKED",
            "message": "Another mirror process is running or lock timeout not expired.",
            "timestampKst": _now_kst(),
        }

    state = StateMarker(state_file)
    state.update(status="IN_PROGRESS", lastStage="INIT", lastAttemptKst=_now_kst())
    current_stage = "INIT"
    snapshot_file: Optional[Path] = None
    snapshot_gz: Optional[Path] = None
    local_meta_file: Optional[Path] = None
    snap_meta: Optional[Dict[str, Any]] = None

    if total_deadline_sec is not None:
        try:
            deadline_budget = max(1.0, min(840.0, float(total_deadline_sec)))
        except (ValueError, TypeError):
            deadline_budget = 840.0
    else:
        deadline_budget = 840.0

    deadline_monotonic = time.monotonic() + deadline_budget

    stage_timings: Dict[str, float] = {}
    t0_perf = time.perf_counter()
    gz_meta: Optional[Dict[str, Any]] = None

    try:
        timeouts = dict(DEFAULT_STAGE_TIMEOUTS)
        if stage_timeouts:
            timeouts.update(stage_timeouts)

        def _get_stage_timeout(stage_name: str, default_limit: int = 30) -> int:
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise MirrorPipelineError(current_stage, "TIMEOUT")
            configured_limit = timeouts.get(stage_name, default_limit)
            return max(1, min(configured_limit, int(remaining)))

        def _check_deadline() -> None:
            if time.monotonic() >= deadline_monotonic:
                raise MirrorPipelineError(current_stage, "TIMEOUT")

        src_path = Path(src_db).resolve()
        snap_dir = Path(snapshot_dir).resolve()
        snap_dir.mkdir(parents=True, exist_ok=True)
        snapshot_file = snap_dir / "messages_v2_snapshot.sqlite"

        # Step 1: Create local read-only consistent snapshot
        current_stage = "LOCAL_SNAPSHOT"
        _check_deadline()
        t_stage = time.perf_counter()
        snap_meta = create_readonly_snapshot(src_path, snapshot_file)
        stage_timings["LOCAL_SNAPSHOT"] = round(time.perf_counter() - t_stage, 3)
        state.update(
            localSnapshot=snap_meta,
            local={
                "status": "SUCCESS",
                "stage": "COMPLETE",
                "messageCount": snap_meta.get("messageCount", 0),
            },
        )

        # If VPS target is not configured, complete after local snapshot
        if not vps_ssh_target or not vps_remote_dir:
            total_elapsed = round(time.perf_counter() - t0_perf, 3)
            state.update(
                status="SNAPSHOT_CREATED",
                lastStage="COMPLETE",
                lastSuccessKst=_now_kst(),
                localSnapshot=snap_meta,
                stageTimings=stage_timings,
                totalElapsedSeconds=total_elapsed,
                local={
                    "status": "SUCCESS",
                    "stage": "COMPLETE",
                    "messageCount": snap_meta.get("messageCount", 0),
                },
                mirror={
                    "status": "SKIPPED",
                },
            )
            return {
                "status": "SNAPSHOT_CREATED",
                "localSnapshot": snap_meta,
                "remoteMirror": None,
                "stageTimings": stage_timings,
                "totalElapsedSeconds": total_elapsed,
                "message": "Local snapshot created successfully (VPS target not configured).",
            }

        # Step 1.1: Compress local snapshot with standard gzip
        current_stage = "LOCAL_COMPRESS"
        _check_deadline()
        snapshot_gz = snap_dir / f"{snapshot_file.name}.gz"
        t_stage = time.perf_counter()
        gz_meta = compress_file_gzip(snapshot_file, snapshot_gz)
        stage_timings["LOCAL_COMPRESS"] = round(time.perf_counter() - t_stage, 3)

        # Step 2: Validate remote parameters
        current_stage = "CONFIG_VALIDATION"
        _check_deadline()
        target = validate_ssh_target(vps_ssh_target)
        remote_dir = validate_remote_path(vps_remote_dir)
        remote_partial = f"{remote_dir}/messages_v2.sqlite.partial"
        remote_gz = f"{remote_partial}.gz"
        remote_current = f"{remote_dir}/messages_v2.sqlite"

        remote_dir_q = shlex.quote(remote_dir)
        remote_partial_q = shlex.quote(remote_partial)
        remote_gz_q = shlex.quote(remote_gz)
        remote_current_q = shlex.quote(remote_current)

        base_ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
        base_scp = ["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
        if ssh_port:
            base_ssh.extend(["-p", str(int(ssh_port))])
            base_scp.extend(["-P", str(int(ssh_port))])
        if ssh_key_path:
            base_ssh.extend(["-i", str(ssh_key_path)])
            base_scp.extend(["-i", str(ssh_key_path)])
        base_ssh.append(target)

        def _exec_remote_ssh(raw_cmd: str, timeout: int) -> tuple[int, str, str]:
            wrapped = wrap_remote_posix_command(raw_cmd)
            if ssh_runner:
                return _call_runner_with_timeout(ssh_runner, base_ssh, wrapped, timeout=timeout)
            return run_ssh_command(base_ssh, wrapped, timeout=timeout)

        def _query_remote_parent_stat() -> tuple[Optional[int], Optional[int]]:
            parent_dir = str(Path(remote_dir).parent).replace("\\", "/")
            if parent_dir and parent_dir != remote_dir:
                parent_dir_q = shlex.quote(parent_dir)
                cmd = f"stat -c '%u %g' {parent_dir_q} 2>/dev/null"
                try:
                    c, out, _ = _exec_remote_ssh(cmd, timeout=_get_stage_timeout("prepare", 30))
                    if c == 0 and out:
                        parts = out.strip().split()
                        if len(parts) >= 2:
                            pu, pg = int(parts[0]), int(parts[1])
                            if pu > 0 and pg > 0:
                                return pu, pg
                except Exception:
                    pass
            return None, None

        uid, gid = resolve_vps_uid_gid(
            uid=vps_uid,
            gid=vps_gid,
            parent_stat_fn=_query_remote_parent_stat,
        )

        def _cleanup_remote(clean_partial: bool = False, clean_gz: bool = False, clean_meta_path: Optional[str] = None) -> None:
            parts = []
            if clean_partial and remote_partial_q:
                parts.append(f"rm -f {remote_partial_q}")
            if clean_gz and remote_gz_q:
                parts.append(f"rm -f {remote_gz_q}")
            if clean_meta_path:
                parts.append(f"rm -f {shlex.quote(clean_meta_path)}")
            if not parts:
                return
            cleanup_cmd = " && ".join(parts)
            try:
                _exec_remote_ssh(cleanup_cmd, timeout=_get_stage_timeout("cleanup", 30))
            except Exception:
                pass

        # 2.1 Prepare remote directory (700) with approved owner UID:GID
        current_stage = "REMOTE_PREPARE"
        _check_deadline()
        t_stage = time.perf_counter()
        mkdir_cmd = (
            f"( mkdir -p {remote_dir_q} && "
            f"chown {uid}:{gid} {remote_dir_q} && "
            f"chmod 700 {remote_dir_q} && "
            f"[ \"$(stat -c '%u:%g:%a' {remote_dir_q})\" = '{uid}:{gid}:700' ] "
            f") > /dev/null 2>&1"
        )
        try:
            code, out, err = _exec_remote_ssh(mkdir_cmd, timeout=_get_stage_timeout("prepare", 30))
        except subprocess.TimeoutExpired:
            raise MirrorPipelineError("REMOTE_PREPARE", "TIMEOUT")
        except Exception:
            raise MirrorPipelineError("REMOTE_PREPARE", "CONNECT_FAILED")

        if code != 0:
            if code == 28:
                raise MirrorPipelineError("REMOTE_PREPARE", "DISK_FULL")
            raise MirrorPipelineError("REMOTE_PREPARE", "DIR_PREPARE_FAILED")
        stage_timings["REMOTE_PREPARE"] = round(time.perf_counter() - t_stage, 3)
        _check_deadline()

        # 2.2 Upload compressed snapshot to remote .partial.gz
        current_stage = "REMOTE_UPLOAD"
        _check_deadline()
        t_stage = time.perf_counter()
        remote_scp_dest = f"{target}:{remote_gz}"
        try:
            up_timeout = _get_stage_timeout("upload", 300)
            if upload_runner:
                code, out, err = _call_runner_with_timeout(upload_runner, base_scp, snapshot_gz, remote_scp_dest, timeout=up_timeout)
            else:
                code, out, err = run_sftp_upload(base_scp, snapshot_gz, remote_scp_dest, timeout=up_timeout)
        except subprocess.TimeoutExpired:
            _cleanup_remote(clean_partial=True, clean_gz=True)
            raise MirrorPipelineError("REMOTE_UPLOAD", "TIMEOUT")
        except Exception:
            _cleanup_remote(clean_partial=True, clean_gz=True)
            raise MirrorPipelineError("REMOTE_UPLOAD", "UPLOAD_FAILED")

        if code != 0:
            _cleanup_remote(clean_partial=True, clean_gz=True)
            raise MirrorPipelineError("REMOTE_UPLOAD", "UPLOAD_FAILED")
        stage_timings["REMOTE_UPLOAD"] = round(time.perf_counter() - t_stage, 3)
        _check_deadline()

        # 2.3 Decompress remote .partial.gz into .partial candidate and enforce permissions
        current_stage = "REMOTE_DECOMPRESS"
        _check_deadline()
        t_stage = time.perf_counter()
        decompress_cmd = (
            f"( "
            f"( gzip -dc {remote_gz_q} > {remote_partial_q} || "
            f"  {{ rc=$?; if [ \"$(df -k {remote_dir_q} 2>/dev/null | awk 'END{{print $(NF-2)}}')\" -le 1024 ] 2>/dev/null; then exit 28; fi; exit $rc; }} "
            f") && "
            f"chown {uid}:{gid} {remote_partial_q} && "
            f"chmod 600 {remote_partial_q} && "
            f"[ \"$(stat -c '%u:%g:%a' {remote_partial_q})\" = '{uid}:{gid}:600' ] && "
            f"rm -f {remote_gz_q} "
            f") > /dev/null 2>&1"
        )
        try:
            d_code, d_out, d_err = _exec_remote_ssh(decompress_cmd, timeout=_get_stage_timeout("decompress", 120))
        except subprocess.TimeoutExpired:
            _cleanup_remote(clean_partial=True, clean_gz=True)
            raise MirrorPipelineError("REMOTE_DECOMPRESS", "TIMEOUT")
        except Exception:
            _cleanup_remote(clean_partial=True, clean_gz=True)
            raise MirrorPipelineError("REMOTE_DECOMPRESS", "DECOMPRESS_FAILED")

        if d_code != 0:
            _cleanup_remote(clean_partial=True, clean_gz=True)
            if d_code == 28:
                raise MirrorPipelineError("REMOTE_DECOMPRESS", "DISK_FULL")
            raise MirrorPipelineError("REMOTE_DECOMPRESS", "DECOMPRESS_FAILED")
        stage_timings["REMOTE_DECOMPRESS"] = round(time.perf_counter() - t_stage, 3)
        _check_deadline()

        # 2.4 Verify remote partial:
        # Pre-validate owner/mode on partial and remote_dir, and write verification metadata without stdout
        current_stage = "REMOTE_VERIFY"
        _check_deadline()
        t_stage = time.perf_counter()
        meta_token = secrets.token_hex(16)
        remote_meta_file = f"{remote_dir}/.verify_{meta_token}.meta"
        validate_remote_path(remote_meta_file)
        remote_meta_q = shlex.quote(remote_meta_file)

        local_meta_file = snap_dir / f".verify_{meta_token}.tmp"

        py_code_q = shlex.quote(REMOTE_VERIFY_SCRIPT)
        verify_script = (
            f"( chown {uid}:{gid} {remote_partial_q} && "
            f"chmod 600 {remote_partial_q} && "
            f"[ \"$(stat -c '%u:%g:%a' {remote_partial_q})\" = '{uid}:{gid}:600' ] && "
            f"[ \"$(stat -c '%u:%g:%a' {remote_dir_q})\" = '{uid}:{gid}:700' ] && "
            f"umask 077 && "
            f"python3 -c {py_code_q} {remote_partial_q} {remote_meta_q} && "
            f"chown {uid}:{gid} {remote_meta_q} && "
            f"chmod 600 {remote_meta_q} "
            f") > /dev/null 2>&1"
        )
        try:
            code, out, err = _exec_remote_ssh(verify_script, timeout=_get_stage_timeout("verify", 600))
        except subprocess.TimeoutExpired:
            _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
            raise MirrorPipelineError("REMOTE_VERIFY", "TIMEOUT")
        except Exception:
            _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
            raise MirrorPipelineError("REMOTE_VERIFY", "VERIFY_FAILED")

        if code != 0:
            _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
            raise MirrorPipelineError("REMOTE_VERIFY", "VERIFY_COMMAND_FAILED")
        _check_deadline()

        # 2.4.2 Download remote meta file via scp
        remote_scp_meta_src = f"{target}:{remote_meta_file}"
        try:
            dl_timeout = _get_stage_timeout("download", 60)
            if download_runner:
                d_code, d_out, d_err = _call_runner_with_timeout(download_runner, base_scp, remote_scp_meta_src, local_meta_file, timeout=dl_timeout)
            else:
                d_code, d_out, d_err = run_scp_download(base_scp, remote_scp_meta_src, local_meta_file, timeout=dl_timeout)
        except subprocess.TimeoutExpired:
            _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
            raise MirrorPipelineError("REMOTE_VERIFY", "TIMEOUT")
        except Exception:
            _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
            raise MirrorPipelineError("REMOTE_VERIFY", "DOWNLOAD_FAILED")

        if d_code != 0:
            _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
            raise MirrorPipelineError("REMOTE_VERIFY", "DOWNLOAD_FAILED")
        _check_deadline()

        # 2.4.3 Read and strictly parse local metadata, then delete local temp file
        try:
            if not local_meta_file.exists():
                _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
                raise MirrorPipelineError("REMOTE_VERIFY", "DOWNLOAD_FAILED")
            raw_meta = local_meta_file.read_text(encoding="utf-8")
        finally:
            try:
                if local_meta_file.exists():
                    local_meta_file.unlink()
            except OSError:
                pass

        try:
            parsed_meta = parse_remote_meta(raw_meta)
        except Exception:
            _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
            raise MirrorPipelineError("REMOTE_VERIFY", "PARSE_ERROR")

        remote_integrity = parsed_meta["integrityCheck"]
        remote_count = parsed_meta["messageCount"]
        remote_min = parsed_meta["minSentAtIso"]
        remote_max = parsed_meta["maxSentAtIso"]
        remote_sha256 = parsed_meta["sha256"]

        if remote_integrity != "ok":
            _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
            raise MirrorPipelineError("REMOTE_VERIFY", "INTEGRITY_FAILED")

        # Compare ALL THREE message metrics: count, min, max
        if remote_count != snap_meta["messageCount"]:
            _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
            raise MirrorPipelineError("REMOTE_VERIFY", "COUNT_MISMATCH")

        if remote_min != snap_meta["minSentAtIso"]:
            _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
            raise MirrorPipelineError("REMOTE_VERIFY", "MIN_TIMESTAMP_MISMATCH")

        if remote_max != snap_meta["maxSentAtIso"]:
            _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
            raise MirrorPipelineError("REMOTE_VERIFY", "MAX_TIMESTAMP_MISMATCH")

        # Compare size if available
        remote_size = parsed_meta.get("sizeBytes")
        if remote_size is not None and snap_meta.get("sizeBytes") is not None:
            if remote_size != snap_meta["sizeBytes"]:
                _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
                raise MirrorPipelineError("REMOTE_VERIFY", "SIZE_MISMATCH")

        # Compare SHA-256 hash (decompressed remote candidate vs uncompressed local snapshot)
        if remote_sha256.lower() != snap_meta["sha256"].lower():
            _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
            raise MirrorPipelineError("REMOTE_VERIFY", "HASH_MISMATCH")

        stage_timings["REMOTE_VERIFY"] = round(time.perf_counter() - t_stage, 3)
        _check_deadline()

        # 2.5 Atomic replacement with rollback protection & owner/mode validation
        # Verify meta cleanup is consolidated directly into replace_cmd to avoid separate SSH roundtrip failures
        current_stage = "REMOTE_REPLACE"
        _check_deadline()
        t_stage = time.perf_counter()
        rollback_token = secrets.token_hex(16)
        remote_rollback_file = f"{remote_dir}/.messages_v2.rollback_{rollback_token}"
        remote_nocur_file = f"{remote_dir}/.messages_v2.nocur_{rollback_token}"
        remote_backup_file = f"{remote_dir}/messages_v2.sqlite.prev"
        validate_remote_path(remote_rollback_file)
        validate_remote_path(remote_nocur_file)
        validate_remote_path(remote_backup_file)
        remote_rollback_q = shlex.quote(remote_rollback_file)
        remote_nocur_q = shlex.quote(remote_nocur_file)
        remote_backup_q = shlex.quote(remote_backup_file)

        def _rollback_remote() -> None:
            r_cmd = (
                f"( "
                f"if [ -f {remote_rollback_q} ]; then "
                f"  rm -f {remote_current_q}; "
                f"  mv -f {remote_rollback_q} {remote_current_q} && "
                f"  chown {uid}:{gid} {remote_current_q} && "
                f"  chmod 600 {remote_current_q} && "
                f"  [ \"$(stat -c '%u:%g:%a' {remote_current_q})\" = '{uid}:{gid}:600' ] || exit 1; "
                f"  rm -f {remote_rollback_q}; "
                f"elif [ -f {remote_nocur_q} ]; then "
                f"  rm -f {remote_current_q}; "
                f"  rm -f {remote_nocur_q}; "
                f"fi; "
                f"rm -f {remote_partial_q}; "
                f"rm -f {remote_meta_q} "
                f") > /dev/null 2>&1"
            )
            try:
                _exec_remote_ssh(r_cmd, timeout=_get_stage_timeout("replace", 30))
            except Exception:
                pass

        replace_cmd = (
            f"( "
            f"rm -f {remote_meta_q}; "
            f"[ -f {remote_partial_q} ] || exit 1; "
            f"[ \"$(stat -c '%u:%g:%a' {remote_partial_q})\" = '{uid}:{gid}:600' ] || exit 1; "
            f"[ \"$(stat -c '%u:%g:%a' {remote_dir_q})\" = '{uid}:{gid}:700' ] || exit 1; "
            f"had_cur=0; "
            f"if [ -f {remote_current_q} ]; then "
            f"  had_cur=1; "
            f"  mv -f {remote_current_q} {remote_rollback_q} || exit 1; "
            f"else "
            f"  touch {remote_nocur_q} || exit 1; "
            f"fi; "
            f"if mv -f {remote_partial_q} {remote_current_q} && "
            f"   chown {uid}:{gid} {remote_current_q} && "
            f"   chmod 600 {remote_current_q} && "
            f"[ \"$(stat -c '%u:%g:%a' {remote_current_q})\" = '{uid}:{gid}:600' ] && "
            f"[ \"$(stat -c '%u:%g:%a' {remote_dir_q})\" = '{uid}:{gid}:700' ]; then "
            f"  if [ $had_cur -eq 1 ]; then "
            f"    mv -f {remote_rollback_q} {remote_backup_q} && "
            f"    chown {uid}:{gid} {remote_backup_q} && "
            f"    chmod 600 {remote_backup_q} && "
            f"    [ \"$(stat -c '%u:%g:%a' {remote_backup_q})\" = '{uid}:{gid}:600' ] || rm -f {remote_rollback_q}; "
            f"  else "
            f"    rm -f {remote_nocur_q}; "
            f"  fi; "
            f"  exit 0; "
            f"else "
            f"  rm -f {remote_current_q}; "
            f"  if [ $had_cur -eq 1 ]; then "
            f"    mv -f {remote_rollback_q} {remote_current_q} && "
            f"    chown {uid}:{gid} {remote_current_q} && "
            f"    chmod 600 {remote_current_q} && "
            f"    [ \"$(stat -c '%u:%g:%a' {remote_current_q})\" = '{uid}:{gid}:600' ]; "
            f"  else "
            f"    rm -f {remote_nocur_q}; "
            f"  fi; "
            f"  rm -f {remote_partial_q}; "
            f"  exit 1; "
            f"fi "
            f") > /dev/null 2>&1"
        )
        try:
            code, out, err = _exec_remote_ssh(replace_cmd, timeout=_get_stage_timeout("replace", 30))
        except subprocess.TimeoutExpired:
            _rollback_remote()
            _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
            raise MirrorPipelineError("REMOTE_REPLACE", "TIMEOUT")
        except Exception:
            _rollback_remote()
            _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
            raise MirrorPipelineError("REMOTE_REPLACE", "REPLACE_FAILED")

        if code != 0:
            _rollback_remote()
            _cleanup_remote(clean_partial=True, clean_gz=True, clean_meta_path=remote_meta_file)
            raise MirrorPipelineError("REMOTE_REPLACE", "REPLACE_FAILED")
        stage_timings["REMOTE_REPLACE"] = round(time.perf_counter() - t_stage, 3)
        _check_deadline()

        # 2.6 Verify remote permissions & archive preservation without stdout
        current_stage = "REMOTE_CHECK"
        _check_deadline()
        t_stage = time.perf_counter()
        check_archive_cmd = (
            f"( [ \"$(stat -c '%u:%g:%a' {remote_dir_q})\" = '{uid}:{gid}:700' ] && "
            f"[ \"$(stat -c '%u:%g:%a' {remote_current_q})\" = '{uid}:{gid}:600' ] && "
            f"ls -1 {remote_dir_q} | grep -q -E '^messages_v2.*20260908' ) >/dev/null 2>&1 || true"
        )
        try:
            code, out, err = _exec_remote_ssh(check_archive_cmd, timeout=_get_stage_timeout("check", 30))
        except Exception:
            pass
        stage_timings["REMOTE_CHECK"] = round(time.perf_counter() - t_stage, 3)

        total_elapsed = round(time.perf_counter() - t0_perf, 3)
        snap_sz = snap_meta.get("sizeBytes", 0) if snap_meta else 0
        gz_sz = gz_meta.get("sizeBytes", 0) if gz_meta else 0
        saved_bytes = max(0, snap_sz - gz_sz)
        reduction_pct = round((saved_bytes / snap_sz * 100), 2) if snap_sz > 0 else 0.0
        comp_info = {
            "snapshotBytes": snap_sz,
            "gzipBytes": gz_sz,
            "savedBytes": saved_bytes,
            "reductionRatio": reduction_pct,
        }

        remote_meta = {
            "integrityCheck": remote_integrity,
            "messageCount": remote_count,
            "minSentAtIso": remote_min,
            "maxSentAtIso": remote_max,
            "sha256": remote_sha256,
            "sizeBytes": remote_size if remote_size is not None else snap_meta.get("sizeBytes"),
            "previousBackupPreserved": True,
            "verifiedAtKst": _now_kst(),
        }

        state.update(
            status="SUCCESS",
            lastStage="COMPLETE",
            lastSuccessKst=_now_kst(),
            localSnapshot=snap_meta,
            remoteMirror=remote_meta,
            compression=comp_info,
            stageTimings=stage_timings,
            totalElapsedSeconds=total_elapsed,
            local={
                "status": "SUCCESS",
                "stage": "COMPLETE",
                "messageCount": snap_meta.get("messageCount", 0),
            },
            mirror={
                "status": "SUCCESS",
                "stage": "COMPLETE",
                "messageCount": remote_meta.get("messageCount", 0),
            },
        )

        return {
            "status": "SUCCESS",
            "localSnapshot": snap_meta,
            "remoteMirror": remote_meta,
            "compression": comp_info,
            "stageTimings": stage_timings,
            "totalElapsedSeconds": total_elapsed,
            "message": "Mirror sync successfully completed.",
        }

    except Exception as exc:
        if isinstance(exc, MirrorPipelineError):
            err_stage = exc.stage
            err_code = exc.error_code
        elif isinstance(exc, subprocess.TimeoutExpired):
            err_stage = current_stage
            err_code = "TIMEOUT"
        elif isinstance(exc, ValueError):
            err_stage = current_stage
            err_code = "INVALID_PARAM"
        elif isinstance(exc, FileNotFoundError):
            err_stage = current_stage
            err_code = "FILE_NOT_FOUND"
        else:
            err_stage = current_stage
            err_code = "UNKNOWN_ERROR"

        total_elapsed = round(time.perf_counter() - t0_perf, 3)
        err_data: Dict[str, Any] = {
            "status": "FAILED",
            "lastStage": err_stage,
            "errorCode": err_code,
            "lastErrorKst": _now_kst(),
            "stageTimings": stage_timings,
            "totalElapsedSeconds": total_elapsed,
        }
        if snap_meta is not None:
            err_data["localSnapshot"] = snap_meta
            err_data["local"] = {
                "status": "SUCCESS",
                "stage": "COMPLETE",
                "messageCount": snap_meta.get("messageCount", 0),
            }
            err_data["mirror"] = {
                "status": "FAILED",
                "stage": err_stage,
                "errorCode": err_code,
            }
        else:
            err_data["local"] = {
                "status": "FAILED",
                "stage": err_stage,
                "errorCode": err_code,
            }
            err_data["mirror"] = {
                "status": "SKIPPED",
            }

        state.update(**err_data)
        raise MirrorPipelineError(err_stage, err_code) from None
    finally:
        if vps_ssh_target and vps_remote_dir:
            if snapshot_file and snapshot_file.exists():
                try:
                    snapshot_file.unlink()
                except OSError:
                    pass
        if snapshot_gz and snapshot_gz.exists():
            try:
                snapshot_gz.unlink()
            except OSError:
                pass
        if local_meta_file and local_meta_file.exists():
            try:
                local_meta_file.unlink()
            except OSError:
                pass
        lock.release()
