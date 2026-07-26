"""Find KakaoTalk's old hardcoded PRAGMA AES key from local binaries.

This targets the pre-2025 EDB scheme used by the 2021/2023 backup files whose
first encrypted SQLite block is identical across chatLogs.

Usage:
  python findkey_from_exe.py <USER_ID> --step 1
  python findkey_from_exe.py <USER_ID> --step 1 --full

If this succeeds it saves the derived DB key/iv to config.json.
"""
from __future__ import annotations

import argparse
import base64
import glob
import multiprocessing as mp
import os
import re
import sys
from typing import Iterable, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kwin import config as cfgmod
from kwin.keyderiv import (
    check_oracle,
    generate_key_iv,
    pragma_candidates,
    pragma_fig3,
)
from kwin.deviceinfo import list_user_dirs, read_deviceinfo


ROOT = os.path.dirname(os.path.abspath(__file__))
_USER_DIRS = list_user_dirs()
DEFAULT_BACKUP = (
    os.path.join(_USER_DIRS[0], "chat_data")
    if _USER_DIRS
    else os.path.join(ROOT, "data", "backup")
)
DEFAULT_PROGRAM_DIR = r"C:\Program Files (x86)\Kakao\KakaoTalk"
PRAGMA_SRC = read_deviceinfo().pragma_source


def normalize_uid(value: str) -> str:
    value = value.strip()
    if not value.isdigit():
        raise ValueError("userId must be decimal digits")
    return str(int(value))


def get_userid(args) -> str:
    if args.user_id:
        return normalize_uid(args.user_id)
    p = os.path.join(ROOT, "userid_result.txt")
    if os.path.exists(p):
        text = open(p, encoding="utf-8", errors="ignore").read()
        m = re.search(r"userId=(\d+)", text)
        if m:
            return normalize_uid(m.group(1))
    sys.exit("No userId. Pass it as the first argument.")


def first_page(chat_data: str) -> Tuple[bytes, str]:
    edbs = sorted(
        glob.glob(os.path.join(chat_data, "chatLogs_*.edb")),
        key=os.path.getsize,
        reverse=True,
    )
    if not edbs:
        sys.exit(f"no chatLogs_*.edb in {chat_data}")
    with open(edbs[0], "rb") as f:
        return f.read(16), os.path.basename(edbs[0])


def discover_binaries(program_dir: str,
                      extra: Sequence[str]) -> List[str]:
    out: List[str] = []
    patterns = [
        os.path.join(program_dir, "KakaoTalk.exe"),
        os.path.join(program_dir, "**", "*.dll"),
        os.path.join(program_dir, "**", "*.exe"),
    ]
    for pattern in patterns:
        out.extend(glob.glob(pattern, recursive=True))
    for item in extra:
        if os.path.isdir(item):
            out.extend(glob.glob(os.path.join(item, "**", "*.exe"), recursive=True))
            out.extend(glob.glob(os.path.join(item, "**", "*.dll"), recursive=True))
        elif os.path.isfile(item):
            out.append(item)

    uniq = []
    seen = set()
    for path in out:
        path = os.path.abspath(path)
        low = path.lower()
        if low in seen:
            continue
        seen.add(low)
        try:
            if os.path.getsize(path) >= 16:
                uniq.append(path)
        except OSError:
            pass
    return uniq


def _test_hardcoded(hardcoded: bytes, uid: str, c0: bytes, full: bool):
    pragma = pragma_fig3(PRAGMA_SRC, hardcoded)
    key, iv = generate_key_iv(pragma, uid)
    if check_oracle(key, iv, c0):
        return key, iv
    if full:
        for _label, pragma in pragma_candidates(PRAGMA_SRC, hardcoded):
            key, iv = generate_key_iv(pragma, uid)
            if check_oracle(key, iv, c0):
                return key, iv
    return None


ASCII_RE = re.compile(rb"[ -~]{8,128}")
HEX32_RE = re.compile(rb"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32}(?![0-9A-Fa-f])")
B64_RE = re.compile(rb"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{22,44}={0,2}(?![A-Za-z0-9+/=])")


def _candidate_keys_from_bytes(data: bytes) -> Iterable[bytes]:
    seen: Set[bytes] = set()

    def emit(value: bytes):
        if len(value) in (16, 24, 32) and value not in seen:
            seen.add(value)
            return value
        return None

    for m in HEX32_RE.finditer(data):
        try:
            value = emit(bytes.fromhex(m.group().decode("ascii")))
            if value:
                yield value
        except Exception:
            pass

    for m in B64_RE.finditer(data):
        raw = m.group()
        try:
            dec = base64.b64decode(raw + b"=" * ((-len(raw)) % 4), validate=True)
            value = emit(dec)
            if value:
                yield value
        except Exception:
            pass

    for m in ASCII_RE.finditer(data):
        s = m.group()
        for n in (16, 24, 32):
            if len(s) == n:
                value = emit(s)
                if value:
                    yield value
            if len(s) > n:
                for i in range(0, len(s) - n + 1):
                    value = emit(s[i:i + n])
                    if value:
                        yield value


def scan_strings(paths: Sequence[str], uid: str, c0: bytes, full: bool):
    tested = 0
    for path in paths:
        try:
            data = open(path, "rb").read()
        except OSError:
            continue
        local = 0
        for hardcoded in _candidate_keys_from_bytes(data):
            tested += 1
            local += 1
            result = _test_hardcoded(hardcoded, uid, c0, full)
            if result:
                key, iv = result
                return hardcoded, path, -1, key, iv
        print(f"  strings {os.path.basename(path)}: {local} candidates", flush=True)
    print(f"string candidate scan tested {tested} keys", flush=True)
    return None


def _scan_range(job):
    path, start, end, step, uid, c0, full = job
    with open(path, "rb") as f:
        data = f.read()
    last = min(end, len(data) - 16)
    i = start
    while i <= last:
        hardcoded = data[i:i + 16]
        if hardcoded and hardcoded != b"\x00" * 16 and hardcoded != hardcoded[:1] * 16:
            result = _test_hardcoded(hardcoded, uid, c0, full)
            if result:
                key, iv = result
                return hardcoded, path, i, key, iv
        i += step
    return None


def scan(paths: Sequence[str], uid: str, c0: bytes,
         step: int, full: bool, workers: int):
    jobs = []
    for path in paths:
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        span = (size + workers - 1) // workers
        for index in range(workers):
            raw_start = index * span
            start = ((raw_start + step - 1) // step) * step
            end = min(size - 16, (index + 1) * span - 1)
            if start <= end:
                jobs.append((path, start, end, step, uid, c0, full))

    mode = "full variants" if full else "Fig.3 fast"
    print(
        f"parallel scan: files={len(paths)} chunks={len(jobs)} "
        f"workers={workers} step={step} mode={mode}",
        flush=True,
    )
    try:
        pool = mp.Pool(processes=workers)
    except PermissionError:
        print("multiprocessing blocked; falling back to sequential scan", flush=True)
        for completed, job in enumerate(jobs, 1):
            hit = _scan_range(job)
            if hit:
                return hit
            print(f"  completed {completed}/{len(jobs)} chunks", flush=True)
        return None

    terminated = False
    try:
        completed = 0
        for hit in pool.imap_unordered(_scan_range, jobs):
            completed += 1
            if hit:
                pool.terminate()
                terminated = True
                return hit
            if completed % max(1, workers) == 0:
                print(f"  completed {completed}/{len(jobs)} chunks", flush=True)
    finally:
        if not terminated:
            pool.close()
        pool.join()
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("user_id", nargs="?")
    p.add_argument("--backup-dir", default=DEFAULT_BACKUP)
    p.add_argument("--program-dir", default=DEFAULT_PROGRAM_DIR)
    p.add_argument("--extra-bin", action="append", default=[],
                   help="extra exe/dll file or directory to scan")
    p.add_argument("--step", type=int, default=4)
    p.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    p.add_argument("--full", action="store_true",
                   help="try all pragma variants, not only exact Fig.3")
    p.add_argument("--raw", action="store_true",
                   help="also scan every raw binary window after string candidates")
    args = p.parse_args(argv)

    uid = get_userid(args)
    c0, edb = first_page(args.backup_dir)
    paths = discover_binaries(args.program_dir, args.extra_bin)
    print(f"userId={uid} oracle={edb} C0={c0.hex()}")
    print(f"backup={args.backup_dir}")
    print("binaries:")
    for path in paths:
        print(f"  {path} ({os.path.getsize(path):,} bytes)")
    if not paths:
        sys.exit("No binaries to scan.")

    hit = scan_strings(paths, uid, c0, args.full)
    if not hit and args.raw:
        hit = scan(paths, uid, c0, args.step, args.full, args.workers)
    if not hit:
        sys.exit(
            "hardcoded key not found. The userId may be wrong, the key may be "
            "in an unscanned old binary, or this build no longer carries it. "
            "Use --raw for the slow exhaustive scan."
        )

    hardcoded, path, off, key, iv = hit
    print("\nFOUND hardcoded key")
    print("  hardcoded:", hardcoded.hex())
    print("  file     :", path)
    print("  offset   :", hex(off))
    print("  db key   :", key.hex())
    print("  db iv    :", iv.hex())
    saved = cfgmod.save_derived(None, key, iv, user_id=uid)
    print("  saved    :", saved)
    print("Next:")
    print(f"  python -m kwin --user-dir \"{os.path.dirname(args.backup_dir)}\" decrypt")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
