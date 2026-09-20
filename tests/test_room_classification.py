"""Focused unit tests for Kakao room classification (isOpenChat and roomCategory).

Uses ONLY synthetic in-memory/temporary SQLite databases.
Tests classification rules, schema extension, DB migration/backfill, and API responses.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend import server
from kwin import collect


class TestRoomClassification(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.decrypted_dir = os.path.join(self.temp_dir.name, "decrypted")
        os.makedirs(self.decrypted_dir, exist_ok=True)
        self.out_db = os.path.join(self.temp_dir.name, "messages_v2.sqlite")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_classify_room_type_rules(self):
        """Verify strict classification rules for all known room types and fallbacks."""
        # Expected mapping: (isOpenChat, roomCategory)
        cases = [
            ("OM", (1, "open_group")),
            ("OD", (1, "open_direct")),
            ("MultiChat", (0, "normal_group")),
            ("DirectChat", (0, "normal_direct")),
            ("PlusChat", (0, "channel")),
            ("MemoChat", (0, "memo")),
            ("UnknownType", (None, "unknown")),
            ("", (None, "unknown")),
            (None, (None, "unknown")),
        ]
        for room_type, expected in cases:
            with self.subTest(room_type=room_type):
                result = collect.classify_room_type(room_type)
                self.assertEqual(result, expected)

    def test_load_rooms_and_write_metadata_populates_classification(self):
        """Verify load_rooms extracts and write_metadata saves isOpenChat and roomCategory."""
        chatlist_path = os.path.join(self.decrypted_dir, "chatListInfo.sqlite")
        con = sqlite3.connect(chatlist_path)
        con.execute("""
            CREATE TABLE chatRoomList(
                chatId INTEGER PRIMARY KEY,
                type TEXT,
                activeMembersCount INTEGER,
                useCustomChatRoomTitle INTEGER,
                chatRoomTitle TEXT,
                lastUpdatedAt INTEGER,
                lastChatMessage TEXT,
                directChatMemberId INTEGER,
                titleDisplayMembers TEXT
            )
        """)
        rooms_data = [
            (101, "OM", 50, 0, "Open Group 1", 1700000000, "hi", None, None),
            (102, "OD", 2, 0, "Open Direct 1", 1700000001, "hello", None, None),
            (103, "MultiChat", 5, 0, "Normal Group", 1700000002, "hey", None, None),
            (104, "DirectChat", 2, 0, "Normal Direct", 1700000003, "yo", 999, None),
            (105, "PlusChat", 1, 0, "Kakao Channel", 1700000004, "notice", None, None),
            (106, "MemoChat", 1, 0, "My Memo", 1700000005, "note", None, None),
            (107, "CustomUnknown", 3, 0, "Custom Room", 1700000006, "test", None, None),
            (108, None, 1, 0, "Null Type Room", 1700000007, "none", None, None),
        ]
        con.executemany("INSERT INTO chatRoomList VALUES (?,?,?,?,?,?,?,?,?)", rooms_data)
        con.commit()
        con.close()

        loaded = collect.load_rooms(chatlist_path, {999: "Friend"})
        self.assertEqual(len(loaded), 8)

        # Check loaded dict
        self.assertEqual(loaded[101]["type"], "OM")
        self.assertEqual(loaded[101]["isOpenChat"], 1)
        self.assertEqual(loaded[101]["roomCategory"], "open_group")

        self.assertEqual(loaded[102]["type"], "OD")
        self.assertEqual(loaded[102]["isOpenChat"], 1)
        self.assertEqual(loaded[102]["roomCategory"], "open_direct")

        self.assertEqual(loaded[103]["type"], "MultiChat")
        self.assertEqual(loaded[103]["isOpenChat"], 0)
        self.assertEqual(loaded[103]["roomCategory"], "normal_group")

        self.assertEqual(loaded[104]["type"], "DirectChat")
        self.assertEqual(loaded[104]["isOpenChat"], 0)
        self.assertEqual(loaded[104]["roomCategory"], "normal_direct")

        self.assertEqual(loaded[105]["type"], "PlusChat")
        self.assertEqual(loaded[105]["isOpenChat"], 0)
        self.assertEqual(loaded[105]["roomCategory"], "channel")

        self.assertEqual(loaded[106]["type"], "MemoChat")
        self.assertEqual(loaded[106]["isOpenChat"], 0)
        self.assertEqual(loaded[106]["roomCategory"], "memo")

        self.assertEqual(loaded[107]["type"], "CustomUnknown")
        self.assertIsNone(loaded[107]["isOpenChat"])
        self.assertEqual(loaded[107]["roomCategory"], "unknown")

        self.assertIsNone(loaded[108]["type"])
        self.assertIsNone(loaded[108]["isOpenChat"])
        self.assertEqual(loaded[108]["roomCategory"], "unknown")

        # Write to SQLite
        collect.write_metadata(self.out_db, {999: "Friend"}, loaded, [])

        # Verify SQLite schema and stored rows
        out_con = sqlite3.connect(self.out_db)
        out_con.row_factory = sqlite3.Row
        try:
            cols = [r[1] for r in out_con.execute("PRAGMA table_info(rooms)").fetchall()]
            self.assertIn("type", cols)
            self.assertIn("isOpenChat", cols)
            self.assertIn("roomCategory", cols)

            r_om = out_con.execute("SELECT * FROM rooms WHERE chatId=101").fetchone()
            self.assertEqual(r_om["type"], "OM")
            self.assertEqual(r_om["isOpenChat"], 1)
            self.assertEqual(r_om["roomCategory"], "open_group")

            r_multi = out_con.execute("SELECT * FROM rooms WHERE chatId=103").fetchone()
            self.assertEqual(r_multi["type"], "MultiChat")
            self.assertEqual(r_multi["isOpenChat"], 0)
            self.assertEqual(r_multi["roomCategory"], "normal_group")

            r_unk = out_con.execute("SELECT * FROM rooms WHERE chatId=107").fetchone()
            self.assertEqual(r_unk["type"], "CustomUnknown")
            self.assertIsNone(r_unk["isOpenChat"])
            self.assertEqual(r_unk["roomCategory"], "unknown")
        finally:
            out_con.close()

    def test_schema_migration_and_backfill(self):
        """Verify migrate_rooms adds missing columns and backfills existing rows without data loss."""
        con = sqlite3.connect(self.out_db)
        con.execute("""
            CREATE TABLE rooms(
                chatId INTEGER PRIMARY KEY,
                title TEXT,
                titleSource TEXT,
                type TEXT,
                activeMembersCount INTEGER,
                useCustomChatRoomTitle INTEGER,
                directChatMemberId INTEGER,
                lastUpdatedAt INTEGER,
                lastChatMessage TEXT
            )
        """)
        legacy_rows = [
            (1, "Room 1", "chatRoomTitle", "OM", 10, 0, None, 100, "hi"),
            (2, "Room 2", "chatRoomTitle", "OD", 2, 0, None, 200, "hello"),
            (3, "Room 3", "chatRoomTitle", "MultiChat", 4, 0, None, 300, "hey"),
            (4, "Room 4", "chatRoomTitle", "DirectChat", 2, 0, 99, 400, "yo"),
            (5, "Room 5", "chatRoomTitle", "PlusChat", 1, 0, None, 500, "notice"),
            (6, "Room 6", "chatRoomTitle", "MemoChat", 1, 0, None, 600, "memo"),
            (7, "Room 7", "chatRoomTitle", "SpecialChat", 3, 0, None, 700, "special"),
            (8, "Room 8", "chatRoomTitle", None, 2, 0, None, 800, "none"),
        ]
        con.executemany("INSERT INTO rooms VALUES (?,?,?,?,?,?,?,?,?)", legacy_rows)
        con.commit()
        con.close()

        # Run migration
        collect.migrate_rooms(self.out_db)

        # Verify
        con = sqlite3.connect(self.out_db)
        con.row_factory = sqlite3.Row
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(rooms)").fetchall()]
            self.assertIn("isOpenChat", cols)
            self.assertIn("roomCategory", cols)

            rows = {r["chatId"]: r for r in con.execute("SELECT * FROM rooms").fetchall()}
            self.assertEqual(rows[1]["isOpenChat"], 1)
            self.assertEqual(rows[1]["roomCategory"], "open_group")
            self.assertEqual(rows[1]["type"], "OM")

            self.assertEqual(rows[2]["isOpenChat"], 1)
            self.assertEqual(rows[2]["roomCategory"], "open_direct")

            self.assertEqual(rows[3]["isOpenChat"], 0)
            self.assertEqual(rows[3]["roomCategory"], "normal_group")

            self.assertEqual(rows[4]["isOpenChat"], 0)
            self.assertEqual(rows[4]["roomCategory"], "normal_direct")

            self.assertEqual(rows[5]["isOpenChat"], 0)
            self.assertEqual(rows[5]["roomCategory"], "channel")

            self.assertEqual(rows[6]["isOpenChat"], 0)
            self.assertEqual(rows[6]["roomCategory"], "memo")

            self.assertIsNone(rows[7]["isOpenChat"])
            self.assertEqual(rows[7]["roomCategory"], "unknown")

            self.assertIsNone(rows[8]["isOpenChat"])
            self.assertEqual(rows[8]["roomCategory"], "unknown")
        finally:
            con.close()

    def test_api_rooms_and_meta_expose_classification_fields(self):
        """Verify list_rooms and get_room API endpoints expose isOpenChat and roomCategory."""
        # Create full DB with messages and rooms
        con = sqlite3.connect(self.out_db)
        con.execute("""
            CREATE TABLE messages(
                chatId INTEGER, logId INTEGER, authorId INTEGER, authorName TEXT,
                type INTEGER, message TEXT, sentAt INTEGER, sentAtIso TEXT,
                PRIMARY KEY(chatId, logId)
            )
        """)
        con.execute("""
            CREATE TABLE rooms(
                chatId INTEGER PRIMARY KEY,
                title TEXT,
                titleSource TEXT,
                type TEXT,
                isOpenChat INTEGER,
                roomCategory TEXT,
                activeMembersCount INTEGER,
                useCustomChatRoomTitle INTEGER,
                directChatMemberId INTEGER,
                lastUpdatedAt INTEGER,
                lastChatMessage TEXT
            )
        """)
        con.execute("INSERT INTO messages VALUES (101, 1, 10, 'A', 1, 'hi', 1700000000, '2023-11-14T22:13:20')")
        con.execute("INSERT INTO messages VALUES (102, 2, 20, 'B', 1, 'bye', 1700000001, '2023-11-14T22:13:21')")
        con.execute("INSERT INTO rooms VALUES (101, 'Open Room', 'chatRoomTitle', 'OM', 1, 'open_group', 10, 0, NULL, 1700000000, 'hi')")
        con.execute("INSERT INTO rooms VALUES (102, 'Direct Room', 'chatRoomTitle', 'DirectChat', 0, 'normal_direct', 2, 0, 20, 1700000001, 'bye')")
        con.commit()
        con.close()

        from pathlib import Path
        with patch.object(server, "_find_default_db", return_value=Path(self.out_db)):
            # 1. list_rooms
            res = server.list_rooms()
            self.assertEqual(len(res["rooms"]), 2)
            room_map = {r["chatId"]: r for r in res["rooms"]}

            self.assertEqual(room_map["101"]["roomType"], "OM")
            self.assertEqual(room_map["101"]["isOpenChat"], 1)
            self.assertEqual(room_map["101"]["roomCategory"], "open_group")

            self.assertEqual(room_map["102"]["roomType"], "DirectChat")
            self.assertEqual(room_map["102"]["isOpenChat"], 0)
            self.assertEqual(room_map["102"]["roomCategory"], "normal_direct")

            # 2. get_room meta endpoint
            meta101 = server.get_room("101")
            self.assertEqual(meta101["isOpenChat"], 1)
            self.assertEqual(meta101["roomCategory"], "open_group")
            self.assertEqual(meta101["room"]["isOpenChat"], 1)
            self.assertEqual(meta101["room"]["roomCategory"], "open_group")

            meta102 = server.get_room("102")
            self.assertEqual(meta102["isOpenChat"], 0)
            self.assertEqual(meta102["roomCategory"], "normal_direct")
            self.assertEqual(meta102["room"]["isOpenChat"], 0)
            self.assertEqual(meta102["room"]["roomCategory"], "normal_direct")


if __name__ == "__main__":
    unittest.main()
