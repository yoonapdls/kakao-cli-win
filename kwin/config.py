"""Configuration: the one secret (hardcoded AES key) plus optional hints.

Precedence: env vars override config.json. The hardcoded key can be given as
hex ("aabb..."), base64, or a 0x-prefixed hex. Length must be 16, 24, or 32.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(os.path.dirname(HERE), "config.json")


def _parse_key(s: str) -> bytes:
    s = s.strip()
    if not s:
        raise ValueError("empty key")
    if s.startswith("0x"):
        s = s[2:]
    # try hex first
    try:
        b = bytes.fromhex(s)
        if len(b) in (16, 24, 32):
            return b
    except ValueError:
        pass
    b = base64.b64decode(s + "=" * (-len(s) % 4))
    if len(b) not in (16, 24, 32):
        raise ValueError(f"key length {len(b)} not in (16,24,32)")
    return b


@dataclass
class Config:
    hardcoded_key: Optional[bytes]
    user_id: Optional[str] = None
    brute_min: int = 1
    brute_max: int = 400_000_000  # KakaoTalk userIds are 9-digit, up to ~4e8
    derived_key: Optional[bytes] = None
    derived_iv: Optional[bytes] = None
    path: Optional[str] = None

    @property
    def has_key(self) -> bool:
        return self.hardcoded_key is not None

    @property
    def has_derived(self) -> bool:
        return self.derived_key is not None and self.derived_iv is not None


def save_derived(path: Optional[str], key: bytes, iv: bytes,
                 user_id: Optional[str] = None,
                 pragma: Optional[str] = None) -> str:
    p = path or DEFAULT_CONFIG
    data = {}
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    data["derived_key"] = key.hex()
    data["derived_iv"] = iv.hex()
    if user_id:
        data["user_id"] = str(int(user_id))
    if pragma:
        data["pragma"] = pragma
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return p


def load(path: Optional[str] = None) -> Config:
    data = {}
    p = path or DEFAULT_CONFIG
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)

    key_str = os.environ.get("KAKAO_HARDCODED_KEY") or data.get("hardcoded_key")
    key = _parse_key(key_str) if key_str else None

    uid = os.environ.get("KAKAO_USER_ID") or data.get("user_id")
    if uid:
        uid = str(uid).strip()
        if not uid.isdigit():
            raise ValueError("user_id must contain decimal digits only")
        # The Windows implementation uses String(userId), not fixed-width
        # zero-padding.  Leading zeroes therefore change the derived DB key.
        uid = str(int(uid))
    else:
        uid = None

    def _hex(v):
        return bytes.fromhex(v) if v else None

    return Config(
        hardcoded_key=key,
        user_id=uid,
        brute_min=int(data.get("brute_min", 1)),
        brute_max=int(data.get("brute_max", 400_000_000)),
        derived_key=_hex(data.get("derived_key")),
        derived_iv=_hex(data.get("derived_iv")),
        path=p,
    )
