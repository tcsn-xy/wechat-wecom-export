"""联系人、群名与群成员名字的解析。

微信 4.x 把名字分散在三个地方：

* ``contact`` 表        —— 昵称 / 备注 / 微信号
* ``chat_room`` 的 ext_buffer      —— 群成员 ``wxid -> 群昵称`` 的 protobuf 片段
* ``chat_room_info_detail`` 的 ext_buffer_ —— 同上，另一份快照

群自己的显示名在 ``contact.nick_name`` 里；为空时退回群 ID。
"""
from __future__ import annotations

import os
from typing import Dict, Optional, Set

from . import keys


# 显示名的取值优先级（可用环境变量 WXEXPORT_NAME_ORDER 覆盖）
#   默认 remark,room_nick,nick,alias —— 依据是微信自己生成的系统消息里
#   用的就是备注，群昵称一次都没出现过。
NAME_PRIORITY = tuple(
    x for x in os.environ.get(
        "WXEXPORT_NAME_ORDER", "remark,room_nick,nick,alias").split(",") if x)


def _clean(s) -> str:
    if s is None:
        return ""
    if isinstance(s, bytes):
        s = s.decode("utf-8", "replace")
    return str(s).strip()


def _parse_ext_buffer(blob: bytes, out: Dict[str, str], ids: Optional[set] = None):
    """从群成员的 protobuf 碎片里抠 wxid 和群昵称。

    形如 ``\\x13wxid_xxx`` 是成员 ID，紧跟的 ``\\x12<len><名字>`` 是群昵称。
    注意：**很多成员没有群昵称**，所以只按 ``\\x12`` 解析会严重少算人数，
    这里把所有出现过的 wxid 都收进 ``ids``。
    """
    if not blob:
        return
    data = bytes(blob)
    i = 0
    while True:
        i = data.find(b"\x13wxid_", i)
        if i < 0:
            break
        end = i + 1
        while end < len(data) and 32 <= data[end] < 127:
            end += 1
        wxid = data[i + 1:end].decode("ascii", "ignore")
        if wxid and ids is not None:
            ids.add(wxid)
        if end + 1 < len(data) and data[end] == 0x12:
            ln = data[end + 1]
            try:
                nm = data[end + 2:end + 2 + ln].decode("utf-8").strip()
                # 名字里不该出现控制字符，过滤掉误判
                if nm and not any(ord(ch) < 32 for ch in nm):
                    out.setdefault(wxid, nm)
            except Exception:
                pass
        i = end


class Directory:
    """一次性载入联系人/群信息，供导出时反复查询。

    名字分两层，**不能混**：

    * ``names``      —— 联系人层面的微信昵称/备注，全局唯一；
    * ``room_names`` —— **每个群各自的群昵称**，同一个人在不同群里可以完全不同。

    这点很容易写错：群昵称只对设置了它的那个群有效。早期版本把各群的群昵称
    合并成一张全局表，结果在 A 群的昵称会串到 B 群去。
    """

    def __init__(self, contact_db: str, capture_path: str):
        self.names: Dict[str, str] = {}          # wxid -> 微信昵称/备注
        self.rooms: Dict[str, str] = {}          # roomid -> 群名
        self.room_owner: Dict[str, str] = {}     # roomid -> 群主 wxid
        self.members: Dict[str, list] = {}       # roomid -> [wxid]
        self.room_names: Dict[str, Dict[str, str]] = {}  # roomid -> {wxid: 群昵称}
        self.alias: Dict[str, str] = {}          # wxid -> 微信号
        self.nicks: Dict[str, str] = {}          # wxid -> 微信昵称
        self.remarks: Dict[str, str] = {}        # wxid -> 备注
        self._load(contact_db, capture_path)

    # ---------------------------------------------------------------- #
    def _load(self, contact_db: str, capture_path: str):
        kr = keys.Keyring(capture_path)
        c = kr.connect(contact_db)

        for u, nick, remark, alias in c.execute(
                "SELECT username, nick_name, remark, alias FROM contact"):
            u = _clean(u)
            if not u:
                continue
            nick, remark, alias = _clean(nick), _clean(remark), _clean(alias)
            if alias:
                self.alias[u] = alias
            if nick:
                self.nicks[u] = nick
            if remark:
                self.remarks[u] = remark
            nm = remark or nick or alias
            if nm:
                self.names[u] = nm

        # 好友表里的备注也可能比 contact 更全
        for u, nick, remark in c.execute(
                "SELECT username, nick_name, remark FROM stranger"):
            u = _clean(u)
            if not u:
                continue
            nick, remark = _clean(nick), _clean(remark)
            if nick:
                self.nicks.setdefault(u, nick)
            if remark:
                self.remarks.setdefault(u, remark)
                self.names.setdefault(u, remark)

        blobs_by_room: Dict[str, list] = {}
        for u, owner, ext in c.execute(
                "SELECT username, owner, ext_buffer FROM chat_room"):
            u = _clean(u)
            if not u:
                continue
            self.room_owner[u] = _clean(owner)
            self.rooms[u] = self.names.get(u, "")
            blobs_by_room.setdefault(u, []).append(ext)

        try:
            for u, ext in c.execute(
                    "SELECT username_, ext_buffer_ FROM chat_room_info_detail"):
                u = _clean(u)
                if u:
                    blobs_by_room.setdefault(u, []).append(ext)
        except Exception:
            pass

        for room, blobs in blobs_by_room.items():
            got: Dict[str, str] = {}
            ids: set = set()
            for b in blobs:
                if b:
                    _parse_ext_buffer(b, got, ids)
            # 群昵称只属于这个群，绝不能并到全局表里去
            self.room_names[room] = got
            if ids:
                self.members[room] = sorted(ids)

        try:
            for room_id, member_id in c.execute(
                    "SELECT room_id, member_id FROM chatroom_member"):
                rid, mid = _clean(room_id), _clean(member_id)
                if rid and mid:
                    self.members.setdefault(rid, [])
                    if mid not in self.members[rid]:
                        self.members[rid].append(mid)
        except Exception:
            pass

        c.close()

    # ---------------------------------------------------------------- #
    def display(self, wxid: str, room: Optional[str] = None,
                fallback: Optional[str] = None) -> str:
        """某人在某个群里的显示名。

        优先级（用真实数据验证过，见下）：

            备注 > 群昵称 > 微信昵称 > 微信号 > wxid

        验证方法：微信自己生成的系统消息（「谁邀请谁入群」这类
        ``<sysmsg>``）里出现的名字，就是微信实际展示的名字。统计本机数据发现
        备注出现 168 次、群昵称出现 0 次的成员，说明 **备注会盖过群昵称**；
        没设备注的成员才轮到群昵称，两者都没有才用微信昵称。

        想改成「群昵称优先」，把下面的 NAME_PRIORITY 调换顺序即可。
        """
        if not wxid:
            return fallback or "未知"
        rn = self.room_names.get(room, {}).get(wxid, "") if room else ""
        for order in NAME_PRIORITY:
            v = {"remark": self.remarks.get(wxid, ""),
                 "room_nick": rn,
                 "nick": self.nicks.get(wxid, ""),
                 "alias": self.alias.get(wxid, "")}[order]
            if v:
                return v
        return fallback or wxid

    def nickname_in(self, room: str, wxid: str) -> str:
        """只用群昵称，没有就返回空串。"""
        return self.room_names.get(room, {}).get(wxid, "")

    def profile(self, wxid: str, room: Optional[str] = None) -> Dict[str, str]:
        """一个人的全部身份字段，用来落成成员名单。

        字段含义（别搞混）：

        ``wxid``      微信内部 ID，形如 ``wxid_example``，永远有
        ``alias``     微信号（自己设的登录 ID），形如 ``example_alias``，
                      **只有好友才有**，陌生人微信不给
        ``room_nick`` 群昵称，**只有在这个群显式设过才有**
        ``nick``      微信昵称
        ``remark``    你给他设的备注，只有好友才有
        ``shown``     群里实际显示的名字 = 备注 → 群昵称 → 微信昵称 → 微信号
        ``explicit_room_nick`` 群昵称是不是显式设的
        """
        return {
            "wxid": wxid,
            "shown": self.display(wxid, room),
            "room_nick": self.nickname_in(room, wxid) if room else "",
            "explicit_room_nick": "1" if (room and self.nickname_in(room, wxid)) else "0",
            "nick": self.nicks.get(wxid, ""),
            "remark": self.remarks.get(wxid, ""),
            "alias": self.alias.get(wxid, ""),
        }

    def room_title(self, chat_id: str) -> str:
        nm = self.rooms.get(chat_id) or self.names.get(chat_id)
        return nm or ""

    def member_count(self, room_id: str) -> int:
        return len(self.members.get(room_id, []))

    def describe(self, chat_id: str, self_wxid: str) -> Dict:
        is_room = chat_id.endswith("@chatroom")
        title = self.room_title(chat_id)
        return {
            "chat_id": chat_id,
            "is_room": is_room,
            "title": title or ("未命名群聊" if is_room else chat_id),
            "owner": self.room_owner.get(chat_id, ""),
            "members": self.member_count(chat_id),
            "member_ids": self.members.get(chat_id, []),
            "alias": self.alias.get(chat_id, ""),
        }
