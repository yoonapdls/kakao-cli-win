"""Comprehensive unit tests for SQLite backup snapshot, ProcessLock, StateMarker,
VPS mirror pipeline, and post-sync hook installer.

Uses ONLY synthetic in-memory/temporary SQLite databases.
Verifies:
1. SQLite backup API read-only consistent snapshot & integrity check (no paths leaked)
2. Atomic ProcessLock with O_CREAT|O_EXCL and safe stale lock recovery
3. Atomic StateMarker via temp file + os.replace without path or raw error leakage
4. STOP marker / disable handling (sanitized output)
5. Remote mirror update lifecycle (.partial -> remote verify -> atomic replace)
6. Remote verification comparing count, minSentAtIso, maxSentAtIso, and sha256
7. Partial cleanup on ALL failures (count/min/max mismatch, hash mismatch, parse error, timeout)
8. Remote path validation, option injection prevention, POSIX quoting, and SSH hardening
9. State persistence of lastStage & errorCode instead of raw lastError
10. Sensitive payload and path protection (no message bodies, usernames, or absolute paths)
11. install-post-sync-mirror.ps1 PowerShell workflow (idempotency, backup, uninstall, conditional hook)
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from kwin import mirror


class TestMirrorPipeline(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base_path = Path(self.temp_dir.name)
        self.src_db = self.base_path / "messages_v2.sqlite"
        self.snapshot_dir = self.base_path / "snapshot"
        self.state_file = self.base_path / "mirror_state.json"
        self.lock_file = self.base_path / ".mirror.lock"
        self.stop_file = self.base_path / "STOP_MIRROR"

        # Create a synthetic messages_v2.sqlite
        conn = sqlite3.connect(str(self.src_db))
        conn.execute("""
            CREATE TABLE messages (
                chatId INTEGER,
                logId INTEGER PRIMARY KEY,
                authorId INTEGER,
                authorName TEXT,
                type INTEGER,
                message TEXT,
                sentAt INTEGER,
                sentAtIso TEXT
            )
        """)
        conn.executemany(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
            [
                (1, 101, 10, "Alice", 1, "Hello world", 1700000000, "2023-11-14T22:13:20"),
                (1, 102, 20, "Bob", 1, "Confidential secret chat", 1700000010, "2023-11-14T22:13:30"),
                (2, 201, 30, "Charlie", 1, "Another room message", 1700000020, "2023-11-14T22:13:40"),
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_readonly_snapshot_creation_and_path_sanitization(self):
        """Verify snapshot creation, correct stats, and absence of snapshotPath in return dict."""
        snap_file = self.snapshot_dir / "test_snapshot.sqlite"
        res = mirror.create_readonly_snapshot(self.src_db, snap_file)

        self.assertTrue(snap_file.exists())
        self.assertNotIn("snapshotPath", res)
        self.assertEqual(res["integrityCheck"], "ok")
        self.assertEqual(res["messageCount"], 3)
        self.assertEqual(res["minSentAtIso"], "2023-11-14T22:13:20")
        self.assertEqual(res["maxSentAtIso"], "2023-11-14T22:13:40")

        computed_sha = mirror.compute_sha256(snap_file)
        self.assertEqual(res["sha256"], computed_sha)

        # Verify snapshot content
        conn = sqlite3.connect(str(snap_file))
        try:
            count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
            self.assertEqual(count, 3)
        finally:
            conn.close()

    def test_statemarker_atomic_write_and_sanitization(self):
        """Verify StateMarker persists atomically via temp+os.replace and strips prohibited fields."""
        marker = mirror.StateMarker(self.state_file)
        marker.update(
            status="SUCCESS",
            snapshotPath="/secret/path/to/snap.sqlite",
            remoteTarget="secret-host",
            lastError="Some secret error message",
        )

        state = marker.read()
        self.assertEqual(state["status"], "SUCCESS")
        self.assertNotIn("snapshotPath", state)
        self.assertNotIn("remoteTarget", state)
        self.assertNotIn("lastError", state)
        self.assertTrue(self.state_file.exists())

    def test_statemarker_transition_failure_to_success_cleans_stale_failure_fields(self):
        """Verify StateMarker on transition to SUCCESS removes errorCode/lastErrorKst and sets lastStage=COMPLETE."""
        marker = mirror.StateMarker(self.state_file)
        # Previous failure state
        marker.update(
            status="FAILED",
            lastStage="REMOTE_VERIFY",
            errorCode="TIMEOUT",
            lastErrorKst="2026-09-12T17:00:00+09:00",
        )
        prev_state = marker.read()
        self.assertEqual(prev_state["status"], "FAILED")
        self.assertEqual(prev_state["lastStage"], "REMOTE_VERIFY")
        self.assertEqual(prev_state["errorCode"], "TIMEOUT")
        self.assertIn("lastErrorKst", prev_state)

        # Transition to SUCCESS
        marker.update(
            status="SUCCESS",
            lastSuccessKst="2026-09-12T18:00:00+09:00",
        )
        new_state = marker.read()
        self.assertEqual(new_state["status"], "SUCCESS")
        self.assertEqual(new_state["lastStage"], "COMPLETE")
        self.assertNotIn("errorCode", new_state)
        self.assertNotIn("lastErrorKst", new_state)

    def test_statemarker_in_progress_cleans_stale_failure_fields(self):
        """Verify StateMarker on starting new execution (IN_PROGRESS) removes stale failure fields."""
        marker = mirror.StateMarker(self.state_file)
        # Previous failure state
        marker.update(
            status="FAILED",
            lastStage="REMOTE_VERIFY",
            errorCode="TIMEOUT",
            lastErrorKst="2026-09-12T17:00:00+09:00",
        )

        # New execution starts
        marker.update(
            status="IN_PROGRESS",
            lastAttemptKst="2026-09-12T18:00:00+09:00",
        )
        state = marker.read()
        self.assertEqual(state["status"], "IN_PROGRESS")
        self.assertEqual(state["lastStage"], "INIT")
        self.assertNotIn("errorCode", state)
        self.assertNotIn("lastErrorKst", state)

    def test_sync_mirror_failure_then_success_regression(self):
        """Regression test: sync_mirror fails at verify, then subsequent run succeeds, removing errorCode and setting lastStage=COMPLETE."""
        call_count = {"verify": 0}

        def mock_ssh_runner(ssh_cmd, remote_cmd, timeout=None):
            if "PRAGMA integrity_check;" in remote_cmd:
                call_count["verify"] += 1
                if call_count["verify"] == 1:
                    raise subprocess.TimeoutExpired(cmd="verify", timeout=600)
            return 0, "", ""

        def mock_upload(scp_cmd, local_p, remote_dst):
            return 0, "", ""

        def mock_download(scp_cmd, remote_src, local_dest):
            snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
            actual_sha = mirror.compute_sha256(snap_path)
            content = json.dumps({
                "integrityCheck": "ok",
                "messageCount": 3,
                "minSentAtIso": "2023-11-14T22:13:20",
                "maxSentAtIso": "2023-11-14T22:13:40",
                "sha256": actual_sha,
            })
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        # Run 1: Failure
        with self.assertRaises(mirror.MirrorPipelineError) as ctx:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh_runner,
                upload_runner=mock_upload,
                download_runner=mock_download,
            )
        self.assertEqual(ctx.exception.stage, "REMOTE_VERIFY")
        self.assertEqual(ctx.exception.error_code, "TIMEOUT")

        fail_state = mirror.StateMarker(self.state_file).read()
        self.assertEqual(fail_state["status"], "FAILED")
        self.assertEqual(fail_state["lastStage"], "REMOTE_VERIFY")
        self.assertEqual(fail_state["errorCode"], "TIMEOUT")
        self.assertIn("lastErrorKst", fail_state)

        # Run 2: Success
        res = mirror.sync_mirror(
            src_db=self.src_db,
            snapshot_dir=self.snapshot_dir,
            state_file=self.state_file,
            lock_file=self.lock_file,
            stop_file=self.stop_file,
            vps_ssh_target="user@test.vps",
            vps_remote_dir="/var/data/kakao",
            ssh_runner=mock_ssh_runner,
            upload_runner=mock_upload,
            download_runner=mock_download,
        )
        self.assertEqual(res["status"], "SUCCESS")

        succ_state = mirror.StateMarker(self.state_file).read()
        self.assertEqual(succ_state["status"], "SUCCESS")
        self.assertEqual(succ_state["lastStage"], "COMPLETE")
        self.assertNotIn("errorCode", succ_state)
        self.assertNotIn("lastErrorKst", succ_state)

    def test_powershell_post_sync_mirror_status_no_error_code_on_success(self):
        """Verify post-sync-mirror.ps1 -Status does not display Error Code when status is SUCCESS."""
        post_sync_script = Path(__file__).resolve().parent.parent / "post-sync-mirror.ps1"
        self.assertTrue(post_sync_script.exists())

        test_dir = self.base_path / "ps_status_test"
        test_dir.mkdir(parents=True, exist_ok=True)
        ps_copy = test_dir / "post-sync-mirror.ps1"
        shutil.copy2(post_sync_script, ps_copy)

        out_dir = test_dir / "data" / "output" / "test_user"
        out_dir.mkdir(parents=True, exist_ok=True)
        state_file = out_dir / "mirror_state.json"

        # Case 1: Normal SUCCESS state (clean marker)
        state_file.write_text(
            json.dumps({
                "status": "SUCCESS",
                "lastStage": "COMPLETE",
                "lastSuccessKst": "2026-09-12T18:00:00+09:00",
                "localSnapshot": {"messageCount": 42},
            }),
            encoding="utf-8",
        )

        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(ps_copy), "-Status"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Mirror Status: SUCCESS", proc.stdout)
        self.assertIn("Last Stage   : COMPLETE", proc.stdout)
        self.assertNotIn("Error Code", proc.stdout)

        # Case 2: Legacy state with status SUCCESS but stale errorCode
        state_file.write_text(
            json.dumps({
                "status": "SUCCESS",
                "lastStage": "COMPLETE",
                "errorCode": "TIMEOUT",
                "lastErrorKst": "2026-09-12T17:00:00+09:00",
                "lastSuccessKst": "2026-09-12T18:00:00+09:00",
                "localSnapshot": {"messageCount": 42},
            }),
            encoding="utf-8",
        )

        proc2 = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(ps_copy), "-Status"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc2.returncode, 0)
        self.assertIn("Mirror Status: SUCCESS", proc2.stdout)
        self.assertNotIn("Error Code", proc2.stdout)


    def test_process_lock_atomic_o_creat_excl_and_stale_recovery(self):
        """Verify ProcessLock uses atomic creation and breaks stale locks safely."""
        lock1 = mirror.ProcessLock(self.lock_file, timeout_seconds=1)
        lock2 = mirror.ProcessLock(self.lock_file, timeout_seconds=1)

        self.assertTrue(lock1.acquire())
        self.assertFalse(lock2.acquire())

        # Test stale lock recovery: lock held by nonexistent pid
        lock1.release()
        self.lock_file.write_text(
            json.dumps({"pid": 99999999, "timestamp": time.time() - 100}),
            encoding="utf-8",
        )
        # lock2 should recognize stale lock and acquire it
        self.assertTrue(lock2.acquire())
        lock2.release()
        self.assertFalse(self.lock_file.exists())

    def test_stop_marker_bypasses_sync_and_sanitizes_return(self):
        """Verify STOP marker skips execution and returns STOPPED status without stopFile path."""
        self.stop_file.write_text("Disabled", encoding="utf-8")

        res = mirror.sync_mirror(
            src_db=self.src_db,
            snapshot_dir=self.snapshot_dir,
            state_file=self.state_file,
            lock_file=self.lock_file,
            stop_file=self.stop_file,
            vps_ssh_target="user@remote.vps",
            vps_remote_dir="/opt/kakao/backup",
        )

        self.assertEqual(res["status"], "STOPPED")
        self.assertNotIn("stopFile", res)
        self.assertNotIn("remoteTarget", res)
        self.assertFalse(self.snapshot_dir.exists())

    def test_lock_active_sanitizes_return(self):
        """Verify LOCKED status returns sanitized response without lockFile path."""
        lock = mirror.ProcessLock(self.lock_file)
        self.assertTrue(lock.acquire())
        try:
            res = mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
            )
            self.assertEqual(res["status"], "LOCKED")
            self.assertNotIn("lockFile", res)
        finally:
            lock.release()

    def test_mock_remote_pipeline_successful_sync_and_sanitization(self):
        """Verify remote mirror workflow, SSH hardening options, sanitized return fields, and 600s verify timeout."""
        self.assertEqual(mirror.DEFAULT_STAGE_TIMEOUTS["verify"], 600)
        self.assertEqual(mirror.DEFAULT_STAGE_TIMEOUTS["download"], 60)
        self.assertEqual(mirror.DEFAULT_STAGE_TIMEOUTS["upload"], 300)
        self.assertEqual(mirror.DEFAULT_STAGE_TIMEOUTS["prepare"], 30)
        self.assertEqual(mirror.DEFAULT_STAGE_TIMEOUTS["replace"], 30)
        self.assertEqual(mirror.DEFAULT_STAGE_TIMEOUTS["cleanup"], 30)

        commands_run = []
        recorded_timeouts = {}

        def mock_ssh_runner(ssh_cmd, remote_cmd, timeout=None):
            commands_run.append(("ssh", ssh_cmd, remote_cmd))
            if "PRAGMA integrity_check;" in remote_cmd:
                recorded_timeouts["verify"] = timeout
            # Remote commands (mkdir, verify script, clean meta, replace, check) produce no stdout
            return 0, "", ""

        def mock_upload_runner(scp_cmd, local_path, remote_dest):
            commands_run.append(("upload", scp_cmd, remote_dest))
            return 0, "uploaded", ""

        def mock_download_runner(scp_cmd, remote_src, local_dest):
            commands_run.append(("download", scp_cmd, remote_src))
            snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
            actual_sha = mirror.compute_sha256(snap_path)
            content = f"ok\n3|2023-11-14T22:13:20|2023-11-14T22:13:40\n{actual_sha}"
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        res = mirror.sync_mirror(
            src_db=self.src_db,
            snapshot_dir=self.snapshot_dir,
            state_file=self.state_file,
            lock_file=self.lock_file,
            stop_file=self.stop_file,
            vps_ssh_target="user@test.vps",
            vps_remote_dir="/var/data/kakao",
            ssh_runner=mock_ssh_runner,
            upload_runner=mock_upload_runner,
            download_runner=mock_download_runner,
        )

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["localSnapshot"]["messageCount"], 3)
        self.assertNotIn("snapshotPath", res["localSnapshot"])
        self.assertEqual(res["remoteMirror"]["messageCount"], 3)
        self.assertNotIn("remoteTarget", res["remoteMirror"])
        self.assertNotIn("remoteCurrent", res["remoteMirror"])

        # Check verify timeout 전달값이 600
        self.assertEqual(recorded_timeouts.get("verify"), 600)

        # Check SSH options
        ssh_cmds = [c[1] for c in commands_run if c[0] == "ssh"]
        for cmd in ssh_cmds:
            self.assertIn("-o", cmd)
            self.assertIn("BatchMode=yes", cmd)
            self.assertIn("StrictHostKeyChecking=yes", cmd)

        scp_cmds = [c[1] for c in commands_run if c[0] in ("upload", "download")]
        for cmd in scp_cmds:
            self.assertIn("-o", cmd)
            self.assertIn("BatchMode=yes", cmd)
            self.assertIn("StrictHostKeyChecking=yes", cmd)

        # Check atomic replace was called
        remote_cmds = [c[2] for c in commands_run if c[0] == "ssh"]
        self.assertTrue(any("mv -f" in c and "messages_v2.sqlite" in c for c in remote_cmds))
        # Check remote meta cleanup was called
        self.assertTrue(any("rm -f" in c and ".verify_" in c for c in remote_cmds))

        # Check all remote SSH commands are wrapped with explicit exit and preserve exit code
        self.assertGreater(len(remote_cmds), 0)
        for c in remote_cmds:
            self.assertTrue(c.startswith("sh -c '"), f"Command must be wrapped in sh -c: {c}")
            self.assertTrue(c.endswith("; rc=$?; exit $rc'"), f"Command must explicitly exit: {c}")

        # Check state file
        state = mirror.StateMarker(self.state_file).read()
        self.assertEqual(state["status"], "SUCCESS")
        self.assertNotIn("remoteTarget", state.get("remoteMirror", {}))
        self.assertNotIn("lastError", state)

        # Also verify that default run_ssh_command receives timeout=600 for verify stage
        run_ssh_verify_timeouts = []

        def mock_run_ssh_cmd(ssh_cmd, remote_cmd, timeout=30):
            if "PRAGMA integrity_check;" in remote_cmd:
                run_ssh_verify_timeouts.append(timeout)
            return 0, "", ""

        with unittest.mock.patch("kwin.mirror.run_ssh_command", side_effect=mock_run_ssh_cmd):
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                upload_runner=mock_upload_runner,
                download_runner=mock_download_runner,
            )

        self.assertIn(600, run_ssh_verify_timeouts)

    def test_remote_verify_compares_all_three_metrics_and_cleans_up(self):
        """Verify that mismatch on count, min, max, or sha256 triggers cleanup and fails before mv."""
        cases = [
            # (count, min, max, sha, expected_error)
            (999, "2023-11-14T22:13:20", "2023-11-14T22:13:40", "MATCH_SHA", "COUNT_MISMATCH"),
            (3, "1999-01-01T00:00:00", "2023-11-14T22:13:40", "MATCH_SHA", "MIN_TIMESTAMP_MISMATCH"),
            (3, "2023-11-14T22:13:20", "2099-12-31T23:59:59", "MATCH_SHA", "MAX_TIMESTAMP_MISMATCH"),
            (3, "2023-11-14T22:13:20", "2023-11-14T22:13:40", "WRONG_SHA", "HASH_MISMATCH"),
        ]

        for mock_count, mock_min, mock_max, mock_sha, expected_error in cases:
            with self.subTest(error_code=expected_error):
                commands_run = []

                def mock_ssh_runner(ssh_cmd, remote_cmd):
                    commands_run.append(remote_cmd)
                    return 0, "", ""

                def mock_upload_runner(scp_cmd, local_path, remote_dest):
                    return 0, "uploaded", ""

                def mock_download_runner(scp_cmd, remote_src, local_dest):
                    snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
                    actual_sha = mirror.compute_sha256(snap_path)
                    sha = actual_sha if mock_sha == "MATCH_SHA" else ("0" * 64)
                    content = f"ok\n{mock_count}|{mock_min}|{mock_max}\n{sha}"
                    Path(local_dest).write_text(content, encoding="utf-8")
                    return 0, "", ""

                with self.assertRaises(mirror.MirrorPipelineError) as ctx:
                    mirror.sync_mirror(
                        src_db=self.src_db,
                        snapshot_dir=self.snapshot_dir,
                        state_file=self.state_file,
                        lock_file=self.lock_file,
                        stop_file=self.stop_file,
                        vps_ssh_target="user@test.vps",
                        vps_remote_dir="/var/data/kakao",
                        ssh_runner=mock_ssh_runner,
                        upload_runner=mock_upload_runner,
                        download_runner=mock_download_runner,
                    )

                self.assertEqual(ctx.exception.error_code, expected_error)
                self.assertEqual(ctx.exception.stage, "REMOTE_VERIFY")

                # Ensure atomic replace (mv) was NEVER called
                self.assertFalse(any("mv -f" in c and "messages_v2.sqlite" in c for c in commands_run))
                # Ensure cleanup was called
                self.assertTrue(any("rm -f" in c and ".partial" in c for c in commands_run))

                # Check state file records lastStage and errorCode, NOT lastError
                state = mirror.StateMarker(self.state_file).read()
                self.assertEqual(state["status"], "FAILED")
                self.assertEqual(state["errorCode"], expected_error)
                self.assertEqual(state["lastStage"], "REMOTE_VERIFY")
                self.assertNotIn("lastError", state)

    def test_remote_verify_parse_error_and_timeout_cleanup(self):
        """Verify that malformed output or timeout triggers partial cleanup."""
        # 1. Parse error
        commands_run = []

        def mock_ssh_parse_err(ssh_cmd, remote_cmd):
            commands_run.append(remote_cmd)
            return 0, "", ""

        def mock_download_parse_err(scp_cmd, remote_src, local_dest):
            Path(local_dest).write_text("malformed-unparseable-output", encoding="utf-8")
            return 0, "", ""

        with self.assertRaises(mirror.MirrorPipelineError) as ctx:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh_parse_err,
                upload_runner=lambda *a: (0, "", ""),
                download_runner=mock_download_parse_err,
            )

        self.assertEqual(ctx.exception.error_code, "PARSE_ERROR")
        self.assertTrue(any("rm -f" in c and ".partial" in c for c in commands_run))
        self.assertFalse(any("mv -f" in c for c in commands_run))

        # 2. Timeout during verify command
        commands_run_to = []

        def mock_ssh_timeout(ssh_cmd, remote_cmd):
            commands_run_to.append(remote_cmd)
            if "PRAGMA integrity_check;" in remote_cmd:
                raise subprocess.TimeoutExpired(cmd=ssh_cmd, timeout=5)
            return 0, "", ""

        with self.assertRaises(mirror.MirrorPipelineError) as ctx:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh_timeout,
                upload_runner=lambda *a: (0, "", ""),
                download_runner=lambda *a: (0, "", ""),
            )

        self.assertEqual(ctx.exception.error_code, "TIMEOUT")
        self.assertTrue(any("rm -f" in c and ".partial" in c for c in commands_run_to))
        self.assertFalse(any("mv -f" in c for c in commands_run_to))

    def test_remote_path_injection_and_quoting(self):
        """Verify remote POSIX paths and targets are strictly validated and quoted."""
        invalid_paths = [
            "-rf /",
            "/var/data\n/kakao",
            "/var/data\0/kakao",
            "/var/data/../../etc",
            "relative/path",
            "",
            "   ",
        ]
        for p in invalid_paths:
            with self.subTest(invalid_path=p):
                with self.assertRaises(ValueError):
                    mirror.validate_remote_path(p)

        invalid_targets = [
            "-oProxyCommand=calc.exe",
            "user@host\ninjection",
            "",
            "   ",
        ]
        for t in invalid_targets:
            with self.subTest(invalid_target=t):
                with self.assertRaises(ValueError):
                    mirror.validate_ssh_target(t)

        self.assertEqual(mirror.posix_quote("/opt/kakao/dir"), "'/opt/kakao/dir'")
        self.assertEqual(mirror.posix_quote("/opt/it's/dir"), "'/opt/it'\\''s/dir'")

    def test_sensitive_payload_protection(self):
        """Verify that stats and metadata contain zero message text, usernames, or confidential content."""
        snap_file = self.snapshot_dir / "snapshot_clean.sqlite"
        res = mirror.create_readonly_snapshot(self.src_db, snap_file)

        raw_json = json.dumps(res)
        self.assertNotIn("Alice", raw_json)
        self.assertNotIn("Bob", raw_json)
        self.assertNotIn("Confidential secret chat", raw_json)
        self.assertNotIn("Another room message", raw_json)

    def test_powershell_install_post_sync_mirror_workflow(self):
        """Verify install-post-sync-mirror.ps1 idempotency, backup, uninstall, and conditional execution."""
        installer_path = Path(__file__).resolve().parent.parent / "install-post-sync-mirror.ps1"
        self.assertTrue(installer_path.exists())

        mock_sync_script = self.base_path / "sync-on-login.ps1"
        mock_post_sync = self.base_path / "post-sync-mirror.ps1"
        mock_trigger_flag = self.base_path / "triggered.txt"

        mock_post_sync.write_text(
            f'param([switch]$UploadOnly)\n'
            f'if ($UploadOnly) {{\n'
            f'    Set-Content -LiteralPath "{mock_trigger_flag}" -Value "TRIGGERED_UPLOAD_ONLY"\n'
            f'}} else {{\n'
            f'    Set-Content -LiteralPath "{mock_trigger_flag}" -Value "TRIGGERED_NO_UPLOAD_ONLY"\n'
            f'}}\n',
            encoding="utf-8",
        )

        mock_sync_script.write_text(
            """# Mock sync on login
$ErrorActionPreference = 'Stop'
$started = Get-Date
$stateDir = Join-Path $PSScriptRoot 'state'
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$dst = Join-Path $stateDir 'last-sync.json'
$record = [ordered]@{
    status = 'SUCCESS'
    started_at = $started.ToString('o')
    completed_at = (Get-Date).ToString('o')
    elapsed_seconds = 0.5
    ready = $true
    messages_before = 100
    messages_after = 105
    db_updated_kst = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
}
$tmp = Join-Path $stateDir 'last-sync.json.tmp'
Set-Content -LiteralPath $tmp -Value ($record | ConvertTo-Json -Compress) -Encoding UTF8
Move-Item -LiteralPath $tmp -Destination $dst -Force
Write-Host "Sync completed successfully"
""",
            encoding="utf-8",
        )

        # 1. Install hook
        cmd_install = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer_path),
            "-TargetScript",
            str(mock_sync_script),
            "-PostSyncScript",
            str(mock_post_sync),
        ]
        proc = subprocess.run(cmd_install, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("installed successfully", proc.stdout)

        content_after_install = mock_sync_script.read_text(encoding="utf-8")
        self.assertIn("# >>> kakao-post-sync-mirror >>>", content_after_install)
        self.assertIn("# <<< kakao-post-sync-mirror <<<", content_after_install)

        # Verify backup exists
        backup_file = mock_sync_script.with_suffix(".ps1.bak")
        self.assertTrue(backup_file.exists())

        # 2. Idempotency: run install again
        proc_idempotent = subprocess.run(cmd_install, capture_output=True, text=True)
        self.assertEqual(proc_idempotent.returncode, 0)
        self.assertIn("already installed", proc_idempotent.stdout)

        # Count occurrences of hook: should be exactly 1
        self.assertEqual(mock_sync_script.read_text(encoding="utf-8").count("# >>> kakao-post-sync-mirror >>>"), 1)

        # 3. Test execution: when last-sync.json is SUCCESS and fresh, post-sync script is triggered with -UploadOnly
        proc_run = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(mock_sync_script)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc_run.returncode, 0)
        self.assertTrue(mock_trigger_flag.exists())
        self.assertEqual(mock_trigger_flag.read_text(encoding="utf-8").strip(), "TRIGGERED_UPLOAD_ONLY")

        # 4. Uninstall hook
        cmd_uninstall = cmd_install + ["-Uninstall"]
        proc_uninstall = subprocess.run(cmd_uninstall, capture_output=True, text=True)
        self.assertEqual(proc_uninstall.returncode, 0)
        self.assertIn("uninstalled successfully", proc_uninstall.stdout)

        content_after_uninstall = mock_sync_script.read_text(encoding="utf-8")
        self.assertNotIn("# >>> kakao-post-sync-mirror >>>", content_after_uninstall)

    def test_post_sync_mirror_hook_fail_closed_and_validation(self):
        """Verify post-sync mirror hook fail-closed semantics:
        - marker missing -> skip (MARKER_NOT_FOUND)
        - parse error -> skip (PARSE_ERROR)
        - sync failed -> skip (SYNC_NOT_SUCCESS)
        - sync not ready -> skip (SYNC_NOT_SUCCESS)
        - stale marker (older than $started or >300s) -> skip (STALE_MARKER)
        - no sensitive paths or details leaked in output
        - successful fresh marker invokes post-sync-mirror.ps1 exactly once with -UploadOnly
        """
        installer_path = Path(__file__).resolve().parent.parent / "install-post-sync-mirror.ps1"
        self.assertTrue(installer_path.exists())

        test_dir = self.base_path / "fail_closed_test"
        test_dir.mkdir(parents=True, exist_ok=True)

        mock_post_sync = test_dir / "post-sync-mirror.ps1"
        trigger_log = test_dir / "trigger_log.txt"

        mock_post_sync.write_text(
            f'param([switch]$UploadOnly)\n'
            f'Add-Content -LiteralPath "{trigger_log}" -Value "CALL:$($UploadOnly.IsPresent)"\n',
            encoding="utf-8",
        )

        state_dir = test_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        marker_file = state_dir / "last-sync.json"

        # Helper to install hook into a script and run it
        def run_hook_test(script_content: str, extra_env: dict | None = None):
            if trigger_log.exists():
                trigger_log.unlink()
            script_path = test_dir / "test_runner.ps1"
            script_path.write_text(script_content, encoding="utf-8")

            # Install hook
            install_cmd = [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(installer_path),
                "-TargetScript", str(script_path),
                "-PostSyncScript", str(mock_post_sync),
                "-Force",
            ]
            res_install = subprocess.run(install_cmd, capture_output=True, text=True)
            self.assertEqual(res_install.returncode, 0)

            # Run target script
            run_cmd = [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(script_path),
            ]
            env = os.environ.copy()
            if extra_env:
                env.update(extra_env)
            return subprocess.run(run_cmd, capture_output=True, text=True, env=env)

        # 1a. Case: Marker missing when dst is defined (fail-closed)
        if marker_file.exists():
            marker_file.unlink()
        proc = run_hook_test(
            f"""$ErrorActionPreference = 'Stop'
$started = Get-Date
$dst = "{marker_file}"
# No last-sync.json created
Write-Host "Sync ended without marker"
"""
        )
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(trigger_log.exists())
        self.assertIn("Skipped: MARKER_NOT_FOUND", proc.stdout)

        # 1b. Case: No dst set, no MarkerFile, and LOCALAPPDATA has no marker (fail-closed)
        nonexistent_appdata = test_dir / "nonexistent_appdata"
        proc = run_hook_test(
            """$ErrorActionPreference = 'Stop'
Write-Host "Sync ended without dst or marker"
""",
            extra_env={"LOCALAPPDATA": str(nonexistent_appdata)},
        )
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(trigger_log.exists())
        self.assertIn("Skipped: MARKER_NOT_FOUND", proc.stdout)

        # 2. Case: Marker parse error (corrupt JSON)
        marker_file.write_text("{corrupt-json: invalid", encoding="utf-8")
        proc = run_hook_test(
            f"""$ErrorActionPreference = 'Stop'
$started = Get-Date
$dst = "{marker_file}"
Write-Host "Sync wrote corrupt marker"
"""
        )
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(trigger_log.exists())
        self.assertIn("Skipped: PARSE_ERROR", proc.stdout)

        # 3. Case: Sync status FAILED
        failed_payload = {
            "status": "FAILED",
            "stage": "sync",
            "error_code": "BACKEND_ERROR",
            "started_at": "2026-09-12T10:00:00+09:00",
            "completed_at": "2026-09-12T10:00:05+09:00",
        }
        marker_file.write_text(json.dumps(failed_payload), encoding="utf-8")
        proc = run_hook_test(
            f"""$ErrorActionPreference = 'Stop'
$started = Get-Date
$dst = "{marker_file}"
Write-Host "Sync ended with failure"
"""
        )
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(trigger_log.exists())
        self.assertIn("Skipped: SYNC_NOT_SUCCESS", proc.stdout)

        # 4. Case: Sync status SUCCESS but ready is false
        not_ready_payload = {
            "status": "SUCCESS",
            "started_at": "2026-09-12T10:00:00+09:00",
            "completed_at": "2026-09-12T10:00:05+09:00",
            "ready": False,
        }
        marker_file.write_text(json.dumps(not_ready_payload), encoding="utf-8")
        proc = run_hook_test(
            f"""$ErrorActionPreference = 'Stop'
$started = Get-Date
$dst = "{marker_file}"
Write-Host "Sync not ready"
"""
        )
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(trigger_log.exists())
        self.assertIn("Skipped: SYNC_NOT_SUCCESS", proc.stdout)

        # 5. Case: Stale marker with $started variable in scope
        # Marker has timestamp from hours ago; current $started is now
        stale_payload = {
            "status": "SUCCESS",
            "started_at": "2020-01-01T00:00:00+09:00",
            "completed_at": "2020-01-01T00:01:00+09:00",
            "ready": True,
        }
        marker_file.write_text(json.dumps(stale_payload), encoding="utf-8")
        proc = run_hook_test(
            f"""$ErrorActionPreference = 'Stop'
$started = Get-Date
$dst = "{marker_file}"
Write-Host "Sync failed to update old marker"
"""
        )
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(trigger_log.exists())
        self.assertIn("Skipped: STALE_MARKER", proc.stdout)

        # 6. Case: Stale marker in standalone mode (no $started, completed_at > 300s ago)
        marker_file.write_text(json.dumps(stale_payload), encoding="utf-8")
        proc = run_hook_test(
            f"""$ErrorActionPreference = 'Stop'
$dst = "{marker_file}"
Write-Host "Standalone script run"
"""
        )
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(trigger_log.exists())
        self.assertIn("Skipped: STALE_MARKER", proc.stdout)

        # 7. Case: Success and current execution -> triggers -UploadOnly exactly once
        proc = run_hook_test(
            f"""$ErrorActionPreference = 'Stop'
$started = Get-Date
$dst = "{marker_file}"
$record = [ordered]@{{
    status = 'SUCCESS'
    started_at = $started.ToString('o')
    completed_at = (Get-Date).ToString('o')
    ready = $true
    elapsed_seconds = 1.0
}}
Set-Content -LiteralPath $dst -Value ($record | ConvertTo-Json -Compress) -Encoding UTF8
Write-Host "Sync completed successfully"
"""
        )
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(trigger_log.exists())
        log_lines = trigger_log.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(log_lines), 1, "post-sync-mirror runner must be invoked exactly once")
        self.assertEqual(log_lines[0], "CALL:True", "Must be called with -UploadOnly switch")
        self.assertIn("Triggering post-sync mirror (-UploadOnly)...", proc.stdout)

        # Information security verification: no sensitive paths leaked in hook stdout
        self.assertNotIn("messages_v2", proc.stdout)
        self.assertNotIn("password", proc.stdout.lower())
        self.assertNotIn("secret", proc.stdout.lower())

    def test_meta_download_failure_and_timeout_preserves_current(self):
        """Verify that meta file download failure or timeout triggers cleanup and preserves current."""
        # 1. Download command fails
        commands_run = []

        def mock_ssh_runner(ssh_cmd, remote_cmd):
            commands_run.append(remote_cmd)
            return 0, "", ""

        def mock_download_fail(scp_cmd, remote_src, local_dest):
            return 1, "", "Permission denied or scp error"

        with self.assertRaises(mirror.MirrorPipelineError) as ctx:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh_runner,
                upload_runner=lambda *a: (0, "", ""),
                download_runner=mock_download_fail,
            )

        self.assertEqual(ctx.exception.error_code, "DOWNLOAD_FAILED")
        self.assertEqual(ctx.exception.stage, "REMOTE_VERIFY")
        self.assertFalse(any("mv -f" in c and "messages_v2.sqlite" in c for c in commands_run))
        self.assertTrue(any("rm -f" in c and ".partial" in c for c in commands_run))

        # 2. Download command times out
        commands_run_to = []

        def mock_download_timeout(scp_cmd, remote_src, local_dest):
            raise subprocess.TimeoutExpired(cmd=scp_cmd, timeout=30)

        with self.assertRaises(mirror.MirrorPipelineError) as ctx_to:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=lambda ssh_cmd, remote_cmd: (commands_run_to.append(remote_cmd) or (0, "", "")),
                upload_runner=lambda *a: (0, "", ""),
                download_runner=mock_download_timeout,
            )

        self.assertEqual(ctx_to.exception.error_code, "TIMEOUT")
        self.assertEqual(ctx_to.exception.stage, "REMOTE_VERIFY")
        self.assertFalse(any("mv -f" in c and "messages_v2.sqlite" in c for c in commands_run_to))
        self.assertTrue(any("rm -f" in c and ".partial" in c for c in commands_run_to))

    def test_cleanup_failure_preserves_current(self):
        """Verify that replace failure triggers rollback and cleanup, preserving current."""
        # Case A: Verification succeeds, but remote replace (which integrates meta cleanup) fails
        commands_run_a = []

        def mock_ssh_replace_fail(ssh_cmd, remote_cmd):
            commands_run_a.append(remote_cmd)
            # Fail the replace command (which starts with rm -f .verify_)
            if "mv -f" in remote_cmd and "messages_v2.sqlite" in remote_cmd:
                return 1, "", "replace command failed"
            return 0, "", ""

        def mock_download_runner_ok(scp_cmd, remote_src, local_dest):
            snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
            actual_sha = mirror.compute_sha256(snap_path)
            content = f"ok\n3|2023-11-14T22:13:20|2023-11-14T22:13:40\n{actual_sha}"
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        with self.assertRaises(mirror.MirrorPipelineError) as ctx_a:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh_replace_fail,
                upload_runner=lambda *a: (0, "", ""),
                download_runner=mock_download_runner_ok,
            )

        self.assertEqual(ctx_a.exception.error_code, "REPLACE_FAILED")
        self.assertEqual(ctx_a.exception.stage, "REMOTE_REPLACE")
        # Ensure rollback and cleanup were called
        self.assertTrue(any(".rollback" in c for c in commands_run_a), "Rollback must be attempted on replace failure")
        self.assertTrue(any(".verify_" in c and "rm -f" in c for c in commands_run_a), "Verify meta cleanup must be executed")

        # Case B: Verification fails (count mismatch), and subsequent _cleanup_remote raises an exception
        commands_run_b = []

        def mock_ssh_cleanup_throws(ssh_cmd, remote_cmd):
            commands_run_b.append(remote_cmd)
            if "rm -f" in remote_cmd and "gzip" not in remote_cmd:
                raise RuntimeError("SSH connection lost during cleanup")
            return 0, "", ""

        def mock_download_mismatch(scp_cmd, remote_src, local_dest):
            snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
            actual_sha = mirror.compute_sha256(snap_path)
            content = f"ok\n999|2023-11-14T22:13:20|2023-11-14T22:13:40\n{actual_sha}"
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        with self.assertRaises(mirror.MirrorPipelineError) as ctx_b:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh_cleanup_throws,
                upload_runner=lambda *a: (0, "", ""),
                download_runner=mock_download_mismatch,
            )

        # Original error (COUNT_MISMATCH) is preserved, not masked by cleanup failure
        self.assertEqual(ctx_b.exception.error_code, "COUNT_MISMATCH")
        self.assertEqual(ctx_b.exception.stage, "REMOTE_VERIFY")
        # mv -f messages_v2.sqlite must NEVER be called
        self.assertFalse(any("mv -f" in c and "messages_v2.sqlite" in c for c in commands_run_b))

    def test_parse_remote_meta_both_formats(self):
        """Verify strict parsing of both JSON and delimiter formats, and validation error handling."""
        valid_sha = "a" * 64

        # 1. Valid Delimiter Multi-line
        delim_multi = f"ok\n42|2026-01-01T00:00:00|2026-01-02T00:00:00\n{valid_sha}"
        parsed = mirror.parse_remote_meta(delim_multi)
        self.assertEqual(parsed["integrityCheck"], "ok")
        self.assertEqual(parsed["messageCount"], 42)
        self.assertEqual(parsed["minSentAtIso"], "2026-01-01T00:00:00")
        self.assertEqual(parsed["maxSentAtIso"], "2026-01-02T00:00:00")
        self.assertEqual(parsed["sha256"], valid_sha)

        # 2. Valid Delimiter Single-line
        delim_single = f"ok|10|2026-01-01T00:00:00|2026-01-02T00:00:00|{valid_sha}"
        parsed_s = mirror.parse_remote_meta(delim_single)
        self.assertEqual(parsed_s["messageCount"], 10)
        self.assertEqual(parsed_s["sha256"], valid_sha)

        # 3. Valid JSON
        json_meta = json.dumps({
            "integrityCheck": "ok",
            "messageCount": 100,
            "minSentAtIso": "2026-01-01T00:00:00",
            "maxSentAtIso": "2026-01-02T00:00:00",
            "sha256": valid_sha.upper(),  # Should be normalized to lowercase
        })
        parsed_j = mirror.parse_remote_meta(json_meta)
        self.assertEqual(parsed_j["messageCount"], 100)
        self.assertEqual(parsed_j["sha256"], valid_sha.lower())

        # 4. Invalid cases
        invalid_inputs = [
            "",
            "   ",
            "ok",
            "ok\nnot_numbers",
            f"ok\n-5|2026-01-01|2026-01-02\n{valid_sha}",  # negative count
            f"ok\n10|2026-01-01|2026-01-02\ninvalid_sha",  # invalid sha
            json.dumps({"integrityCheck": "ok"}),  # missing fields
            json.dumps({"integrityCheck": "ok", "messageCount": -1, "sha256": valid_sha}),
            "[]",  # non-dict JSON
        ]
        for inv in invalid_inputs:
            with self.subTest(invalid_input=inv):
                with self.assertRaises(ValueError):
                    mirror.parse_remote_meta(inv)

    def test_no_stdout_on_remote_verify_command(self):
        """Verify that remote verify command redirects all output to meta file and produces no stdout."""
        commands_captured = []

        def mock_ssh_capture(ssh_cmd, remote_cmd):
            commands_captured.append(remote_cmd)
            return 0, "", ""

        def mock_download(scp_cmd, remote_src, local_dest):
            snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
            actual_sha = mirror.compute_sha256(snap_path)
            content = f"ok\n3|2023-11-14T22:13:20|2023-11-14T22:13:40\n{actual_sha}"
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        mirror.sync_mirror(
            src_db=self.src_db,
            snapshot_dir=self.snapshot_dir,
            state_file=self.state_file,
            lock_file=self.lock_file,
            stop_file=self.stop_file,
            vps_ssh_target="user@test.vps",
            vps_remote_dir="/var/data/kakao",
            ssh_runner=mock_ssh_capture,
            upload_runner=lambda *a: (0, "", ""),
            download_runner=mock_download,
        )

        # Verify command must redirect subshell output with >
        verify_cmds = [c for c in commands_captured if "PRAGMA integrity_check;" in c]
        self.assertEqual(len(verify_cmds), 1)
        vc = verify_cmds[0]
        self.assertIn(") > ", vc)
        self.assertIn(".meta", vc)
        self.assertIn("chmod 600", vc)
        self.assertIn("umask 077", vc)
        self.assertIn("python3 -c", vc)
        self.assertIn("mode=ro", vc)
        self.assertIn("2>&1", vc)
        self.assertNotIn("sqlite3 ", vc)

        # Check archive command must redirect stdout to /dev/null
        check_cmds = [c for c in commands_captured if "20260908" in c]
        self.assertEqual(len(check_cmds), 1)
        self.assertIn(">/dev/null", check_cmds[0])

    def test_wrap_remote_posix_command_semantics_and_idempotence(self):
        """Verify wrap_remote_posix_command input validation, explicit exit wrapping, and idempotence."""
        # 1. Validation errors
        invalid_inputs = ["", "   ", None, 123, []]
        for inv in invalid_inputs:
            with self.subTest(invalid_input=inv):
                with self.assertRaises(ValueError):
                    mirror.wrap_remote_posix_command(inv)  # type: ignore

        # 2. Simple command wrapping
        wrapped = mirror.wrap_remote_posix_command("echo hello")
        self.assertEqual(wrapped, "sh -c 'echo hello; rc=$?; exit $rc'")

        # 3. Trailing semicolon / whitespace stripping
        wrapped_semi = mirror.wrap_remote_posix_command("mkdir -p /opt/dir;   ")
        self.assertEqual(wrapped_semi, "sh -c 'mkdir -p /opt/dir; rc=$?; exit $rc'")

        # 4. Complex compound command with single quotes, subshell, and pipes
        complex_cmd = "umask 077 && ( python3 -c 'import sys; print(sys.argv[1])' '/path/db' | awk '{print $1}' ) > '/path/meta'"
        wrapped_complex = mirror.wrap_remote_posix_command(complex_cmd)
        self.assertTrue(wrapped_complex.startswith("sh -c '"))
        self.assertTrue(wrapped_complex.endswith("; rc=$?; exit $rc'"))
        self.assertIn("awk", wrapped_complex)
        self.assertIn("python3 -c", wrapped_complex)
        self.assertNotIn("sqlite3", wrapped_complex)

        # 5. Idempotence: wrapping an already-wrapped command returns it unchanged
        wrapped_twice = mirror.wrap_remote_posix_command(wrapped)
        self.assertEqual(wrapped_twice, wrapped)
        wrapped_complex_twice = mirror.wrap_remote_posix_command(wrapped_complex)
        self.assertEqual(wrapped_complex_twice, wrapped_complex)

        # 6. Alias test
        self.assertEqual(mirror.wrap_posix_command("exit 0"), "sh -c 'exit 0; rc=$?; exit $rc'")

    def test_run_ssh_command_argv_and_exit_code_preservation(self):
        """Verify run_ssh_command wraps command in argv and faithfully preserves exit codes."""
        ssh_base = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "user@host.vps"]
        raw_cmd = "stat -c '%a' /opt/kakao > /tmp/stat.txt"

        for mock_rc in (0, 1, 42, 127):
            with self.subTest(mock_returncode=mock_rc):
                mock_proc = subprocess.CompletedProcess(
                    args=[],
                    returncode=mock_rc,
                    stdout="some output\n",
                    stderr="",
                )
                with unittest.mock.patch("subprocess.run", return_value=mock_proc) as mock_run:
                    code, out, err = mirror.run_ssh_command(ssh_base, raw_cmd, timeout=45)

                    self.assertEqual(code, mock_rc)
                    self.assertEqual(out, "some output")
                    self.assertEqual(err, "")
                    mock_run.assert_called_once()

                    # Verify actual argv passed to subprocess.run
                    called_args, called_kwargs = mock_run.call_args
                    argv = called_args[0]
                    self.assertEqual(argv[:len(ssh_base)], ssh_base)
                    self.assertEqual(len(argv), len(ssh_base) + 1)

                    # Last argument must be the wrapped command with explicit exit
                    wrapped_arg = argv[-1]
                    self.assertTrue(wrapped_arg.startswith("sh -c '"))
                    self.assertTrue(wrapped_arg.endswith("; rc=$?; exit $rc'"))
                    self.assertEqual(wrapped_arg, mirror.wrap_remote_posix_command(raw_cmd))
                    self.assertEqual(called_kwargs.get("timeout"), 45)

    def test_posix_explicit_exit_and_exit_code_preservation_real_shell(self):
        """Verify exit code preservation and explicit termination using a real POSIX shell if available."""
        sh_path = shutil.which("sh") or shutil.which("bash") or r"C:\Program Files\Git\bin\sh.exe"
        if not (sh_path and os.path.exists(sh_path)):
            self.skipTest("No POSIX sh/bash found on system to test real shell execution.")

        test_cases = [
            ("echo 'success test' > /dev/null", 0),
            ("exit 0", 0),
            ("exit 1", 1),
            ("exit 42", 42),
            ("false && echo 'unreachable'", 1),
            ("(exit 19) && echo 'unreachable'", 19),
            ("( umask 077 && (exit 13) ) > /dev/null", 13),
            ("echo 'first second' | awk '{print $1}' > /dev/null", 0),
        ]

        for cmd, expected_rc in test_cases:
            with self.subTest(command=cmd, expected_rc=expected_rc):
                wrapped = mirror.wrap_remote_posix_command(cmd)
                proc = subprocess.run(
                    [str(sh_path), "-c", wrapped],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(proc.returncode, expected_rc, f"Failed for cmd={cmd!r}")

    def test_sync_mirror_all_remote_stages_and_failure_rollback_wrapped(self):
        """Verify that every remote command executed across all pipeline stages is wrapped with explicit exit."""
        recorded_cmds = []

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            recorded_cmds.append(remote_cmd)
            return 0, "", ""

        def mock_upload(scp_cmd, local_path, remote_dest):
            return 0, "", ""

        def mock_download(scp_cmd, remote_src, local_dest):
            snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
            actual_sha = mirror.compute_sha256(snap_path)
            content = f"ok\n3|2023-11-14T22:13:20|2023-11-14T22:13:40\n{actual_sha}"
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        # 1. Success workflow
        res = mirror.sync_mirror(
            src_db=self.src_db,
            snapshot_dir=self.snapshot_dir,
            state_file=self.state_file,
            lock_file=self.lock_file,
            stop_file=self.stop_file,
            vps_ssh_target="user@test.vps",
            vps_remote_dir="/var/data/kakao",
            ssh_runner=mock_ssh,
            upload_runner=mock_upload,
            download_runner=mock_download,
        )
        self.assertEqual(res["status"], "SUCCESS")

        # Must have recorded commands for prepare, verify, meta cleanup, replace, check
        self.assertGreaterEqual(len(recorded_cmds), 5)
        for cmd in recorded_cmds:
            self.assertTrue(cmd.startswith("sh -c '"), f"Command must start with sh -c ': {cmd}")
            self.assertTrue(cmd.endswith("; rc=$?; exit $rc'"), f"Command must explicitly exit: {cmd}")

        # 2. Rollback cleanup on verify failure
        recorded_cleanup_cmds = []

        def mock_ssh_fail_verify(ssh_cmd, remote_cmd, timeout=None):
            recorded_cleanup_cmds.append(remote_cmd)
            if "PRAGMA integrity_check;" in remote_cmd:
                return 1, "", "verification failed"
            return 0, "", ""

        with self.assertRaises(mirror.MirrorPipelineError):
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh_fail_verify,
                upload_runner=mock_upload,
                download_runner=mock_download,
            )

        # Verify that rollback cleanup (rm -f .partial) is ALSO wrapped with explicit exit
        cleanup_calls = [c for c in recorded_cleanup_cmds if "rm -f" in c and ".partial" in c]
        self.assertGreaterEqual(len(cleanup_calls), 1)
        for c in cleanup_calls:
            self.assertTrue(c.startswith("sh -c '"), f"Cleanup command must start with sh -c ': {c}")
            self.assertTrue(c.endswith("; rc=$?; exit $rc'"), f"Cleanup command must explicitly exit: {c}")

    def test_sync_mirror_actual_subprocess_argv_all_stages(self):
        """Verify actual argv passed to subprocess.run across all stages when run_ssh_command is used."""
        captured_subprocess_runs = []

        snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"

        def mock_upload(scp_cmd, local_path, remote_dest):
            return 0, "", ""

        def mock_download(scp_cmd, remote_src, local_dest):
            actual_sha = mirror.compute_sha256(snap_path)
            content = f"ok\n3|2023-11-14T22:13:20|2023-11-14T22:13:40\n{actual_sha}"
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        def fake_subprocess_run(cmd, *args, **kwargs):
            captured_subprocess_runs.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        with unittest.mock.patch("subprocess.run", side_effect=fake_subprocess_run):
            res = mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                upload_runner=mock_upload,
                download_runner=mock_download,
            )

        self.assertEqual(res["status"], "SUCCESS")
        ssh_invocations = [cmd for cmd in captured_subprocess_runs if cmd and cmd[0] == "ssh"]
        self.assertGreaterEqual(len(ssh_invocations), 5)

        for argv in ssh_invocations:
            last_arg = argv[-1]
            self.assertTrue(last_arg.startswith("sh -c '"), f"Subprocess argv must have sh -c: {last_arg}")
            self.assertTrue(last_arg.endswith("; rc=$?; exit $rc'"), f"Subprocess argv must have exit: {last_arg}")

    def test_remote_verify_script_executes_with_python_stdlib(self):
        """Synthetic test: execute REMOTE_VERIFY_SCRIPT directly via sys.executable and verify JSON output, zero stdout/stderr, mode=ro."""
        meta_file = self.snapshot_dir / "synthetic_meta.json"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

        proc = subprocess.run(
            [sys.executable, "-c", mirror.REMOTE_VERIFY_SCRIPT, str(self.src_db), str(meta_file)],
            capture_output=True,
            text=True,
        )

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "", "stdout must be strictly empty")
        self.assertEqual(proc.stderr, "", "stderr must be strictly empty")
        self.assertTrue(meta_file.exists())

        raw_meta = meta_file.read_text(encoding="utf-8")
        parsed = mirror.parse_remote_meta(raw_meta)

        self.assertEqual(parsed["integrityCheck"], "ok")
        self.assertEqual(parsed["messageCount"], 3)
        self.assertEqual(parsed["minSentAtIso"], "2023-11-14T22:13:20")
        self.assertEqual(parsed["maxSentAtIso"], "2023-11-14T22:13:40")
        self.assertEqual(parsed["sha256"], mirror.compute_sha256(self.src_db))

    def test_remote_verify_script_handles_missing_messages_table(self):
        """Synthetic test: execute REMOTE_VERIFY_SCRIPT on database without messages table."""
        empty_db = self.base_path / "no_messages.sqlite"
        c = sqlite3.connect(str(empty_db))
        c.execute("CREATE TABLE other (id INT)")
        c.commit()
        c.close()

        meta_file = self.snapshot_dir / "no_msg_meta.json"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

        proc = subprocess.run(
            [sys.executable, "-c", mirror.REMOTE_VERIFY_SCRIPT, str(empty_db), str(meta_file)],
            capture_output=True,
            text=True,
        )

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")

        parsed = mirror.parse_remote_meta(meta_file.read_text(encoding="utf-8"))
        self.assertEqual(parsed["integrityCheck"], "ok")
        self.assertEqual(parsed["messageCount"], 0)
        self.assertIsNone(parsed["minSentAtIso"])
        self.assertIsNone(parsed["maxSentAtIso"])

    def test_remote_verify_script_exits_nonzero_on_corrupt_database(self):
        """Synthetic test: execute REMOTE_VERIFY_SCRIPT on corrupt file, ensuring non-zero exit and empty stdout."""
        corrupt_db = self.base_path / "corrupt.sqlite"
        corrupt_db.write_text("NOT A SQLITE DATABASE HEADER", encoding="utf-8")

        meta_file = self.snapshot_dir / "corrupt_meta.json"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

        proc = subprocess.run(
            [sys.executable, "-c", mirror.REMOTE_VERIFY_SCRIPT, str(corrupt_db), str(meta_file)],
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "", "stdout must be empty on error")
        self.assertFalse(meta_file.exists(), "meta file should not be created on DB error")

    def test_remote_verify_missing_python_triggers_fast_verify_command_failed(self):
        """Synthetic test: missing python3 (exit 127) triggers VERIFY_COMMAND_FAILED, cleans up, and preserves current."""
        commands_run = []

        def mock_ssh_missing_py3(ssh_cmd, remote_cmd, timeout=None):
            commands_run.append(remote_cmd)
            if "python3 -c" in remote_cmd:
                return 127, "", ""  # Simulate /bin/sh: python3: not found (exit 127, stdout/stderr discarded to /dev/null)
            return 0, "", ""

        def mock_upload(scp_cmd, local_p, remote_dst):
            return 0, "", ""

        with self.assertRaises(mirror.MirrorPipelineError) as ctx:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh_missing_py3,
                upload_runner=mock_upload,
                download_runner=lambda *a: (0, "", ""),
            )

        self.assertEqual(ctx.exception.stage, "REMOTE_VERIFY")
        self.assertEqual(ctx.exception.error_code, "VERIFY_COMMAND_FAILED")

        # Ensure current DB was NOT replaced
        self.assertFalse(any("mv -f" in c and "messages_v2.sqlite" in c for c in commands_run))
        # Ensure cleanup was performed for .partial and .meta
        self.assertTrue(any("rm -f" in c and ".partial" in c for c in commands_run))

        state = mirror.StateMarker(self.state_file).read()
        self.assertEqual(state["status"], "FAILED")
        self.assertEqual(state["lastStage"], "REMOTE_VERIFY")
        self.assertEqual(state["errorCode"], "VERIFY_COMMAND_FAILED")

    def test_remote_verify_command_structure_and_no_sqlite3_cli(self):
        """Synthetic test: verify command uses python3 stdlib, mode=ro, /dev/null redirect, and has NO sqlite3 CLI."""
        self.assertNotIn("sqlite3 ", mirror.REMOTE_VERIFY_SCRIPT)
        self.assertIn("mode=ro", mirror.REMOTE_VERIFY_SCRIPT)

        commands_captured = []

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            commands_captured.append(remote_cmd)
            return 0, "", ""

        def mock_download(scp_cmd, remote_src, local_dest):
            snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
            actual_sha = mirror.compute_sha256(snap_path)
            content = json.dumps({
                "integrityCheck": "ok",
                "messageCount": 3,
                "minSentAtIso": "2023-11-14T22:13:20",
                "maxSentAtIso": "2023-11-14T22:13:40",
                "sha256": actual_sha,
            })
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        mirror.sync_mirror(
            src_db=self.src_db,
            snapshot_dir=self.snapshot_dir,
            state_file=self.state_file,
            lock_file=self.lock_file,
            stop_file=self.stop_file,
            vps_ssh_target="user@test.vps",
            vps_remote_dir="/var/data/kakao",
            ssh_runner=mock_ssh,
            upload_runner=lambda *a: (0, "", ""),
            download_runner=mock_download,
        )

        verify_cmds = [c for c in commands_captured if "python3 -c" in c]
        self.assertEqual(len(verify_cmds), 1)
        vc = verify_cmds[0]
        self.assertIn("python3 -c", vc)
        self.assertIn("mode=ro", vc)
        self.assertIn("/dev/null", vc)
        self.assertIn("2>&1", vc)
        self.assertNotIn("sqlite3 ", vc)

    def test_validate_positive_id(self):
        """Verify validate_positive_id strictly enforces positive integers (> 0)."""
        self.assertEqual(mirror.validate_positive_id(10000), 10000)
        self.assertEqual(mirror.validate_positive_id("10000"), 10000)
        self.assertEqual(mirror.validate_positive_id(1), 1)

        for invalid in (0, -1, "-10", "0", "abc", None, True, False, 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    mirror.validate_positive_id(invalid)

    def test_resolve_vps_uid_gid_precedence(self):
        """Verify resolution precedence: explicit args > env vars > config > parent fallback > default 10000:10000."""
        # 1. Default fallback
        with mock.patch.dict(os.environ, {}, clear=True):
            uid, gid = mirror.resolve_vps_uid_gid(config_paths=[])
            self.assertEqual((uid, gid), (10000, 10000))

        # 2. Config file precedence
        cfg_file = self.base_path / "mock_config.json"
        cfg_file.write_text(json.dumps({"KAKAO_VPS_UID": 20001, "KAKAO_VPS_GID": 20002}), encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=True):
            uid, gid = mirror.resolve_vps_uid_gid(config_paths=[cfg_file])
            self.assertEqual((uid, gid), (20001, 20002))

        # 3. Env var overrides config
        with mock.patch.dict(os.environ, {"KAKAO_VPS_UID": "30001", "KAKAO_VPS_GID": "30002"}):
            uid, gid = mirror.resolve_vps_uid_gid(config_paths=[cfg_file])
            self.assertEqual((uid, gid), (30001, 30002))

        # 4. Explicit arguments override env vars
        with mock.patch.dict(os.environ, {"KAKAO_VPS_UID": "30001", "KAKAO_VPS_GID": "30002"}):
            uid, gid = mirror.resolve_vps_uid_gid(uid=40001, gid=40002, config_paths=[cfg_file])
            self.assertEqual((uid, gid), (40001, 40002))

        # 5. Parent stat fallback when callable provided
        with mock.patch.dict(os.environ, {}, clear=True):
            uid, gid = mirror.resolve_vps_uid_gid(
                config_paths=[],
                parent_stat_fn=lambda: (50001, 50002)
            )
            self.assertEqual((uid, gid), (50001, 50002))

        # 6. Invalid non-positive values raise ValueError (fail-closed)
        for bad in (0, -1, "invalid"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    mirror.resolve_vps_uid_gid(uid=bad, config_paths=[])

    def test_remote_prepare_and_replace_owner_mode_fail_closed(self):
        """Verify remote prepare, verify, and replace enforce uid:gid and fail-closed rollback."""
        commands_captured = []

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            commands_captured.append(remote_cmd)
            return 0, "", ""

        def mock_download(scp_cmd, remote_src, local_dest):
            snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
            actual_sha = mirror.compute_sha256(snap_path)
            content = json.dumps({
                "integrityCheck": "ok",
                "messageCount": 3,
                "minSentAtIso": "2023-11-14T22:13:20",
                "maxSentAtIso": "2023-11-14T22:13:40",
                "sha256": actual_sha,
            })
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        res = mirror.sync_mirror(
            src_db=self.src_db,
            snapshot_dir=self.snapshot_dir,
            state_file=self.state_file,
            lock_file=self.lock_file,
            stop_file=self.stop_file,
            vps_ssh_target="user@test.vps",
            vps_remote_dir="/opt/data/profiles/mikaca/private/kakao-processing/current",
            vps_uid=10000,
            vps_gid=10000,
            ssh_runner=mock_ssh,
            upload_runner=lambda *a: (0, "", ""),
            download_runner=mock_download,
        )
        self.assertEqual(res["status"], "SUCCESS")

        # 1. Prepare stage must chown and chmod dir, and verify with stat
        prep_cmds = [c for c in commands_captured if "mkdir -p" in c]
        self.assertEqual(len(prep_cmds), 1)
        self.assertIn("chown 10000:10000", prep_cmds[0])
        self.assertIn("chmod 700", prep_cmds[0])
        self.assertIn("stat -c", prep_cmds[0])
        self.assertIn("%u:%g:%a", prep_cmds[0])
        self.assertIn("10000:10000:700", prep_cmds[0])

        # 2. Verify stage must enforce 10000:10000:600 on partial and verify dir ownership
        ver_cmds = [c for c in commands_captured if "python3 -c" in c]
        self.assertEqual(len(ver_cmds), 1)
        self.assertIn("chown 10000:10000", ver_cmds[0])
        self.assertIn("chmod 600", ver_cmds[0])
        self.assertIn("10000:10000:600", ver_cmds[0])
        self.assertIn("10000:10000:700", ver_cmds[0])

        # 3. Replace stage must have atomic replace with rollback protection
        rep_cmds = [c for c in commands_captured if "mv -f" in c and "messages_v2.sqlite" in c]
        self.assertTrue(len(rep_cmds) >= 1)
        rep = rep_cmds[0]
        self.assertIn("chown 10000:10000", rep)
        self.assertIn("chmod 600", rep)
        self.assertIn("10000:10000:600", rep)
        # Rollback mechanism should be present
        self.assertIn("rollback", rep)

        # 4. Invariant: chown -R must NEVER be used (preserves archive ownership/mode)
        for c in commands_captured:
            self.assertNotIn("chown -R", c)

    def test_remote_prepare_owner_failure_fails_closed(self):
        """Verify prepare failure (e.g. chown/chmod/stat fail) stops pipeline and never uploads or replaces."""
        commands_captured = []

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            commands_captured.append(remote_cmd)
            if "mkdir -p" in remote_cmd:
                return 1, "", "permission denied"
            return 0, "", ""

        upload_called = []

        with self.assertRaises(mirror.MirrorPipelineError) as ctx:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh,
                upload_runner=lambda *a: (upload_called.append(1), (0, "", ""))[1],
            )

        self.assertEqual(ctx.exception.stage, "REMOTE_PREPARE")
        self.assertEqual(ctx.exception.error_code, "DIR_PREPARE_FAILED")
        self.assertEqual(len(upload_called), 0)
        self.assertFalse(any("mv -f" in c for c in commands_captured))

        state = mirror.StateMarker(self.state_file).read()
        self.assertEqual(state["status"], "FAILED")
        self.assertEqual(state["lastStage"], "REMOTE_PREPARE")
        self.assertEqual(state["errorCode"], "DIR_PREPARE_FAILED")

    def test_remote_verify_owner_failure_cleans_partial_and_aborts_replace(self):
        """Verify that failure during verify stage (including owner/stat check) cleans partial and never calls replace."""
        commands_captured = []

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            commands_captured.append(remote_cmd)
            if "python3 -c" in remote_cmd:
                # Simulate stat or verify failure
                return 1, "", "verification/stat failed"
            return 0, "", ""

        with self.assertRaises(mirror.MirrorPipelineError) as ctx:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh,
                upload_runner=lambda *a: (0, "", ""),
            )

        self.assertEqual(ctx.exception.stage, "REMOTE_VERIFY")
        self.assertEqual(ctx.exception.error_code, "VERIFY_COMMAND_FAILED")
        # Ensure partial cleanup was invoked
        self.assertTrue(any("rm -f" in c and ".partial" in c for c in commands_captured))
        # Ensure mv replace was never called
        self.assertFalse(any("mv -f" in c and "messages_v2.sqlite" in c for c in commands_captured))

    def test_remote_replace_failure_executes_rollback_preserving_original_current(self):
        """Verify that replace failure triggers rollback execution to preserve existing current DB."""
        commands_captured = []

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            commands_captured.append(remote_cmd)
            # When replace script runs (containing rollback token), simulate failure
            if "had_cur" in remote_cmd and "mv -f" in remote_cmd:
                return 1, "", "replace post-chmod failed"
            return 0, "", ""

        def mock_download(scp_cmd, remote_src, local_dest):
            snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
            actual_sha = mirror.compute_sha256(snap_path)
            content = json.dumps({
                "integrityCheck": "ok",
                "messageCount": 3,
                "minSentAtIso": "2023-11-14T22:13:20",
                "maxSentAtIso": "2023-11-14T22:13:40",
                "sha256": actual_sha,
            })
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        with self.assertRaises(mirror.MirrorPipelineError) as ctx:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh,
                upload_runner=lambda *a: (0, "", ""),
                download_runner=mock_download,
            )

        self.assertEqual(ctx.exception.stage, "REMOTE_REPLACE")
        self.assertEqual(ctx.exception.error_code, "REPLACE_FAILED")

        # Check that rollback command was executed
        rollback_cmds = [c for c in commands_captured if "rollback" in c]
        self.assertTrue(len(rollback_cmds) >= 1)

        # Check state marker
        state = mirror.StateMarker(self.state_file).read()
        self.assertEqual(state["status"], "FAILED")
        self.assertEqual(state["lastStage"], "REMOTE_REPLACE")
        self.assertEqual(state["errorCode"], "REPLACE_FAILED")

    def test_remote_replace_timeout_executes_rollback(self):
        """Verify that timeout during replace triggers remote rollback."""
        commands_captured = []

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            commands_captured.append(remote_cmd)
            if "had_cur" in remote_cmd and "mv -f" in remote_cmd:
                raise subprocess.TimeoutExpired(cmd=ssh_cmd, timeout=30)
            return 0, "", ""

        def mock_download(scp_cmd, remote_src, local_dest):
            snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
            actual_sha = mirror.compute_sha256(snap_path)
            content = json.dumps({
                "integrityCheck": "ok",
                "messageCount": 3,
                "minSentAtIso": "2023-11-14T22:13:20",
                "maxSentAtIso": "2023-11-14T22:13:40",
                "sha256": actual_sha,
            })
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        with self.assertRaises(mirror.MirrorPipelineError) as ctx:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh,
                upload_runner=lambda *a: (0, "", ""),
                download_runner=mock_download,
            )

        self.assertEqual(ctx.exception.stage, "REMOTE_REPLACE")
        self.assertEqual(ctx.exception.error_code, "TIMEOUT")

        # Check that rollback attempt was made
        rollback_calls = [c for c in commands_captured if "rollback" in c and "had_cur" not in c]
        self.assertTrue(len(rollback_calls) >= 1)
        r_cmd = rollback_calls[-1]
        # Must remove new current before restoring rollback file
        rm_cur_pos = r_cmd.find("rm -f /var/data/kakao/messages_v2.sqlite")
        mv_cur_pos = r_cmd.find("mv -f /var/data/kakao/.messages_v2.rollback_")
        self.assertNotEqual(rm_cur_pos, -1, "Rollback command must remove new current DB")
        self.assertNotEqual(mv_cur_pos, -1, "Rollback command must move rollback file to current")
        self.assertLess(rm_cur_pos, mv_cur_pos, "rm -f current must execute BEFORE mv -f rollback current")
        # Must NOT require [ ! -f current ] because new current already exists
        self.assertNotIn("! -f /var/data/kakao/messages_v2.sqlite", r_cmd)
        # Must verify ownership and mode 600 after restore
        self.assertIn("chmod 600 /var/data/kakao/messages_v2.sqlite", r_cmd)
        self.assertIn("stat -c", r_cmd)
        self.assertIn("%u:%g:%a", r_cmd)
        self.assertIn("10000:10000:600", r_cmd)
        # Must clean up partial
        self.assertIn("rm -f /var/data/kakao/messages_v2.sqlite.partial", r_cmd)

    def test_remote_replace_failure_without_prior_current_cleans_new_current(self):
        """Verify that when no prior current existed, replace timeout/failure cleans up new current."""
        commands_captured = []

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            commands_captured.append(remote_cmd)
            # Fail during replace
            if "had_cur" in remote_cmd and "mv -f" in remote_cmd:
                raise subprocess.TimeoutExpired(cmd=ssh_cmd, timeout=30)
            return 0, "", ""

        def mock_download(scp_cmd, remote_src, local_dest):
            snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
            actual_sha = mirror.compute_sha256(snap_path)
            content = json.dumps({
                "integrityCheck": "ok",
                "messageCount": 3,
                "minSentAtIso": "2023-11-14T22:13:20",
                "maxSentAtIso": "2023-11-14T22:13:40",
                "sha256": actual_sha,
            })
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        with self.assertRaises(mirror.MirrorPipelineError) as ctx:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh,
                upload_runner=lambda *a: (0, "", ""),
                download_runner=mock_download,
            )

        self.assertEqual(ctx.exception.stage, "REMOTE_REPLACE")
        self.assertEqual(ctx.exception.error_code, "TIMEOUT")

        # Check replace_cmd handles no-prior-current (nocur marker)
        replace_cmds = [c for c in commands_captured if "had_cur" in c]
        self.assertTrue(len(replace_cmds) >= 1)
        rep_cmd = replace_cmds[0]
        self.assertIn("nocur", rep_cmd, "replace_cmd must track no-prior-current case")

        # Check rollback command cleans up new current when nocur marker exists
        rollback_calls = [c for c in commands_captured if "rollback" in c]
        self.assertTrue(len(rollback_calls) >= 1)
        r_cmd = rollback_calls[0]
        self.assertIn("nocur", r_cmd, "Rollback command must inspect nocur marker")
        self.assertIn("rm -f /var/data/kakao/messages_v2.sqlite", r_cmd)

    def test_powershell_post_sync_mirror_rejects_zero_or_negative_vps_uid_gid(self):
        """Verify post-sync-mirror.ps1 fails closed when VpsUid or VpsGid is <= 0."""
        post_sync_script = Path(__file__).resolve().parent.parent / "post-sync-mirror.ps1"
        self.assertTrue(post_sync_script.exists())

        test_dir = self.base_path / "ps_uid_test"
        test_dir.mkdir(parents=True, exist_ok=True)
        ps_copy = test_dir / "post-sync-mirror.ps1"
        shutil.copy2(post_sync_script, ps_copy)

        # Explicit invalid values must fail immediately in PowerShell
        for bad_arg, param_name in [
            (["-VpsUid", "0"], "VpsUid"),
            (["-VpsUid", "-1"], "VpsUid"),
            (["-VpsGid", "0"], "VpsGid"),
            (["-VpsGid", "-5"], "VpsGid"),
        ]:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(ps_copy), "-UploadOnly"] + bad_arg,
                capture_output=True,
                text=True,
                cwd=str(test_dir),
            )
            self.assertNotEqual(
                proc.returncode, 0,
                f"Expected non-zero exit for {bad_arg}, got code={proc.returncode}"
            )
            clean_text = "".join(f"{proc.stdout} {proc.stderr}".split())
            expected_fragment = f"{param_name}mustbeapositiveinteger(>0)"
            self.assertIn(
                expected_fragment,
                clean_text,
                f"Expected validation error {expected_fragment} in output for {bad_arg}, got: {proc.stdout}\n{proc.stderr}"
            )
            self.assertNotIn("Running kwin v2mirror", proc.stdout)

    def test_powershell_post_sync_mirror_omitted_vs_explicit_uid_gid(self):
        """Verify post-sync-mirror.ps1 distinguishes omitted from explicit 0/negative UID/GID."""
        post_sync_script = Path(__file__).resolve().parent.parent / "post-sync-mirror.ps1"
        self.assertTrue(post_sync_script.exists())

        test_dir = self.base_path / "ps_omitted_test"
        test_dir.mkdir(parents=True, exist_ok=True)
        ps_copy = test_dir / "post-sync-mirror.ps1"
        shutil.copy2(post_sync_script, ps_copy)

        log_file = test_dir / "captured_args.txt"
        wrapper = test_dir / "wrapper.ps1"
        wrapper.write_text(
            '$LogPath = $args[0]\n'
            '$Forward = if ($args.Length -gt 1) { $args[1..($args.Length - 1)] -join " " } else { "" }\n'
            'function global:python.exe {\n'
            '    param([Parameter(ValueFromRemainingArguments=$true)]$AllArgs)\n'
            '    [System.IO.File]::WriteAllText($LogPath, ($AllArgs -join " "))\n'
            '    $global:LASTEXITCODE = 0\n'
            '}\n'
            'Invoke-Expression "& \'$PSScriptRoot\\post-sync-mirror.ps1\' $Forward"\n',
            encoding="utf-8",
        )

        # Case 1: Omitted UID and GID -> should NOT pass --vps-uid or --vps-gid to python
        if log_file.exists():
            log_file.unlink()
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(wrapper), str(log_file), "-UploadOnly"],
            capture_output=True,
            text=True,
            cwd=str(test_dir),
        )
        self.assertEqual(proc.returncode, 0, f"Expected success when omitted, got err: {proc.stderr}")
        self.assertTrue(log_file.exists(), f"Log file does not exist. stdout={proc.stdout!r}, stderr={proc.stderr!r}")
        captured_text = log_file.read_text(encoding="utf-8")
        self.assertIn("-m kwin v2mirror", captured_text)
        self.assertIn("--upload-only", captured_text)
        self.assertNotIn("--vps-uid", captured_text)
        self.assertNotIn("--vps-gid", captured_text)

        # Case 2: Explicit positive UID and GID -> forwards to python
        log_file.unlink()
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(wrapper), str(log_file), "-UploadOnly", "-VpsUid", "20001", "-VpsGid", "20002"],
            capture_output=True,
            text=True,
            cwd=str(test_dir),
        )
        self.assertEqual(proc.returncode, 0, f"Expected success for valid IDs, got err: {proc.stderr}")
        self.assertTrue(log_file.exists())
        captured_text = log_file.read_text(encoding="utf-8")
        self.assertIn("--vps-uid 20001", captured_text)
        self.assertIn("--vps-gid 20002", captured_text)

        # Case 3: Explicit 0 -> fails immediately in PowerShell, python never executed
        log_file.unlink()
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(wrapper), str(log_file), "-UploadOnly", "-VpsUid", "0"],
            capture_output=True,
            text=True,
            cwd=str(test_dir),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(log_file.exists(), "Python should not have been called on invalid VpsUid")

    def test_cli_v2mirror_uid_gid_forwarding(self):
        """Verify cli.cmd_v2mirror parses and forwards vps_uid and vps_gid to sync_mirror."""
        from kwin import cli

        class MockArgs:
            user = None
            db = str(self.src_db)
            vps_target = "user@test.vps"
            vps_dir = "/var/data/kakao"
            vps_uid = 12345
            vps_gid = 54321
            ssh_key = None
            ssh_port = None
            upload_only = True
            disable = False
            enable = False

        called_kwargs = {}

        def mock_sync_mirror(**kwargs):
            called_kwargs.update(kwargs)
            return {"status": "SUCCESS"}

        with mock.patch("kwin.mirror.sync_mirror", side_effect=mock_sync_mirror):
            cli.cmd_v2mirror(MockArgs())

        self.assertEqual(called_kwargs.get("vps_uid"), 12345)
        self.assertEqual(called_kwargs.get("vps_gid"), 54321)


    def test_windows_and_remote_temp_files_cleaned_up_in_finally_on_success_and_failure(self):
        """Verify Windows plaintext snapshot and verification temp files are cleaned up in finally on both success and failure."""
        snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"

        def mock_upload(scp_cmd, local_p, remote_dst):
            return 0, "", ""

        def mock_download_success(scp_cmd, remote_src, local_dest):
            # snap_path must exist while pipeline is executing
            self.assertTrue(snap_path.exists(), "Snapshot must exist during transfer/verify")
            actual_sha = mirror.compute_sha256(snap_path)
            actual_sz = snap_path.stat().st_size
            content = json.dumps({
                "integrityCheck": "ok",
                "messageCount": 3,
                "minSentAtIso": "2023-11-14T22:13:20",
                "maxSentAtIso": "2023-11-14T22:13:40",
                "sha256": actual_sha,
                "sizeBytes": actual_sz,
            })
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        # Case 1: Success cleans up Windows plaintext snapshot
        res = mirror.sync_mirror(
            src_db=self.src_db,
            snapshot_dir=self.snapshot_dir,
            state_file=self.state_file,
            lock_file=self.lock_file,
            stop_file=self.stop_file,
            vps_ssh_target="user@test.vps",
            vps_remote_dir="/var/data/kakao",
            ssh_runner=lambda c, r, timeout=None: (0, "", ""),
            upload_runner=mock_upload,
            download_runner=mock_download_success,
        )
        self.assertEqual(res["status"], "SUCCESS")
        self.assertFalse(snap_path.exists(), "Windows plaintext snapshot must be deleted in finally on success")
        # Ensure no temp verification files remain
        leftover_tmps = list(self.snapshot_dir.glob(".verify_*.tmp"))
        self.assertEqual(len(leftover_tmps), 0)

        # Case 2: Upload failure cleans up Windows plaintext snapshot
        def mock_upload_fail(scp_cmd, local_p, remote_dst):
            return 1, "", "SCP upload failed"

        with self.assertRaises(mirror.MirrorPipelineError):
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=lambda c, r, timeout=None: (0, "", ""),
                upload_runner=mock_upload_fail,
            )
        self.assertFalse(snap_path.exists(), "Windows plaintext snapshot must be deleted in finally on failure")

    def test_source_database_immutability(self):
        """Verify source database is never mutated by mirror sync, and works even when marked read-only on disk."""
        import stat

        initial_sha = mirror.compute_sha256(self.src_db)
        initial_size = self.src_db.stat().st_size

        # Mark source file read-only on Windows filesystem
        os.chmod(self.src_db, stat.S_IREAD)
        try:
            def mock_upload(scp_cmd, local_p, remote_dst):
                return 0, "", ""

            def mock_download(scp_cmd, remote_src, local_dest):
                actual_sha = mirror.compute_sha256(self.snapshot_dir / "messages_v2_snapshot.sqlite")
                content = json.dumps({
                    "integrityCheck": "ok",
                    "messageCount": 3,
                    "minSentAtIso": "2023-11-14T22:13:20",
                    "maxSentAtIso": "2023-11-14T22:13:40",
                    "sha256": actual_sha,
                })
                Path(local_dest).write_text(content, encoding="utf-8")
                return 0, "", ""

            res = mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=lambda c, r, timeout=None: (0, "", ""),
                upload_runner=mock_upload,
                download_runner=mock_download,
            )
            self.assertEqual(res["status"], "SUCCESS")

            # Verify source DB was not mutated
            final_sha = mirror.compute_sha256(self.src_db)
            final_size = self.src_db.stat().st_size
            self.assertEqual(initial_sha, final_sha)
            self.assertEqual(initial_size, final_size)
        finally:
            # Restore write permission for cleanup
            os.chmod(self.src_db, stat.S_IWRITE | stat.S_IREAD)

    def test_remote_verify_size_mismatch_fails_and_cleans_partial(self):
        """Verify that size mismatch between remote verification and local snapshot triggers SIZE_MISMATCH and cleans partial."""
        commands_run = []

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            commands_run.append(remote_cmd)
            return 0, "", ""

        def mock_upload(scp_cmd, local_p, remote_dst):
            return 0, "", ""

        def mock_download(scp_cmd, remote_src, local_dest):
            actual_sha = mirror.compute_sha256(self.snapshot_dir / "messages_v2_snapshot.sqlite")
            content = json.dumps({
                "integrityCheck": "ok",
                "messageCount": 3,
                "minSentAtIso": "2023-11-14T22:13:20",
                "maxSentAtIso": "2023-11-14T22:13:40",
                "sha256": actual_sha,
                "sizeBytes": 9999999,  # Intentionally mismatched size
            })
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        with self.assertRaises(mirror.MirrorPipelineError) as ctx:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh,
                upload_runner=mock_upload,
                download_runner=mock_download,
            )

        self.assertEqual(ctx.exception.stage, "REMOTE_VERIFY")
        self.assertEqual(ctx.exception.error_code, "SIZE_MISMATCH")

        # Verify partial cleanup was executed
        self.assertTrue(any("rm -f /var/data/kakao/messages_v2.sqlite.partial" in c for c in commands_run))
        # Verify atomic replace was NOT executed
        self.assertFalse(any("mv -f /var/data/kakao/messages_v2.sqlite.partial" in c for c in commands_run))

    def test_remote_replace_preserves_previous_normal_copy_with_mode_600(self):
        """Verify that atomic replacement preserves previous current DB as messages_v2.sqlite.prev with mode 600 and UID:GID."""
        commands_run = []

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            commands_run.append(remote_cmd)
            return 0, "", ""

        def mock_upload(scp_cmd, local_p, remote_dst):
            return 0, "", ""

        def mock_download(scp_cmd, remote_src, local_dest):
            actual_sha = mirror.compute_sha256(self.snapshot_dir / "messages_v2_snapshot.sqlite")
            actual_sz = (self.snapshot_dir / "messages_v2_snapshot.sqlite").stat().st_size
            content = json.dumps({
                "integrityCheck": "ok",
                "messageCount": 3,
                "minSentAtIso": "2023-11-14T22:13:20",
                "maxSentAtIso": "2023-11-14T22:13:40",
                "sha256": actual_sha,
                "sizeBytes": actual_sz,
            })
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        res = mirror.sync_mirror(
            src_db=self.src_db,
            snapshot_dir=self.snapshot_dir,
            state_file=self.state_file,
            lock_file=self.lock_file,
            stop_file=self.stop_file,
            vps_ssh_target="user@test.vps",
            vps_remote_dir="/var/data/kakao",
            vps_uid=10000,
            vps_gid=10000,
            ssh_runner=mock_ssh,
            upload_runner=mock_upload,
            download_runner=mock_download,
        )

        self.assertEqual(res["status"], "SUCCESS")
        self.assertTrue(res["remoteMirror"].get("previousBackupPreserved"))

        # Inspect replace command
        replace_cmds = [c for c in commands_run if "had_cur" in c]
        self.assertTrue(len(replace_cmds) >= 1)
        rep = replace_cmds[0]
        self.assertIn("messages_v2.sqlite.prev", rep, "Replace script must preserve previous copy as .prev")
        self.assertIn("10000:10000", rep)
        self.assertIn("600", rep)


    def test_gzip_compression_savings_and_decompression_integrity(self):
        """Verify gzip compression reduces database size and decompresses with byte-for-byte SHA-256 match."""
        snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
        mirror.create_readonly_snapshot(self.src_db, snap_path)
        raw_size = snap_path.stat().st_size
        raw_sha = mirror.compute_sha256(snap_path)

        gz_path = self.snapshot_dir / "messages_v2_snapshot.sqlite.gz"
        comp_meta = mirror.compress_file_gzip(snap_path, gz_path)

        self.assertTrue(gz_path.exists())
        self.assertLess(comp_meta["sizeBytes"], raw_size)
        self.assertEqual(comp_meta["sizeBytes"], gz_path.stat().st_size)

        # Decompress and verify SHA-256
        import gzip
        decompressed_data = gzip.decompress(gz_path.read_bytes())
        import hashlib
        decomp_sha = hashlib.sha256(decompressed_data).hexdigest()
        self.assertEqual(decomp_sha, raw_sha)

    def test_mirror_pipeline_gzip_upload_and_decompression_success(self):
        """Verify mirror pipeline compresses locally, uploads .gz, decompresses remotely into .partial, and verifies."""
        uploaded_files = []
        commands_run = []

        def mock_upload(scp_cmd, local_p, remote_dst):
            uploaded_files.append((str(local_p), remote_dst))
            self.assertTrue(str(local_p).endswith(".gz"), f"Uploaded local file must be .gz: {local_p}")
            self.assertTrue(remote_dst.endswith(".gz"), f"Remote dest must be .gz: {remote_dst}")
            return 0, "", ""

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            commands_run.append(remote_cmd)
            return 0, "", ""

        def mock_download(scp_cmd, remote_src, local_dest):
            snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
            actual_sha = mirror.compute_sha256(snap_path)
            content = json.dumps({
                "integrityCheck": "ok",
                "messageCount": 3,
                "minSentAtIso": "2023-11-14T22:13:20",
                "maxSentAtIso": "2023-11-14T22:13:40",
                "sha256": actual_sha,
                "sizeBytes": snap_path.stat().st_size,
            })
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        res = mirror.sync_mirror(
            src_db=self.src_db,
            snapshot_dir=self.snapshot_dir,
            state_file=self.state_file,
            lock_file=self.lock_file,
            stop_file=self.stop_file,
            vps_ssh_target="user@test.vps",
            vps_remote_dir="/var/data/kakao",
            ssh_runner=mock_ssh,
            upload_runner=mock_upload,
            download_runner=mock_download,
        )

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(len(uploaded_files), 1)
        self.assertTrue(uploaded_files[0][0].endswith("messages_v2_snapshot.sqlite.gz"))
        self.assertTrue(uploaded_files[0][1].endswith("messages_v2.sqlite.partial.gz"))

        # Check remote decompression was executed
        decompress_cmds = [c for c in commands_run if "REMOTE_DECOMPRESS" in c or "gzip" in c or "decompress" in c]
        self.assertGreaterEqual(len(decompress_cmds), 1, "Remote decompression command must be run")

        # Check remote .gz cleanup was executed
        gz_cleanup_cmds = [c for c in commands_run if "rm -f" in c and "messages_v2.sqlite.partial.gz" in c]
        self.assertGreaterEqual(len(gz_cleanup_cmds), 1, "Remote .partial.gz must be cleaned up")

        # Verify no temp files left locally
        self.assertFalse((self.snapshot_dir / "messages_v2_snapshot.sqlite").exists())
        self.assertFalse((self.snapshot_dir / "messages_v2_snapshot.sqlite.gz").exists())

    def test_mirror_failure_vps_disk_full(self):
        """Verify remote disk full (ENOSPC, exit code 28) during decompression fails with DISK_FULL, preserves current, and cleans temp files."""
        commands_run = []

        def mock_upload(scp_cmd, local_p, remote_dst):
            return 0, "", ""

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            commands_run.append(remote_cmd)
            # Fail decompression with exit code 28 (ENOSPC)
            if "messages_v2.sqlite.partial.gz" in remote_cmd and ("gzip" in remote_cmd or "decompress" in remote_cmd or "copyfileobj" in remote_cmd):
                return 28, "", "No space left on device"
            return 0, "", ""

        with self.assertRaises(mirror.MirrorPipelineError) as ctx:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh,
                upload_runner=mock_upload,
            )

        self.assertEqual(ctx.exception.stage, "REMOTE_DECOMPRESS")
        self.assertEqual(ctx.exception.error_code, "DISK_FULL")

        # Verify cleanup of partial and partial.gz was run
        cleanup_cmds = [c for c in commands_run if "rm -f" in c]
        self.assertTrue(any("messages_v2.sqlite.partial.gz" in c for c in cleanup_cmds), "Must clean up remote .partial.gz")
        self.assertTrue(any("messages_v2.sqlite.partial" in c for c in cleanup_cmds), "Must clean up remote .partial")

        # Atomic replace must NOT be run
        self.assertFalse(any("mv -f" in c and "messages_v2.sqlite" in c for c in commands_run))

    def test_mirror_failure_local_compression_failed(self):
        """Verify local compression failure raises COMPRESS_FAILED, cleans local temp files, and leaves remote untouched."""
        def mock_bad_compress(src, dst):
            raise mirror.MirrorPipelineError("LOCAL_COMPRESS", "COMPRESS_FAILED")

        with unittest.mock.patch("kwin.mirror.compress_file_gzip", side_effect=mock_bad_compress):
            with self.assertRaises(mirror.MirrorPipelineError) as ctx:
                mirror.sync_mirror(
                    src_db=self.src_db,
                    snapshot_dir=self.snapshot_dir,
                    state_file=self.state_file,
                    lock_file=self.lock_file,
                    stop_file=self.stop_file,
                    vps_ssh_target="user@test.vps",
                    vps_remote_dir="/var/data/kakao",
                )

        self.assertEqual(ctx.exception.stage, "LOCAL_COMPRESS")
        self.assertEqual(ctx.exception.error_code, "COMPRESS_FAILED")

        # Verify local temp files cleaned up
        self.assertFalse((self.snapshot_dir / "messages_v2_snapshot.sqlite").exists())
        self.assertFalse((self.snapshot_dir / "messages_v2_snapshot.sqlite.gz").exists())

    def test_mirror_failure_remote_decompression_corrupt_gzip(self):
        """Verify decompression failure (exit code 1) triggers DECOMPRESS_FAILED and cleans up remote partials."""
        commands_run = []

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            commands_run.append(remote_cmd)
            if "messages_v2.sqlite.partial.gz" in remote_cmd and ("gzip" in remote_cmd or "decompress" in remote_cmd or "copyfileobj" in remote_cmd):
                return 1, "", "gzip: stdin: invalid compressed data"
            return 0, "", ""

        with self.assertRaises(mirror.MirrorPipelineError) as ctx:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh,
                upload_runner=lambda scp, lp, rd: (0, "", ""),
            )

        self.assertEqual(ctx.exception.stage, "REMOTE_DECOMPRESS")
        self.assertEqual(ctx.exception.error_code, "DECOMPRESS_FAILED")

        # Cleanups executed
        cleanup_cmds = [c for c in commands_run if "rm -f" in c]
        self.assertTrue(any("messages_v2.sqlite.partial.gz" in c for c in cleanup_cmds))
        self.assertTrue(any("messages_v2.sqlite.partial" in c for c in cleanup_cmds))

    def test_mirror_failure_sha256_mismatch_after_decompression(self):
        """Verify decompressed remote candidate hash mismatch against original uncompressed snapshot fails at REMOTE_VERIFY:HASH_MISMATCH."""
        commands_run = []

        def mock_download(scp_cmd, remote_src, local_dest):
            content = json.dumps({
                "integrityCheck": "ok",
                "messageCount": 3,
                "minSentAtIso": "2023-11-14T22:13:20",
                "maxSentAtIso": "2023-11-14T22:13:40",
                "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
                "sizeBytes": (self.snapshot_dir / "messages_v2_snapshot.sqlite").stat().st_size,
            })
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            commands_run.append(remote_cmd)
            return 0, "", ""

        with self.assertRaises(mirror.MirrorPipelineError) as ctx:
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=mock_ssh,
                upload_runner=lambda scp, lp, rd: (0, "", ""),
                download_runner=mock_download,
            )

        self.assertEqual(ctx.exception.stage, "REMOTE_VERIFY")
        self.assertEqual(ctx.exception.error_code, "HASH_MISMATCH")

        # Replace never called, partial cleanup called
        self.assertFalse(any("mv -f" in c and "messages_v2.sqlite" in c for c in commands_run))
        self.assertTrue(any("rm -f" in c and "messages_v2.sqlite.partial" in c for c in commands_run))

    def test_mirror_monotonic_deadline_budget_and_timeout(self):
        """Verify stages use remaining monotonic deadline budget and do not stack fixed timeouts exceeding total deadline."""
        recorded_timeouts = []

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            if timeout is not None:
                recorded_timeouts.append((remote_cmd[:30], timeout))
            return 0, "", ""

        def mock_upload(scp_cmd, local_p, remote_dst, timeout=None):
            if timeout is not None:
                recorded_timeouts.append(("upload", timeout))
            return 0, "", ""

        def mock_download(scp_cmd, remote_src, local_dest, timeout=None):
            snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
            actual_sha = mirror.compute_sha256(snap_path)
            content = json.dumps({
                "integrityCheck": "ok",
                "messageCount": 3,
                "minSentAtIso": "2023-11-14T22:13:20",
                "maxSentAtIso": "2023-11-14T22:13:40",
                "sha256": actual_sha,
                "sizeBytes": snap_path.stat().st_size,
            })
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        # Test 1: total_deadline_sec=5 binds stage timeouts to <= 5s
        res = mirror.sync_mirror(
            src_db=self.src_db,
            snapshot_dir=self.snapshot_dir,
            state_file=self.state_file,
            lock_file=self.lock_file,
            stop_file=self.stop_file,
            vps_ssh_target="user@test.vps",
            vps_remote_dir="/var/data/kakao",
            ssh_runner=mock_ssh,
            upload_runner=mock_upload,
            download_runner=mock_download,
            total_deadline_sec=5,
        )
        self.assertEqual(res["status"], "SUCCESS")
        for tag, to in recorded_timeouts:
            self.assertLessEqual(to, 5, f"Stage timeout for {tag} must not exceed remaining deadline 5, got {to}")

        # Test 2: total_deadline_sec=1 with simulated elapsed time triggers TIMEOUT
        with unittest.mock.patch("time.monotonic", side_effect=[0.0, 0.5, 10.0, 10.5, 11.0]):
            with self.assertRaises(mirror.MirrorPipelineError) as ctx:
                mirror.sync_mirror(
                    src_db=self.src_db,
                    snapshot_dir=self.snapshot_dir,
                    state_file=self.state_file,
                    lock_file=self.lock_file,
                    stop_file=self.stop_file,
                    vps_ssh_target="user@test.vps",
                    vps_remote_dir="/var/data/kakao",
                    ssh_runner=mock_ssh,
                    upload_runner=mock_upload,
                    total_deadline_sec=1,
                )
            self.assertEqual(ctx.exception.error_code, "TIMEOUT")

    def test_mirror_state_marker_separates_local_success_and_mirror_failure(self):
        """Verify mirror_state.json clearly separates local collection/snapshot success and message count from mirror failure."""
        def mock_upload_fail(scp_cmd, local_p, remote_dst):
            return 1, "", "upload failed"

        with self.assertRaises(mirror.MirrorPipelineError):
            mirror.sync_mirror(
                src_db=self.src_db,
                snapshot_dir=self.snapshot_dir,
                state_file=self.state_file,
                lock_file=self.lock_file,
                stop_file=self.stop_file,
                vps_ssh_target="user@test.vps",
                vps_remote_dir="/var/data/kakao",
                ssh_runner=lambda c, r, timeout=None: (0, "", ""),
                upload_runner=mock_upload_fail,
            )

        state = mirror.StateMarker(self.state_file).read()
        self.assertEqual(state["status"], "FAILED")
        self.assertEqual(state["lastStage"], "REMOTE_UPLOAD")
        self.assertEqual(state["errorCode"], "UPLOAD_FAILED")

        # Local snapshot information is preserved and marked SUCCESS
        self.assertIn("localSnapshot", state)
        self.assertEqual(state["localSnapshot"]["messageCount"], 3)
        self.assertIn("local", state)
        self.assertEqual(state["local"]["status"], "SUCCESS")
        self.assertEqual(state["local"]["messageCount"], 3)

        # Mirror information is marked FAILED with specific stage and error code
        self.assertIn("mirror", state)
        self.assertEqual(state["mirror"]["status"], "FAILED")
        self.assertEqual(state["mirror"]["stage"], "REMOTE_UPLOAD")
        self.assertEqual(state["mirror"]["errorCode"], "UPLOAD_FAILED")


    def test_success_path_no_separate_meta_cleanup_ssh_call(self):
        """Verify that temporary verify meta cleanup is integrated into replace command without a separate SSH call."""
        commands_run = []

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            commands_run.append(remote_cmd)
            return 0, "", ""

        def mock_upload(scp_cmd, local_p, remote_dst, timeout=None):
            return 0, "", ""

        def mock_download(scp_cmd, remote_src, local_dest, timeout=None):
            snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
            actual_sha = mirror.compute_sha256(snap_path)
            content = f"ok\n3|2023-11-14T22:13:20|2023-11-14T22:13:40\n{actual_sha}"
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        res = mirror.sync_mirror(
            src_db=self.src_db,
            snapshot_dir=self.snapshot_dir,
            state_file=self.state_file,
            lock_file=self.lock_file,
            stop_file=self.stop_file,
            vps_ssh_target="user@test.vps",
            vps_remote_dir="/var/data/kakao",
            ssh_runner=mock_ssh,
            upload_runner=mock_upload,
            download_runner=mock_download,
        )

        self.assertEqual(res["status"], "SUCCESS")

        # 1. No standalone SSH command for verify meta cleanup
        standalone_clean_meta = [
            c for c in commands_run
            if "rm -f" in c and ".verify_" in c and ("messages_v2" not in c and "mv -f" not in c)
        ]
        self.assertEqual(
            len(standalone_clean_meta),
            0,
            f"Temporary verify meta cleanup must not be executed as a separate SSH command: {standalone_clean_meta}"
        )

        # 2. Verify meta cleanup is integrated into the replace command
        replace_cmds = [c for c in commands_run if "mv -f" in c and "messages_v2.sqlite" in c]
        self.assertEqual(len(replace_cmds), 1, "Exactly one replace command must be executed")
        self.assertTrue(
            any(".verify_" in c and "rm -f" in c for c in replace_cmds),
            f"Replace command must integrate rm -f for temporary verify meta file: {replace_cmds[0]}"
        )

    def test_all_verify_failure_paths_clean_meta_leaving_zero_residue(self):
        """Verify that all verification failure paths clean up temporary verify meta file leaving 0 residue."""
        failure_cases = [
            ("count_mismatch", f"ok\n999|2023-11-14T22:13:20|2023-11-14T22:13:40\nSHA", "COUNT_MISMATCH"),
            ("min_mismatch", f"ok\n3|2020-01-01T00:00:00|2023-11-14T22:13:40\nSHA", "MIN_TIMESTAMP_MISMATCH"),
            ("max_mismatch", f"ok\n3|2023-11-14T22:13:20|2099-01-01T00:00:00\nSHA", "MAX_TIMESTAMP_MISMATCH"),
            ("hash_mismatch", f"ok\n3|2023-11-14T22:13:20|2023-11-14T22:13:40\n{('f' * 64)}", "HASH_MISMATCH"),
            ("integrity_bad", f"corrupt\n3|2023-11-14T22:13:20|2023-11-14T22:13:40\nSHA", "INTEGRITY_FAILED"),
            ("parse_error", "totally-malformed-meta", "PARSE_ERROR"),
        ]

        snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
        # Temporarily create snapshot to compute true sha
        actual_sha = mirror.compute_sha256(self.src_db)

        for case_name, meta_template, expected_err in failure_cases:
            with self.subTest(case=case_name):
                commands_run = []
                meta_content = meta_template.replace("SHA", actual_sha)

                def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
                    commands_run.append(remote_cmd)
                    return 0, "", ""

                def mock_upload(scp_cmd, local_p, remote_dst, timeout=None):
                    return 0, "", ""

                def mock_download(scp_cmd, remote_src, local_dest, timeout=None):
                    Path(local_dest).write_text(meta_content, encoding="utf-8")
                    return 0, "", ""

                with self.assertRaises(mirror.MirrorPipelineError) as ctx:
                    mirror.sync_mirror(
                        src_db=self.src_db,
                        snapshot_dir=self.snapshot_dir,
                        state_file=self.state_file,
                        lock_file=self.lock_file,
                        stop_file=self.stop_file,
                        vps_ssh_target="user@test.vps",
                        vps_remote_dir="/var/data/kakao",
                        ssh_runner=mock_ssh,
                        upload_runner=mock_upload,
                        download_runner=mock_download,
                    )

                self.assertEqual(ctx.exception.error_code, expected_err)
                self.assertEqual(ctx.exception.stage, "REMOTE_VERIFY")

                # Atomic replacement must NEVER be invoked
                self.assertFalse(
                    any("mv -f" in c and "messages_v2.sqlite" in c for c in commands_run),
                    "Atomic replacement must never be executed on verification failure"
                )

                # Cleanup command must be called and must include both .partial and .verify_ (0 residue)
                cleanup_cmds = [c for c in commands_run if "rm -f" in c]
                self.assertTrue(
                    any(".partial" in c and ".verify_" in c for c in cleanup_cmds),
                    f"Cleanup on failure must clean up both partial and meta file: {cleanup_cmds}"
                )

    def test_transient_clean_meta_timeout_eliminated_by_integration(self):
        """Verify that the production incident (30s timeout on separate clean_meta SSH call) is eliminated."""
        commands_run = []

        def mock_ssh(ssh_cmd, remote_cmd, timeout=None):
            commands_run.append(remote_cmd)
            # In old implementation: standalone clean_meta_cmd was called here and timed out
            if "rm -f" in remote_cmd and ".verify_" in remote_cmd and ("messages_v2" not in remote_cmd and "mv -f" not in remote_cmd):
                raise subprocess.TimeoutExpired(cmd=ssh_cmd, timeout=30)
            return 0, "", ""

        def mock_upload(scp_cmd, local_p, remote_dst, timeout=None):
            return 0, "", ""

        def mock_download(scp_cmd, remote_src, local_dest, timeout=None):
            snap_path = self.snapshot_dir / "messages_v2_snapshot.sqlite"
            actual_sha = mirror.compute_sha256(snap_path)
            content = f"ok\n3|2023-11-14T22:13:20|2023-11-14T22:13:40\n{actual_sha}"
            Path(local_dest).write_text(content, encoding="utf-8")
            return 0, "", ""

        # In old code: raised MirrorPipelineError("REMOTE_VERIFY", "TIMEOUT")
        res = mirror.sync_mirror(
            src_db=self.src_db,
            snapshot_dir=self.snapshot_dir,
            state_file=self.state_file,
            lock_file=self.lock_file,
            stop_file=self.stop_file,
            vps_ssh_target="user@test.vps",
            vps_remote_dir="/var/data/kakao",
            ssh_runner=mock_ssh,
            upload_runner=mock_upload,
            download_runner=mock_download,
        )
        self.assertEqual(res["status"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()
