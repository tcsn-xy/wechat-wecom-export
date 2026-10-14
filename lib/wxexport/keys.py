"""SQLCipher 密钥推导与只读打开。

原理
----
微信 4.x 本机数据库是 SQLCipher 加密的。它对每个库的做法是：

    CCKeyDerivationPBKDF(kCCPBKDF2, password, 32,
                         salt = <库文件前 16 字节>, 16,
                         kCCPRFHmacAlgSHA512, 256000,
                         derivedKey, 32)

其中 ``password`` 是整个账号共用的 32 字节随机口令（一次登录内不变），
``salt`` 是每个库自己的文件头。派生出的 32 字节直接作为 AES-256 的密钥交给
``CCCryptorCreate``，所以 SQLCipher 侧用 ``PRAGMA key = "x'<hex>'"`` 打开即可。

那个 32 字节口令无法离线算出，只能从微信进程里抓——见 ``scripts/capture_keys.py``。
"""
from __future__ import annotations

import hashlib
from urllib.parse import quote
import json
import os
from typing import Optional

import sqlcipher3

PBKDF2_ROUNDS = 256000
SALT_LEN = 16
KEY_LEN = 32


# --------------------------------------------------------------------------- #
def load_capture(path: str) -> list:
    """读 capture.jsonl，返回全部记录。"""
    recs = []
    if not os.path.exists(path):
        return recs
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except ValueError:
                continue
    return recs


def load_passphrase(path: str) -> bytes:
    """取出账号级口令（256000 轮那条 PBKDF 记录的 password）。"""
    cands = []
    for r in load_capture(path):
        if r.get("t") == "pbkdf" and r.get("rounds") == PBKDF2_ROUNDS and r.get("pw"):
            cands.append(bytes.fromhex(r["pw"]))
    if not cands:
        raise SystemExit(
            "在 %s 里没有找到 256000 轮的 PBKDF 记录。\n"
            "请先运行 scripts/capture_keys.py 重新捕获密钥。" % path)

    # 微信可能对多个库派发同一个口令；取出现次数最多的那个
    best, best_n = cands[0], 0
    for c in set(cands):
        n = cands.count(c)
        if n > best_n:
            best, best_n = c, n
    return best


def db_salt(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read(SALT_LEN)


def derive_key(passphrase: bytes, salt: bytes,
               rounds: int = PBKDF2_ROUNDS, dklen: int = KEY_LEN) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", passphrase, salt, rounds, dklen=dklen)


def db_key(path: str, passphrase: bytes) -> str:
    """某个库文件的 SQLCipher raw key（hex）。"""
    return derive_key(passphrase, db_salt(path)).hex()


# --------------------------------------------------------------------------- #
class Keyring:
    """缓存口令，按库路径给出密钥，避免每个库都重算 PBKDF2。"""

    def __init__(self, capture_path: str):
        self.capture_path = capture_path
        self._passphrase: Optional[bytes] = None
        self._cache: dict = {}

    @property
    def passphrase(self) -> bytes:
        if self._passphrase is None:
            self._passphrase = load_passphrase(self.capture_path)
        return self._passphrase

    def key_for(self, db_path: str) -> str:
        salt=db_salt(db_path)
        key=(db_path,salt)
        if key not in self._cache:
            self._cache[key] = derive_key(self.passphrase,salt).hex()
        return self._cache[key]

    def connect(self, db_path: str):
        """只读打开。"""
        if not os.path.exists(db_path):
            raise FileNotFoundError(db_path)
        conn = sqlcipher3.connect("file:"+quote(os.path.abspath(db_path))+"?mode=ro",uri=True)
        conn.execute('PRAGMA key = "x\'%s\'"' % self.key_for(db_path))
        conn.execute("PRAGMA cipher_page_size = 4096")
        conn.execute("PRAGMA query_only = ON")
        return conn

    def verify(self, db_path: str) -> bool:
        c=None
        try:
            c = self.connect(db_path)
            c.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return True
        except Exception:
            return False
        finally:
            if c is not None:
                c.close()


def self_test(capture_path: str, db_path: str) -> bool:
    """确认某个库能否用现有密钥打开。"""
    return Keyring(capture_path).verify(db_path)
