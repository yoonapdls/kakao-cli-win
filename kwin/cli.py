"""kwin CLI — status / probe / brute / decrypt / collect.

    python -m kwin status
    python -m kwin probe            # resolve key+userId against the SQLite oracle
    python -m kwin brute            # brute-force userId (needs hardcoded key)
    python -m kwin decrypt          # decrypt all .edb of the active user dir
    python -m kwin collect          # merge decrypted DBs into one sqlite
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import struct
import sys
import time
from datetime import datetime, timezone, timedelta

from . import config as cfgmod
from . import appstate, collect, decryptor, deviceinfo
from .keyderiv import generate_key_iv, check_oracle, pragma_candidates, solve, Solution

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ROOT = os.environ.get(
    "KAKAO_WIN_OUT",
    os.path.join(PROJECT_ROOT, "data", "output"),
)
V2_KEYS = os.path.join(OUT_ROOT, "v2_keys.json")
V2_KEYS_LEGACY = os.path.join(PROJECT_ROOT, "v2_keys.json")
KST = timezone(timedelta(hours=9), "KST")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _kst(ts) -> str:
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=KST).strftime("%Y-%m-%d %H:%M:%S KST")
    except (TypeError, ValueError, OSError):
        return str(ts)


def _active_user_dir(args) -> str:
    if getattr(args, "user_dir", None):
        return args.user_dir
    dirs = deviceinfo.list_user_dirs()
    if not dirs:
        sys.exit("No KakaoTalk user data directory found.")
    return dirs[0]


def _first_edb(user_dir: str) -> str:
    cds = os.path.join(user_dir, "chat_data")
    edbs = sorted(glob.glob(os.path.join(cds, "chatLogs_*.edb")), key=os.path.getsize, reverse=True)
    if not edbs:
        sys.exit(f"No chatLogs_*.edb under {cds}")
    return edbs[0]


def _scheme_hint(user_dir: str, cfg) -> str:
    cds = os.path.join(user_dir, "chat_data")
    edbs = sorted(
        glob.glob(os.path.join(cds, "chatLogs_*.edb")),
        key=os.path.getsize,
        reverse=True,
    )
    if not edbs:
        return "no chatLogs_*.edb"

    sampled = edbs[: min(12, len(edbs))]
    first_blocks = []
    for path in sampled:
        try:
            with open(path, "rb") as f:
                first_blocks.append(f.read(16))
        except OSError:
            pass
    uniq = {b for b in first_blocks if b}
    if not uniq:
        return "could not read first blocks"

    if len(uniq) == 1:
        hint = "old/common first block"
    else:
        hint = "per-file first block (likely current/new scheme)"

    check = ""
    if cfg.has_derived:
        try:
            ok = check_oracle(cfg.derived_key, cfg.derived_iv, decryptor.first_page(edbs[0]))
            check = "; configured derived key: " + ("OK" if ok else "FAIL for this folder")
        except OSError:
            check = "; configured derived key: not checked"

    return f"{hint}; sampled={len(sampled)} unique_first_blocks={len(uniq)}{check}"


def cmd_status(args):
    di = deviceinfo.read_deviceinfo()
    cfg = cfgmod.load(args.config)
    dirs = deviceinfo.list_user_dirs()
    print("== Device ==")
    print("  sys_uuid   :", di.sys_uuid)
    print("  hdd_model  :", di.hdd_model)
    print("  hdd_serial :", di.hdd_serial)
    print("  pragma src :", di.pragma_source)
    print("  accounts   :", ", ".join(di.accounts) or "(none)")
    print("  phone      :", di.phone or "(none)")
    print("== User dirs ==")
    for d in dirs:
        cd = os.path.join(d, "chat_data")
        n = len(glob.glob(os.path.join(cd, "chatLogs_*.edb"))) if os.path.isdir(cd) else 0
        print(f"  {os.path.basename(d)}  ({n} chatLogs edb)")
    if dirs or getattr(args, "user_dir", None):
        selected = _active_user_dir(args)
        print("== Selected DB ==")
        print("  path       :", selected)
        print("  scheme     :", _scheme_hint(selected, cfg))
    print("== Config ==")
    print("  hardcoded_key:", "SET" if cfg.has_key else "MISSING (required)")
    print("  user_id      :", cfg.user_id or "(unset; will brute-force)")
    print("  derived_key  :", "SET" if cfg.has_derived else "MISSING")


def cmd_appstate(args):
    user_dir = _active_user_dir(args)
    path = appstate.find_for_user_dir(user_dir)
    if not path:
        sys.exit(f"No appstate.dat under {user_dir}")
    state = appstate.parse_file(path)
    print("== AppState v2 envelope ==")
    print("  path        :", state.path)
    print("  info_prefix :", state.info_prefix.decode("utf-8", "replace")
          or state.info_prefix.hex())
    print("  salt        :", state.salt.hex(), f"({len(state.salt)} bytes)")
    print("  wrapped DEKs:", len(state.wrapped_dek_map))
    print("== Samples ==")
    for name, wrapped in appstate.sample_entries(state, args.limit):
        print(f"  {name}  wrapped_len={len(wrapped)}  {wrapped.hex()}")


def cmd_v2scan(args):
    """Try to find a SQLCipher raw DEK for the current/v2 DB in memory."""
    from . import memkey, sqlcipher_v4

    user_dir = _active_user_dir(args)
    state_path = appstate.find_for_user_dir(user_dir)
    if not state_path:
        sys.exit(f"No appstate.dat under {user_dir}")
    state = appstate.parse_file(state_path)
    chat_dir = os.path.join(user_dir, "chat_data")
    edbs = sorted(
        glob.glob(os.path.join(chat_dir, "chatLogs_*.edb")),
        key=os.path.getmtime,
        reverse=True,
    )
    if not edbs:
        sys.exit(f"No chatLogs_*.edb under {chat_dir}")
    target = edbs[min(args.index, len(edbs) - 1)]
    rel = "chat_data\\" + os.path.basename(target)
    wrapped = state.wrapped_for(rel)
    if not wrapped:
        sys.exit(f"No wrapped DEK for {rel} in appstate.dat")
    first_page = decryptor.first_page(target)
    pids = memkey.find_pids()
    if not pids:
        sys.exit("KakaoTalk.exe is not running.")

    needles = [
        state.salt,
        first_page[:16],
        wrapped,
        sqlcipher_v4.SQLITE_MAGIC,
        b"CREATE TABLE chatLogs",
        b"chatLogs",
        rel.encode("utf-8", "ignore"),
        rel.encode("utf-16-le", "ignore"),
        target.encode("utf-8", "ignore"),
        target.encode("utf-16-le", "ignore"),
        os.path.basename(target).encode("utf-8", "ignore"),
        os.path.basename(target).encode("utf-16-le", "ignore"),
    ]
    needles = [n for n in needles if n]
    radius = args.radius_kb << 10
    max_region = args.max_region_mb << 20
    print("== V2 SQLCipher raw-key scan ==", flush=True)
    print("  target :", target, flush=True)
    print("  rel    :", rel, flush=True)
    print("  wrapped:", wrapped.hex(), f"({len(wrapped)} bytes)", flush=True)
    print("  pids   :", pids, flush=True)
    print(f"  radius : {args.radius_kb} KB, step={args.step}", flush=True)

    def candidate_offsets(data: bytes, base: int):
        ranges = []
        for needle in needles:
            pos = 0
            while True:
                pos = data.find(needle, pos)
                if pos < 0:
                    break
                ranges.append((max(0, pos - radius),
                               min(len(data), pos + len(needle) + radius)))
                pos += max(1, len(needle))
        if not ranges:
            return []
        ranges.sort()
        merged = []
        for lo, hi in ranges:
            if not merged or lo > merged[-1][1]:
                merged.append([lo, hi])
            elif hi > merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
        return [(base + lo, lo, hi) for lo, hi in merged]

    checked = 0
    for pid in pids:
        h = memkey._open(pid)
        try:
            for base, size, _prot in memkey.iter_regions(h):
                if size <= 0 or size > max_region:
                    continue
                data = memkey.read_region(h, base, size)
                ranges = candidate_offsets(data, base)
                if not ranges:
                    continue
                for addr, lo, hi in ranges:
                    chunk = data[lo:hi]
                    print(f"  window {hex(addr)} size={len(chunk):,}", flush=True)
                    limit = len(chunk) - 32
                    for off in range(0, max(0, limit + 1), args.step):
                        key = chunk[off:off + 32]
                        if key == b"\x00" * 32 or key == key[:1] * 32:
                            continue
                        checked += 1
                        hit = sqlcipher_v4.try_raw_key(first_page, key)
                        if hit:
                            print("FOUND V2 RAW KEY", flush=True)
                            print("  pid    :", pid, flush=True)
                            print("  address:", hex(addr + off), flush=True)
                            print("  key    :", key.hex(), flush=True)
                            print("  variant:", hit.variant, flush=True)
                            print("  checked:", checked, flush=True)
                            return
                    print(f"    checked so far: {checked:,}", flush=True)
        finally:
            memkey.k32.CloseHandle(h)
    sys.exit(f"No SQLCipher raw key found near v2 markers. checked={checked:,}")


def cmd_v2status(args):
    """Summarize what is known/unknown for the current SQLCipher/v2 DB."""
    user_dir = _active_user_dir(args)
    cfg = cfgmod.load(args.config)
    state_path = appstate.find_for_user_dir(user_dir)
    chat_dir = os.path.join(user_dir, "chat_data")
    edbs = sorted(
        glob.glob(os.path.join(chat_dir, "chatLogs_*.edb")),
        key=os.path.getmtime,
        reverse=True,
    )
    print("== KakaoTalk current/v2 status ==")
    print("  user_dir :", user_dir)
    print("  user_id  :", cfg.user_id or "(unknown)")
    print("  old key  :", "SET" if cfg.has_derived else "MISSING")
    print("  chatLogs :", len(edbs))
    print("  appstate :", state_path or "(missing)")

    if not state_path:
        print("  verdict  : no v2 appstate.dat found")
        return
    state = appstate.parse_file(state_path)
    wrapped_chatlogs = sum(
        1 for k in state.wrapped_dek_map if k.startswith("chat_data\\chatLogs_")
    )
    print("  prefix   :", state.info_prefix.decode("utf-8", "replace")
          or state.info_prefix.hex())
    print("  salt     :", state.salt.hex(), f"({len(state.salt)} bytes)")
    print("  wrapped  :", len(state.wrapped_dek_map),
          f"entries; chatLogs={wrapped_chatlogs}")

    missing = []
    for path in edbs:
        rel = "chat_data\\" + os.path.basename(path)
        if rel not in state.wrapped_dek_map:
            missing.append(rel)
    print("  map check:", "OK" if not missing else f"missing {len(missing)} chatLogs")

    if edbs:
        first = edbs[0]
        first_page = decryptor.first_page(first)
        print("  sample   :", os.path.basename(first))
        print("  db salt  :", first_page[:16].hex())
        print("  sqlite?  :", first_page[:16] == b"SQLite format 3\x00")
        if cfg.has_derived:
            print("  old-key check:",
                  "OK" if check_oracle(cfg.derived_key, cfg.derived_iv, first_page)
                  else "FAIL")

    print("== Needed for latest/current DB ==")
    print("  required : SQLCipher raw key for each DB, or the KEK/master key")
    print("             that unwraps appstate.dat wrapped_dek_map entries.")
    print("  note     : USER_ID is already known for the old scheme; current v2")
    print("             is blocked on SQLCipher key/KEK, not on USER_ID.")


def _v2_target_pages(user_dir: str, chatlogs_only: bool = False):
    """Return (rel, path, first_page) for v2 SQLCipher DB candidates.

    The first implementation only tested chatLogs_*.edb.  For room/user name
    mapping we also need metadata DBs such as chatListInfo.edb, TalkUserDB.edb,
    and talk_user_prf.edb, so use appstate.dat's wrapped_dek_map as the source
    of truth when it is available.
    """
    targets = []
    seen = set()

    def add(rel: str):
        rel = rel.replace("/", "\\")
        if rel in seen or not rel.lower().endswith(".edb"):
            return
        if chatlogs_only and "\\chatlogs_" not in rel.lower():
            return
        path = os.path.join(user_dir, rel)
        if not os.path.exists(path):
            return
        try:
            first = decryptor.first_page(path)
        except OSError:
            return
        seen.add(rel)
        targets.append((rel, path, first))

    state_path = appstate.find_for_user_dir(user_dir)
    if state_path:
        try:
            state = appstate.parse_file(state_path)
            for rel in state.wrapped_dek_map:
                add(rel)
        except Exception:
            pass

    # Fallback and also catches unwrapped legacy-ish files that appstate may not
    # list.  Keep chatLogs first by recent mtime for useful progress output.
    for path in sorted(
        glob.glob(os.path.join(user_dir, "chat_data", "chatLogs_*.edb")),
        key=os.path.getmtime,
        reverse=True,
    ):
        add("chat_data\\" + os.path.basename(path))
    for path in glob.glob(os.path.join(user_dir, "**", "*.edb"), recursive=True):
        rel = os.path.relpath(path, user_dir).replace("/", "\\")
        add(rel)

    def priority(item):
        rel = item[0].lower()
        if "\\chatlogs_" in rel:
            return (0, rel)
        if "chatlistinfo" in rel:
            return (1, rel)
        if "talkuserdb" in rel or "talk_user" in rel or "\\contacts\\" in rel:
            return (2, rel)
        return (3, rel)

    targets.sort(key=priority)
    return targets


def _v2_chatlog_pages(user_dir: str):
    return [(path, first) for _rel, path, first in _v2_target_pages(
        user_dir, chatlogs_only=True
    )]


def _read_process_regions(pid: int, max_region_mb: int = 128):
    from . import memkey
    regions = []
    h = memkey._open(pid)
    try:
        for base, size, _prot in memkey.iter_regions(h):
            if size <= 0 or size > (max_region_mb << 20):
                continue
            regions.append((base, memkey.read_region(h, base, size)))
    finally:
        memkey.k32.CloseHandle(h)
    return regions


def _read_from_regions(regions, addr: int, n: int):
    for base, data in regions:
        if base <= addr and addr + n <= base + len(data):
            return data[addr - base:addr - base + n]
    return None


def _codec_contexts_from_regions(regions):
    # Observed SQLCipher codec_ctx settings in current KakaoTalk:
    # store_pass, kdf_iter=256000, fast=2, salt=16, key=32, iv=16,
    # block=16, page=4096, plaintext-header-ish=0x63, reserve=80,
    # hmac=64, hmac_alg=0, kdf_alg=2, flags=0x1b, then pointers.
    pat = struct.pack("<IIIIIIII", 256000, 2, 16, 32, 16, 16, 4096, 0x63)
    out = []
    seen = set()
    for base, data in regions:
        pos = 0
        while True:
            pos = data.find(pat, pos)
            if pos < 0:
                break
            ctx_addr = base + pos - 4
            raw = _read_from_regions(regions, ctx_addr, 92)
            if raw:
                vals = struct.unpack("<" + "I" * 23, raw)
                if vals[7] == 4096 and vals[9] == 80 and vals[10] == 64 and vals[14] & 1:
                    if ctx_addr not in seen:
                        seen.add(ctx_addr)
                        out.append((ctx_addr, vals))
            pos += 4
    return out


def _candidate_v2_keys_from_codecs(regions, codecs):
    keys = []
    seen = set()
    for ctx_addr, vals in codecs:
        for label, caddr in (("read", vals[19]), ("write", vals[20])):
            craw = _read_from_regions(regions, caddr, 96)
            if not craw:
                continue
            dws = struct.unpack("<" + "I" * 24, craw[:96])
            # In the observed SQLCipher cipher_ctx layout, fields 2 and 3 point
            # to the 32-byte cipher key and 32-byte HMAC key respectively.  Only
            # the cipher key passes the page oracle.
            for field in (2, 3):
                ptr = dws[field]
                key = _read_from_regions(regions, ptr, 32)
                if not key or key == b"\x00" * 32:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                keys.append({
                    "codec": ctx_addr,
                    "cipher_ctx": caddr,
                    "which": label,
                    "field": field,
                    "ptr": ptr,
                    "key": key,
                })
    return keys


def cmd_v2recover(args):
    """Recover SQLCipher raw keys currently loaded in KakaoTalk memory."""
    from . import memkey, sqlcipher_v4

    user_dir = _active_user_dir(args)
    targets = _v2_target_pages(user_dir, getattr(args, "chatlogs_only", False))
    pages = [(rel, first_page) for rel, _path, first_page in targets]
    if not pages:
        sys.exit("No current v2 .edb targets found.")
    pids = memkey.find_pids()
    if not pids:
        sys.exit("KakaoTalk.exe is not running.")

    out = args.output or V2_KEYS
    recovered = {}
    details = []
    existing_key_path = out
    if not os.path.exists(existing_key_path) and os.path.exists(V2_KEYS_LEGACY):
        existing_key_path = V2_KEYS_LEGACY
    if not getattr(args, "fresh", False) and os.path.exists(existing_key_path):
        try:
            with open(existing_key_path, "r", encoding="utf-8") as f:
                old = json.load(f)
            if old.get("user_dir") == user_dir:
                recovered.update(old.get("keys", {}))
                details.extend(old.get("details", []))
        except (OSError, json.JSONDecodeError):
            pass

    chatlog_count = sum(1 for rel, _first in pages if "\\chatlogs_" in rel.lower())
    meta_count = len(pages) - chatlog_count
    print("== V2 key recovery ==", flush=True)
    print("  targets :", len(pages),
          f"(chatLogs={chatlog_count}, metadata={meta_count})", flush=True)
    print("  existing:", len(recovered), "keys", flush=True)
    print("  pids    :", pids, flush=True)
    found_now = 0
    for pid in pids:
        print("  reading memory pid", pid, flush=True)
        regions = _read_process_regions(pid, args.max_region_mb)
        codecs = _codec_contexts_from_regions(regions)
        keys = _candidate_v2_keys_from_codecs(regions, codecs)
        print(f"  codec contexts: {len(codecs)}, candidate keys: {len(keys)}",
              flush=True)
        for item in keys:
            key = item["key"]
            for rel, first_page in pages:
                if rel in recovered:
                    continue
                hit = sqlcipher_v4.try_raw_key(first_page, key)
                if not hit:
                    continue
                recovered[rel] = key.hex()
                found_now += 1
                details.append({
                    "rel": rel,
                    "key": key.hex(),
                    "pid": pid,
                    "codec": hex(item["codec"]),
                    "cipher_ctx": hex(item["cipher_ctx"]),
                    "ptr": hex(item["ptr"]),
                    "which": item["which"],
                    "field": item["field"],
                    "variant": hit.variant,
                })
                shown = key.hex() if getattr(args, "show_keys", False) else key.hex()[:8] + "..."
                print("  FOUND", rel, shown, flush=True)

    data = {
        "user_dir": user_dir,
        "keys": recovered,
        "details": details,
    }
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("Recovered", len(recovered), "current/v2 DB keys",
          f"({found_now} new) ->", out)
    if not recovered:
        sys.exit("No v2 SQLCipher keys recovered. Open recent chat rooms and retry.")


def cmd_v2decrypt(args):
    """Decrypt current/v2 chatLogs using keys recovered by v2recover."""
    from . import sqlcipher_v4

    key_path = args.keys or V2_KEYS
    if not os.path.exists(key_path) and not args.keys and os.path.exists(V2_KEYS_LEGACY):
        key_path = V2_KEYS_LEGACY
    if not os.path.exists(key_path):
        sys.exit(f"No v2 key file at {key_path}. Run `python -m kwin v2recover`.")
    with open(key_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    user_dir = args.user_dir or data.get("user_dir") or _active_user_dir(args)
    keys = data.get("keys", {})
    out_dir = os.path.join(OUT_ROOT, os.path.basename(user_dir), "v2_decrypted")
    os.makedirs(out_dir, exist_ok=True)
    ok = fail = missing = 0
    for rel, key_hex in sorted(keys.items()):
        src = os.path.join(user_dir, rel)
        if not os.path.exists(src):
            missing += 1
            continue
        dst = os.path.join(out_dir, os.path.basename(src).replace(".edb", ".sqlite"))
        try:
            if sqlcipher_v4.decrypt_file(src, dst, bytes.fromhex(key_hex)):
                ok += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            print("  !", rel, e, file=sys.stderr)
    print(f"V2 decrypted {ok} OK, {fail} failed, {missing} missing -> {out_dir}")


def cmd_v2collect(args):
    """Merge decrypted current/v2 chatLogs into one messages_v2.sqlite."""
    user_dir = _active_user_dir(args)
    dec_dir = os.path.join(OUT_ROOT, os.path.basename(user_dir), "v2_decrypted")
    if not os.path.isdir(dec_dir):
        sys.exit(f"No v2 decrypted dir at {dec_dir}. Run `v2decrypt` first.")
    contacts = {}
    talk_user = os.path.join(dec_dir, "TalkUserDB.sqlite")
    if os.path.exists(talk_user):
        contacts = collect.load_contacts(talk_user)
    rooms = {}
    room_members = []
    chat_list = os.path.join(dec_dir, "chatListInfo.sqlite")
    if os.path.exists(chat_list):
        rooms = collect.load_rooms(chat_list, contacts)
        room_members = collect.load_room_members(chat_list)
    out_db = os.path.join(OUT_ROOT, os.path.basename(user_dir), "messages_v2.sqlite")
    n = collect.build(dec_dir, out_db, contacts)
    collect.write_metadata(out_db, contacts, rooms, room_members)
    print(
        f"Collected {n} v2 messages from {len(contacts)} contacts "
        f"{len(rooms)} rooms and {len(room_members)} room-members -> {out_db}"
    )


def cmd_v2recent(args):
    """Inspect the consolidated current/v2 message DB."""
    user_dir = _active_user_dir(args)
    db_path = args.db or os.path.join(
        OUT_ROOT, os.path.basename(user_dir), "messages_v2.sqlite"
    )
    if not os.path.exists(db_path):
        sys.exit(f"No messages_v2.sqlite at {db_path}. Run `v2all` first.")

    import sqlite3
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        total = cur.execute("SELECT count(*) FROM messages").fetchone()[0]
        stats = cur.execute(
            "SELECT min(sentAt), max(sentAt), count(distinct chatId) FROM messages"
        ).fetchone()
        print("== messages_v2.sqlite ==")
        print("  path    :", db_path)
        print("  messages:", total)
        print("  chats   :", stats[2])
        if stats[0] and stats[1]:
            print("  range   :", _kst(stats[0]), "->", _kst(stats[1]))
        print("== recent rows ==")
        rows = cur.execute(
            """SELECT chatId, logId, authorId, type, sentAt, sentAtIso,
                      length(coalesce(message, '')) AS message_len,
                      message
               FROM messages
               ORDER BY sentAt DESC, logId DESC
               LIMIT ?""",
            (args.limit,),
        ).fetchall()
        for row in rows:
            chat_id, log_id, author_id, typ, sent_at, sent_iso, msg_len, msg = row
            line = (
                f"  chat={chat_id} log={log_id} author={author_id} "
                f"type={typ} sentAt={sent_at} kst={_kst(sent_at)} len={msg_len}"
            )
            if args.text:
                preview = (msg or "").replace("\r", " ").replace("\n", " ")
                if len(preview) > args.preview:
                    preview = preview[:args.preview] + "..."
                line += f" text={preview!r}"
            print(line)
    finally:
        con.close()


def _messages_v2_path(args) -> str:
    user_dir = _active_user_dir(args)
    return getattr(args, "db", None) or os.path.join(
        OUT_ROOT, os.path.basename(user_dir), "messages_v2.sqlite"
    )


def _require_messages_v2(args) -> str:
    db_path = _messages_v2_path(args)
    if not os.path.exists(db_path):
        sys.exit(f"No messages_v2.sqlite at {db_path}. Run `v2all` first.")
    return db_path


def _has_table(cur, name: str) -> bool:
    return bool(cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone())


def _room_rows(cur, limit: int):
    has_rooms = _has_table(cur, "rooms")
    room_select = (
        "coalesce(room.title, 'chatId:' || r.chatId) AS title, "
        "coalesce(room.titleSource, 'fallback') AS titleSource, "
        "room.type AS roomType, room.activeMembersCount"
        if has_rooms else
        "'chatId:' || r.chatId AS title, 'fallback' AS titleSource, "
        "NULL AS roomType, NULL AS activeMembersCount"
    )
    room_join = "LEFT JOIN rooms room ON room.chatId = r.chatId" if has_rooms else ""
    return cur.execute(
        f"""WITH room_stats AS (
               SELECT chatId,
                      count(*) AS message_count,
                      sum(CASE WHEN message IS NOT NULL AND length(message) > 0
                               THEN 1 ELSE 0 END) AS text_count,
                      min(sentAt) AS first_sent,
                      max(sentAt) AS last_sent
                 FROM messages
                GROUP BY chatId
           ),
           last_rows AS (
               SELECT m.chatId, m.logId, m.authorId, m.type, m.sentAt,
                      m.sentAtIso, m.message
                 FROM messages m
                 JOIN room_stats r
                   ON r.chatId = m.chatId AND r.last_sent = m.sentAt
                WHERE m.logId = (
                      SELECT max(m2.logId)
                        FROM messages m2
                       WHERE m2.chatId = m.chatId
                         AND m2.sentAt = r.last_sent
                      )
           )
           SELECT r.chatId, {room_select},
                  r.message_count, r.text_count,
                  r.first_sent, r.last_sent,
                  l.logId, l.authorId, l.type, l.sentAtIso, l.message
             FROM room_stats r
             LEFT JOIN last_rows l ON l.chatId = r.chatId
             {room_join}
            ORDER BY r.last_sent DESC, r.message_count DESC
            LIMIT ?""",
        (limit,),
    ).fetchall()


def _preview(value, width: int) -> str:
    text = (value or "").replace("\r", " ").replace("\n", " ")
    if len(text) > width:
        return text[:width] + "..."
    return text


def _display_user(author_id, author_name) -> str:
    if author_name and str(author_name) != str(author_id):
        return f"{author_name}(id={author_id})"
    return f"id={author_id}"


def cmd_v2rooms(args):
    """List recovered current/v2 chat rooms from messages_v2.sqlite."""
    import sqlite3
    db_path = _require_messages_v2(args)
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        rows = _room_rows(cur, args.limit)
        total_rooms = cur.execute(
            "SELECT count(distinct chatId) FROM messages"
        ).fetchone()[0]
        total_messages = cur.execute("SELECT count(*) FROM messages").fetchone()[0]
        print("== recovered v2 chat rooms ==")
        print("  db      :", db_path)
        print("  rooms   :", total_rooms)
        print("  messages:", total_messages)
        has_room_names = bool(cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rooms'"
        ).fetchone())
        print("  metadata:", "rooms table OK" if has_room_names else "no rooms table; run v2collect")
        print()
        for idx, row in enumerate(rows, 1):
            (chat_id, title, title_source, room_type, members_count, count,
             text_count, first_sent, last_sent, log_id, author_id, typ,
             sent_iso, msg) = row
            line = (
                f"{idx:>3}. {title} "
                f"(chatId={chat_id}, roomType={room_type}, members={members_count}, "
                f"titleSource={title_source}) "
                f"messages={count} text={text_count} "
                f"first={_kst(first_sent)} last={_kst(last_sent)} "
                f"lastLog={log_id} authorId={author_id} msgType={typ}"
            )
            if args.text:
                line += f" preview={_preview(msg, args.preview)!r}"
            else:
                line += f" lastLen={len(msg or '')}"
            print(line)
    finally:
        con.close()


def _resolve_chat_selector(cur, selector: str, list_limit: int) -> int:
    if selector.isdigit():
        value = int(selector)
        rows = _room_rows(cur, max(list_limit, value))
        if 1 <= value <= len(rows):
            return int(rows[value - 1][0])
        exists = cur.execute(
            "SELECT 1 FROM messages WHERE chatId = ? LIMIT 1",
            (value,),
        ).fetchone()
        if exists:
            return value
    sys.exit(
        f"Unknown chat selector {selector!r}. Use `v2rooms` and pass either "
        "the list number or the full chatId."
    )


def cmd_v2chat(args):
    """Print recent messages for one recovered current/v2 chat."""
    import sqlite3

    db_path = _require_messages_v2(args)
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        chat_id = _resolve_chat_selector(cur, args.selector, args.list_limit)
        total = cur.execute(
            "SELECT count(*) FROM messages WHERE chatId = ?",
            (chat_id,),
        ).fetchone()[0]
        room_title = None
        if _has_table(cur, "rooms"):
            room = cur.execute(
                "SELECT title, titleSource, type, activeMembersCount FROM rooms WHERE chatId = ?",
                (chat_id,),
            ).fetchone()
            if room:
                room_title = room
        print("== v2 chat messages ==")
        print("  db      :", db_path)
        print("  chatId  :", chat_id)
        if room_title:
            title, source, room_type, members = room_title
            print("  title   :", title)
            print("  room    :", f"type={room_type}, members={members}, titleSource={source}")
        print("  total   :", total)
        print("  showing :", args.limit)
        print()
        rows = cur.execute(
            """SELECT logId, authorId, authorName, type, sentAt, sentAtIso, message
                 FROM messages
                WHERE chatId = ?
                ORDER BY sentAt DESC, logId DESC
                LIMIT ?""",
            (chat_id, args.limit),
        ).fetchall()
        if args.chrono:
            rows = list(reversed(rows))
        for row in rows:
            log_id, author_id, author_name, typ, sent_at, sent_iso, msg = row
            line = (
                f"[{_kst(sent_at)}] log={log_id} "
                f"author={_display_user(author_id, author_name)} "
                f"type={typ}"
            )
            if not args.no_text:
                line += f" | {_preview(msg, args.preview)}"
            else:
                line += f" len={len(msg or '')}"
            print(line)
    finally:
        con.close()


def cmd_v2members(args):
    """List members for one recovered room using chatListInfo + TalkUserDB metadata."""
    import sqlite3

    db_path = _require_messages_v2(args)
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        chat_id = _resolve_chat_selector(cur, args.selector, args.list_limit)
        if not _has_table(cur, "room_members"):
            sys.exit("No room_members table. Run `python -m kwin v2collect` after metadata decrypt.")
        room_title = None
        if _has_table(cur, "rooms"):
            room = cur.execute(
                "SELECT title, type, activeMembersCount FROM rooms WHERE chatId = ?",
                (chat_id,),
            ).fetchone()
            if room:
                room_title = room
        print("== v2 room members ==")
        print("  chatId :", chat_id)
        if room_title:
            title, room_type, members = room_title
            print("  title  :", title)
            print("  room   :", f"type={room_type}, members={members}")
        print()
        rows = cur.execute(
            """SELECT rm.userId, coalesce(c.name, rm.userId), rm.isActive,
                      rm.watermark
                 FROM room_members rm
                 LEFT JOIN contacts c ON c.userId = rm.userId
                WHERE rm.chatId = ?
                ORDER BY rm.isActive DESC, c.name IS NULL, c.name, rm.userId
                LIMIT ?""",
            (chat_id, args.limit),
        ).fetchall()
        for idx, (user_id, name, is_active, watermark) in enumerate(rows, 1):
            print(
                f"{idx:>4}. {_display_user(user_id, name)} "
                f"active={is_active} watermark={watermark}"
            )
    finally:
        con.close()


def cmd_v2all(args):
    """Recover, decrypt, and collect current/v2 chatLogs in one pass."""
    cmd_v2recover(args)
    if getattr(args, "keys", None) is None:
        args.keys = getattr(args, "output", None)
    cmd_v2decrypt(args)
    cmd_v2collect(args)


def cmd_v2sync(args):
    """Refresh current/v2 output using already recovered keys."""
    cmd_v2decrypt(args)
    cmd_v2collect(args)


def _resolve(args, cfg) -> Solution:
    if cfg.has_derived:
        return Solution("from-memory", "", cfg.user_id or "", cfg.derived_key, cfg.derived_iv)
    if not cfg.has_key:
        sys.exit("hardcoded_key is not configured. Set it in config.json or "
                 "KAKAO_HARDCODED_KEY. See README (§ obtaining the key).")
    di = deviceinfo.read_deviceinfo()
    edb = _first_edb(_active_user_dir(args))
    fp = decryptor.first_page(edb)
    uids = [cfg.user_id] if cfg.user_id else _brute_ids(cfg.brute_min, cfg.brute_max)
    sol = solve(di.pragma_source, cfg.hardcoded_key, fp, uids)
    if not sol:
        sys.exit("No (pragma,userId) satisfied the SQLite oracle. Key wrong, "
                 "userId outside range, or files use the post-2025-08 scheme.")
    return sol


def _brute_ids(lo, hi):
    for i in range(lo, hi):
        # The paper uses String(i), not a fixed-width zero-padded representation.
        yield str(i)


def cmd_probe(args):
    sol = _resolve(args, cfgmod.load(args.config))
    print("SOLVED")
    print("  pragma variant:", sol.pragma_label)
    print("  user_id       :", sol.user_id)
    print("  key (hex)     :", sol.key.hex())
    print("  iv  (hex)     :", sol.iv.hex())
    print("\nAdd this to config.json to skip brute-force next time:")
    print(json.dumps({"user_id": sol.user_id}, ensure_ascii=False))


def cmd_brute(args):
    """Optimized userId brute-force against the oracle (hardcoded key required)."""
    cfg = cfgmod.load(args.config)
    if not cfg.has_key:
        sys.exit("hardcoded_key required for brute-force.")
    di = deviceinfo.read_deviceinfo()
    user_dir = _active_user_dir(args)
    edb = _first_edb(user_dir)
    fp = decryptor.first_page(edb)
    cands = pragma_candidates(di.pragma_source, cfg.hardcoded_key)  # computed once
    lo, hi = cfg.brute_min, cfg.brute_max
    t0 = time.time()
    for n, i in enumerate(range(lo, hi)):
        uid = str(i)
        for label, pragma in cands:
            key, iv = generate_key_iv(pragma, uid)
            if check_oracle(key, iv, fp):
                print(f"\nFOUND user_id={uid} variant={label} key={key.hex()} iv={iv.hex()}")
                return
        if n and n % 200000 == 0:
            rate = n / (time.time() - t0)
            print(f"  ...{i:,} ({rate:,.0f}/s)", file=sys.stderr)
    sys.exit("Exhausted range without a match.")


def cmd_extractkey(args):
    """Scan the running KakaoTalk process memory for the derived AES key/iv."""
    from . import memkey
    cfg = cfgmod.load(args.config)
    user_dir = _active_user_dir(args)
    edb = _first_edb(user_dir)
    fp = decryptor.first_page(edb)
    pids = memkey.find_pids()
    if not pids:
        sys.exit("KakaoTalk.exe is not running. Launch it and open a 2023 chat, then retry.")
    step = getattr(args, "step", 8)
    near = getattr(args, "near", False)
    radius_mb = getattr(args, "radius_mb", 1)
    max_region_mb = getattr(args, "max_region_mb", 128)
    if near:
        print(
            f"NEAR scan: pids={pids} step={step} radius={radius_mb}MB "
            f"max_region={max_region_mb}MB",
            flush=True,
        )
    else:
        print(f"FULL scan: pids={pids} step={step}", flush=True)
    print(f"Scanning pids {pids} (step={step}) — this is a one-time cost...", file=sys.stderr)

    def prog(done, rate, windows=None):
        if windows is None:
            print(f"  ...{done>>20} MB ({rate:.1f} MB/s)", flush=True)
        else:
            print(f"  ...{done>>20} MB near {windows} windows ({rate:.1f} MB/s)",
                  flush=True)

    needles = [
        user_dir,
        os.path.basename(user_dir).split("_")[0],
        os.path.dirname(user_dir),
        os.path.join(user_dir, "chat_data"),
        edb,
        os.path.basename(edb),
        "chatLogs_",
        "TalkUserDB.edb",
    ]

    for pid in pids:
        if near:
            mk = memkey.scan_near(
                pid, fp, needles, key_sizes=(16,), step=step,
                radius=radius_mb << 20,
                max_region=max_region_mb << 20,
                progress=prog,
            )
        else:
            mk = memkey.scan(pid, fp, key_sizes=(16,), step=step, progress=prog)
        if mk:
            p = cfgmod.save_derived(cfg.path, mk.key, mk.iv)
            print("FOUND derived key in memory")
            print("  key:", mk.key.hex())
            print("  iv :", mk.iv.hex())
            print("  at : pid", pid, "addr", hex(mk.address))
            print("  saved to", p, "-> now run: python -m kwin decrypt")
            return
    sys.exit("No key found. The running KakaoTalk may be logged into a different "
             "account or using the post-2025-08 scheme. Try step=1, or open the "
             "2023 chat rooms so KakaoTalk loads their key into memory.")


def cmd_recover(args):
    """Recover pragma+userId from targeted live-memory artifacts."""
    from . import memkey, recovery

    di = deviceinfo.read_deviceinfo()
    dirs = deviceinfo.list_user_dirs()
    if not dirs:
        sys.exit("No KakaoTalk user directories found.")
    users_home = os.path.dirname(dirs[0])
    targets = [os.path.basename(d) for d in dirs
               if len(os.path.basename(d)) == 40]
    fp = decryptor.first_page(_first_edb(_active_user_dir(args)))
    report_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "memory_inspection.txt",
    )
    if getattr(args, "report_only", False):
        if not os.path.exists(report_path):
            sys.exit(f"No existing report at {report_path}")
        numbers, pragmas = recovery.read_report_candidates(report_path)
    else:
        pids = memkey.find_pids()
        if not pids:
            sys.exit("KakaoTalk.exe is not running. Log in, open the main "
                     "window, then retry.")
        needles = [
            "userId", "userid", "user_id", "memberId", "accountId",
            *di.accounts, di.phone or "", *targets,
        ]
        print(f"Scanning KakaoTalk pids {pids} for account artifacts...",
              file=sys.stderr)
        hits, numbers, pragmas = recovery.scan_processes(pids, needles)
        recovery.report(report_path, hits, numbers, pragmas)
    print(f"Candidates: numeric={len(numbers)}, pragma={len(pragmas)}")
    print("Report:", report_path)

    recovered = recovery.verify_pairs(
        pragmas, numbers, users_home, targets, fp
    )
    if not recovered:
        sys.exit(
            "No candidate pair decrypted the SQLite header. Open several "
            "chat rooms and retry. The report contains the non-secret "
            "diagnostic contexts."
        )
    saved = cfgmod.save_derived(
        args.config, recovered.key, recovered.iv,
        user_id=recovered.user_id, pragma=recovered.pragma,
    )
    print("RECOVERED")
    print("  user_id  :", recovered.user_id)
    print("  directory:", recovered.directory)
    print("  dir match:", recovered.directory_match)
    print("  key      :", recovered.key.hex())
    print("  iv       :", recovered.iv.hex())
    print("  saved    :", saved)
    print("Next: python -m kwin decrypt")


def cmd_decrypt(args):
    cfg = cfgmod.load(args.config)
    sol = _resolve(args, cfg)
    user_dir = _active_user_dir(args)
    cds = os.path.join(user_dir, "chat_data")
    out_dir = os.path.join(OUT_ROOT, os.path.basename(user_dir), "decrypted")
    os.makedirs(out_dir, exist_ok=True)
    targets = sorted(glob.glob(os.path.join(cds, "chatLogs_*.edb")))
    targets += [os.path.join(user_dir, "TalkUserDB.edb")]
    ok = fail = 0
    for src in targets:
        if not os.path.exists(src):
            continue
        name = os.path.basename(src).replace(".edb", ".sqlite")
        dst = os.path.join(out_dir, name)
        try:
            if decryptor.decrypt_file(src, sol.key, sol.iv, dst):
                ok += 1
            else:
                fail += 1
        except Exception as e:  # noqa
            fail += 1
            print(f"  ! {name}: {e}", file=sys.stderr)
    print(f"Decrypted {ok} OK, {fail} not-SQLite -> {out_dir}")


def cmd_collect(args):
    user_dir = _active_user_dir(args)
    dec_dir = os.path.join(OUT_ROOT, os.path.basename(user_dir), "decrypted")
    if not os.path.isdir(dec_dir):
        sys.exit(f"No decrypted dir at {dec_dir}. Run `decrypt` first.")
    contacts = collect.load_contacts(os.path.join(dec_dir, "TalkUserDB.sqlite"))
    out_db = os.path.join(OUT_ROOT, os.path.basename(user_dir), "messages.sqlite")
    n = collect.build(dec_dir, out_db, contacts)
    print(f"Collected {n} messages from {len(contacts)} contacts -> {out_db}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="kwin", description="KakaoTalk Windows local decryptor")
    p.add_argument("--config", default=None)
    p.add_argument("--user-dir", default=None, help="override active user data dir")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in [("status", cmd_status), ("appstate", cmd_appstate),
                     ("v2status", cmd_v2status),
                     ("v2scan", cmd_v2scan),
                     ("v2recover", cmd_v2recover),
                     ("v2decrypt", cmd_v2decrypt),
                     ("v2collect", cmd_v2collect),
                     ("v2recent", cmd_v2recent),
                     ("v2rooms", cmd_v2rooms),
                     ("v2chat", cmd_v2chat),
                     ("v2members", cmd_v2members),
                     ("v2all", cmd_v2all),
                     ("v2sync", cmd_v2sync),
                     ("probe", cmd_probe), ("brute", cmd_brute),
                     ("extractkey", cmd_extractkey),
                     ("recover", cmd_recover),
                     ("decrypt", cmd_decrypt), ("collect", cmd_collect)]:
        sp = sub.add_parser(name)
        sp.set_defaults(func=fn)
        if name == "appstate":
            sp.add_argument("--limit", type=int, default=10)
        if name == "v2scan":
            sp.add_argument("--index", type=int, default=0,
                            help="which recent chatLogs file to target")
            sp.add_argument("--radius-kb", type=int, default=64)
            sp.add_argument("--max-region-mb", type=int, default=128)
            sp.add_argument("--step", type=int, default=4)
        if name == "v2recover":
            sp.add_argument("--max-region-mb", type=int, default=128)
            sp.add_argument("--output", default=None)
            sp.add_argument("--fresh", action="store_true",
                            help="do not merge with existing v2_keys.json")
            sp.add_argument("--chatlogs-only", action="store_true",
                            help="only recover chatLogs_*.edb keys")
            sp.add_argument("--show-keys", action="store_true",
                            help="print full raw keys to console")
        if name == "v2decrypt":
            sp.add_argument("--keys", default=None)
        if name == "v2recent":
            sp.add_argument("--db", default=None)
            sp.add_argument("--limit", type=int, default=10)
            sp.add_argument("--text", action="store_true")
            sp.add_argument("--preview", type=int, default=80)
        if name == "v2rooms":
            sp.add_argument("--db", default=None)
            sp.add_argument("--limit", type=int, default=50)
            sp.add_argument("--text", action="store_true")
            sp.add_argument("--preview", type=int, default=80)
        if name == "v2chat":
            sp.add_argument("selector", help="room list number from v2rooms, or chatId")
            sp.add_argument("--db", default=None)
            sp.add_argument("--limit", type=int, default=100)
            sp.add_argument("--list-limit", type=int, default=200)
            sp.add_argument("--preview", type=int, default=500)
            sp.add_argument("--no-text", action="store_true")
            sp.add_argument(
                "--chrono", action="store_true",
                help="print oldest-to-newest within the selected recent window",
            )
        if name == "v2members":
            sp.add_argument("selector", help="room list number from v2rooms, or chatId")
            sp.add_argument("--db", default=None)
            sp.add_argument("--limit", type=int, default=200)
            sp.add_argument("--list-limit", type=int, default=200)
        if name == "v2all":
            sp.add_argument("--max-region-mb", type=int, default=128)
            sp.add_argument("--output", default=None)
            sp.add_argument("--keys", default=None)
            sp.add_argument("--fresh", action="store_true",
                            help="do not merge with existing v2_keys.json")
            sp.add_argument("--chatlogs-only", action="store_true",
                            help="only recover chatLogs_*.edb keys")
            sp.add_argument("--show-keys", action="store_true",
                            help="print full raw keys to console")
        if name == "v2sync":
            sp.add_argument("--keys", default=None)
        if name == "extractkey":
            sp.add_argument("--step", type=int, default=8)
            sp.add_argument(
                "--near", action="store_true",
                help="scan only near DB/path markers first",
            )
            sp.add_argument(
                "--radius-mb", type=int, default=1,
                help="window size around each marker when --near is used",
            )
            sp.add_argument(
                "--max-region-mb", type=int, default=128,
                help="skip larger memory regions when --near is used",
            )
        if name == "recover":
            sp.add_argument(
                "--report-only", action="store_true",
                help="reuse memory_inspection.txt without rescanning memory",
            )
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
