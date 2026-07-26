"""kwin — KakaoTalk-on-Windows local chat decryption & collection toolkit.

Read-only, offline, single-machine, own-data. Port of the macOS kakaocli
concept to the Windows KakaoTalk .edb (AES-128-CBC, 4096-byte page) scheme.
"""
__all__ = ["deviceinfo", "keyderiv", "decryptor", "collect", "config", "aes"]
