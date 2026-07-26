"""End-to-end pipeline self-test that needs NO real KakaoTalk key.

It fabricates the exact derivation KakaoTalk uses (with a stand-in hardcoded
key + userId), builds a fake encrypted 'page', then proves that keyderiv.solve,
the oracle, and the page decryptor all recover it. If this passes, the only
thing standing between the toolkit and real data is the genuine hardcoded key.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kwin import keyderiv, decryptor, userdir
from kwin.aes import aes_cbc_encrypt

PRAGMA_SRC = "00000000-0000-4000-8000-000000000000|TEST_DISK_MODEL|TEST_DISK_SERIAL"
FAKE_KEY = bytes(range(16))          # stand-in for the real hardcoded key
TRUE_UID = "1987654321"


def make_fake_edb(path):
    """Encrypt a minimal but real SQLite db exactly the way KakaoTalk would."""
    # 1) build a tiny real sqlite file
    tmp = path + ".plain"
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE chatLogs(id INTEGER, authorId INTEGER, type INTEGER, message TEXT, sendAt INTEGER)")
    con.execute("INSERT INTO chatLogs VALUES (1, 42, 1, 'hello kakao', 1650000000)")
    con.commit(); con.close()
    plain = open(tmp, "rb").read()
    os.remove(tmp)
    # pad to 4096-page multiple
    if len(plain) % decryptor.PAGE:
        plain += b"\x00" * (decryptor.PAGE - len(plain) % decryptor.PAGE)
    # 2) derive exactly as the paper's Fig. 3 specifies.
    label = "pkcs7|b64(sha512(ct))"
    pragma = keyderiv.pragma_fig3(PRAGMA_SRC, FAKE_KEY)
    key, iv = keyderiv.generate_key_iv(pragma, TRUE_UID)
    # 3) encrypt per page
    enc = bytearray()
    for i in range(0, len(plain), decryptor.PAGE):
        enc += aes_cbc_encrypt(key, iv, plain[i:i + decryptor.PAGE])
    open(path, "wb").write(enc)
    return label, pragma, key, iv


def main():
    edb = os.path.join(os.path.dirname(__file__), "_selftest.edb")
    label, pragma, real_key, real_iv = make_fake_edb(edb)
    fp = decryptor.first_page(edb)

    # brute a small window that contains TRUE_UID
    lo = int(TRUE_UID) - 3
    uids = (str(x) for x in range(lo, lo + 7))
    sol = keyderiv.solve(PRAGMA_SRC, FAKE_KEY, fp, uids)
    assert sol, "solve() failed to recover key/userId"
    assert sol.user_id == TRUE_UID, sol.user_id
    assert sol.key == real_key and sol.iv == real_iv, "derived key/iv mismatch"
    print(f"[ok] solve recovered user_id={sol.user_id} variant={sol.pragma_label}")

    # Verify the exact two-stage user-directory construction from Fig. 6.
    home = r"C:\Users\example\AppData\Local\Kakao\KakaoTalk\users"
    expected_dir = userdir.directory_name(pragma, home, int(TRUE_UID))
    recovered = userdir.find_user_id(
        pragma, home, expected_dir, range(int(TRUE_UID) - 2, int(TRUE_UID) + 3)
    )
    assert recovered == int(TRUE_UID), recovered
    print(f"[ok] directory-name oracle recovered user_id={recovered}")

    # decrypt full file and read it back as sqlite
    out = os.path.join(os.path.dirname(__file__), "_selftest.sqlite")
    ok = decryptor.decrypt_file(edb, sol.key, sol.iv, out)
    assert ok, "decrypted output is not a SQLite db"
    con = sqlite3.connect(out)
    row = con.execute("SELECT message FROM chatLogs WHERE id=1").fetchone()
    con.close()
    assert row and row[0] == "hello kakao", row
    print(f"[ok] decrypted + read message: {row[0]!r}")

    for f in (edb, out):
        try: os.remove(f)
        except OSError: pass
    print("\nSELF-TEST PASSED - pipeline is correct; only the real hardcoded key is missing.")


if __name__ == "__main__":
    main()
