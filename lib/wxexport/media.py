"""微信 4.x 本地图片缓存的解密。

.dat 容器格式（V2，微信 4.x 主流）
--------------------------------
    偏移  长度  含义
    0     6     magic  ``\\x07\\x08V2\\x08\\x07``（老版本是 V1）
    6     4     aes_size   —— 前这么多字节是 AES-128/256-ECB 加密的
    10    4     xor_size   —— 末尾这么多字节是与单字节 key 异或的
    14    1     保留
    15    ...   payload

    payload[0 : align16(aes_size)]        AES-ECB 解密，取前 aes_size 字节
    payload[align16(aes_size) : -xor_size] 明文直接拷贝
    payload[-xor_size : ]                 逐字节异或

V2 的 AES key 是 ``md5(uin + wxid)[:16]``；V1 用固定 key ``cfcd208495d565ef``。
少数文件是纯异或（没有 magic），靠试探图片文件头还原。

另外微信新版本会把图片塞进 ``wxgf`` 容器，里面往往是 HEVC 码流，需要解码
成普通 JPEG 才能给浏览器看。
"""
from __future__ import annotations

import collections
import hashlib
import io
import os
import struct
from typing import Dict, List, Optional, Tuple

from Crypto.Cipher import AES

V1_MAGIC = b"\x07\x08V1\x08\x07"
V2_MAGIC = b"\x07\x08V2\x08\x07"
V1_KEY = b"cfcd208495d565ef"

# 明文可能的开头
_MAGICS = {
    b"\xff\xd8\xff": "jpg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"RIFF": "webp",
    b"BM": "bmp",
}


# --------------------------------------------------------------------------- #
def image_ext(data: bytes) -> Optional[str]:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"GIF8":
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    return None


def plain_ext(data: bytes) -> Optional[str]:
    """非图片的普通附件类型。"""
    if image_ext(data):
        return image_ext(data)
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        return "zip"
    if data[4:8] == b"ftyp":
        return "mp4"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return "mp3"
    if data[:4] == b"\x00\x00\x00\x01" or data[:4] == b"\x00\x00\x00\x18":
        return "mp4"
    return None


def align16(n: int) -> int:
    return (n // 16 + 1) * 16


# --------------------------------------------------------------------------- #
def detect_xor_key(files: List[str]) -> int:
    """从一批 V2 文件里统计出异或字节。

    对 JPEG 而言明文最后两字节是 ``FF D9``，所以 key = 密文末字节 ^ 0xD9。
    """
    cnt = collections.Counter()
    for f in files:
        try:
            with open(f, "rb") as fh:
                head = fh.read(15)
                if len(head) < 15 or head[:6] != V2_MAGIC:
                    continue
                _aes, xor_size = struct.unpack("<II", head[6:14])
                if xor_size <= 0:
                    continue
                fh.seek(-1, os.SEEK_END)
                last = fh.read(1)
        except OSError:
            continue
        if last:
            cnt[last[0] ^ 0xD9] += 1
    return cnt.most_common(1)[0][0] if cnt else 0x37


def decrypt_dat(data: bytes, v2key: bytes, xorkey: int) -> Optional[bytes]:
    """解开一个 .dat，返回明文（可能是图片，也可能是 wxgf 容器）。"""
    if data[:6] == V2_MAGIC:
        key = v2key
    elif data[:6] == V1_MAGIC:
        key = V1_KEY
    else:
        # 纯异或：用图片文件头反推 key
        for magic in (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF"):
            if len(data) < len(magic):
                continue
            k = data[0] ^ magic[0]
            if all(data[i] ^ k == m for i, m in enumerate(magic)):
                return bytes(b ^ k for b in data)
        return None

    aes_size, xor_size = struct.unpack("<II", data[6:14])
    payload = data[15:]
    aligned = align16(aes_size)
    if aligned > len(payload):
        return None
    pt = AES.new(key, AES.MODE_ECB).decrypt(payload[:aligned])[:aes_size]
    raw_end = len(payload) - xor_size
    raw = payload[aligned:raw_end] if raw_end > aligned else b""
    tail = bytes(b ^ xorkey for b in payload[raw_end:]) if xor_size else b""
    return pt + raw + tail


# --------------------------------------------------------------------------- #
def unwrap_wxgf(data: bytes) -> Tuple[bytes, Optional[str]]:
    """wxgf 容器里通常嵌着一张普通 JPEG/PNG，能直接抠出来。"""
    if data[:4] != b"wxgf":
        return data, None
    end = min(len(data), 4096)
    for i in range(4, end):
        if data[i:i + 3] == b"\xff\xd8\xff":
            return data[i:], "jpg"
        if data[i:i + 4] == b"\x89PNG":
            return data[i:], "png"
    return data, "wxgf"


def hevc_to_jpeg(data: bytes, quality: int = 92) -> Optional[bytes]:
    """把 wxgf 里的 HEVC 码流解码成 JPEG。需要 PyAV。"""
    try:
        import av
    except Exception:
        return None
    i = data.find(b"\x00\x00\x00\x01")
    if i < 0:
        i = data.find(b"\x00\x00\x01")
    if i < 0:
        return None
    try:
        container = av.open(io.BytesIO(data[i:]), format="hevc")
        for frame in container.decode(video=0):
            img = frame.to_image()
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            return buf.getvalue()
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------- #
class ImageIndex:
    """某个会话媒体目录的图片索引：``message md5 -> (源文件, 明文, 扩展名)``。

    优先用原图，原图没缓存时退回 ``_t.dat`` 缩略图。
    """

    def __init__(self, media_dir: str, v2key: bytes, xorkey: Optional[int] = None):
        self.dir = media_dir
        self.v2key = v2key
        self.xorkey = xorkey if xorkey is not None else 0x37
        self.by_md5: Dict[str, Tuple[str, bytes, str]] = {}
        self.thumbs: Dict[str, Tuple[str, bytes, str]] = {}
        self.scanned = 0
        self.failed = 0
        self.hevc_decoded = 0

    # -------------------------------------------------------------- #
    def _collect(self):
        full, thumb = [], []
        if not os.path.isdir(self.dir):
            return full, thumb
        for r, _dirs, fs in os.walk(self.dir):
            for x in fs:
                if not x.endswith(".dat"):
                    continue
                p = os.path.join(r, x)
                (thumb if x.endswith("_t.dat") else full).append(p)
        return sorted(full), sorted(thumb)

    def _one(self, path: str):
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            return None
        pt = decrypt_dat(raw, self.v2key, self.xorkey)
        if not pt:
            return None
        data, forced = unwrap_wxgf(pt)
        if forced == "wxgf":
            jpg = hevc_to_jpeg(data)
            if jpg:
                data, forced = jpg, "jpg"
                self.hevc_decoded += 1
        ext = forced or image_ext(data)
        if not ext or ext == "wxgf":
            if forced == "wxgf":
                return path, pt, pt, "wxgf"
            return None
        return path, pt, data, ext

    def build(self):
        full, thumb = self._collect()
        if not self.xorkey or self.xorkey == 0x37:
            self.xorkey = detect_xor_key(full + thumb)
        for p in full:
            self.scanned += 1
            r = self._one(p)
            if not r:
                self.failed += 1
                continue
            path, pt, data, ext = r
            self.by_md5[hashlib.md5(pt).hexdigest()] = (path, data, ext)
        for p in thumb:
            self.scanned += 1
            r = self._one(p)
            if not r:
                self.failed += 1
                continue
            path, pt, data, ext = r
            md5 = hashlib.md5(pt).hexdigest()
            if md5 not in self.by_md5:
                # 标记来源是缩略图，渲染时给出提示
                self.thumbs[md5] = (path, data, ext + "#thumb")
        return self

    def get(self, md5: str):
        hit = self.by_md5.get(md5)
        if hit:
            return hit
        return self.thumbs.get(md5)


# --------------------------------------------------------------------------- #
def extra_attachments(media_dir: str) -> List[Tuple[str, str]]:
    """目录里已有的普通附件（PDF/ZIP/MP4……），返回 ``(路径, 扩展名)``。"""
    out = []
    if not os.path.isdir(media_dir):
        return out
    for r, _dirs, fs in os.walk(media_dir):
        for x in fs:
            p = os.path.join(r, x)
            ext = os.path.splitext(x)[1].lower().lstrip(".")
            if ext in ("pdf", "zip", "mp4", "mov", "doc", "docx", "xls", "xlsx",
                       "ppt", "pptx", "txt", "amr", "silk", "mp3", "wav"):
                out.append((p, ext))
    return out
