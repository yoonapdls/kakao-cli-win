"""Focused unit tests for Kakao reply relationship preservation (Steps 1-3).

Uses ONLY synthetic in-memory/temporary SQLite databases.
Tests schema extension, field extraction, JSON robustness, and missing columns.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from kwin import collect


class TestReplyRelationships(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.decrypted_dir = os.path.join(self.temp_dir.name, "decrypted")
        os.makedirs(self.decrypted_dir, exist_ok=True)
        self.out_db = os.path.join(self.temp_dir.name, "messages_v2.sqlite")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_synthetic_chatlogs(self, filename: str, cols: list[str], rows: list[tuple]):
        db_path = os.path.join(self.decrypted_dir, filename)
        con = sqlite3.connect(db_path)
        col_defs = ", ".join(f"{c} TEXT" if "json" in c.lower() or c in ("message", "supplement", "attachement") else f"{c} INTEGER" for c in cols)
        con.execute(f"CREATE TABLE chatLogs({col_defs})")
        placeholders = ", ".join("?" for _ in cols)
        con.executemany(f"INSERT INTO chatLogs VALUES ({placeholders})", rows)
        con.commit()
        con.close()
        return db_path

    def test_schema_has_required_columns(self):
        """Verify messages table schema has all extended reply and relation columns."""
        cols = ["id", "authorId", "type", "message", "sendAt"]
        rows = [(101, 1, 1, "hello", 1700000000)]
        self._create_synthetic_chatlogs("chatLogs_1.sqlite", cols, rows)

        collect.build(self.decrypted_dir, self.out_db, {})

        con = sqlite3.connect(self.out_db)
        try:
            cur = con.cursor()
            table_cols = [r[1] for r in cur.execute("PRAGMA table_info(messages)").fetchall()]
        finally:
            con.close()

        expected = [
            "chatId", "logId", "authorId", "authorName", "type", "message",
            "sentAt", "sentAtIso", "threadId", "threadScope", "prevLogId",
            "referer", "attachmentSrcLogId", "supplementJson", "attachmentJson",
            "replyToLogId",
        ]
        for c in expected:
            self.assertIn(c, table_cols, f"Missing required column: {c}")

    def test_structural_extraction_and_normalization(self):
        """Verify normalization of threadId, threadScope, src_logId, replyToLogId, prevLogId, referer."""
        cols = [
            "id", "authorId", "type", "message", "sendAt",
            "threadId", "prevLogId", "referer", "supplement", "attachement",
        ]
        # Row 1: Root message
        # Row 2: Reply comment in thread (threadId=1001, scope=3, prevLogId=1001, referer=0)
        # Row 3: Quoted reply (src_logId=1001, threadId=0)
        # Row 4: Both thread comment AND quoted reply to another message (threadId=1001, src_logId=1002)
        # Row 5: threadId is 0 -> normalized to NULL
        rows = [
            (1001, 10, 1, "root message", 1700000001, 0, 0, 0, None, None),
            (1002, 11, 1, "thread reply", 1700000002, 1001, 1001, 0, json.dumps({"scope": 3, "threadId": 1001}), None),
            (1003, 12, 1, "quote reply", 1700000003, 0, 1002, 102, None, json.dumps({"src_logId": 1001, "src_type": 1, "src_message": "root"})),
            (1004, 13, 1, "thread comment quoting another", 1700000004, 1001, 1003, 0, json.dumps({"scope": 2, "threadId": 1001}), json.dumps({"src_logId": 1002, "src_type": 1})),
            (1005, 14, 1, "zero threadId normalized", 1700000005, 0, 1004, -1, None, None),
        ]
        self._create_synthetic_chatlogs("chatLogs_99.sqlite", cols, rows)

        collect.build(self.decrypted_dir, self.out_db, {10: "Alice", 11: "Bob"})

        con = sqlite3.connect(self.out_db)
        con.row_factory = sqlite3.Row
        try:
            cur = con.cursor()
            res = {r["logId"]: r for r in cur.execute("SELECT * FROM messages WHERE chatId=99").fetchall()}
        finally:
            con.close()

        self.assertEqual(len(res), 5)

        # Row 1: root
        r1 = res[1001]
        self.assertIsNone(r1["threadId"])
        self.assertIsNone(r1["threadScope"])
        self.assertEqual(r1["prevLogId"], 0)
        self.assertEqual(r1["referer"], 0)
        self.assertIsNone(r1["attachmentSrcLogId"])
        self.assertIsNone(r1["replyToLogId"])
        self.assertIsNone(r1["supplementJson"])
        self.assertIsNone(r1["attachmentJson"])

        # Row 2: thread comment
        r2 = res[1002]
        self.assertEqual(r2["threadId"], 1001)
        self.assertEqual(r2["threadScope"], 3)
        self.assertEqual(r2["prevLogId"], 1001)
        self.assertEqual(r2["referer"], 0)
        self.assertIsNone(r2["attachmentSrcLogId"])
        self.assertIsNone(r2["replyToLogId"])
        self.assertIsNotNone(r2["supplementJson"])
        self.assertIn("scope", r2["supplementJson"])

        # Row 3: quote reply
        r3 = res[1003]
        self.assertIsNone(r3["threadId"])
        self.assertIsNone(r3["threadScope"])
        self.assertEqual(r3["prevLogId"], 1002)
        self.assertEqual(r3["referer"], 102)
        self.assertEqual(r3["attachmentSrcLogId"], 1001)
        self.assertEqual(r3["replyToLogId"], 1001)
        self.assertIsNotNone(r3["attachmentJson"])
        self.assertIn("src_logId", r3["attachmentJson"])

        # Row 4: thread parent AND quote reply source separated
        r4 = res[1004]
        self.assertEqual(r4["threadId"], 1001)
        self.assertEqual(r4["threadScope"], 2)
        self.assertEqual(r4["attachmentSrcLogId"], 1002)
        self.assertEqual(r4["replyToLogId"], 1002)
        self.assertNotEqual(r4["threadId"], r4["replyToLogId"])

        # Row 5: 0 threadId normalized to NULL, negative referer preserved
        r5 = res[1005]
        self.assertIsNone(r5["threadId"])
        self.assertEqual(r5["referer"], -1)

    def test_malformed_json_does_not_drop_messages(self):
        """Verify corrupt or malformed JSON preserves message row and verbatim strings."""
        cols = [
            "id", "authorId", "type", "message", "sendAt",
            "threadId", "prevLogId", "referer", "supplement", "attachement",
        ]
        rows = [
            (2001, 1, 1, "broken supplement", 1700000010, 100, 0, 0, "{bad: json", None),
            (2002, 1, 1, "broken attachment", 1700000011, 0, 2001, 0, None, "{not valid json!!!"),
        ]
        self._create_synthetic_chatlogs("chatLogs_50.sqlite", cols, rows)

        total = collect.build(self.decrypted_dir, self.out_db, {})
        self.assertEqual(total, 2)

        con = sqlite3.connect(self.out_db)
        con.row_factory = sqlite3.Row
        try:
            r1 = con.execute("SELECT * FROM messages WHERE logId=2001").fetchone()
            self.assertEqual(r1["threadId"], 100)
            self.assertIsNone(r1["threadScope"])  # fallback due to malformed JSON
            self.assertEqual(r1["supplementJson"], "{bad: json")

            r2 = con.execute("SELECT * FROM messages WHERE logId=2002").fetchone()
            self.assertIsNone(r2["attachmentSrcLogId"])
            self.assertIsNone(r2["replyToLogId"])
            self.assertEqual(r2["attachmentJson"], "{not valid json!!!")
        finally:
            con.close()

    def test_absent_source_columns_supported(self):
        """Verify build succeeds when source DB lacks new columns (schema backward compatibility)."""
        cols = ["id", "authorId", "type", "message", "sendAt"]
        rows = [(3001, 1, 1, "legacy schema message", 1700000020)]
        self._create_synthetic_chatlogs("chatLogs_10.sqlite", cols, rows)

        total = collect.build(self.decrypted_dir, self.out_db, {})
        self.assertEqual(total, 1)

        con = sqlite3.connect(self.out_db)
        con.row_factory = sqlite3.Row
        try:
            r = con.execute("SELECT * FROM messages WHERE logId=3001").fetchone()
            self.assertEqual(r["logId"], 3001)
            self.assertIsNone(r["threadId"])
            self.assertIsNone(r["threadScope"])
            self.assertIsNone(r["prevLogId"])
            self.assertIsNone(r["referer"])
            self.assertIsNone(r["attachmentSrcLogId"])
            self.assertIsNone(r["supplementJson"])
            self.assertIsNone(r["attachmentJson"])
            self.assertIsNone(r["replyToLogId"])
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
