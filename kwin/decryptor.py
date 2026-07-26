"""Decrypt a whole .edb file into a plain SQLite database.

Read-only on the source. Decryption is per-4096-byte page, AES-128-CBC, no
padding, iv reset each page (SQLCipher-like, but with a fixed derived iv).
"""
from __future__ import annotations

import os
from typing import Optional

from .aes import aes_cbc_decrypt
from .keyderiv import SQLITE_MAGIC

PAGE = 4096


def decrypt_bytes(key: bytes, iv: bytes, enc: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(enc), PAGE):
        chunk = enc[i:i + PAGE]
        if len(chunk) % 16 != 0:
            # trailing partial (WAL fragments etc.) — leave as-is
            out += chunk
            continue
        out += aes_cbc_decrypt(key, iv, chunk)
    return bytes(out)


def decrypt_file(src_edb: str, key: bytes, iv: bytes, dst_sqlite: str) -> bool:
    """Decrypt src_edb -> dst_sqlite. Returns True if the result looks like SQLite."""
    with open(src_edb, "rb") as f:
        enc = f.read()
    dec = decrypt_bytes(key, iv, enc)
    ok = dec[:16] == SQLITE_MAGIC
    os.makedirs(os.path.dirname(os.path.abspath(dst_sqlite)), exist_ok=True)
    with open(dst_sqlite, "wb") as f:
        f.write(dec)
    return ok


def first_page(src_edb: str) -> bytes:
    with open(src_edb, "rb") as f:
        return f.read(PAGE)
