"""Local API server for kakao-cli-windows.

Run from the project root:

    python backend/server.py

The server is localhost-only. It serves the React build from web/dist when
available and exposes local-only APIs under /api/*.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("KAKAO_WIN_DATA", PROJECT_ROOT / "data"))
CONFIG_DIR = DATA_DIR / "config"
OUT_ROOT = Path(os.environ.get("KAKAO_WIN_OUT", DATA_DIR / "output"))
WEB_DIST = PROJECT_ROOT / "web" / "dist"
LEGACY_OUT_ROOT = Path.home() / "Downloads" / "kakao-win-output"
USE_LEGACY_FALLBACK = os.environ.get("KAKAO_WIN_LEGACY_FALLBACK", "").lower() in {"1", "true", "yes", "on"}
JOBS_FILE = CONFIG_DIR / "automation_jobs.json"
STYLE_FILE = CONFIG_DIR / "summary_style.json"
KST = timezone(timedelta(hours=9), "KST")
SYNC_LOCK = threading.Lock()

DEFAULT_SUMMARY_STYLE: Dict[str, str] = {
    "style": "structured",
    "outputFormat": "markdown",
    "language": "ko",
    "prompt": (
        "대화 내용을 핵심 이슈, 결정사항, 할 일, 주의할 점으로 나눠 "
        "간결하게 요약해줘. 불확실한 내용은 추측하지 말고 원문 기준으로만 정리해줘."
    ),
}


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)


def _kst(ts: Any) -> str:
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=KST).strftime("%Y-%m-%d %H:%M:%S KST")
    except (TypeError, ValueError, OSError):
        return str(ts)


def _now_kst() -> str:
    return datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M:%S KST")


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ")


def _user_label(user_id: Any, name: Any) -> str:
    user_id_s = "" if user_id is None else str(user_id)
    name_s = _clean_text(name)
    if name_s and name_s != user_id_s:
        return f"{name_s} ({user_id_s})"
    return user_id_s


def _file_mtime_kst(path: Path) -> str:
    try:
        return _kst(int(path.stat().st_mtime))
    except OSError:
        return ""


def _parse_kst_datetime(value: str, *, end_of_day: bool = False) -> Optional[int]:
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    normalized = value.replace("T", " ")
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(normalized, fmt)
            if fmt == "%Y-%m-%d" and end_of_day:
                dt = dt.replace(hour=23, minute=59, second=59)
            return int(dt.replace(tzinfo=KST).timestamp())
        except ValueError:
            continue
    raise ValueError(f"invalid datetime: {value!r}. Use YYYY-MM-DD, YYYY-MM-DDTHH:MM, or unix seconds.")


def _find_default_db() -> Optional[Path]:
    candidates: List[Path] = []
    bases = [OUT_ROOT]
    if USE_LEGACY_FALLBACK:
        bases.append(LEGACY_OUT_ROOT)
    for base in bases:
        if not base.is_dir():
            continue
        candidates.extend(base.glob("*/messages_v2.sqlite"))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
    )


def _table_count(con: sqlite3.Connection, name: str) -> Optional[int]:
    if not _has_table(con, name):
        return None
    return int(con.execute(f"SELECT count(*) FROM {name}").fetchone()[0])


def api_status() -> Dict[str, Any]:
    _ensure_dirs()
    db_path = _find_default_db()
    keys_path = OUT_ROOT / "v2_keys.json"
    keys_count = 0
    if keys_path.exists():
        try:
            data = json.loads(keys_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                keys_count = len(data.get("keys", data))
        except (OSError, json.JSONDecodeError):
            keys_count = 0

    status: Dict[str, Any] = {
        "ok": True,
        "projectRoot": str(PROJECT_ROOT),
        "dataDir": str(DATA_DIR),
        "outRoot": str(OUT_ROOT),
        "webDist": str(WEB_DIST),
        "legacyFallback": USE_LEGACY_FALLBACK,
        "keysFile": str(keys_path),
        "keysExists": keys_path.exists(),
        "keysCount": keys_count,
        "db": str(db_path) if db_path else "",
        "dbExists": bool(db_path),
        "dbUpdatedKst": _file_mtime_kst(db_path) if db_path else "",
    }
    if not db_path:
        status["ready"] = False
        status["nextStep"] = "Open KakaoTalk rooms and run POST /api/recover-sync."
        return status

    con = _connect(db_path)
    try:
        status["ready"] = True
        status["counts"] = {
            "messages": _table_count(con, "messages"),
            "rooms": _table_count(con, "rooms"),
            "contacts": _table_count(con, "contacts"),
            "roomMembers": _table_count(con, "room_members"),
        }
        if _has_table(con, "messages"):
            row = con.execute(
                "SELECT min(sentAt), max(sentAt), count(distinct chatId) FROM messages"
            ).fetchone()
            status["messageRange"] = {
                "firstSent": row[0],
                "lastSent": row[1],
                "firstSentKst": _kst(row[0]),
                "lastSentKst": _kst(row[1]),
                "distinctChatIds": row[2],
            }
    finally:
        con.close()
    return status


def list_rooms(q: str = "", limit: int = 300) -> Dict[str, Any]:
    db_path = _find_default_db()
    if not db_path:
        return {
            "db": "",
            "dbUpdatedKst": "",
            "rooms": [],
            "warning": "messages_v2.sqlite not found. Run sync after recovering keys.",
        }
    con = _connect(db_path)
    try:
        has_rooms = _has_table(con, "rooms")
        where = ""
        params: List[Any] = []
        if q:
            if has_rooms:
                where = "WHERE CAST(r.chatId AS TEXT) LIKE ? OR room.title LIKE ?"
                params.extend([f"%{q}%", f"%{q}%"])
            else:
                where = "WHERE CAST(r.chatId AS TEXT) LIKE ?"
                params.append(f"%{q}%")

        title_expr = (
            "coalesce(room.title, 'chatId:' || r.chatId)"
            if has_rooms
            else "'chatId:' || r.chatId"
        )
        room_join = "LEFT JOIN rooms room ON room.chatId = r.chatId" if has_rooms else ""
        room_fields = (
            "room.type AS roomType, room.activeMembersCount, room.titleSource, room.lastChatMessage"
            if has_rooms
            else "NULL AS roomType, NULL AS activeMembersCount, NULL AS titleSource, NULL AS lastChatMessage"
        )
        sql = f"""
            WITH r AS (
                SELECT chatId,
                       count(*) AS messageCount,
                       sum(CASE WHEN message IS NOT NULL AND length(message) > 0
                                THEN 1 ELSE 0 END) AS textCount,
                       min(sentAt) AS firstSent,
                       max(sentAt) AS lastSent
                  FROM messages
                 GROUP BY chatId
            ),
            last_rows AS (
                SELECT m.chatId, m.message, m.authorName, m.authorId,
                       m.type AS lastMessageType, m.sentAtIso
                  FROM messages m
                  JOIN r ON r.chatId = m.chatId AND r.lastSent = m.sentAt
                 WHERE m.logId = (
                       SELECT max(m2.logId)
                         FROM messages m2
                        WHERE m2.chatId = m.chatId
                          AND m2.sentAt = r.lastSent
                 )
            )
            SELECT r.chatId, {title_expr} AS title,
                   r.messageCount, r.textCount, r.firstSent, r.lastSent,
                   l.message AS lastMessage, l.authorId AS lastAuthorId,
                   l.authorName AS lastAuthorName, l.lastMessageType,
                   l.sentAtIso AS lastSentIso,
                   {room_fields}
              FROM r
              LEFT JOIN last_rows l ON l.chatId = r.chatId
              {room_join}
              {where}
             ORDER BY r.lastSent DESC, r.messageCount DESC
             LIMIT ?
        """
        params.append(limit)
        rooms = []
        for row in con.execute(sql, params):
            rooms.append(
                {
                    "chatId": str(row["chatId"]),
                    "title": _clean_text(row["title"]),
                    "roomType": row["roomType"],
                    "activeMembersCount": row["activeMembersCount"],
                    "titleSource": row["titleSource"],
                    "messageCount": row["messageCount"],
                    "textCount": row["textCount"],
                    "firstSent": row["firstSent"],
                    "lastSent": row["lastSent"],
                    "firstSentKst": _kst(row["firstSent"]),
                    "lastSentKst": _kst(row["lastSent"]),
                    "lastMessage": _clean_text(row["lastMessage"]),
                    "lastAuthor": _user_label(row["lastAuthorId"], row["lastAuthorName"]),
                    "lastMessageType": row["lastMessageType"],
                }
            )
        return {
            "db": str(db_path),
            "dbUpdatedKst": _file_mtime_kst(db_path),
            "rooms": rooms,
        }
    finally:
        con.close()


def _room_meta(con: sqlite3.Connection, chat_id: str) -> Dict[str, Any]:
    if not _has_table(con, "rooms"):
        return {"chatId": str(chat_id), "title": f"chatId:{chat_id}"}
    row = con.execute(
        """SELECT chatId, title, titleSource, type, activeMembersCount,
                  useCustomChatRoomTitle, directChatMemberId, lastUpdatedAt,
                  lastChatMessage
             FROM rooms
            WHERE chatId = ?""",
        (chat_id,),
    ).fetchone()
    if not row:
        return {"chatId": str(chat_id), "title": f"chatId:{chat_id}"}
    return {
        "chatId": str(row["chatId"]),
        "title": _clean_text(row["title"]),
        "titleSource": row["titleSource"],
        "roomType": row["type"],
        "activeMembersCount": row["activeMembersCount"],
        "useCustomChatRoomTitle": row["useCustomChatRoomTitle"],
        "directChatMemberId": str(row["directChatMemberId"]) if row["directChatMemberId"] is not None else "",
        "lastUpdatedAt": row["lastUpdatedAt"],
        "lastUpdatedAtKst": _kst(row["lastUpdatedAt"]),
        "lastChatMessage": _clean_text(row["lastChatMessage"]),
    }


def _message_filters(chat_id: str, qs: Dict[str, List[str]]) -> tuple[str, List[Any]]:
    where = ["chatId = ?"]
    params: List[Any] = [chat_id]

    date = qs.get("date", [""])[0]
    if date:
        start = _parse_kst_datetime(date, end_of_day=False)
        end = _parse_kst_datetime(date, end_of_day=True)
        where.append("sentAt BETWEEN ? AND ?")
        params.extend([start, end])

    start_value = qs.get("from", qs.get("start", [""]))[0]
    end_value = qs.get("to", qs.get("end", [""]))[0]
    if start_value:
        where.append("sentAt >= ?")
        params.append(_parse_kst_datetime(start_value, end_of_day=False))
    if end_value:
        where.append("sentAt <= ?")
        params.append(_parse_kst_datetime(end_value, end_of_day=True))

    q = qs.get("q", [""])[0].strip()
    if q:
        where.append("(message LIKE ? OR authorName LIKE ? OR CAST(authorId AS TEXT) LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])

    author_id = qs.get("authorId", [""])[0].strip()
    if author_id:
        where.append("CAST(authorId AS TEXT) = ?")
        params.append(author_id)

    msg_type = qs.get("type", [""])[0].strip()
    if msg_type:
        where.append("CAST(type AS TEXT) = ?")
        params.append(msg_type)

    return " AND ".join(where), params


def list_messages(chat_id: str, qs: Dict[str, List[str]]) -> Dict[str, Any]:
    db_path = _find_default_db()
    if not db_path:
        raise RuntimeError("messages_v2.sqlite not found. Run POST /api/recover-sync first.")
    limit = min(max(int(qs.get("limit", ["100"])[0]), 1), 5000)
    offset = max(int(qs.get("offset", ["0"])[0]), 0)
    order = qs.get("order", ["asc"])[0].lower()
    if order not in {"asc", "desc"}:
        raise ValueError("order must be asc or desc")

    con = _connect(db_path)
    try:
        where_sql, params = _message_filters(chat_id, qs)
        total = con.execute(f"SELECT count(*) FROM messages WHERE {where_sql}", params).fetchone()[0]
        rows = con.execute(
            f"""SELECT chatId, logId, authorId, authorName, type, message, sentAt, sentAtIso
                  FROM messages
                 WHERE {where_sql}
                 ORDER BY sentAt {order.upper()}, logId {order.upper()}
                 LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        ).fetchall()
        messages = [
            {
                "chatId": str(row["chatId"]),
                "logId": str(row["logId"]),
                "authorId": str(row["authorId"]) if row["authorId"] is not None else "",
                "authorName": _clean_text(row["authorName"]),
                "author": _user_label(row["authorId"], row["authorName"]),
                "type": row["type"],
                "message": row["message"] or "",
                "sentAt": row["sentAt"],
                "sentAtIso": row["sentAtIso"],
                "sentAtKst": _kst(row["sentAt"]),
            }
            for row in rows
        ]
        return {
            "db": str(db_path),
            "dbUpdatedKst": _file_mtime_kst(db_path),
            "room": _room_meta(con, chat_id),
            "query": {
                "chatId": str(chat_id),
                "limit": limit,
                "offset": offset,
                "order": order,
                "date": qs.get("date", [""])[0],
                "from": qs.get("from", qs.get("start", [""]))[0],
                "to": qs.get("to", qs.get("end", [""]))[0],
                "q": qs.get("q", [""])[0],
                "authorId": qs.get("authorId", [""])[0],
                "type": qs.get("type", [""])[0],
            },
            "total": total,
            "count": len(messages),
            "messages": messages,
        }
    finally:
        con.close()


def list_members(chat_id: str, qs: Dict[str, List[str]]) -> Dict[str, Any]:
    db_path = _find_default_db()
    if not db_path:
        raise RuntimeError("messages_v2.sqlite not found. Run POST /api/recover-sync first.")
    limit = min(max(int(qs.get("limit", ["1000"])[0]), 1), 10000)
    con = _connect(db_path)
    try:
        if not _has_table(con, "room_members"):
            return {"db": str(db_path), "room": _room_meta(con, chat_id), "members": [], "warning": "room_members table not found"}
        rows = con.execute(
            """SELECT rm.chatId, rm.userId, coalesce(c.name, CAST(rm.userId AS TEXT)) AS name,
                      rm.isActive, rm.watermark
                 FROM room_members rm
                 LEFT JOIN contacts c ON c.userId = rm.userId
                WHERE rm.chatId = ?
                ORDER BY rm.isActive DESC, name ASC
                LIMIT ?""",
            (chat_id, limit),
        ).fetchall()
        members = [
            {
                "chatId": str(row["chatId"]),
                "userId": str(row["userId"]),
                "name": _clean_text(row["name"]),
                "isActive": row["isActive"],
                "watermark": row["watermark"],
            }
            for row in rows
        ]
        return {
            "db": str(db_path),
            "room": _room_meta(con, chat_id),
            "count": len(members),
            "members": members,
        }
    finally:
        con.close()


def list_users(q: str = "", limit: int = 300) -> Dict[str, Any]:
    db_path = _find_default_db()
    if not db_path:
        raise RuntimeError("messages_v2.sqlite not found. Run POST /api/recover-sync first.")
    limit = min(max(limit, 1), 1000)
    con = _connect(db_path)
    try:
        if not _has_table(con, "contacts"):
            return {"db": str(db_path), "users": [], "warning": "contacts table not found"}
        where = ""
        params: List[Any] = []
        if q:
            where = "WHERE name LIKE ? OR CAST(userId AS TEXT) LIKE ?"
            params.extend([f"%{q}%", f"%{q}%"])
        rows = con.execute(
            f"SELECT userId, name FROM contacts {where} ORDER BY name ASC LIMIT ?",
            [*params, limit],
        ).fetchall()
        return {
            "db": str(db_path),
            "count": len(rows),
            "users": [
                {"userId": str(row["userId"]), "name": _clean_text(row["name"])}
                for row in rows
            ],
        }
    finally:
        con.close()


def export_messages(chat_id: str, qs: Dict[str, List[str]]) -> tuple[str, bytes, str]:
    fmt = qs.get("format", ["json"])[0].lower()
    data = list_messages(chat_id, qs)
    safe_chat_id = "".join(ch for ch in str(chat_id) if ch.isalnum() or ch in "-_")
    if fmt == "json":
        return (
            f"messages_{safe_chat_id}.json",
            json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
            "application/json; charset=utf-8",
        )
    if fmt == "csv":
        out = io.StringIO()
        writer = csv.DictWriter(
            out,
            fieldnames=["sentAtKst", "authorName", "authorId", "type", "message", "chatId", "logId"],
        )
        writer.writeheader()
        for msg in data["messages"]:
            writer.writerow({
                "sentAtKst": msg["sentAtKst"],
                "authorName": msg["authorName"],
                "authorId": msg["authorId"],
                "type": msg["type"],
                "message": msg["message"],
                "chatId": msg["chatId"],
                "logId": msg["logId"],
            })
        return (
            f"messages_{safe_chat_id}.csv",
            out.getvalue().encode("utf-8-sig"),
            "text/csv; charset=utf-8",
        )
    raise ValueError("format must be json or csv")


def _load_json_list(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def load_jobs() -> Dict[str, Any]:
    return {"jobsFile": str(JOBS_FILE), "jobs": _load_json_list(JOBS_FILE)}


def save_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    chat_id = str(payload.get("chatId") or "").strip()
    if not chat_id:
        raise ValueError("chatId is required")
    job = {
        "id": f"job-{int(time.time() * 1000)}",
        "createdAtKst": _now_kst(),
        "chatId": chat_id,
        "roomTitle": _clean_text(payload.get("roomTitle")),
        "scheduleTime": str(payload.get("scheduleTime") or "오전 09:00"),
        "repeatMode": str(payload.get("repeatMode") or "daily"),
        "weekdays": [str(x) for x in payload.get("weekdays", []) if str(x).strip()],
        "period": str(payload.get("period") or "since_last_run"),
        "style": str(payload.get("style") or "structured"),
        "destination": str(payload.get("destination") or "local_file"),
        "outputRoot": str(OUT_ROOT),
    }
    _ensure_dirs()
    jobs = _load_json_list(JOBS_FILE)
    jobs.append(job)
    JOBS_FILE.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "job": job, "jobsFile": str(JOBS_FILE)}


def delete_job(job_id: str) -> Dict[str, Any]:
    jobs = _load_json_list(JOBS_FILE)
    kept = [j for j in jobs if str(j.get("id") or "") != str(job_id)]
    deleted = len(jobs) - len(kept)
    if not deleted:
        raise ValueError(f"job not found: {job_id}")
    _ensure_dirs()
    JOBS_FILE.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "deleted": deleted, "jobs": kept, "jobsFile": str(JOBS_FILE)}


def load_style() -> Dict[str, Any]:
    style = dict(DEFAULT_SUMMARY_STYLE)
    if STYLE_FILE.exists():
        try:
            loaded = json.loads(STYLE_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                style.update({k: str(v) for k, v in loaded.items() if v is not None})
        except (OSError, json.JSONDecodeError):
            pass
    return {"styleFile": str(STYLE_FILE), "style": style}


def save_style(payload: Dict[str, Any]) -> Dict[str, Any]:
    style = load_style()["style"]
    for key in ("style", "outputFormat", "language", "prompt"):
        if key in payload and payload[key] is not None:
            style[key] = str(payload[key])
    _ensure_dirs()
    STYLE_FILE.write_text(json.dumps(style, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "styleFile": str(STYLE_FILE), "style": style}


def run_sync() -> Dict[str, Any]:
    if not SYNC_LOCK.acquire(blocking=False):
        raise RuntimeError("A Kakao DB job is already running")
    try:
        _ensure_dirs()
        result = _run_kwin("v2sync", timeout=300)
        return {"ok": True, "result": result, **list_rooms(limit=300)}
    finally:
        SYNC_LOCK.release()


def run_recover() -> Dict[str, Any]:
    if not SYNC_LOCK.acquire(blocking=False):
        raise RuntimeError("A Kakao DB job is already running")
    try:
        _ensure_dirs()
        result = _run_kwin("v2recover", timeout=600)
        return {"ok": True, "result": result, "status": api_status()}
    finally:
        SYNC_LOCK.release()


def _run_kwin(command: str, timeout: int) -> Dict[str, Any]:
    env = os.environ.copy()
    env["KAKAO_WIN_OUT"] = str(OUT_ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", "kwin", command],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    result = {
        "command": f"python -m kwin {command}",
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "ranAtKst": _now_kst(),
    }
    if proc.returncode != 0:
        raise RuntimeError((proc.stdout + "\n" + proc.stderr).strip()[-4000:])
    return result


def run_recover_sync() -> Dict[str, Any]:
    if not SYNC_LOCK.acquire(blocking=False):
        raise RuntimeError("A Kakao DB job is already running")
    try:
        _ensure_dirs()
        recover = _run_kwin("v2recover", timeout=600)
        sync = _run_kwin("v2sync", timeout=300)
        return {
            "ok": True,
            "result": {
                "recover": recover,
                "sync": sync,
                "ranAtKst": _now_kst(),
            },
            **list_rooms(limit=300),
        }
    finally:
        SYNC_LOCK.release()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        if length > 512 * 1024:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON object is required")
        return data

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/") if p]
        try:
            if parsed.path == "/api/health":
                self._json({
                    "ok": True,
                    "projectRoot": str(PROJECT_ROOT),
                    "dataDir": str(DATA_DIR),
                    "outRoot": str(OUT_ROOT),
                    "webDist": str(WEB_DIST),
                    "legacyFallback": USE_LEGACY_FALLBACK,
                })
                return
            if parsed.path == "/api/status":
                self._json(api_status())
                return
            if parsed.path == "/api/rooms":
                q = qs.get("q", [""])[0]
                limit = min(max(int(qs.get("limit", ["300"])[0]), 1), 1000)
                self._json(list_rooms(q, limit))
                return
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "rooms" and parts[3] == "messages":
                self._json(list_messages(parts[2], qs))
                return
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "rooms" and parts[3] == "members":
                self._json(list_members(parts[2], qs))
                return
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "rooms" and parts[3] == "export":
                filename, body, content_type = export_messages(parts[2], qs)
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/users":
                q = qs.get("q", [""])[0]
                limit = min(max(int(qs.get("limit", ["300"])[0]), 1), 1000)
                self._json(list_users(q, limit))
                return
            if parsed.path == "/api/jobs":
                self._json(load_jobs())
                return
            if parsed.path == "/api/style":
                self._json(load_style())
                return
            self._serve_static(parsed.path)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/jobs":
                self._json(save_job(self._body()))
                return
            if parsed.path == "/api/style":
                self._json(save_style(self._body()))
                return
            if parsed.path == "/api/recover":
                self._json(run_recover())
                return
            if parsed.path == "/api/sync":
                self._json(run_sync())
                return
            if parsed.path == "/api/recover-sync":
                self._json(run_recover_sync())
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/api/jobs":
                self._json(delete_job(qs.get("id", [""])[0]))
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)

    def _serve_static(self, path: str) -> None:
        if not WEB_DIST.exists():
            self._send(
                200,
                (
                    "React build not found. Run `cd web && npm.cmd install && npm.cmd run build`, "
                    "or use `npm.cmd run dev` during development."
                ).encode("utf-8"),
                "text/plain; charset=utf-8",
            )
            return
        rel = path.lstrip("/") or "index.html"
        target = (WEB_DIST / rel).resolve()
        if not str(target).startswith(str(WEB_DIST.resolve())) or not target.exists() or target.is_dir():
            target = WEB_DIST / "index.html"
        ctype = "text/html; charset=utf-8"
        if target.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        elif target.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif target.suffix == ".svg":
            ctype = "image/svg+xml"
        self._send(200, target.read_bytes(), ctype)

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    args = parser.parse_args(argv)
    _ensure_dirs()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("kakao-cli-windows backend")
    print("  url :", f"http://{args.host}:{args.port}/")
    print("  data:", DATA_DIR)
    print("  stop: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping backend.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
