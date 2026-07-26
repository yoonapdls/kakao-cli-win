"""Parse KakaoTalk's current/v2 appstate.dat envelope metadata.

Newer Windows KakaoTalk builds no longer use the old account-wide AES-CBC DB
key directly for every .edb.  The active user directory contains appstate.dat,
which is a small CBOR document:

    {
      "info_prefix": b"v2:",
      "salt": <32 bytes>,
      "wrapped_dek_map": [
        [b"chat_data\\chatLogs_....edb", <40-byte wrapped DEK>],
        ...
      ]
    }

This module intentionally only parses the local metadata.  It does not know the
wrapping/master key yet; that still has to be recovered or derived separately.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


class CborMap(list):
    """CBOR maps can have list/byte-array keys, so keep raw pairs."""


class CborReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ValueError("truncated CBOR")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def length(self, ai: int) -> int:
        if ai < 24:
            return ai
        if ai == 24:
            return self.read(1)[0]
        if ai == 25:
            return int.from_bytes(self.read(2), "big")
        if ai == 26:
            return int.from_bytes(self.read(4), "big")
        if ai == 27:
            return int.from_bytes(self.read(8), "big")
        raise ValueError("indefinite-length CBOR is not supported")

    def obj(self) -> Any:
        ib = self.read(1)[0]
        major = ib >> 5
        ai = ib & 31
        if major == 0:
            return self.length(ai)
        if major == 1:
            return -1 - self.length(ai)
        if major == 2:
            return self.read(self.length(ai))
        if major == 3:
            return self.read(self.length(ai)).decode("utf-8", "replace")
        if major == 4:
            return [self.obj() for _ in range(self.length(ai))]
        if major == 5:
            pairs = CborMap()
            for _ in range(self.length(ai)):
                pairs.append((self.obj(), self.obj()))
            return pairs
        if major == 7:
            if ai == 20:
                return False
            if ai == 21:
                return True
            if ai == 22:
                return None
        raise ValueError(f"unsupported CBOR item: major={major} ai={ai}")


def _byte_array(value: Any) -> Optional[bytes]:
    if isinstance(value, bytes):
        return value
    if (isinstance(value, list) and not isinstance(value, CborMap)
            and all(isinstance(x, int) and 0 <= x <= 255 for x in value)):
        return bytes(value)
    return None


def _key_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    b = _byte_array(value)
    if b is not None:
        return b.decode("utf-8", "replace")
    return str(value)


def _lookup(map_pairs: CborMap, name: str) -> Any:
    for key, value in map_pairs:
        if _key_string(key) == name:
            return value
    return None


@dataclass
class AppState:
    path: str
    info_prefix: bytes
    salt: bytes
    wrapped_dek_map: Dict[str, bytes]

    def wrapped_for(self, rel_path: str) -> Optional[bytes]:
        norm = rel_path.replace("/", "\\")
        return self.wrapped_dek_map.get(norm)


def parse_file(path: str) -> AppState:
    data = open(path, "rb").read()
    reader = CborReader(data)
    root = reader.obj()
    if reader.pos != len(data):
        raise ValueError(f"trailing bytes after CBOR: {len(data) - reader.pos}")
    if not isinstance(root, CborMap):
        raise ValueError("appstate root is not a CBOR map")

    prefix = _byte_array(_lookup(root, "info_prefix")) or b""
    salt = _byte_array(_lookup(root, "salt")) or _byte_array(_lookup(root, "dsalt")) or b""
    raw_map = _lookup(root, "wrapped_dek_map")
    if not isinstance(raw_map, list):
        raw_map = []

    entries: Dict[str, bytes] = {}
    for item in raw_map:
        if not isinstance(item, list) or len(item) != 2:
            continue
        name_b = _byte_array(item[0])
        wrapped = _byte_array(item[1])
        if not name_b or not wrapped:
            continue
        entries[name_b.decode("utf-8", "replace")] = wrapped

    return AppState(
        path=os.path.abspath(path),
        info_prefix=prefix,
        salt=salt,
        wrapped_dek_map=entries,
    )


def find_for_user_dir(user_dir: str) -> Optional[str]:
    path = os.path.join(user_dir, "appstate.dat")
    return path if os.path.exists(path) else None


def sample_entries(state: AppState, limit: int = 10) -> List[Tuple[str, bytes]]:
    return list(state.wrapped_dek_map.items())[:limit]
