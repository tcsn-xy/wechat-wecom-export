"""从解密后的企业微信库里读会话、成员和消息。

**字段名一律运行时发现，不写死。** 企业微信升级会改 schema（甚至改表名），
所以流程是：``PRAGMA table_info`` 拿到真实列 → 按候选名映射 → 缺列就降级。
想确认真实结构跑 ``scripts/wecom_inspect.py``。

本机 5.0.7 实测：

* ``message.db`` → ``message_table``（17 列，content 是 BLOB：明文 / XML / protobuf 三种）
* ``session.db`` → ``conversation_table``（68 行）、``conversation_user_table``（**本机是空的**）
* ``user.db``    → ``user_table``（53 行，本公司同事）、``wechat_contactV1``（362 行，
  微信联系人）、``external_user_relation_v3``（外部联系人备注）

会话 id 形如 ``R:`` 群、``S:`` 单聊、``M:`` 微信联系人、``Y:``/``O:`` 系统或应用。
"""
from __future__ import annotations

import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from . import pbwire

# 候选列名（按优先级）。企业微信不同版本/不同表用过其中不同的写法。
COLS = {
    "msg_id":     ("message_id", "msg_id", "msgid", "id"),
    "server_id":  ("server_id", "svrid"),
    "sequence":   ("sequence", "seq", "sort_seq"),
    "sender":     ("sender_id", "from_user", "sender", "from_user_name", "talker"),
    "conversation": ("conversation_id", "conv_id", "session_id", "talker_id", "chat_id"),
    "ctype":      ("content_type", "msg_type", "type", "message_type"),
    "time":       ("send_time", "create_time", "msg_time", "timestamp", "time"),
    "content":    ("content", "msg_content", "message_content", "body"),
    "extra":      ("extra_content", "local_extra_content", "extra"),
}
CONV_COLS = {
    "id":   ("id", "conversation_id", "conv_id"),
    "name": ("name", "roomname_remark", "display_name", "title"),
    "time": ("last_message_time", "update_time", "sort_time"),
}
USER_COLS = {
    "id":       ("id", "user_id", "username"),
    "name":     ("name", "nick_name", "nickname"),
    "real_name": ("real_name", "english_name"),
    "account":  ("account", "alias"),
    "corp":     ("external_corp_name", "corp_name"),
    "remark":   ("remarks", "remark", "real_remarks", "corp_remark"),
}
MEMBER_COLS = {
    "conversation": ("conversation_id", "conv_id", "session_id"),
    "user":         ("user_id", "userid", "username", "member_id"),
    "nick":         ("nick_name", "nickname", "room_nick"),
}

# 名字来源：(表名, id 列, [(列, 优先级)])，优先级大的盖小的
NAME_TABLES = (
    ("user_table", ("id",), (("name", 2), ("real_name", 1), ("account", 1))),
    ("wechat_contactV1", ("wxid",), (("name", 2),)),
    ("room_robot", ("robotid",), (("name", 2),)),
    ("external_user_relation_v3", ("user_id",),
     (("remarks", 4), ("real_remarks", 3), ("corp_remark", 3))),
)


# --------------------------------------------------------------------------- #
def open_ro(path) -> sqlite3.Connection:
    """只读打开；明文快照是静止文件，用 immutable 更快也更安全。"""
    conn = sqlite3.connect("file:%s?mode=ro&immutable=1" % quote(str(Path(path).absolute())), uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    conn.row_factory = sqlite3.Row
    return conn


def tables(conn) -> list:
    return [r[0] for r in conn.execute(
        "select name from sqlite_master where type='table' order by name")]


def columns(conn, table: str) -> list:
    return [r[1] for r in conn.execute('pragma table_info("%s")' % table)]


def pick(cols: list, cands) -> str | None:
    low = {c.lower(): c for c in cols}
    for want in cands:
        if want in low:
            return low[want]
    for c in cols:                      # 退一步：包含关系
        for want in cands:
            if want in c.lower():
                return c
    return None


def mapping(conn, table: str, spec: dict) -> dict:
    cols = columns(conn, table)
    return {key: pick(cols, cands) for key, cands in spec.items()}


def find_table(conn, hints) -> str | None:
    names = tables(conn)
    for n in names:
        if n in hints:
            return n
    for n in names:
        for h in hints:
            if h in n.lower():
                return n
    return None


# --------------------------------------------------------------------------- #
# 正文解码
# --------------------------------------------------------------------------- #
_XML_TAG_TEXT = re.compile(r"<([a-zA-Z_]+)>(.*?)</\1>", re.S)
_TYPE_HINTS = [
    (b"<img", "[图片]"), (b"<voice", "[语音]"), (b"<video", "[视频]"),
    (b"<file", "[文件]"), (b"<location", "[位置]"), (b"<emoji", "[表情]"),
    (b"<appmsg", "[链接/应用消息]"), (b"<card", "[名片]"),
    (b"<revoke", "[撤回]"), (b"<sysmsg", "[系统消息]"),
    (b"<weapp", "[小程序]"),
]
_PURE_IDS = re.compile(r"^[0-9;,:\s]+$")


def _utf8_text(blob: bytes) -> str | None:
    try:
        s = blob.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return s if s else None


_RUN = re.compile(rb"[\x20-\x7e\xc2-\xf4][\x20-\x7e\x80-\xbf]{2,}")


def _loose_text(blob: bytes) -> str:
    """兜底：连 protobuf 都解不动时，直接把里面最长的可读串捞出来。

    实测有些类型（14/15）外面套的是十六进制、里面又不是标准 protobuf
    （字段号 0，正规解析器会拒），但正文确实是可读 UTF-8。
    """
    best = ""
    for m in _RUN.finditer(blob):
        try:
            s = m.group(0).decode("utf-8")
        except UnicodeDecodeError:
            continue
        s = s.strip()
        if len(s) > len(best) and not _PURE_IDS.match(s) and not s.startswith("http"):
            best = s
    return best[:2000]


def _text_from_pb(blob: bytes) -> str:
    """从 protobuf 里挑出最像「正文」的那个字符串（嵌得最深、最长的）。"""
    try:
        msg = pbwire.parse(blob)
    except pbwire.PbError:
        return _loose_text(blob)
    cands = []
    for path, f in pbwire.walk(msg):
        if not isinstance(f.value, str):
            continue
        s = f.value.strip()
        if not s or _PURE_IDS.match(s):
            continue
        cands.append((len(path), len(s), s))
    if not cands:
        return _loose_text(blob)
    cands.sort(key=lambda t: (-t[0], -t[1]))
    return cands[0][2][:2000]


def _xml_text(stripped: str) -> str:
    try:
        root = ET.fromstring(stripped)
        for tag in ("title", "des", "content", "text", "nickname", "displayname"):
            for node in root.iter(tag):
                if node.text and node.text.strip():
                    return node.text.strip()
    except ET.ParseError:
        pass
    for tag, value in _XML_TAG_TEXT.findall(stripped):
        if tag in ("title", "des", "content"):
            plain = re.sub(r"<[^>]+>", "", value).strip()
            if plain:
                return plain
    plain = re.sub(r"<[^>]+>", " ", stripped)
    plain = re.sub(r"\s+", " ", plain).strip()
    if plain and not plain.startswith("?"):
        return plain
    # 一句正文都没有（纯媒体/系统 XML）→ 给类型占位符，
    # **绝不把原始 XML 摊进记录里**（个人微信那边踩过这个坑）
    low = stripped.lower()
    for needle, label in _TYPE_HINTS:
        if needle.decode() in low:
            return label
    return "[XML消息]"


def _clean_text(text: str, ctype=None, depth: int = 0) -> str:
    """收尾清理：十六进制外壳再解一层；一串成员 id 换成占位符。"""
    text = (text or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d+(;\d+){2,}", text):
        return "[群成员变动]"
    if (depth < 2 and len(text) >= 16 and len(text) % 2 == 0
            and re.fullmatch(r"[0-9a-fA-F]+", text)):
        try:
            inner = bytes.fromhex(text)
        except ValueError:
            inner = b""
        if inner:
            got = decode_content(inner, ctype, depth + 1)
            if got:
                return got
    return text


def decode_content(blob, ctype=None, depth: int = 0) -> str:
    """把一条消息的 content 变成人能读的一行字。

    企业微信 5.0.7 的 content 有三种形态（实测）：

    * 明文 UTF-8（可能是 XML，也可能就是人话）；
    * protobuf 外壳（常见 ``\\n<正文`` 或 ``\\n\\xda\\x01<十六进制>``）；
    * 纯二进制（图片/文件/语音等，靠 content_type 与 XML 标签给占位符）。

    所以顺序是：控制字节开头 → 先按 protobuf 解；解出来若还是十六进制，再解一层。
    """
    if blob is None:
        return ""
    if isinstance(blob, str):
        blob = blob.encode("utf-8", "replace")
    if not blob:
        return ""

    if depth == 0 and blob[0] < 0x20:
        text = _text_from_pb(blob)
        if text:
            return _clean_text(text, ctype, depth)

    text = _utf8_text(blob)
    if text is not None:
        stripped = text.strip()
        if stripped.startswith("<"):
            return _xml_text(stripped)
        if stripped:
            return _clean_text(stripped, ctype, depth)
        return ""

    low = blob[:64].lower()
    for needle, label in _TYPE_HINTS:
        if needle in low:
            return label
    text = _text_from_pb(blob)
    if text:
        return _clean_text(text, ctype, depth)
    return "[%s]" % ("非文本消息" if ctype is None else "类型 %s" % ctype)


# --------------------------------------------------------------------------- #
@dataclass
class Message:
    conv_id: str
    msg_id: str
    sender_id: str
    sender: str
    time: int
    ctype: str
    text: str
    raw_len: int = 0
    account_id: str = ""
    source_dataset: str = ""
    source_db: str = "message.db"
    source_table: str = ""
    raw_content: bytes | str | None = None
    raw_extra: bytes | str | None = None

    @property
    def stable_id(self):
        from localio import identity
        return identity(self.account_id, self.source_dataset, self.source_db,
                        self.source_table, self.conv_id, self.msg_id)


@dataclass
class Conversation:
    conv_id: str
    name: str
    kind: str = "单聊"
    members: dict = field(default_factory=dict)
    users: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)

    def display(self, uid: str) -> str:
        if not uid:
            return "系统"
        return self.members.get(uid) or self.users.get(uid) or uid


@dataclass
class Dataset:
    path: str
    conversations: dict = field(default_factory=dict)
    users: dict = field(default_factory=dict)
    self_id: str = ""
    notes: list = field(default_factory=list)


def _conv_kind(conv_id: str) -> str:
    cid = str(conv_id)
    if cid.startswith("R:"):
        return "群聊"
    if cid.startswith("S:"):
        return "单聊"
    if cid.startswith(("M:", "O:", "Y:")):
        return "系统/应用"
    return "单聊"


def load_users(files) -> dict:
    """把散在好几张表里的「id → 显示名」拼成一张表。

    本机实测：``user_table`` 只有 53 行（本公司同事），微信联系人、外部联系人
    分别在 ``wechat_contactV1`` / ``external_user_relation_v3`` 里，所以要合并。
    """
    scored = {}
    user_db = files.get("user.db")
    if not user_db:
        return {}
    conn = open_ro(user_db)
    available = set(tables(conn))
    for table, id_cands, name_cols in NAME_TABLES:
        if table not in available:
            continue
        cols = columns(conn, table)
        id_col = pick(cols, id_cands)
        if not id_col:
            continue
        pairs = [(pick(cols, (c,)), prio) for c, prio in name_cols]
        pairs = [(c, p) for c, p in pairs if c]
        if not pairs:
            continue
        sel = ", ".join('"%s"' % c for c, _p in pairs)
        try:
            rows = conn.execute('select "%s", %s from "%s"' % (id_col, sel, table))
        except sqlite3.Error:
            continue
        for row in rows:
            uid = str(row[0] or "")
            if not uid:
                continue
            for (col, prio), value in zip(pairs, list(row)[1:]):
                name = (value or "").strip() if isinstance(value, str) else ""
                if not name or name.lower() == "null":
                    continue
                old = scored.get(uid)
                if old is None or prio > old[0]:
                    scored[uid] = (prio, name)
    conn.close()
    return {uid: name for uid, (_p, name) in scored.items()}


def resolve_conv_name(conv_id: str, raw_name: str, users: dict,
                      hints: dict, self_id: str) -> str:
    """会话显示名：库里的名字 → 快照名字 → 用对方 id 反查通讯录 → 只好用 id。"""
    if raw_name:
        return raw_name
    tokens = [t for t in re.split(r"[^0-9A-Za-z]+", conv_id) if t]
    for t in tokens:
        if t in hints:
            return hints[t]
    for t in tokens:
        if t and t != self_id and t in users:
            return users[t]
    return conv_id


def load_dataset(dataset_dir, name_hints: dict | None = None) -> Dataset:
    """读一个数据集目录（含 message.db / session.db / user.db）。

    ``name_hints``: 明文会话快照里的 {id: 名字}，用来补库里没名字的会话。
    """
    from . import config

    dataset_dir = Path(dataset_dir)
    ds = Dataset(path=str(dataset_dir), users={})
    name_hints = name_hints or {}
    # 目录名形如 example_wecom_user_Data，前缀就是本人 id
    ds.self_id = re.match(r"(\d+)", dataset_dir.name).group(1) \
        if re.match(r"(\d+)", dataset_dir.name) else ""

    files = {p.name: p for p in dataset_dir.rglob("*.db")}
    for core in config.CORE_DBS:
        if core not in files:
            ds.notes.append("缺 %s" % core)

    ds.users = load_users(files)
    ds.notes.append("通讯录 %d 人" % len(ds.users))

    # ---- 会话 ------------------------------------------------------------ #
    session_db = files.get("session.db")
    if session_db:
        try:
            conn = open_ro(session_db)
            table = find_table(conn, ("conversation_table", "conversation"))
            if table:
                m = mapping(conn, table, CONV_COLS)
                sel = ", ".join('"%s"' % c for c in m.values() if c)
                for row in conn.execute('select %s from "%s"' % (sel, table)):
                    vals = [str(v) if v is not None else "" for v in row]
                    rec = dict(zip([k for k, v in m.items() if v], vals))
                    cid = rec.get("id", "")
                    if not cid:
                        cid = "ORPHAN_" + table
                    ds.conversations[cid] = Conversation(
                        conv_id=cid,
                        name=resolve_conv_name(cid, rec.get("name", ""),
                                               ds.users, name_hints, ds.self_id),
                        kind=_conv_kind(cid), users=ds.users)
            mtable = find_table(conn, ("conversation_user_table", "conversation_user"))
            if mtable:
                m = mapping(conn, mtable, MEMBER_COLS)
                if m.get("conversation") and m.get("user"):
                    nick = m.get("nick")
                    sel = '"%s", "%s"%s' % (
                        m["conversation"], m["user"],
                        (', "%s"' % nick) if nick else "")
                    for row in conn.execute('select %s from "%s"' % (sel, mtable)):
                        cid, uid = str(row[0]), str(row[1])
                        name = str(row[2]) if nick and len(row) > 2 and row[2] else ""
                        conv = ds.conversations.get(cid)
                        if conv is None:
                            conv = ds.conversations[cid] = Conversation(
                                conv_id=cid, name=cid, kind=_conv_kind(cid),
                                users=ds.users)
                        if name:
                            conv.members[uid] = name
            conn.close()
        except sqlite3.Error as exc:
            ds.notes.append("session.db 读取失败：%s" % exc)

    # ---- 消息 ------------------------------------------------------------ #
    msg_db = files.get("message.db")
    if msg_db:
        try:
            conn = open_ro(msg_db)
            msg_tables = [t for t in tables(conn)
                          if "message" in t.lower() and not t.startswith("sqlite_")]
            for table in msg_tables:
                m = mapping(conn, table, COLS)
                if not m.get("content") or not m.get("conversation"):
                    if "message" in table and m.get("conversation"):
                        ds.notes.append("表 %s 无 content 列" % table)
                    continue
                wanted = [(k, v) for k, v in m.items() if v]
                sel = ", ".join('"%s"' % v for _k, v in wanted)
                keys = ["_source_rowid"] + [k for k, _v in wanted]
                try:
                    rows = conn.execute('select rowid, %s from "%s"' % (sel, table))
                except sqlite3.Error as exc:
                    raise RuntimeError("表 %s 查询失败" % table) from exc
                n = 0
                for row in rows:
                    rec = dict(zip(keys, row))
                    cid = str(rec.get("conversation") or "")
                    if not cid:
                        continue
                    conv = ds.conversations.get(cid)
                    if conv is None:
                        conv = ds.conversations[cid] = Conversation(
                            conv_id=cid,
                            name=resolve_conv_name(cid, "", ds.users,
                                                   name_hints, ds.self_id),
                            kind=_conv_kind(cid), users=ds.users)
                    raw = rec.get("content")
                    try:
                        t = int(rec.get("time") or 0)
                    except (TypeError, ValueError):
                        t = 0
                    if t > 10 ** 12:
                        t //= 1000
                    msg = Message(
                        conv_id=cid,
                        msg_id=str(rec.get("msg_id") or rec["_source_rowid"]),
                        sender_id=str(rec.get("sender") or ""),
                        sender="",
                        time=t,
                        ctype=str(rec.get("ctype") or ""),
                        text=decode_content(raw, rec.get("ctype")),
                        raw_len=len(raw) if isinstance(raw, (bytes, bytearray)) else 0,
                        account_id=ds.self_id or dataset_dir.name,
                        source_dataset=dataset_dir.name, source_table=table,
                        raw_content=raw, raw_extra=rec.get("extra"),
                    )
                    msg.sender = conv.display(msg.sender_id)
                    conv.messages.append(msg)
                    n += 1
                ds.notes.append("表 %s：%d 条" % (table, n))
            conn.close()
        except sqlite3.Error as exc:
            raise RuntimeError("message.db 读取失败") from exc

    for conv in ds.conversations.values():
        conv.messages.sort(key=lambda x: (x.time, x.msg_id))
    return ds


def name_hints_from_snapshot(raw_root, account=None) -> dict:
    """从明文 conv_snapshot 里取 {id: 会话名}，给上面补名字用。"""
    from . import conv as conv_mod

    hints = {}
    ambiguous=set()
    for c in conv_mod.load_all(raw_root):
        if account and c.account_id!=account:
            continue
        if not c.name:
            continue
        for token in (str(c.peer_id), str(c.conv_id)):
            if token and token != "0":
                if token in hints and hints[token]!=c.name:
                    ambiguous.add(token)
                hints[token] = c.name
    for token in ambiguous:
        hints.pop(token,None)
    return hints


def load_all_plain(plain_root, raw_root=None, with_backups: bool = False) -> list:
    """读明文目录下所有数据集；默认只读当前账号那份。"""
    from . import config, extract

    hints = name_hints_from_snapshot(raw_root or config.RAW)
    out = []
    for path in extract.datasets(plain_root):
        if not with_backups and "Backup" in os.path.basename(path):
            continue
        match=re.match(r"(\d+)",path.name)
        hints=name_hints_from_snapshot(raw_root or config.RAW,match.group(1) if match else path.name)
        ds = load_dataset(path, name_hints=hints)
        ds.notes.insert(0, "会话名提示 %d 个" % len(hints))
        out.append(ds)
    return out
