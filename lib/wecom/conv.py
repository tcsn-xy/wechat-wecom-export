"""解析 CEF 侧的明文会话快照 ``conv_snapshot``。

企业微信的聊天窗口是 CEF 页面，原生侧会把「会话列表 + 每个会话最后一条消息」
以 **明文 protobuf** 写到::

    ~/Library/Containers/com.tencent.WeWorkMac/Data/Documents/Profiles/<id>/conv_snapshot

文件布局：``[u32 长度][32 字节 hex][u32 载荷长度][protobuf]``。
顶层是一串 field 1（每个 = 一个会话）：

===== ==========================================
字段   含义（实测）
===== ==========================================
1     会话 id（int64）
2     会话对象 id
3     会话显示名
6     最后消息时间（unix 秒）
10    最后一条消息：1=消息id, 4=发送者id, 8=发送时间,
      10.1.2.1=正文文本, 14=内容类型, 15.4=发送者显示名
===== ==========================================

这份快照的好处是**明文、不需要数据库密钥**；坏处是只有每个会话最后一条消息。
完整历史还是要解开 ``message.db``（见 ``wecom.crypto`` / ``wecom.keys``）。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from . import pbwire


@dataclass
class LastMessage:
    message_id: int = 0
    sender_id: int = 0
    sender_name: str = ""
    send_time: int = 0
    content_type: int = 0
    text: str = ""


@dataclass
class Conversation:
    conv_id: int
    name: str
    peer_id: int = 0
    last_time: int = 0
    last: LastMessage | None = None
    extra_texts: list = field(default_factory=list)
    account_id: str = ""


def _content_text(block) -> tuple:
    """从最后消息块里挖出「正文文本 + 内容类型」。"""
    ctype = block.num(14)
    wrap = block.sub(10)
    if not wrap:
        return "", ctype
    inner = wrap.sub(1)
    candidates = []
    if inner:
        container = inner.sub(2)
        if container:
            for f in container.all(1):
                if isinstance(f.value, str):
                    candidates.append(f.value)
        for f in inner.fields:
            if isinstance(f.value, str) and f.value not in candidates:
                candidates.append(f.value)
    else:
        for f in wrap.fields:
            if isinstance(f.value, str) and f.value not in candidates:
                candidates.append(f.value)
    return (candidates[0] if candidates else ""), ctype


def parse_bytes(blob: bytes) -> list:
    if len(blob) < 8:
        return []
    n = struct.unpack_from("<I", blob, 0)[0]
    off = 4 + n
    if off + 4 > len(blob):
        return []
    plen = struct.unpack_from("<I", blob, off)[0]
    payload = blob[off + 4: off + 4 + plen]
    msg = pbwire.parse(payload)

    out = []
    for entry in msg.all(1):
        m = entry.value
        if not isinstance(m, pbwire.Message):
            continue
        conv = Conversation(conv_id=m.num(1), name=m.text(3),
                            peer_id=m.num(2), last_time=m.num(6))
        block = m.sub(10)
        if block:
            text, ctype = _content_text(block)
            sender = block.sub(15)
            conv.last = LastMessage(
                message_id=block.num(1),
                sender_id=block.num(4),
                sender_name=(sender.text(4) if sender else ""),
                send_time=block.num(8) or conv.last_time,
                content_type=ctype,
                text=text,
            )
        # 顺带收一下这个会话子树里所有其它中文串（引用、公告摘要之类）
        for _path, f in pbwire.walk(m):
            if isinstance(f.value, str) and len(f.value) >= 2 and \
                    any("\u4e00" <= ch <= "\u9fff" for ch in f.value):
                if f.value != conv.name and f.value not in conv.extra_texts:
                    conv.extra_texts.append(f.value)
        out.append(conv)
    return out


def parse_file(path) -> list:
    return parse_bytes(Path(path).read_bytes())


def find_snapshots(raw_root) -> list:
    """在搬出来的快照里找所有 conv_snapshot。"""
    root = Path(raw_root)
    return sorted(root.glob("Profiles/*/conv_snapshot"))


def load_all(raw_root) -> list:
    from .accounts import profile_accounts
    accounts=profile_accounts(raw_root)
    out = []
    for path in find_snapshots(raw_root):
        for conv in parse_file(path):
            conv.account_id=accounts.get(path.parent.name,path.parent.name)
            out.append(conv)
    # 同一个会话可能在多份快照里出现，保留时间最新的
    best = {}
    for conv in out:
        key=(conv.account_id,conv.conv_id)
        old = best.get(key)
        if old is None or (conv.last_time or 0) > (old.last_time or 0):
            best[key] = conv
    return sorted(best.values(), key=lambda c: c.last_time or 0, reverse=True)
