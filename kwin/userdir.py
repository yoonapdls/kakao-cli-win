"""KakaoTalk Windows user-directory derivation from JKIISC 2023, Fig. 6/7.

The important detail missing from the earlier experimental brute-forcers is
that the directory name depends on *both* the public constant and ``pragma``:

    inner = AES-CBC-PKCS7(str(user_id), MD5("KAKAOTALK_PC_FOREVER"), iv1)
    text  = user_dir_home + "\\" + hex(inner)
    outer = AES-CBC-PKCS7(text, MD5(pragma), iv2)
    user_dir = SHA1(outer).hexdigest()

Therefore the 40-hex directory name cannot be used to recover userId until the
account's pragma (or the executable's hardcoded pragma key) is known.
"""
from __future__ import annotations

import base64
import hashlib
from typing import Iterable, Optional

from .aes import aes_cbc_encrypt


def _pkcs7(data: bytes) -> bytes:
    n = 16 - (len(data) % 16)
    return data + bytes([n]) * n


def _key_iv(source: bytes):
    key = hashlib.md5(source).digest()
    iv = hashlib.md5(base64.b64encode(key)).digest()
    return key, iv


PUBLIC_KEY, PUBLIC_IV = _key_iv(b"KAKAOTALK_PC_FOREVER")


def directory_name(pragma: str, user_dir_home: str, user_id: int) -> str:
    """Generate the 40-hex KakaoTalk user-directory name (paper Fig. 6)."""
    uid_cipher = aes_cbc_encrypt(
        PUBLIC_KEY,
        PUBLIC_IV,
        _pkcs7(str(user_id).encode("utf-8")),
    )
    intermediate = (
        user_dir_home.encode("utf-8") + b"\\" + uid_cipher.hex().encode("ascii")
    )
    pragma_key, pragma_iv = _key_iv(pragma.encode("utf-8"))
    outer = aes_cbc_encrypt(pragma_key, pragma_iv, _pkcs7(intermediate))
    return hashlib.sha1(outer).hexdigest()


def find_user_id(
    pragma: str,
    user_dir_home: str,
    target_dir: str,
    candidates: Iterable[int],
) -> Optional[int]:
    """Reference implementation for correctness; optimized brute may replace it."""
    target = target_dir.lower()
    for user_id in candidates:
        if directory_name(pragma, user_dir_home, user_id) == target:
            return user_id
    return None

