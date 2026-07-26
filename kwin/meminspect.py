"""Targeted, read-only KakaoTalk process-memory inspection.

Unlike the old exhaustive AES-key scan, this looks only for textual artifacts
that commonly surround the account's numeric userId or pragma. Results are
written locally and are never transmitted.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Iterable, List, Set

from . import memkey

ASCII_NUMBER = re.compile(rb"(?<![0-9])[0-9]{6,12}(?![0-9])")
ASCII_B64_88 = re.compile(rb"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{86}==(?![A-Za-z0-9+/=])")
UTF16_NUMBER = re.compile(rb"(?<![0-9]\x00)(?:[0-9]\x00){6,12}(?![0-9]\x00)")
UTF16_B64_88 = re.compile(
    rb"(?<![A-Za-z0-9+/]\x00)(?:[A-Za-z0-9+/]\x00){86}=\x00=\x00"
    rb"(?![A-Za-z0-9+/=]\x00)"
)


@dataclass(frozen=True)
class Hit:
    pid: int
    address: int
    kind: str
    text: str


def _printable_context(data: bytes, start: int, end: int) -> str:
    chunk = data[max(0, start - 160):min(len(data), end + 240)]
    return "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)


def _utf16_context(data: bytes, start: int, end: int) -> str:
    chunk = data[max(0, start - 320):min(len(data), end + 480)]
    if len(chunk) % 2:
        chunk = chunk[:-1]
    return chunk.decode("utf-16-le", "ignore").replace("\x00", ".")


def _valid_pragma(value: str) -> bool:
    try:
        return len(base64.b64decode(value, validate=True)) == 64
    except Exception:
        return False


def scan(pid: int, needles: Iterable[str]) -> List[Hit]:
    h = memkey._open(pid)
    hits: List[Hit] = []
    seen: Set[tuple] = set()
    ascii_needles = [x.encode("utf-8") for x in needles if x]
    utf16_needles = [x.encode("utf-16-le") for x in needles if x]
    try:
        for base, size, _ in memkey.iter_regions(h):
            if size <= 0 or size > (512 << 20):
                continue
            data = memkey.read_region(h, base, size)

            # Keyword/account contexts. These are the highest-value hits.
            for needle in ascii_needles:
                pos = 0
                while True:
                    pos = data.find(needle, pos)
                    if pos < 0:
                        break
                    ctx = _printable_context(data, pos, pos + len(needle))
                    key = ("ascii", ctx)
                    if key not in seen:
                        seen.add(key)
                        hits.append(Hit(pid, base + pos, "ascii-context", ctx))
                    pos += max(1, len(needle))
            for needle in utf16_needles:
                pos = 0
                while True:
                    pos = data.find(needle, pos)
                    if pos < 0:
                        break
                    ctx = _utf16_context(data, pos, pos + len(needle))
                    key = ("utf16", ctx)
                    if key not in seen:
                        seen.add(key)
                        hits.append(Hit(pid, base + pos, "utf16-context", ctx))
                    pos += max(2, len(needle))

            # A pragma is Base64(SHA-512(...)): exactly 88 chars ending ==.
            # Windows builds often keep strings as UTF-16LE, so check both.
            for m in ASCII_NUMBER.finditer(data):
                value = m.group().decode("ascii")
                key = ("number", value)
                if key not in seen:
                    seen.add(key)
                    hits.append(Hit(pid, base + m.start(), "numeric-candidate", value))
            for m in UTF16_NUMBER.finditer(data):
                value = m.group().decode("utf-16-le", "ignore")
                key = ("number", value)
                if key not in seen:
                    seen.add(key)
                    hits.append(Hit(pid, base + m.start(), "numeric-candidate", value))
            for m in ASCII_B64_88.finditer(data):
                value = m.group().decode("ascii")
                if not _valid_pragma(value):
                    continue
                key = ("pragma", value)
                if key not in seen:
                    seen.add(key)
                    hits.append(Hit(pid, base + m.start(), "pragma-candidate", value))
            for m in UTF16_B64_88.finditer(data):
                value = m.group().decode("utf-16-le", "ignore")
                if not _valid_pragma(value):
                    continue
                key = ("pragma", value)
                if key not in seen:
                    seen.add(key)
                    hits.append(Hit(pid, base + m.start(), "pragma-candidate", value))
    finally:
        memkey.k32.CloseHandle(h)
    return hits


def numeric_candidates(hits: Iterable[Hit]) -> List[str]:
    """Return decimal strings seen only inside high-value keyword contexts."""
    out: Set[str] = set()
    for hit in hits:
        if hit.kind == "pragma-candidate":
            continue
        if hit.kind == "numeric-candidate":
            out.add(hit.text)
            continue
        for m in re.finditer(r"(?<![0-9])[0-9]{6,12}(?![0-9])", hit.text):
            out.add(m.group())
    return sorted(out, key=lambda x: (len(x), int(x)))
