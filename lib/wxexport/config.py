"""路径、账号与快照信息的自动发现。

所有模块都通过 :func:`load` 拿到一个 :class:`Paths`，不要在别处硬编码路径。
"""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

# --------------------------------------------------------------------------- #
# 项目根目录：lib/wxexport/config.py -> lib/wxexport -> lib -> <项目根>
# --------------------------------------------------------------------------- #
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB = os.path.join(ROOT, "lib")
BIN = os.path.join(ROOT, "bin")
SCRIPTS = os.path.join(ROOT, "scripts")
LLDB_DIR = os.path.join(ROOT, "lldb")
DOCS = os.path.join(ROOT, "docs")
KEYS = os.path.join(ROOT, "keys")
DATA = os.path.join(ROOT, "data")
EXPORT = os.path.join(ROOT, "export")

CAPTURE_FILE = os.path.join(KEYS, "capture.jsonl")
SNAPSHOT_FILE = os.path.join(DATA, "_snapshot.json")

# 微信自己的沙盒
CONTAINER = os.path.expanduser(
    "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
)

# 旧版本（迁移前）落到 ~/Downloads 的一次性产物，用于自动认领
LEGACY_DUMP = os.path.expanduser("~/Downloads/wx_dump")
LEGACY_EXTRACT = os.path.expanduser("~/wechat-extract")

APP_CANDIDATE_DIRS = [
    "/Applications",
    os.path.expanduser("~/Applications"),
]

SNAPSHOT_VERSION = 1


@dataclass
class Paths:
    """一次导出任务会用到的全部路径。"""

    root: str = ROOT
    # 原始快照（从微信沙盒里克隆出来的只读副本）
    data: str = DATA
    db_storage: str = os.path.join(DATA, "db_storage")
    msg: str = os.path.join(DATA, "msg")
    attach: str = os.path.join(DATA, "msg", "attach")
    ilink: str = os.path.join(DATA, "ilink")
    # 密钥
    capture: str = CAPTURE_FILE
    # 成品
    export: str = EXPORT
    # 快照元信息
    account_dir: Optional[str] = None
    account_path: Optional[str] = None
    wxid: Optional[str] = None
    uin: Optional[str] = None
    wechat_app: Optional[str] = None
    cloned_at: Optional[str] = None
    nickname: Optional[str] = None

    # ---------------------------------------------------------------- #
    @property
    def passphrase_file(self) -> str:
        return self.capture

    def session_db(self) -> str:
        return os.path.join(self.db_storage, "session", "session.db")

    def contact_db(self) -> str:
        return os.path.join(self.db_storage, "contact", "contact.db")

    def emoticon_db(self) -> str:
        return os.path.join(self.db_storage, "emoticon", "emoticon.db")

    def message_dbs(self):
        """按序返回所有消息库；message_1 是更早的历史分片。"""
        d = os.path.join(self.db_storage, "message")
        return self._shards(d, "message")

    def biz_message_dbs(self):
        d = os.path.join(self.db_storage, "message")
        return self._shards(d, "biz_message")

    @staticmethod
    def _shards(directory, prefix):
        if not os.path.isdir(directory):
            return []
        found = [(int(m.group(1)), os.path.join(directory, n))
                 for n in os.listdir(directory)
                 if (m := re.fullmatch(re.escape(prefix) + r"_(\d+)\.db", n))]
        return [p for _, p in sorted(found)]

    def chat_attach_dir(self, chat_id: str) -> str:
        """某个会话的媒体缓存目录，命名为 md5(chat_id)。"""
        import hashlib
        return os.path.join(self.attach, hashlib.md5(chat_id.encode()).hexdigest())

    def ensure_export(self):
        os.makedirs(self.export, exist_ok=True)
        return self.export


# --------------------------------------------------------------------------- #
# 发现逻辑
# --------------------------------------------------------------------------- #
def find_wechat_app() -> Optional[str]:
    """在常见位置找 WeChat.app。"""
    for base in APP_CANDIDATE_DIRS:
        if not os.path.isdir(base):
            continue
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for n in names:
            if n in ("WeChat.app", "微信.app"):
                return os.path.join(base, n)
        # 微信有时被装在带后缀的目录里（例如「未命名文件夹 3/WeChat.app」）
        for n in names:
            sub = os.path.join(base, n, "WeChat.app")
            if os.path.isdir(sub):
                return sub
    return None


def container_accounts(container=CONTAINER):
    """列出沙盒里的账号目录（受 TCC 保护时返回空）。"""
    try:
        return sorted(
            os.path.join(container, n)
            for n in os.listdir(container)
            if n.startswith("wxid_") and os.path.isdir(os.path.join(container, n))
        )
    except OSError:
        return []


def wxid_from_account_dir(account_dir: str) -> str:
    """``wxid_xxx_37a7`` -> ``wxid_xxx``。"""
    name = os.path.basename(account_dir.rstrip("/"))
    m = re.match(r"^(wxid_[A-Za-z0-9]+)_[0-9a-f]+$", name)
    return m.group(1) if m else name


def uin_from_ilink(ilink_dir: str, allow_live=True) -> Optional[str]:
    """从 ``*/kvcomm/config.ini`` 的 last_uin 解出 UIN（base64）。

    微信 4.1.x 有两处放 config.ini：账号目录下的 ``ilink/``，以及
    ``Documents/app_data/{ilink,net,...}/kvcomm/config.ini``。旧脚本只在账号目录里
    找，实测 2026-09-29 的账号目录**没有** ilink/，所以这里先按原路径找，找不到就
    退到容器 ``Documents/app_data`` 再搜一遍。
    """
    roots = [ilink_dir]
    app_data = os.path.join(os.path.dirname(os.path.abspath(CONTAINER)),
                            "app_data")
    if allow_live and os.path.isdir(app_data):
        roots.append(app_data)

    def _scan(root: str) -> Optional[str]:
        if not os.path.isdir(root):
            return None
        for dirpath, _dirs, files in os.walk(root):
            if "config.ini" not in files:
                continue
            try:
                with open(os.path.join(dirpath, "config.ini"),
                          encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.startswith("last_uin="):
                            raw = line.split("=", 1)[1].strip()
                            if not raw:
                                continue
                            try:
                                return base64.b64decode(raw).decode()
                            except Exception:
                                return raw
            except OSError:
                continue
        return None

    for root in roots:
        got = _scan(root)
        if got:
            return got
    return None


def read_snapshot(path=SNAPSHOT_FILE) -> dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as exc:
            raise ValueError("快照元信息无法读取：%s" % path) from exc
    return {}


def write_snapshot(info: dict, path=SNAPSHOT_FILE):
    from localio import atomic_json
    info = dict(info)
    info["version"] = SNAPSHOT_VERSION
    atomic_json(path, info)


# --------------------------------------------------------------------------- #
def load(require_snapshot: bool = False, *, snapshot_root=None, capture=None,
         export_root=None) -> Paths:
    """装配一个 :class:`Paths`。缺失的信息尽力自动补齐。"""
    p = Paths()
    if snapshot_root:
        p.data = os.path.abspath(snapshot_root)
        p.db_storage = os.path.join(p.data, "db_storage")
        p.msg = os.path.join(p.data, "msg")
        p.attach = os.path.join(p.msg, "attach")
        p.ilink = os.path.join(p.data, "ilink")
    if capture:
        p.capture = os.path.abspath(capture)
    if export_root:
        p.export = os.path.abspath(export_root)
    from localio import task_lock,recover
    if os.path.isdir(p.data):
        with task_lock(os.path.join(p.data,".snapshot.lock")):
            recover(os.path.join(p.data,".snapshot.switch.json"))
            snap = read_snapshot(os.path.join(p.data, "_snapshot.json"))
    else:
        snap={}

    if not snapshot_root and not os.path.isdir(p.db_storage) and os.path.isdir(
            os.path.join(LEGACY_DUMP, "db_storage")):
        # 旧的一次性产物还在原位：直接沿用，不搬动
        p.data = LEGACY_DUMP
        p.db_storage = os.path.join(LEGACY_DUMP, "db_storage")
        p.msg = os.path.join(LEGACY_DUMP, "msg")
        p.attach = os.path.join(LEGACY_DUMP, "msg", "attach")
        p.ilink = os.path.join(LEGACY_DUMP, "ilink")

    p.account_dir = snap.get("account_dir")
    p.account_path = snap.get("account_path")
    p.wxid = snap.get("wxid")
    p.uin = snap.get("uin")
    p.wechat_app = snap.get("wechat_app")
    p.cloned_at = snap.get("cloned_at")
    p.nickname = snap.get("nickname")

    if not p.wxid and p.account_dir:
        p.wxid = wxid_from_account_dir(p.account_dir)
    if not p.uin:
        p.uin = uin_from_ilink(p.ilink, allow_live=not snapshot_root)
    if not p.account_dir and not snapshot_root:
        accts = container_accounts()
        if len(accts) == 1:
            p.account_dir = os.path.basename(accts[0])
            p.account_path = accts[0]
            p.wxid = p.wxid or wxid_from_account_dir(p.account_dir)
    if not p.wechat_app and not snapshot_root:
        p.wechat_app = find_wechat_app()

    if require_snapshot and not os.path.isdir(p.db_storage):
        raise SystemExit(
            "还没有数据快照。请先运行： python3 scripts/clone_data.py\n"
            "（或把微信数据放到 %s）" % os.path.join(DATA, "db_storage"))
    return p


def media_v2_key(p: Paths) -> Optional[bytes]:
    """图片 .dat V2 的 AES 密钥： md5(uin + wxid) 的前 16 个字符。

    拿不到 UIN/wxid 时返回 None（而不是直接退出）——增量导出只搬 db_storage、
    不搬 msg/attach，本来就不需要图片密钥；导出器会在 ``_media_for_chat`` 里跳过媒体。
    """
    import hashlib
    if not p.uin or not p.wxid:
        return None
    return hashlib.md5((p.uin + p.wxid).encode()).hexdigest()[:16].encode()
