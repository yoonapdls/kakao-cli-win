"""Regression and invariant tests for sync-on-login.ps1 PowerShell worker.

Verifies:
1. Status timeout retry and recovery within max attempts.
2. Max retries limit enforcement (capped at 3).
3. Total deadline enforcement (strictly < 15 min, budget-aware).
4. Atomic state updates (IN_PROGRESS at start, FAILED overwriting stale SUCCESS).
5. Safe error code allowlisting (no raw exceptions, paths, URLs, or localized strings).
6. Concurrency guard via named Mutex.
7. Marker schema compatibility for VPS watcher and post-sync mirror hook.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = PROJECT_ROOT / "sync-on-login.ps1"


class StubHandler(http.server.BaseHTTPRequestHandler):
    """Configurable stub handler for Kakao server endpoints."""

    def log_message(self, format, *args):
        # Suppress HTTP server stderr logs
        pass

    def do_GET(self):
        routes = self.server.routes.get("GET", {})
        path = self.path.split("?")[0]
        handler_func = routes.get(path)
        if handler_func:
            handler_func(self)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        routes = self.server.routes.get("POST", {})
        path = self.path.split("?")[0]
        handler_func = routes.get(path)
        if handler_func:
            handler_func(self)
        else:
            self.send_response(404)
            self.end_headers()


class MockHttpServer:
    def __init__(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        self.server.routes = {"GET": {}, "POST": {}}
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def register(self, method: str, path: str, func):
        self.server.routes[method][path] = func

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TestSyncOnLoginWorker(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root_dir = Path(self.temp_dir.name)
        self.state_dir = self.root_dir / "state"
        self.log_dir = self.root_dir / "logs"
        self.state_file = self.state_dir / "last-sync.json"
        self.mock_server = MockHttpServer()
        self.api_base = f"http://127.0.0.1:{self.mock_server.port}"
        self.mutex_name = f"Local\\KakaoSyncTest_{int(time.time() * 1000)}_{os.getpid()}"

    def tearDown(self):
        self.mock_server.close()
        self.temp_dir.cleanup()

    def _run_worker(self, extra_args=None, timeout=30):
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SYNC_SCRIPT),
            "-RootDirectory",
            str(self.root_dir),
            "-ApiBase",
            self.api_base,
            "-MutexName",
            self.mutex_name,
            "-SkipProcessCheck",
            "-SkipMirror",
        ]
        if extra_args:
            cmd.extend(extra_args)
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def test_status_timeout_retry_success(self):
        """Worker should retry after an initial /api/status timeout and succeed."""
        status_calls = []

        def handle_health(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        def handle_status(req):
            status_calls.append(time.time())
            if len(status_calls) == 1:
                # First call simulates timeout or 504
                time.sleep(1.2)
                req.send_response(504)
                req.end_headers()
                return
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ready": true, "counts": {"messages": 1000}, "dbUpdatedKst": "2026-09-14 10:00:00 KST"}')

        def handle_sync(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        self.mock_server.register("GET", "/api/health", handle_health)
        self.mock_server.register("GET", "/api/status", handle_status)
        self.mock_server.register("POST", "/api/sync", handle_sync)

        proc = self._run_worker(["-StatusTimeoutSec", "1", "-BackoffSec", "1", "-MaxAttempts", "3"])
        self.assertEqual(proc.returncode, 0, f"Expected 0, got {proc.returncode}. Stderr: {proc.stderr}")
        self.assertTrue(self.state_file.exists())
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state.get("status"), "SUCCESS")
        self.assertEqual(state.get("attempts"), 2)
        self.assertEqual(state.get("messages_before"), 1000)
        self.assertEqual(state.get("messages_after"), 1000)

    def test_max_retries_limited_to_three(self):
        """Worker should stop after max attempts (3) and write FAILED state."""
        status_calls = []

        def handle_health(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        def handle_status(req):
            status_calls.append(time.time())
            req.send_response(503)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"error": "A Kakao DB job is already running"}')

        self.mock_server.register("GET", "/api/health", handle_health)
        self.mock_server.register("GET", "/api/status", handle_status)

        proc = self._run_worker(["-MaxAttempts", "3", "-BackoffSec", "0", "-StatusTimeoutSec", "1"])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(status_calls), 3)

        self.assertTrue(self.state_file.exists())
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state.get("status"), "FAILED")
        self.assertEqual(state.get("attempts"), 3)
        self.assertIn(state.get("error"), ["SERVER_BUSY", "HTTP_503"])

    def test_total_deadline_enforced(self):
        """Worker should abort with DEADLINE_EXCEEDED when total deadline is reached."""
        def handle_health(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        def handle_status(req):
            time.sleep(1.5)
            req.send_response(504)
            req.end_headers()

        self.mock_server.register("GET", "/api/health", handle_health)
        self.mock_server.register("GET", "/api/status", handle_status)

        # Set 2 second total deadline
        proc = self._run_worker(["-TotalDeadlineSec", "2", "-StatusTimeoutSec", "1", "-BackoffSec", "1"])
        self.assertEqual(proc.returncode, 1)

        self.assertTrue(self.state_file.exists())
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state.get("status"), "FAILED")
        self.assertEqual(state.get("error"), "DEADLINE_EXCEEDED")

    def test_failed_overwrites_stale_success_atomically(self):
        """When sync fails, existing SUCCESS state marker must be overwritten by FAILED."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        stale_record = {
            "status": "SUCCESS",
            "started_at": "2026-01-01T00:00:00.0000000+09:00",
            "completed_at": "2026-01-01T00:01:00.0000000+09:00",
            "elapsed_seconds": 60.0,
            "ready": True,
            "messages_before": 500,
            "messages_after": 500,
            "db_updated_kst": "2026-01-01 00:00:00 KST"
        }
        self.state_file.write_text(json.dumps(stale_record), encoding="utf-8")

        def handle_health(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        def handle_status(req):
            req.send_response(500)
            req.end_headers()

        self.mock_server.register("GET", "/api/health", handle_health)
        self.mock_server.register("GET", "/api/status", handle_status)

        proc = self._run_worker(["-MaxAttempts", "1", "-BackoffSec", "0", "-StatusTimeoutSec", "1"])
        self.assertEqual(proc.returncode, 1)

        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state.get("status"), "FAILED")
        self.assertNotEqual(state.get("completed_at"), "2026-01-01T00:01:00.0000000+09:00")
        self.assertNotIn("messages_before", state)

    def test_in_progress_state_written_at_start(self):
        """Worker must write IN_PROGRESS to last-sync.json at the start of execution."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        stale_record = {
            "status": "SUCCESS",
            "started_at": "2026-01-01T00:00:00.0000000+09:00",
            "completed_at": "2026-01-01T00:01:00.0000000+09:00",
        }
        self.state_file.write_text(json.dumps(stale_record), encoding="utf-8")

        in_progress_seen = []

        def handle_health(req):
            # When health check is performed, check the state file
            if self.state_file.exists():
                try:
                    cur_state = json.loads(self.state_file.read_text(encoding="utf-8"))
                    in_progress_seen.append(cur_state.get("status"))
                except Exception:
                    pass
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        def handle_status(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ready": true, "counts": {"messages": 100}, "dbUpdatedKst": "..."}')

        def handle_sync(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        self.mock_server.register("GET", "/api/health", handle_health)
        self.mock_server.register("GET", "/api/status", handle_status)
        self.mock_server.register("POST", "/api/sync", handle_sync)

        proc = self._run_worker()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("IN_PROGRESS", in_progress_seen)

    def test_no_raw_or_localized_exception_leak(self):
        """Error outputs in state and logs must be sanitized allowlisted tokens."""
        def handle_health(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        def handle_status(req):
            # Return HTTP 500 with sensitive / raw content
            req.send_response(500)
            req.send_header("Content-Type", "text/plain; charset=utf-8")
            req.end_headers()
            req.wfile.write(b'SecretException: database at D:\\kakao\\secret.sqlite locked')

        self.mock_server.register("GET", "/api/health", handle_health)
        self.mock_server.register("GET", "/api/status", handle_status)

        proc = self._run_worker(["-MaxAttempts", "1", "-BackoffSec", "0", "-StatusTimeoutSec", "1"])
        self.assertEqual(proc.returncode, 1)

        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state.get("status"), "FAILED")
        error_code = state.get("error_code") or state.get("error")

        # Allowlisted safe error codes
        allowed_codes = {
            "TIMEOUT", "CONNECTION_FAILED", "SERVER_BUSY", "HTTP_500", "HTTP_ERROR",
            "WAITING_READY", "BACKEND_LAUNCH_FAILED", "POST_SYNC_NOT_READY",
            "DEADLINE_EXCEEDED", "MAX_RETRIES_EXCEEDED", "SYNC_FAILED"
        }
        self.assertIn(error_code, allowed_codes)

        raw_state_text = self.state_file.read_text(encoding="utf-8")
        self.assertNotIn("secret.sqlite", raw_state_text)
        self.assertNotIn("D:\\kakao", raw_state_text)
        self.assertNotIn("작업 시간을 초과했습니다", raw_state_text)

        # Check logs
        log_files = list(self.log_dir.glob("worker-*.jsonl"))
        self.assertTrue(len(log_files) > 0)
        log_text = log_files[0].read_text(encoding="utf-8")
        self.assertNotIn("secret.sqlite", log_text)
        self.assertNotIn("D:\\kakao", log_text)
        self.assertNotIn("작업 시간을 초과했습니다", log_text)

    def test_concurrency_mutex_guard(self):
        """Worker must exit 0 immediately if another instance holds the Mutex."""
        # Hold a PowerShell mutex in a background process
        hold_script = f"""
        $mutex = New-Object System.Threading.Mutex($true, '{self.mutex_name}')
        Start-Sleep -Seconds 10
        $mutex.ReleaseMutex()
        $mutex.Dispose()
        """
        proc_holder = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", hold_script]
        )
        try:
            time.sleep(1.0)  # Wait for holder process to acquire mutex

            # Run worker while holder holds mutex
            proc = self._run_worker(timeout=5)
            self.assertEqual(proc.returncode, 0)
            # Mutex skipped run, state file must not be created
            self.assertFalse(self.state_file.exists())
        finally:
            proc_holder.terminate()
            proc_holder.wait()

    def test_success_marker_and_watcher_fields_compatibility(self):
        """Success marker must contain all fields required by VPS watcher and post-sync hook."""
        def handle_health(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        def handle_status(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ready": true, "counts": {"messages": 42}, "dbUpdatedKst": "2026-09-14 11:00:00 KST"}')

        def handle_sync(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        self.mock_server.register("GET", "/api/health", handle_health)
        self.mock_server.register("GET", "/api/status", handle_status)
        self.mock_server.register("POST", "/api/sync", handle_sync)

        proc = self._run_worker()
        self.assertEqual(proc.returncode, 0)

        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state.get("status"), "SUCCESS")
        self.assertTrue(state.get("ready"))
        self.assertEqual(state.get("messages_before"), 42)
        self.assertEqual(state.get("messages_after"), 42)
        self.assertEqual(state.get("db_updated_kst"), "2026-09-14 11:00:00 KST")
        self.assertIn("started_at", state)
        self.assertIn("completed_at", state)
        self.assertIn("elapsed_seconds", state)
        self.assertIn("attempts", state)

        # completed_at must be parseable ISO format (dedupe key for VPS watcher)
        self.assertRegex(state["completed_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_installer_workflow(self):
        """Verify install-sync-worker.ps1 idempotency, backup, and uninstall."""
        installer_script = PROJECT_ROOT / "install-sync-worker.ps1"
        self.assertTrue(installer_script.exists())

        test_dest = self.root_dir / "installed-worker.ps1"
        test_dest.write_text("# Old worker\nexit 0\n", encoding="utf-8")

        # 1. Install
        cmd_install = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(installer_script),
            "-SourceScript", str(SYNC_SCRIPT),
            "-DestinationScript", str(test_dest),
        ]
        proc = subprocess.run(cmd_install, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Install failed: {proc.stderr}")
        self.assertIn("installed successfully", proc.stdout)

        # Check backup created
        backup_file = test_dest.with_name("installed-worker.ps1.bak")
        self.assertTrue(backup_file.exists())
        self.assertEqual(backup_file.read_text(encoding="utf-8").strip(), "# Old worker\nexit 0")

        # 2. Idempotent re-run
        proc_idemp = subprocess.run(cmd_install, capture_output=True, text=True)
        self.assertEqual(proc_idemp.returncode, 0)
        self.assertIn("already up to date", proc_idemp.stdout)

        # 3. Status
        cmd_status = cmd_install + ["-Status"]
        proc_status = subprocess.run(cmd_status, capture_output=True, text=True)
        self.assertEqual(proc_status.returncode, 0)
        self.assertIn("Dest Exists : True", proc_status.stdout)

        # 4. Uninstall
        cmd_uninstall = cmd_install + ["-Uninstall"]
        proc_uninstall = subprocess.run(cmd_uninstall, capture_output=True, text=True)
        self.assertEqual(proc_uninstall.returncode, 0)
        self.assertIn("restored from backup", proc_uninstall.stdout)
        self.assertEqual(test_dest.read_text(encoding="utf-8").strip(), "# Old worker\nexit 0")

    def test_max_attempts_clamped_to_three(self):
        """Passing -MaxAttempts 5 must be clamped to 3 (max 3 attempts invariant)."""
        status_calls = []

        def handle_health(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        def handle_status(req):
            status_calls.append(time.time())
            req.send_response(503)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"error": "server busy"}')

        self.mock_server.register("GET", "/api/health", handle_health)
        self.mock_server.register("GET", "/api/status", handle_status)

        proc = self._run_worker(["-MaxAttempts", "5", "-BackoffSec", "0", "-StatusTimeoutSec", "1"])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(status_calls), 3)

        self.assertTrue(self.state_file.exists())
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state.get("status"), "FAILED")
        self.assertEqual(state.get("attempts"), 3)

    def test_negative_backoff_and_timeout_normalized(self):
        """Worker should normalize negative backoff and timeout to safe bounded values and succeed."""
        status_calls = []

        def handle_health(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        def handle_status(req):
            status_calls.append(time.time())
            if len(status_calls) == 1:
                req.send_response(503)
                req.end_headers()
                return
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ready": true, "counts": {"messages": 10}, "dbUpdatedKst": "..."}')

        def handle_sync(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        self.mock_server.register("GET", "/api/health", handle_health)
        self.mock_server.register("GET", "/api/status", handle_status)
        self.mock_server.register("POST", "/api/sync", handle_sync)

        proc = self._run_worker(["-BackoffSec", "-5", "-StatusTimeoutSec", "-1", "-MaxAttempts", "3"])
        self.assertEqual(proc.returncode, 0, f"Expected 0, got {proc.returncode}. Stderr: {proc.stderr}")
        self.assertTrue(self.state_file.exists())
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state.get("status"), "SUCCESS")
        self.assertEqual(state.get("attempts"), 2)

    def test_unknown_http_status_normalized_to_http_error_and_no_retry(self):
        """Unknown HTTP status codes (e.g. 404) must normalize to HTTP_ERROR and fail immediately without retry."""
        status_calls = []

        def handle_health(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        def handle_status(req):
            status_calls.append(time.time())
            req.send_response(404)
            req.end_headers()

        self.mock_server.register("GET", "/api/health", handle_health)
        self.mock_server.register("GET", "/api/status", handle_status)

        proc = self._run_worker(["-MaxAttempts", "3", "-BackoffSec", "0", "-StatusTimeoutSec", "1"])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(status_calls), 1)  # Must NOT retry

        self.assertTrue(self.state_file.exists())
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state.get("status"), "FAILED")
        self.assertEqual(state.get("error"), "HTTP_ERROR")
        self.assertEqual(state.get("attempts"), 1)

    def test_non_transient_post_sync_not_ready_fails_immediately(self):
        """POST_SYNC_NOT_READY is non-transient and must fail immediately without retry."""
        status_calls = []

        def handle_health(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        def handle_status(req):
            status_calls.append(time.time())
            if len(status_calls) == 1:
                req.send_response(200)
                req.send_header("Content-Type", "application/json")
                req.end_headers()
                req.wfile.write(b'{"ready": true, "counts": {"messages": 10}, "dbUpdatedKst": "..."}')
            else:
                req.send_response(200)
                req.send_header("Content-Type", "application/json")
                req.end_headers()
                req.wfile.write(b'{"ready": false, "counts": {"messages": 10}, "dbUpdatedKst": "..."}')

        def handle_sync(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        self.mock_server.register("GET", "/api/health", handle_health)
        self.mock_server.register("GET", "/api/status", handle_status)
        self.mock_server.register("POST", "/api/sync", handle_sync)

        proc = self._run_worker(["-MaxAttempts", "3", "-BackoffSec", "0", "-StatusTimeoutSec", "1"])
        self.assertEqual(proc.returncode, 1)

        self.assertTrue(self.state_file.exists())
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state.get("status"), "FAILED")
        self.assertEqual(state.get("error"), "POST_SYNC_NOT_READY")
        self.assertEqual(state.get("attempts"), 1)

    def test_transient_server_busy_500_body_retries_and_recovers(self):
        """HTTP 500 with 'already running' body must be classified as SERVER_BUSY and retried."""
        status_calls = []

        def handle_health(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        def handle_status(req):
            status_calls.append(time.time())
            if len(status_calls) == 1:
                req.send_response(500)
                req.send_header("Content-Type", "text/plain")
                req.end_headers()
                req.wfile.write(b'error: database job already running')
                return
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ready": true, "counts": {"messages": 10}, "dbUpdatedKst": "..."}')

        def handle_sync(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        self.mock_server.register("GET", "/api/health", handle_health)
        self.mock_server.register("GET", "/api/status", handle_status)
        self.mock_server.register("POST", "/api/sync", handle_sync)

        proc = self._run_worker(["-MaxAttempts", "3", "-BackoffSec", "0", "-StatusTimeoutSec", "1"])
        self.assertEqual(proc.returncode, 0, f"Expected 0, got {proc.returncode}. Stderr: {proc.stderr}")

        self.assertTrue(self.state_file.exists())
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state.get("status"), "SUCCESS")
        self.assertEqual(state.get("attempts"), 2)

    def test_total_deadline_and_parameter_bounds(self):
        """Verify TotalDeadlineSec clamped to 1..840, MaxAttempts to 1..3, negative values normalized."""
        ps_bounds_test = (
            f"$scriptContent = [System.IO.File]::ReadAllText('{SYNC_SCRIPT}')\n"
            "$idxStart = 0\n"
            "$idxEnd = $scriptContent.IndexOf('$stateDir = Join-Path $root')\n"
            "$scriptSnippet = $scriptContent.Substring($idxStart, $idxEnd)\n"
            "$wrapper = 'function Test-ScriptBounds { ' + $scriptSnippet + \"`n\" + "
            "'[PSCustomObject]@{ MaxAttempts=$MaxAttempts; TotalDeadlineSec=$TotalDeadlineSec; StatusTimeoutSec=$StatusTimeoutSec; SyncTimeoutSec=$SyncTimeoutSec; BackoffSec=$BackoffSec } }'\n"
            "Invoke-Expression $wrapper\n"
            "$upper = Test-ScriptBounds -MaxAttempts 5 -TotalDeadlineSec 1000 -StatusTimeoutSec 1000 -SyncTimeoutSec 2000 -BackoffSec 100\n"
            "$lower = Test-ScriptBounds -MaxAttempts 0 -TotalDeadlineSec -50 -StatusTimeoutSec -5 -SyncTimeoutSec -10 -BackoffSec -5\n"
            "@{ upper = $upper; lower = $lower } | ConvertTo-Json -Compress\n"
        )
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "-"],
            input=ps_bounds_test,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"Stderr: {proc.stderr}")
        res = json.loads(proc.stdout.strip())
        # Upper bounds
        self.assertEqual(res["upper"]["MaxAttempts"], 3)
        self.assertEqual(res["upper"]["TotalDeadlineSec"], 840)
        self.assertLessEqual(res["upper"]["StatusTimeoutSec"], 840)
        self.assertLessEqual(res["upper"]["SyncTimeoutSec"], 840)
        self.assertLessEqual(res["upper"]["BackoffSec"], 60)
        # Lower bounds
        self.assertEqual(res["lower"]["MaxAttempts"], 1)
        self.assertEqual(res["lower"]["TotalDeadlineSec"], 1)
        self.assertGreaterEqual(res["lower"]["StatusTimeoutSec"], 1)
        self.assertGreaterEqual(res["lower"]["SyncTimeoutSec"], 1)
        self.assertGreaterEqual(res["lower"]["BackoffSec"], 0)

    def test_get_safe_error_code_allowlist_direct(self):
        """Test Get-SafeErrorCode for unknown HTTP statuses (e.g. 404, 400, 501) normalizing to HTTP_ERROR."""
        ps_error_code_test = (
            f"$scriptContent = [System.IO.File]::ReadAllText('{SYNC_SCRIPT}')\n"
            "$idx1 = $scriptContent.IndexOf('function Get-SafeErrorCode')\n"
            "$idx2 = $scriptContent.IndexOf('$root = if ($RootDirectory)')\n"
            "$fnDef = $scriptContent.Substring($idx1, $idx2 - $idx1)\n"
            "Invoke-Expression $fnDef\n"
            "$r404 = Get-SafeErrorCode 'HTTP_404'\n"
            "$r400 = Get-SafeErrorCode 'HTTP_400'\n"
            "$r501 = Get-SafeErrorCode 'HTTP_501'\n"
            "$r500 = Get-SafeErrorCode 'HTTP_500'\n"
            "$r502 = Get-SafeErrorCode 'HTTP_502'\n"
            "$r503 = Get-SafeErrorCode 'HTTP_503'\n"
            "$r504 = Get-SafeErrorCode 'HTTP_504'\n"
            "@{ r404=$r404; r400=$r400; r501=$r501; r500=$r500; r502=$r502; r503=$r503; r504=$r504 } | ConvertTo-Json -Compress\n"
        )
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "-"],
            input=ps_error_code_test,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"Stderr: {proc.stderr}")
        res = json.loads(proc.stdout.strip())
        self.assertEqual(res["r404"], "HTTP_ERROR")
        self.assertEqual(res["r400"], "HTTP_ERROR")
        self.assertEqual(res["r501"], "HTTP_ERROR")
        self.assertEqual(res["r500"], "HTTP_500")
        self.assertEqual(res["r502"], "HTTP_502")
        self.assertEqual(res["r503"], "HTTP_503")
        self.assertEqual(res["r504"], "HTTP_504")

    def test_last_sync_marker_local_and_mirror_independent_fields_on_success(self):
        """Verify that last-sync.json records independent local and mirror structures on success while preserving watcher fields."""
        def handle_health(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        def handle_status(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ready": true, "counts": {"messages": 1000}, "dbUpdatedKst": "2026-09-14 10:00:00 KST"}')

        def handle_sync(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        self.mock_server.register("GET", "/api/health", handle_health)
        self.mock_server.register("GET", "/api/status", handle_status)
        self.mock_server.register("POST", "/api/sync", handle_sync)

        proc = self._run_worker()
        self.assertEqual(proc.returncode, 0, f"Expected 0, got {proc.returncode}. Stderr: {proc.stderr}")

        self.assertTrue(self.state_file.exists())
        state = json.loads(self.state_file.read_text(encoding="utf-8"))

        # Top-level watcher compatibility fields
        self.assertEqual(state.get("status"), "SUCCESS")
        self.assertEqual(state.get("stage"), "COMPLETE")
        self.assertIn("completed_at", state)
        self.assertEqual(state.get("messages_after"), 1000)

        # Independent local and mirror structures
        self.assertIn("local", state)
        self.assertIsInstance(state["local"], dict)
        self.assertEqual(state["local"].get("status"), "SUCCESS")
        self.assertEqual(state["local"].get("messages_after"), 1000)

        self.assertIn("mirror", state)
        self.assertIsInstance(state["mirror"], dict)
        self.assertEqual(state["mirror"].get("status"), "SKIPPED")

    def test_last_sync_marker_mirror_failure_marks_overall_failed_and_distinguishes_local_success(self):
        """Verify that when local sync succeeds but mirror fails:
        1. Top-level status is FAILED (never hidden as SUCCESS).
        2. local.status is SUCCESS and mirror.status is FAILED.
        3. mirror failure errorCode is recorded from allowlist.
        4. completed_at ISO timestamp is preserved for watcher dedupe.
        5. Worker process exits with code 1.
        """
        def handle_health(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        def handle_status(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ready": true, "counts": {"messages": 1000}, "dbUpdatedKst": "2026-09-14 10:00:00 KST"}')

        def handle_sync(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        self.mock_server.register("GET", "/api/health", handle_health)
        self.mock_server.register("GET", "/api/status", handle_status)
        self.mock_server.register("POST", "/api/sync", handle_sync)

        # Setup mock failing mirror script and mirror state in root_dir
        mock_output_dir = self.root_dir / "output" / "testuser"
        mock_output_dir.mkdir(parents=True, exist_ok=True)
        mirror_state_file = mock_output_dir / "mirror_state.json"
        mirror_state_file.write_text(json.dumps({
            "status": "FAILED",
            "lastStage": "REMOTE_VERIFY",
            "errorCode": "HASH_MISMATCH",
            "lastErrorKst": "2026-09-24T08:00:00+09:00",
        }), encoding="utf-8")

        mock_post_sync = self.root_dir / "post-sync-mirror.ps1"
        mock_post_sync.write_text(
            "param([switch]$UploadOnly)\n"
            "Write-Error '[post-sync-mirror] kwin v2mirror failed with exit code 1'\n"
            "exit 1\n",
            encoding="utf-8",
        )

        # Run worker WITHOUT -SkipMirror, passing our mock post-sync script
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SYNC_SCRIPT),
            "-RootDirectory",
            str(self.root_dir),
            "-ApiBase",
            self.api_base,
            "-MutexName",
            self.mutex_name,
            "-SkipProcessCheck",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 1, f"Worker must exit 1 on mirror failure. Output: {proc.stdout}\nStderr: {proc.stderr}")

        self.assertTrue(self.state_file.exists())
        state = json.loads(self.state_file.read_text(encoding="utf-8"))

        # Must not hide mirror failure as SUCCESS
        self.assertEqual(state.get("status"), "FAILED", f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nstate:\n{json.dumps(state)}")
        self.assertEqual(state.get("stage"), "REMOTE_VERIFY")
        self.assertEqual(state.get("error"), "HASH_MISMATCH")
        self.assertIn("completed_at", state)

        # Check independent local and mirror structures
        self.assertIn("local", state)
        self.assertEqual(state["local"].get("status"), "SUCCESS")
        self.assertEqual(state["local"].get("messages_after"), 1000)

        self.assertIn("mirror", state)
        self.assertEqual(state["mirror"].get("status"), "FAILED")
        self.assertEqual(state["mirror"].get("error"), "HASH_MISMATCH")

    def test_stale_mirror_state_not_reused_and_records_local_success_mirror_fail_with_diag_log(self):
        """Verify reproduction of observed incident (2026-09-26 09:19):
        1. Local sync succeeds (counts: messages 1,000).
        2. A stale mirror_state.json exists from 2 days prior (status=SUCCESS, lastStage=COMPLETE).
        3. Mirror child process fails with non-zero exit code (1) and produces NO fresh state.
        4. Stale COMPLETE state MUST NOT be reused.
        5. Top-level status is FAILED, but local.status is SUCCESS and mirror.status is FAILED.
        6. stage is not COMPLETE (fallback 'mirror'), error is MIRROR_FAILED.
        7. A separate JSONL diagnostic log is written recording child_exit_code, fresh_state=False,
           lastStage, allowlist-based errorCode without any sensitive payloads/paths.
        """
        def handle_health(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        def handle_status(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ready": true, "counts": {"messages": 1000}, "dbUpdatedKst": "2026-09-14 10:00:00 KST"}')

        def handle_sync(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        self.mock_server.register("GET", "/api/health", handle_health)
        self.mock_server.register("GET", "/api/status", handle_status)
        self.mock_server.register("POST", "/api/sync", handle_sync)

        # Setup STALE mirror_state.json (status=SUCCESS, lastStage=COMPLETE from 2 days ago)
        mock_output_dir = self.root_dir / "output" / "testuser"
        mock_output_dir.mkdir(parents=True, exist_ok=True)
        mirror_state_file = mock_output_dir / "mirror_state.json"
        mirror_state_file.write_text(json.dumps({
            "status": "SUCCESS",
            "lastStage": "COMPLETE",
            "lastSuccessKst": "2026-09-24 10:00:00 KST",
            "localSnapshot": {"messageCount": 500, "sha256": "abcdef"},
            "remoteMirror": {"messageCount": 500, "sha256": "abcdef"},
        }), encoding="utf-8")
        # Set modification time to 2 days in the past
        old_time = time.time() - 172800
        os.utime(mirror_state_file, (old_time, old_time))

        # Mirror child fails with exit code 1 and writes nothing
        mock_post_sync = self.root_dir / "post-sync-mirror.ps1"
        mock_post_sync.write_text(
            "param([switch]$UploadOnly)\n"
            "Write-Error '[post-sync-mirror] child crashed'\n"
            "exit 1\n",
            encoding="utf-8",
        )

        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SYNC_SCRIPT),
            "-RootDirectory",
            str(self.root_dir),
            "-ApiBase",
            self.api_base,
            "-MutexName",
            self.mutex_name,
            "-SkipProcessCheck",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 1, f"Worker must exit 1 on mirror failure. Output: {proc.stdout}\nStderr: {proc.stderr}")

        self.assertTrue(self.state_file.exists())
        state = json.loads(self.state_file.read_text(encoding="utf-8"))

        # Top-level status is FAILED
        self.assertEqual(state.get("status"), "FAILED")
        # Stage must NEVER be COMPLETE on failure!
        self.assertNotEqual(state.get("stage"), "COMPLETE")
        self.assertEqual(state.get("stage"), "mirror")
        self.assertEqual(state.get("error"), "MIRROR_FAILED")

        # Independent local and mirror structures: local MUST be SUCCESS, mirror MUST be FAILED
        self.assertIn("local", state)
        self.assertEqual(state["local"].get("status"), "SUCCESS")
        self.assertEqual(state["local"].get("messages_after"), 1000)

        self.assertIn("mirror", state)
        self.assertEqual(state["mirror"].get("status"), "FAILED")
        self.assertNotEqual(state["mirror"].get("stage"), "COMPLETE")
        self.assertEqual(state["mirror"].get("error"), "MIRROR_FAILED")

        # Verify separate JSONL diagnostic log
        diag_files = list(self.log_dir.glob("mirror-diag-*.jsonl"))
        self.assertTrue(len(diag_files) > 0, f"Expected mirror-diag-*.jsonl in {self.log_dir}, found none.")
        diag_lines = [l.strip() for l in diag_files[0].read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertTrue(len(diag_lines) > 0)
        diag = json.loads(diag_lines[-1])

        # Contract assertions for diagnostic log
        self.assertIn("timestamp", diag)
        self.assertEqual(diag.get("exit_code"), 1)
        self.assertEqual(diag.get("fresh_state"), False)
        self.assertEqual(diag.get("lastStage"), "mirror")
        self.assertEqual(diag.get("errorCode"), "MIRROR_FAILED")

        # Sensitive info check: no path, no remote host, no stdout/stderr, no raw error
        diag_raw = diag_files[0].read_text(encoding="utf-8")
        self.assertNotIn("D:\\kakao", diag_raw)
        self.assertNotIn("child crashed", diag_raw)
        self.assertNotIn("Write-Error", diag_raw)

    def test_mirror_failure_with_unallowlisted_error_code_falls_back_to_mirror_failed(self):
        """Verify that an unallowlisted error code from fresh mirror state falls back to MIRROR_FAILED."""
        def handle_health(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        def handle_status(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ready": true, "counts": {"messages": 1000}, "dbUpdatedKst": "2026-09-14 10:00:00 KST"}')

        def handle_sync(req):
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": true}')

        self.mock_server.register("GET", "/api/health", handle_health)
        self.mock_server.register("GET", "/api/status", handle_status)
        self.mock_server.register("POST", "/api/sync", handle_sync)

        mock_output_dir = self.root_dir / "output" / "testuser"
        mock_output_dir.mkdir(parents=True, exist_ok=True)
        mirror_state_file = mock_output_dir / "mirror_state.json"
        mirror_state_file.write_text(json.dumps({
            "status": "FAILED",
            "lastStage": "REMOTE_VERIFY",
            "errorCode": "ARBITRARY_UNALLOWLISTED_ERROR_CODE_XYZ",
            "lastErrorKst": "2026-09-24T08:00:00+09:00",
        }), encoding="utf-8")

        mock_post_sync = self.root_dir / "post-sync-mirror.ps1"
        mock_post_sync.write_text(
            "param([switch]$UploadOnly)\n"
            "exit 1\n",
            encoding="utf-8",
        )

        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SYNC_SCRIPT),
            "-RootDirectory",
            str(self.root_dir),
            "-ApiBase",
            self.api_base,
            "-MutexName",
            self.mutex_name,
            "-SkipProcessCheck",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 1)

        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        # Unallowlisted error code must fallback to MIRROR_FAILED
        self.assertEqual(state.get("error"), "MIRROR_FAILED")
        self.assertEqual(state["mirror"].get("error"), "MIRROR_FAILED")

    def test_post_sync_mirror_runner_resolves_kwin_from_external_cwd(self):
        """Verify that post-sync-mirror.ps1 ensures PYTHONPATH and sets ProjectRoot so kwin is resolved even when called from an arbitrary external Cwd."""
        repo_root = Path(__file__).resolve().parent.parent
        post_sync_script = repo_root / "post-sync-mirror.ps1"
        self.assertTrue(post_sync_script.exists())

        with tempfile.TemporaryDirectory() as external_cwd:
            cmd = [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(post_sync_script),
                "-Status",
            ]
            proc = subprocess.run(cmd, cwd=external_cwd, capture_output=True, text=True, timeout=15)
            self.assertEqual(proc.returncode, 0, f"Failed from external cwd: {proc.stderr}")

            # Verify that .venv python resolves kwin when invoked from external cwd
            venv_python = repo_root / ".venv" / "Scripts" / "python.exe"
            python_bin = str(venv_python) if venv_python.exists() else sys.executable
            py_cmd = [
                python_bin,
                "-c",
                "import kwin; print('OK')",
            ]
            proc_py = subprocess.run(py_cmd, cwd=external_cwd, capture_output=True, text=True, timeout=15)
            self.assertEqual(proc_py.returncode, 0, f"Python kwin import failed from external cwd: {proc_py.stderr}")


if __name__ == "__main__":
    unittest.main()
