"""Recover a KakaoTalk Windows account key from live process artifacts.

This is deliberately targeted rather than an exhaustive memory-key scan:

1. collect textual pragma and numeric userId candidates near account markers;
2. verify each pair against the paper's exact two-stage user-directory formula;
3. verify the resulting DB key/IV against ``SQLite format 3\0``.

Only a pair satisfying both independent checks is accepted.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from . import meminspect, memkey, userdir
from .keyderiv import check_oracle, generate_key_iv


@dataclass(frozen=True)
class Recovery:
    user_id: str
    pragma: str
    key: bytes
    iv: bytes
    directory: str
    directory_match: bool


def scan_processes(pids: Iterable[int], needles: Iterable[str]):
    hits = []
    for pid in pids:
        hits.extend(meminspect.scan(pid, needles))
    numbers = meminspect.numeric_candidates(hits)
    pragmas = sorted({h.text for h in hits if h.kind == "pragma-candidate"})
    return hits, numbers, pragmas


def _uid_forms(value: str) -> List[str]:
    """Return only representations KakaoTalk may have used for String(userId)."""
    if not value.isdigit():
        return []
    canonical = str(int(value))
    if not (1 <= len(canonical) <= 12):
        return []
    return [canonical]


def verify_pairs(pragmas: Sequence[str],
                 numbers: Sequence[str],
                 users_home: str,
                 target_dirs: Sequence[str],
                 first_page: bytes) -> Optional[Recovery]:
    """Verify candidates with the SQLite header as the primary oracle.

    The exact byte representation of ``users_home`` in the directory formula
    can vary by client build (case, separator, encoding).  It must therefore
    not gate the conclusive DB check.  A key/IV that decrypts the first block
    to the fixed 16-byte SQLite header is sufficient; the directory formula is
    retained as an independent diagnostic.
    """
    targets = {x.lower() for x in target_dirs if len(x) == 40}
    for raw_number in numbers:
        for uid in _uid_forms(raw_number):
            for pragma in pragmas:
                key, iv = generate_key_iv(pragma, uid)
                if not check_oracle(key, iv, first_page):
                    continue
                generated = userdir.directory_name(
                    pragma, users_home, int(uid)
                )
                return Recovery(
                    uid, pragma, key, iv, generated,
                    generated.lower() in targets,
                )
    return None


def report(path: str, hits, numbers, pragmas) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("NUMERIC CANDIDATES\n")
        f.write("\n".join(numbers))
        f.write("\n\nPRAGMA CANDIDATES\n")
        f.write("\n".join(pragmas))
        f.write("\n\nCONTEXTS\n")
        for hit in hits:
            if hit.kind in ("pragma-candidate", "numeric-candidate"):
                continue
            f.write(
                f"\n[{hit.kind}] pid={hit.pid} addr={hex(hit.address)}\n"
            )
            f.write(hit.text + "\n")


def read_report_candidates(path: str):
    """Load numeric/pragma candidates from a report created by ``report``."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    numeric_part, rest = text.split("\n\nPRAGMA CANDIDATES\n", 1)
    pragma_part = rest.split("\n\nCONTEXTS\n", 1)[0]
    numbers = [
        x.strip() for x in numeric_part.splitlines()[1:] if x.strip()
    ]
    pragmas = [x.strip() for x in pragma_part.splitlines() if x.strip()]
    return numbers, pragmas
