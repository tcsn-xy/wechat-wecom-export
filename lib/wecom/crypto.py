"""企业微信 5.x 本地数据库的页加密原语（wxSQLite3 风格 AES-128-CBC）。

和企业微信官方无关，纯属对本地文件格式的观察实现：

* 页大小固定 4096；
* **每页**的 AES key = ``MD5(raw_key || LE32(page_no) || b"sAlT")``；
* **每页**的 IV 由页码驱动的一串线性同余伪随机数再取 MD5；
* 第 1 页把 SQLite header 的前 16 字节整体保留，另外 8 字节存在 16..24；
* 没有个人微信（SQLCipher）那种 80 字节 reserve/HMAC 区。

所以一把 16 字节 raw key 就够了，解密是「逐页独立」的，可以并行。
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

from Crypto.Cipher import AES

PAGE_SIZE = 4096
SQLITE_HEADER = b"SQLite format 3\x00"
KEY_TAG = b"sAlT"


# --------------------------------------------------------------------------- #
# 格式识别
# --------------------------------------------------------------------------- #
def is_plain_sqlite(page: bytes) -> bool:
    return page.startswith(SQLITE_HEADER)


def has_wecom_shape(page: bytes) -> bool:
    """加密库第 1 页会留下 SQLite header 的碎片，用它认格式。"""
    if len(page) < 24 or is_plain_sqlite(page):
        return False
    frag = page[16:24]
    page_size = int.from_bytes(frag[:2], "big")
    if page_size == 1:
        page_size = 65536
    return (512 <= page_size <= 65536
            and page_size & (page_size - 1) == 0
            and frag[5:8] == b"\x40\x20\x20")


def database_format(page: bytes) -> str:
    if is_plain_sqlite(page):
        return "sqlite"
    if has_wecom_shape(page):
        return "wecom-aes128"
    return "unknown"


# --------------------------------------------------------------------------- #
# 密钥派生
# --------------------------------------------------------------------------- #
def _lcg_step(value: int) -> int:
    """wxSQLite3 里那个线性同余发生器的一步。"""
    q = value // 52774
    value = 40692 * (value - 52774 * q) - 3791 * q
    return value if value >= 0 else value + 2147483399


def page_iv(page_number: int) -> bytes:
    if page_number < 1:
        raise ValueError("页号从 1 开始")
    value = page_number + 1
    buf = bytearray()
    for _ in range(4):
        value = _lcg_step(value)
        buf.extend(struct.pack("<I", value & 0xFFFFFFFF))
    return hashlib.md5(bytes(buf)).digest()


def page_key(raw_key: bytes, page_number: int) -> bytes:
    if len(raw_key) != 16:
        raise ValueError("企业微信 raw key 必须正好 16 字节")
    return hashlib.md5(raw_key + struct.pack("<I", page_number) + KEY_TAG).digest()


def _cipher(raw_key: bytes, page_number: int) -> AES:
    return AES.new(page_key(raw_key, page_number), AES.MODE_CBC,
                   page_iv(page_number))


# --------------------------------------------------------------------------- #
# 单页
# --------------------------------------------------------------------------- #
def decrypt_page(raw_key: bytes, page: bytes, page_number: int) -> bytes:
    if len(page) != PAGE_SIZE:
        raise ValueError("页长度必须是 %d，实际 %d" % (PAGE_SIZE, len(page)))
    if page_number == 1 and has_wecom_shape(page):
        frag = page[16:24]
        tail = _cipher(raw_key, 1).decrypt(page[8:16] + page[24:])
        if tail[:8] != frag:
            raise ValueError("第 1 页校验失败，key 不对")
        return SQLITE_HEADER + tail
    return _cipher(raw_key, page_number).decrypt(page)


def encrypt_page(raw_key: bytes, plain: bytes, page_number: int) -> bytes:
    """逆运算，只给自检用，不碰真实数据。"""
    if len(plain) != PAGE_SIZE:
        raise ValueError("页长度必须是 %d" % PAGE_SIZE)
    if page_number != 1:
        return _cipher(raw_key, page_number).encrypt(plain)
    if not is_plain_sqlite(plain):
        raise ValueError("第 1 页必须带 SQLite header")
    ct = _cipher(raw_key, 1).encrypt(plain[16:])
    return bytes(8) + ct[:8] + plain[16:24] + ct[8:]


def verify_key(raw_key: bytes, page_one: bytes) -> bool:
    """拿到第 1 页就能判断 key 对不对（第 100 字节必须是合法 B-tree 页型）。"""
    if len(raw_key) != 16 or len(page_one) < PAGE_SIZE:
        return False
    try:
        plain = decrypt_page(raw_key, page_one[:PAGE_SIZE], 1)
    except (ValueError, KeyError):
        return False
    return is_plain_sqlite(plain) and len(plain) > 100 and plain[100] in (2, 5, 10, 13)


# --------------------------------------------------------------------------- #
# 整库（含 WAL）
# --------------------------------------------------------------------------- #
def _wal_frames(wal: bytes) -> list:
    """解析 WAL，返回「最后一次提交之前」的帧 [(页号, 提交页数, 页数据)]。"""
    if len(wal) < 32:
        return []
    if int.from_bytes(wal[:4], "big") not in (0x377F0682, 0x377F0683):
        return []
    if (int.from_bytes(wal[8:12], "big") or 65536) != PAGE_SIZE:
        return []
    salt = wal[16:24]
    frame_size = 24 + PAGE_SIZE
    frames = []
    off = 32
    while off + frame_size <= len(wal):
        head = wal[off:off + 24]
        page_no = int.from_bytes(head[:4], "big")
        commit = int.from_bytes(head[4:8], "big")
        if page_no < 1:
            break
        if head[8:16] != salt:
            break
        frames.append((page_no, commit, wal[off + 24:off + frame_size]))
        off += frame_size
    last = -1
    for i, (_pn, commit, _p) in enumerate(frames):
        if commit:
            last = i
    return frames[:last + 1] if last >= 0 else []


def decrypt_database_bytes(blob: bytes, raw_key: bytes,
                           wal: bytes | None = None) -> tuple:
    """整库解密为明文 SQLite 字节串，并把已提交的 WAL 帧合并进去。"""
    if len(blob) < PAGE_SIZE or len(blob) % PAGE_SIZE:
        raise ValueError("数据库长度不是 %d 的整数倍" % PAGE_SIZE)
    fmt = database_format(blob[:PAGE_SIZE])
    if fmt == "unknown":
        raise ValueError("不认识的数据库格式")

    out = bytearray()
    for i in range(len(blob) // PAGE_SIZE):
        page = blob[i * PAGE_SIZE:(i + 1) * PAGE_SIZE]
        out.extend(page if fmt == "sqlite" else decrypt_page(raw_key, page, i + 1))

    applied = 0
    final_size = None
    for page_no, commit, page in _wal_frames(wal or b""):
        plain = page if fmt == "sqlite" else decrypt_page(raw_key, page, page_no)
        off = (page_no - 1) * PAGE_SIZE
        if len(out) < off + PAGE_SIZE:
            out.extend(bytes(off + PAGE_SIZE - len(out)))
        out[off:off + PAGE_SIZE] = plain
        applied += 1
        if commit:
            final_size = commit * PAGE_SIZE
    if final_size is not None:
        del out[final_size:]

    return bytes(out), {"source_format": fmt, "pages": len(blob) // PAGE_SIZE,
                        "wal_frames": applied, "output_bytes": len(out)}


def decrypt_database(src: Path, dst: Path, raw_key: bytes,
                     apply_wal: bool = True) -> dict:
    """把 src 解密成 dst（新文件，绝不覆盖源库）。"""
    src = Path(src)
    dst = Path(dst)
    wal_path = Path(str(src) + "-wal")
    wal = None
    if apply_wal and wal_path.exists():
        wal = wal_path.read_bytes()
    plain, info = decrypt_database_bytes(src.read_bytes(), raw_key, wal=wal)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(plain)
    return info
