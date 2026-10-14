"""微信本机媒体（图片/文件/视频/语音）的定位与解包。

数据来源与配对键
----------------
| 类型 | 消息里的键 | 本机位置 | 配对方式 |
|---|---|---|---|
| 图片 | ``md5``（消息 XML） | ``msg/attach/<md5(chat_id)>/*.dat`` | 解密后 ``md5(明文)`` == 消息 md5（实测 400/400 成立） |
| 文件 | ``md5``（appmsg） | ``msg/file/<yyyy-MM>/<原名>`` | ``hardlink.db.file_hardlink_info_v4`` 的 ``md5 -> file_name`` |
| 视频 | ``md5``（videomsg） | ``msg/video/<yyyy-MM>/<md5>.mp4`` | ``video_hardlink_info_v4`` + 文件名即 md5 |
| 语音 | ``local_id``+``create_time`` | ``db_storage/message/media_0.db`` 的 ``VoiceInfo`` | 表内直接关联 |

**只读**：所有读取都走 ``open(..., "rb")``，不写源目录。
"""
from __future__ import annotations

import hashlib
import os
from typing import Dict, Optional, Tuple

from wxexport import keys, media as wxmedia


# --------------------------------------------------------------------------- #
class ChatMediaIndex:
    """某个会话媒体目录（``data/msg/attach/<md5(chat_id)>``）的图片索引。

    与 ``wxexport.media.ImageIndex`` 的区别：这里**不常驻明文**，只记
    ``md5 -> (路径, 扩展名, 是否缩略图)``，真正要写库时再按需解密，省内存。
    """

    def __init__(self, media_dir: str, v2key: bytes, xorkey: Optional[int] = None):
        self.dir = media_dir
        self.v2key = v2key
        self.xorkey = xorkey if xorkey is not None else 0x37
        self.by_md5: Dict[str, Tuple[str, str, bool]] = {}
        self.scanned = 0
        self.failed = 0
        self._built = False

    def _all_dats(self):
        full, thumb = [], []
        if not os.path.isdir(self.dir):
            return full, thumb
        for r, _d, fs in os.walk(self.dir):
            for x in fs:
                if not x.endswith(".dat"):
                    continue
                p = os.path.join(r, x)
                (thumb if x.endswith("_t.dat") else full).append(p)
        return sorted(full), sorted(thumb)

    def decrypt(self, path: str) -> Optional[Tuple[bytes, str]]:
        """复用模块级 ``decrypt_file``，避免两份解密实现漂移。"""
        return decrypt_file(path, self.v2key, self.xorkey)

    def build(self):
        if self._built:
            return self
        full,thumb=self._all_dats()
        if not self.xorkey or self.xorkey==0x37:
            self.xorkey=wxmedia.detect_xor_key(full+thumb)
        for is_thumb,files in ((False,full),(True,thumb)):
            for path in files:
                self.scanned+=1
                try:
                    plain=self.original(path)
                except (OSError,ValueError):
                    self.failed+=1
                    continue
                ext=wxmedia.image_ext(plain) or ("wxgf" if plain.startswith(b"wxgf") else "bin")
                md5=hashlib.md5(plain).hexdigest()
                self.by_md5.setdefault(md5,(path,ext,is_thumb))
        self._built=True
        return self

    def get(self,md5):
        hit=self.by_md5.get(md5)
        if not hit:
            return None
        path,ext,thumb=hit
        return self.original(path),ext,thumb,path

    def original(self,path):
        with open(path,"rb") as fh:
            result=wxmedia.decrypt_dat(fh.read(),self.v2key,self.xorkey)
        if not result:
            raise ValueError("图片解密失败：%s" % path)
        return result


# --------------------------------------------------------------------------- #
class GlobalImageIndex:
    """**跨会话**的图片索引：``md5(明文) -> list[.dat 路径]``。

    为什么需要它
    ------------
    微信把图片缓存放在 ``msg/attach/<md5(chat_id)>/`` 下 —— 目录名是**当时的会话**，
    不是"图片属于谁"。用户把一张图从 A 群转发到 B 群、或者从别人那里收到同一张图，
    磁盘上就多一份落在别的会话目录里。只扫本会话目录会漏掉这些（实测本机
    29,835 条图片消息只配上 2,064 张，而 attach 里其实有 17,745 个 .dat）。

    这里只登记「md5 → 路径」（不常驻明文），要写库时再按路径解密一次。
    """

    def __init__(self, attach_root: str, v2key: bytes):
        self.root = attach_root
        self.v2key = v2key
        self.by_md5: Dict[str, list] = {}
        self.path_ext: Dict[str, str] = {}
        self.scanned = 0
        self.failed = 0
        self._built = False
        self._decrypt_cache: Dict[str, Tuple[bytes, str]] = {}

    @staticmethod
    def _all_dats(attach_root: str):
        full, thumb = [], []
        if not os.path.isdir(attach_root):
            return full, thumb
        for r, _d, fs in os.walk(attach_root):
            for x in fs:
                if not x.endswith(".dat"):
                    continue
                p = os.path.join(r, x)
                (thumb if x.endswith("_t.dat") else full).append(p)
        return sorted(full), sorted(thumb)

    def build(self):
        if self._built:
            return self
        full, thumb = self._all_dats(self.root)
        self.xorkey = wxmedia.detect_xor_key((full + thumb)[:4000])
        for p in full + thumb:
            self.scanned += 1
            if not self._decrypt_ext(p):
                self.failed += 1
        self._built = True
        return self

    def _decrypt_ext(self, path: str):
        """解密一个 .dat，登记 ``md5(明文) -> 路径``；返回扩展名（失败返回 None）。"""
        if path in self.path_ext:
            return self.path_ext[path] or None
        got = decrypt_file(path, self.v2key, self.xorkey)
        if not got:
            self.path_ext[path] = ""
            return None
        data, ext = got
        self.by_md5.setdefault(hashlib.md5(data).hexdigest(), []).append(path)
        self.path_ext[path] = ext
        return ext

    def get(self, md5: str):
        """``md5 -> (明文, 扩展名, 是否缩略图, 路径)``；没有返回 None。"""
        paths = self.by_md5.get(md5)
        if not paths:
            return None
        for p in sorted(paths, key=lambda x: x.endswith("_t.dat")):   # 原图优先
            hit = self._cached_or_read(p)
            if hit:
                return hit
        return None

    def _cached_or_read(self, path: str):
        if path in self._decrypt_cache:
            data, ext = self._decrypt_cache[path]
            return data, ext, path.endswith("_t.dat"), path
        got = decrypt_file(path, self.v2key, getattr(self, "xorkey", 0x70))
        if not got:
            got = decrypt_file(path, self.v2key, 0x37)
        if not got:
            return None
        data, ext = got
        # 限一下内存：本机单张图片最大约 15MB，400 条可能堆到几 GB，所以取小值
        if len(self._decrypt_cache) > 120:
            self._decrypt_cache.clear()
        self._decrypt_cache[path] = (data, ext)
        return data, ext, path.endswith("_t.dat"), path


def decrypt_file(path: str, v2key: bytes, xorkey: int):
    """读 + 解一个 .dat，返回 ``(明文, 扩展名)``。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    pt = wxmedia.decrypt_dat(raw, v2key, xorkey)
    if not pt:
        return None
    data, forced = wxmedia.unwrap_wxgf(pt)
    if forced == "wxgf":
        jpg = wxmedia.hevc_to_jpeg(data)
        if jpg:
            data, forced = jpg, "jpg"
    ext = forced or wxmedia.image_ext(data)
    if not ext or ext == "wxgf":
        return None
    return data, ext


# --------------------------------------------------------------------------- #
class MediaRoot:
    """``data/msg`` 级别的索引：文件/视频的 md5 → 实际路径。

    实测（2026-09-29）微信 ``msg/video`` 的文件名有两种：
    ``<md5>.mp4`` 与 ``<md5>_raw.mp4``（后者是原始码流，**文件名里也带 md5**，
    但前面的 ``_raw`` 后缀让"按 32 位 hex 当文件名"的写法匹配不到 —— 踩过）。
    所以这里既登记「文件名里的 md5」，也登记「前 8MB 内容 md5」兜底：
    抽查 60 个视频：文件名命中 36、内容 md5 命中兜住剩下的可见样本。
    """

    SCAN_HEAD = 8 * 1024 * 1024

    def __init__(self, msg_dir: str, hardlink_db: Optional[str] = None,
                 capture: Optional[str] = None):
        self.msg_dir = msg_dir
        self.hardlink_db = hardlink_db
        self.capture = capture
        self._by_stem: Dict[str, str] = {}
        self._hardlink: Dict[str, str] = {}     # md5 -> file_name
        self._scanned = set()
        self._content_md5: Dict[str, str] = {}
        self._content_built: set = set()

    # -------------------------------------------------------------- #
    @staticmethod
    def _stem_keys(name: str):
        """文件名里能当 md5 用的键：``<md5>.mp4``、``<md5>_raw.mp4``、``<md5>``。"""
        stem = os.path.splitext(name)[0]
        keys = []
        if len(stem) >= 32:
            head = stem[:32].lower()
            if all(ch in "0123456789abcdef" for ch in head):
                keys.append(head)
        if len(stem) == 32 and all(ch in "0123456789abcdef" for ch in stem.lower()):
            keys.append(stem.lower())
        return keys

    def _scan(self, kind: str):
        """``kind`` 取 ``file`` / ``video``，把文件名里的 md5 与完整文件名都登记。"""
        if kind in self._scanned:
            return
        self._scanned.add(kind)
        root = os.path.join(self.msg_dir, kind)
        if not os.path.isdir(root):
            return
        for r, _d, fs in os.walk(root):
            for x in fs:
                if x.startswith(".") or x.endswith("_thumb.jpg"):
                    continue
                p = os.path.join(r, x)
                for k in self._stem_keys(x):
                    self._by_stem.setdefault(k, p)
                self._by_stem.setdefault(x, p)

    def load_hardlink(self):
        """读 ``hardlink.db`` 的 file/video 索引：md5 → file_name。"""
        if self._hardlink or not self.hardlink_db or not os.path.exists(self.hardlink_db):
            return self._hardlink
        kr = keys.Keyring(self.capture) if self.capture else keys.Keyring(
            os.path.join(os.path.dirname(self.hardlink_db), "..", "..", "keys", "capture.jsonl"))
        try:
            c = kr.connect(self.hardlink_db)
        except Exception:
            return self._hardlink
        for tbl in ("file_hardlink_info_v4", "video_hardlink_info_v4"):
            try:
                for md5, name in c.execute(
                        "SELECT md5, file_name FROM %s" % tbl):
                    if md5:
                        self._hardlink[str(md5).lower()] = str(name or "")
            except Exception:
                continue
        return self._hardlink

    def find_any(self, md5: str, file_name: str = "") -> Optional[str]:
        """跨 ``attach`` / ``file`` / ``video`` 找。

        实测有文件被放进"另一个类型"的目录（同一个 md5 既在 ``msg/file`` 又在
        ``msg/video`` 都出现过），所以按类型找不到时再跨目录兜一层，宁可多找不可漏。
        """
        for kind in ("file", "video", "attach"):
            if kind == "attach":
                hit = self._find_in_attach(md5, file_name)
            else:
                hit = self.find(kind, md5, file_name)
            if hit:
                return hit
        return None

    def _find_in_attach(self, md5: str, file_name: str = "") -> Optional[str]:
        """``msg/attach`` 下是加密 .dat，但偶尔也会有明文文件落地。

        同样走一次性内容索引（见 ``_content_index``），不要每次重扫。
        """
        md5 = (md5 or "").lower()
        if not md5:
            return None
        if md5 in self._content_md5:
            return self._content_md5[md5]
        self._content_index("attach")
        return self._content_md5.get(md5)

    def _content_index(self, kind: str):
        """把 ``msg/<kind>/`` 下每个文件的前 8MB md5 建成一张表，**只建一次**。

        ⚠️ 早先是"每次找不到就把整个目录扫一遍算 md5"——微信有 27,000+ 张图片
        配不到时，那就是 O(缺失数 × 文件数) 次读盘，实测十几分钟都不回来。
        现在一次性建索引，后续查询是字典命中。
        """
        if kind in self._content_built:
            return
        self._content_built.add(kind)
        root = os.path.join(self.msg_dir, kind)
        if not os.path.isdir(root):
            return
        for r, _d, fs in os.walk(root):
            for x in fs:
                if x.startswith(".") or x.endswith("_thumb.jpg"):
                    continue
                p = os.path.join(r, x)
                for k in self._stem_keys(x):
                    self._by_stem.setdefault(k, p)
                try:
                    with open(p, "rb") as f:
                        h = hashlib.md5(f.read(self.SCAN_HEAD)).hexdigest()
                except OSError:
                    continue
                self._content_md5.setdefault(h, p)

    def find(self, kind: str, md5: str, file_name: str = "") -> Optional[str]:
        """按 md5 / 原文件名在 ``msg/<kind>/`` 里找实际文件。"""
        if not md5 and not file_name:
            return None
        self._scan(kind)
        md5 = (md5 or "").lower()
        if md5 and md5 in self._by_stem:
            return self._by_stem[md5]
        # 硬链接表里的 file_name（可能是 ``<md5>_raw.mp4``）直接用完整名找
        if file_name:
            hit = self._by_stem.get(file_name)
            if hit:
                return hit
            for k in self._stem_keys(file_name):
                if k in self._by_stem:
                    return self._by_stem[k]
        # 兜底：内容 md5 索引
        if md5:
            if md5 in self._content_md5:
                return self._content_md5[md5]
            self._content_index(kind)
            if md5 in self._content_md5:
                return self._content_md5[md5]
        return None


def sha1_of(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def guess_ext(path: str, data: bytes = b"") -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext:
        return ext
    if data:
        return wxmedia.plain_ext(data) or "bin"
    return "bin"


def probe_image(data: bytes):
    """``(width, height)``；Pillow 不在就当拿不到。"""
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None, None


def probe_video_ms(path: str) -> Optional[int]:
    """用本地 ffmpeg 读时长（毫秒）。绝对路径优先（``/opt/homebrew/bin/ffmpeg`` 软链已断）。"""
    import json
    import shutil
    import subprocess
    exe = shutil.which("ffmpeg")
    if not exe or not os.path.exists(exe):
        for cand in ("/opt/homebrew/Cellar/ffmpeg/8.1.2_1/bin/ffprobe",
                     "/opt/homebrew/bin/ffprobe"):
            if os.path.exists(cand):
                exe = cand
                break
    if not exe or not os.path.exists(exe):
        return None
    if "ffprobe" not in exe:
        base = os.path.join(os.path.dirname(exe), "ffprobe")
        if os.path.exists(base):
            exe = base
    try:
        r = subprocess.run([exe, "-v", "quiet", "-print_format", "json",
                            "-show_format", path],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None
        info = json.loads(r.stdout or "{}")
        dur = info.get("format", {}).get("duration")
        return int(float(dur) * 1000) if dur else None
    except Exception:
        return None
