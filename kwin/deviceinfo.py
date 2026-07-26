"""Read the device-specific inputs KakaoTalk (Windows) uses for key derivation.

All of these come from the local machine — no network, read-only:
  * sys_uuid, hdd_model, hdd_serial  -> HKCU\\Software\\Kakao\\KakaoTalk\\DeviceInfo\\<Last>
  * account list / phone number       -> %LOCALAPPDATA%\\Kakao\\KakaoTalk\\users\\login_list.dat
  * user data directories (40-hex)    -> %LOCALAPPDATA%\\Kakao\\KakaoTalk\\users\\<hash>\\chat_data

The numeric KakaoTalk userId is deliberately NOT stored in plaintext by Kakao;
it is recovered by brute force against the SQLite-header oracle (see keyderiv).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

try:
    import winreg  # Windows only
except ImportError:  # pragma: no cover - allows import on non-Windows for tooling
    winreg = None

KAKAO_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Kakao", "KakaoTalk")
USERS_DIR = os.path.join(KAKAO_ROOT, "users")
DEVICEINFO_KEY = r"Software\Kakao\KakaoTalk\DeviceInfo"

_HEX40 = re.compile(r"^[0-9a-f]{40}$")


@dataclass
class DeviceInfo:
    sys_uuid: str
    hdd_model: str
    hdd_serial: str
    accounts: List[str] = field(default_factory=list)
    phone: Optional[str] = None

    @property
    def pragma_source(self) -> str:
        """The exact string KakaoTalk feeds into the PRAGMA AES step."""
        return f"{self.sys_uuid}|{self.hdd_model}|{self.hdd_serial}"


def read_deviceinfo() -> DeviceInfo:
    if winreg is None:
        raise RuntimeError("winreg unavailable — this must run on Windows")

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, DEVICEINFO_KEY, 0, winreg.KEY_READ) as k:
        try:
            last = winreg.QueryValueEx(k, "Last")[0]
        except FileNotFoundError:
            last = None

    subkeys = _list_subkeys(DEVICEINFO_KEY)
    active = last if last in subkeys else (subkeys[0] if subkeys else None)
    if not active:
        raise RuntimeError("No DeviceInfo subkey found under registry")

    sub = DEVICEINFO_KEY + "\\" + active
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub, 0, winreg.KEY_READ) as k:
        sys_uuid = winreg.QueryValueEx(k, "sys_uuid")[0]
        hdd_model = winreg.QueryValueEx(k, "hdd_model")[0]
        hdd_serial = winreg.QueryValueEx(k, "hdd_serial")[0]

    accounts, phone = _read_login_list()
    return DeviceInfo(sys_uuid, hdd_model, hdd_serial, accounts, phone)


def _list_subkeys(path: str) -> List[str]:
    out: List[str] = []
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as k:
        i = 0
        while True:
            try:
                out.append(winreg.EnumKey(k, i))
                i += 1
            except OSError:
                break
    return out


def _read_login_list():
    path = os.path.join(USERS_DIR, "login_list.dat")
    accounts: List[str] = []
    phone: Optional[str] = None
    if os.path.exists(path):
        raw = open(path, "rb").read().decode("utf-8", "ignore")
        parts = raw.split("|")
        for p in parts:
            p = p.strip()
            if "@" in p:
                accounts.append(p)
            elif p.isdigit() and len(p) >= 10:
                phone = p
    return accounts, phone


def list_user_dirs() -> List[str]:
    """Return absolute paths of the 40-hex-char user data directories."""
    if not os.path.isdir(USERS_DIR):
        return []
    dirs = []
    for name in os.listdir(USERS_DIR):
        base = name.split("_")[0]
        if _HEX40.match(base) and os.path.isdir(os.path.join(USERS_DIR, name)):
            dirs.append(os.path.join(USERS_DIR, name))
    # Prefer the ones that actually carry a chat_data folder, largest first.
    dirs.sort(key=lambda d: (os.path.isdir(os.path.join(d, "chat_data")),
                             _dir_size(os.path.join(d, "chat_data"))), reverse=True)
    return dirs


def _dir_size(path: str) -> int:
    if not os.path.isdir(path):
        return 0
    total = 0
    for f in os.listdir(path):
        fp = os.path.join(path, f)
        if os.path.isfile(fp):
            total += os.path.getsize(fp)
    return total
