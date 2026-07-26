"""Read decrypted KakaoTalk SQLite files and consolidate into one DB + exports.

Schema is introspected (column names vary a little across versions):
  * TalkUserDB.talkUser: userid, nickName, friendNickName
  * chatLogs_<chatId>.chatLogs: id, type, chatId, authorId, message, sendAt/createdAt
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _decode_text(value: Any) -> Any:
    """Decode KakaoTalk TEXT fields robustly.

    Old KakaoTalk DBs normally contain UTF-8 text, but reading as bytes keeps us
    safe if a row has legacy/non-UTF8 text.  Numeric SQLite values are returned
    unchanged.
    """
    if value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        for enc in ("utf-8", "cp949", "euc-kr"):
            try:
                return value.decode(enc)
            except UnicodeDecodeError:
                pass
        return value.decode("utf-8", "replace")
    return value


def _to_int(value: Any) -> Any:
    value = _decode_text(value)
    if isinstance(value, str) and value.isdigit():
        try:
            return int(value)
        except ValueError:
            return value
    return value


def _cols(con: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [_decode_text(r[1]) for r in con.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def _pick(cols: List[str], *cands: str) -> Optional[str]:
    low = {c.lower(): c for c in cols}
    for c in cands:
        if c.lower() in low:
            return low[c.lower()]
    return None


def load_contacts(talkuserdb_sqlite: str) -> Dict[int, str]:
    contacts: Dict[int, str] = {}
    if not os.path.exists(talkuserdb_sqlite):
        return contacts
    con = sqlite3.connect(talkuserdb_sqlite)
    con.text_factory = bytes
    try:
        table = "talkUser" if _cols(con, "talkUser") else None
        if not table:
            return contacts
        cols = _cols(con, table)
        uid = _pick(cols, "userid", "userId", "id")
        nick = _pick(cols, "nickName", "name")
        friend = _pick(cols, "friendNickName")
        sel = ", ".join(c for c in (uid, nick, friend) if c)
        for row in con.execute(f"SELECT {sel} FROM {table}"):
            u = _to_int(row[0])
            names = [_decode_text(x) for x in row[1:] if x]
            contacts[u] = names[-1] if names else str(u)
    finally:
        con.close()
    return contacts


def _json_names(value: Any) -> List[str]:
    text = _decode_text(value)
    if not text:
        return []
    if not isinstance(text, str):
        text = str(text)
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return [text] if text.strip() else []

    names: List[str] = []

    def walk(v):
        if isinstance(v, dict):
            for key in ("nickName", "nickname", "name", "displayName", "userName"):
                val = v.get(key)
                if isinstance(val, str) and val.strip():
                    names.append(val.strip())
                    return
            for val in v.values():
                walk(val)
        elif isinstance(v, list):
            for val in v:
                walk(val)
        elif isinstance(v, str) and v.strip():
            # titleDisplayMembers can sometimes be a simple string list.
            names.append(v.strip())

    walk(obj)
    return names


def load_rooms(chatlist_sqlite: str, contacts: Dict[int, str]) -> Dict[int, Dict[str, Any]]:
    """Load chat room metadata from current/v2 chatListInfo.sqlite."""
    rooms: Dict[int, Dict[str, Any]] = {}
    if not os.path.exists(chatlist_sqlite):
        return rooms
    con = sqlite3.connect(chatlist_sqlite)
    con.text_factory = bytes
    try:
        table = "chatRoomList" if _cols(con, "chatRoomList") else None
        if not table:
            return rooms
        cols = _cols(con, table)
        c_chat = _pick(cols, "chatId")
        c_type = _pick(cols, "type")
        c_count = _pick(cols, "activeMembersCount")
        c_custom = _pick(cols, "useCustomChatRoomTitle")
        c_title = _pick(cols, "chatRoomTitle")
        c_updated = _pick(cols, "lastUpdatedAt")
        c_last_msg = _pick(cols, "lastChatMessage")
        c_direct = _pick(cols, "directChatMemberId")
        c_members = _pick(cols, "titleDisplayMembers")
        wanted = [
            c_chat, c_type, c_count, c_custom, c_title, c_updated,
            c_last_msg, c_direct, c_members,
        ]
        sel_cols = [c for c in wanted if c]
        if not c_chat or not sel_cols:
            return rooms
        for row in con.execute(f"SELECT {', '.join(sel_cols)} FROM {table}"):
            d = dict(zip(sel_cols, row))
            chat_id = _to_int(d.get(c_chat))
            if not isinstance(chat_id, int):
                continue
            direct_id = _to_int(d.get(c_direct)) if c_direct else None
            raw_title = _decode_text(d.get(c_title)) if c_title else None
            member_names = _json_names(d.get(c_members)) if c_members else []
            title = raw_title.strip() if isinstance(raw_title, str) and raw_title.strip() else ""
            source = "chatRoomTitle" if title else ""
            if not title and isinstance(direct_id, int) and direct_id in contacts:
                title = contacts[direct_id]
                source = "directChatMemberId"
            if not title and member_names:
                title = ", ".join(dict.fromkeys(member_names[:8]))
                source = "titleDisplayMembers"
            if not title:
                title = f"chatId:{chat_id}"
                source = "fallback"
            rooms[chat_id] = {
                "chatId": chat_id,
                "title": title,
                "titleSource": source,
                "type": _decode_text(d.get(c_type)) if c_type else None,
                "activeMembersCount": _to_int(d.get(c_count)) if c_count else None,
                "useCustomChatRoomTitle": _to_int(d.get(c_custom)) if c_custom else None,
                "directChatMemberId": direct_id,
                "lastUpdatedAt": _to_int(d.get(c_updated)) if c_updated else None,
                "lastChatMessage": _decode_text(d.get(c_last_msg)) if c_last_msg else None,
            }
    finally:
        con.close()
    return rooms


def load_room_members(chatlist_sqlite: str) -> List[Dict[str, Any]]:
    """Load chatId/userId membership rows from current/v2 chatListInfo.sqlite."""
    members: List[Dict[str, Any]] = []
    if not os.path.exists(chatlist_sqlite):
        return members
    con = sqlite3.connect(chatlist_sqlite)
    con.text_factory = bytes
    try:
        if not _cols(con, "chatMembers"):
            return members
        cols = _cols(con, "chatMembers")
        c_chat = _pick(cols, "chatId")
        c_user = _pick(cols, "userId")
        c_active = _pick(cols, "isActive")
        c_watermark = _pick(cols, "watermark")
        wanted = [c_chat, c_user, c_active, c_watermark]
        sel_cols = [c for c in wanted if c]
        if not c_chat or not c_user:
            return members
        for row in con.execute(f"SELECT {', '.join(sel_cols)} FROM chatMembers"):
            d = dict(zip(sel_cols, row))
            chat_id = _to_int(d.get(c_chat))
            user_id = _to_int(d.get(c_user))
            if not isinstance(chat_id, int) or not isinstance(user_id, int):
                continue
            members.append({
                "chatId": chat_id,
                "userId": user_id,
                "isActive": _to_int(d.get(c_active)) if c_active else None,
                "watermark": _to_int(d.get(c_watermark)) if c_watermark else None,
            })
    finally:
        con.close()
    return members


def _iso(ts) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def build(decrypted_dir: str, out_sqlite: str, contacts: Dict[int, str]) -> int:
    """Merge all chatLogs_*.sqlite in decrypted_dir into out_sqlite. Returns row count."""
    out = sqlite3.connect(out_sqlite)
    # This is a generated consolidation DB. Rebuild it from the decrypted files
    # every run so stale rows from an earlier extraction cannot linger.
    out.execute("DROP TABLE IF EXISTS messages")
    out.execute("""CREATE TABLE IF NOT EXISTS messages(
        chatId INTEGER, logId INTEGER, authorId INTEGER, authorName TEXT,
        type INTEGER, message TEXT, sentAt INTEGER, sentAtIso TEXT,
        PRIMARY KEY(chatId, logId))""")
    total = 0
    for path in sorted(glob.glob(os.path.join(decrypted_dir, "chatLogs_*.sqlite"))):
        base = os.path.basename(path)
        try:
            chat_id = int(base[len("chatLogs_"):-len(".sqlite")])
        except ValueError:
            chat_id = 0
        con = sqlite3.connect(path)
        con.text_factory = bytes
        try:
            if not _cols(con, "chatLogs"):
                continue
            cols = _cols(con, "chatLogs")
            c_id = _pick(cols, "id", "logId", "_id")
            c_author = _pick(cols, "authorId", "userId")
            c_type = _pick(cols, "type")
            c_msg = _pick(cols, "message")
            c_time = _pick(cols, "sendAt", "createdAt", "sentAt", "created_at")
            sel = ", ".join(c for c in (c_id, c_author, c_type, c_msg, c_time) if c)
            for row in con.execute(f"SELECT {sel} FROM chatLogs"):
                d = dict(zip([c for c in (c_id, c_author, c_type, c_msg, c_time) if c], row))
                author = _to_int(d.get(c_author))
                ts = d.get(c_time)
                out.execute(
                    "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?)",
                    (chat_id, d.get(c_id), author, contacts.get(author, str(author)),
                     d.get(c_type), _decode_text(d.get(c_msg)), ts, _iso(ts)))
                total += 1
        except sqlite3.Error:
            pass
        finally:
            con.close()
    out.commit()
    out.close()
    return total


def write_metadata(out_sqlite: str, contacts: Dict[int, str],
                   rooms: Dict[int, Dict[str, Any]],
                   room_members: Optional[List[Dict[str, Any]]] = None) -> None:
    """Write contact and room lookup tables into the consolidated DB."""
    out = sqlite3.connect(out_sqlite)
    try:
        out.execute("DROP TABLE IF EXISTS contacts")
        out.execute("""CREATE TABLE contacts(
            userId INTEGER PRIMARY KEY,
            name TEXT
        )""")
        out.executemany(
            "INSERT OR REPLACE INTO contacts(userId, name) VALUES (?, ?)",
            sorted(contacts.items()),
        )

        out.execute("DROP TABLE IF EXISTS rooms")
        out.execute("""CREATE TABLE rooms(
            chatId INTEGER PRIMARY KEY,
            title TEXT,
            titleSource TEXT,
            type TEXT,
            activeMembersCount INTEGER,
            useCustomChatRoomTitle INTEGER,
            directChatMemberId INTEGER,
            lastUpdatedAt INTEGER,
            lastChatMessage TEXT
        )""")
        out.executemany(
            """INSERT OR REPLACE INTO rooms(
                chatId, title, titleSource, type, activeMembersCount,
                useCustomChatRoomTitle, directChatMemberId, lastUpdatedAt,
                lastChatMessage
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    r.get("chatId"), r.get("title"), r.get("titleSource"),
                    r.get("type"), r.get("activeMembersCount"),
                    r.get("useCustomChatRoomTitle"), r.get("directChatMemberId"),
                    r.get("lastUpdatedAt"), r.get("lastChatMessage"),
                )
                for _chat_id, r in sorted(rooms.items())
            ],
        )

        out.execute("DROP TABLE IF EXISTS room_members")
        out.execute("""CREATE TABLE room_members(
            chatId INTEGER,
            userId INTEGER,
            isActive INTEGER,
            watermark INTEGER,
            PRIMARY KEY(chatId, userId)
        )""")
        out.executemany(
            """INSERT OR REPLACE INTO room_members(
                chatId, userId, isActive, watermark
            ) VALUES (?, ?, ?, ?)""",
            [
                (
                    r.get("chatId"), r.get("userId"),
                    r.get("isActive"), r.get("watermark"),
                )
                for r in (room_members or [])
            ],
        )
        out.commit()
    finally:
        out.close()
