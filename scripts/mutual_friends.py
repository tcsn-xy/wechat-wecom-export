#!/usr/bin/env python3
"""微信 ↔ 企业微信「共友」与相关聊天记录统计。

口径分三档（越往下越松，**松口径会把同名商家号误算进去**，看结论用第一档）：

* **A 严格**：企微**单聊**（``S:``）的对方 == 微信联系人，且**姓名一致**。
  这是唯一能确证的"同一个人两个 App 都在聊"。
* **B 姓名**：企微联系人的名字 ∩
  微信联系人（备注或昵称）。同名 ≈ 疑似共友，但含大量同名商家/客服号。
* **C 互通消息**：微信里的 ``<数字>@openim`` 会话/发送者（企业微信外部联系人
  在微信里给你的消息）。这批 ID 与企微 ID 不应直接关联。

两个库的 ID 是**不同命名空间**，不能直接对：

    wx_chat.contacts.contact_id   →  wxid_xxx / gh_xxx / @chatroom / v3_…@stranger
    wecom_chat.contacts.contact_id →  数字（企微 userid）/ ozynqs…（企微侧的微信联系人 openid）

``O = 微信里给好友设的微信号(alias)`` 与企微 ``user_table.account/mobile`` 不能用作可靠的跨平台身份关联。

    ./.venv/bin/python scripts/mutual_friends.py
    ./.venv/bin/python scripts/mutual_friends.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from mysql_store import conn as my  # noqa: E402

SELF_WX = os.environ.get("WXEXPORT_SELF_WXID", "")
SELF_WECOM = os.environ.get("WXEXPORT_SELF_WECOM_ID", "")


def _ph(vals):
    return "(" + ",".join("'" + v.replace("'", "''") + "'" for v in vals) + ")"


def collect():
    if not SELF_WECOM:
        raise ValueError("Set WXEXPORT_SELF_WECOM_ID to your own enterprise account ID")
    w = my.connect("wx_chat")
    e = my.connect("wecom_chat")
    out = {}

    # ---------------- A：企微单聊对方 ↔ 微信联系人（姓名一致） ----------------
    wecom_names = {r["contact_id"]: r["real_name"] for r in my.query(
        e, "SELECT `contact_id`,`real_name` FROM `contacts` "
           "WHERE `real_name` IS NOT NULL AND `real_name`<>''")}
    wx_by_name = {}
    for r in my.query(w, "SELECT `contact_id`,`remark`,`nick_name` FROM `contacts`"):
        n = r["remark"] or r["nick_name"]
        if n:
            wx_by_name.setdefault(n, r["contact_id"])

    peers = set()
    for r in my.query(e, "SELECT `chat_id` FROM `chats` WHERE `chat_id` LIKE 'S:%%'"):
        for p in r["chat_id"][2:].split("_"):
            if p and p != SELF_WECOM:
                peers.add(p)
    pairs = []
    for p in sorted(peers):
        name = wecom_names.get(p) or ""
        wxid = wx_by_name.get(name)
        if wxid:
            pairs.append({"wecom_id": p, "name": name, "wxid": wxid})
    rows = []
    for pr in pairs:
        ec = my.scalar(e, "SELECT COUNT(*) FROM `messages` m JOIN `chats` c "
                          "ON c.`chat_pk`=m.`chat_pk` WHERE c.`chat_id` LIKE %s",
                       ("S:%s_%%" % pr["wecom_id"],)) or 0
        eo = my.scalar(e, "SELECT COUNT(*) FROM `messages` m JOIN `chats` c "
                          "ON c.`chat_pk`=m.`chat_pk` WHERE c.`chat_id` LIKE %s "
                          "AND m.`sender_id`=%s",
                       ("S:%s_%%" % pr["wecom_id"], pr["wecom_id"])) or 0
        wc_ = my.scalar(w, "SELECT COUNT(*) FROM `messages` WHERE `chat_id`=%s",
                        (pr["wxid"],)) or 0
        wo = my.scalar(w, "SELECT COUNT(*) FROM `messages` WHERE `chat_id`=%s "
                          "AND `sender_id`=%s", (pr["wxid"], pr["wxid"])) or 0
        wg = my.scalar(w, "SELECT COUNT(*) FROM `messages` m JOIN `chats` c ON "
                          "c.`chat_pk`=m.`chat_pk` WHERE c.`is_room`=1 AND "
                          "m.`sender_id`=%s", (pr["wxid"],)) or 0
        rows.append({**pr, "wecom_msgs": int(ec), "wecom_from_peer": int(eo),
                     "wx_1v1_msgs": int(wc_), "wx_1v1_from_peer": int(wo),
                     "wx_group_msgs": int(wg)})
    out["A_strict"] = {
        "count": len(rows),
        "wecom_1v1_msgs": sum(r["wecom_msgs"] for r in rows),
        "wecom_1v1_from_peer": sum(r["wecom_from_peer"] for r in rows),
        "wx_1v1_msgs": sum(r["wx_1v1_msgs"] for r in rows),
        "wx_1v1_from_peer": sum(r["wx_1v1_from_peer"] for r in rows),
        "wx_group_msgs": sum(r["wx_group_msgs"] for r in rows),
        "rows": rows,
    }

    # ---------------- B：姓名层面的疑似共友 ----------------
    common = sorted(set(wecom_names.values()) & set(wx_by_name))
    wx_ids = [wx_by_name[n] for n in common]
    if wx_ids:
        ph = _ph(wx_ids)
        b = {
            "count": len(common),
            "wx_msgs_from_them": int(my.scalar(
                w, "SELECT COUNT(*) FROM `messages` WHERE `sender_id` IN %s"
                % ph) or 0),
            "wx_1v1_convs": int(my.scalar(w, """
                SELECT COUNT(*) FROM `chats` c WHERE c.`is_room`=0 AND EXISTS
                (SELECT 1 FROM `messages` m WHERE m.`chat_pk`=c.`chat_pk`
                 AND m.`sender_id` IN %s)""" % ph) or 0),
            "wx_group_convs": int(my.scalar(w, """
                SELECT COUNT(DISTINCT c.`chat_id`) FROM `messages` m
                JOIN `chats` c ON c.`chat_pk`=m.`chat_pk`
                WHERE c.`is_room`=1 AND m.`sender_id` IN %s""" % ph) or 0),
        }
    else:
        b = {"count": 0}
    out["B_name"] = b

    # ---------------- C：互通消息（@openim） ----------------
    c = {
        "openim_chats": int(my.scalar(
            w, "SELECT COUNT(*) FROM `chats` WHERE `chat_id` LIKE '%%@openim'") or 0),
        "openim_msgs": int(my.scalar(
            w, "SELECT COUNT(*) FROM `messages` WHERE `chat_id` LIKE '%%@openim'") or 0),
        "openim_senders_in_groups": int(my.scalar(
            w, "SELECT COUNT(DISTINCT `sender_id`) FROM `messages` "
               "WHERE `sender_id` LIKE '%%@openim'") or 0),
    }
    ids = {r["sender_id"].split("@")[0] for r in my.query(
        w, "SELECT DISTINCT `sender_id` FROM `messages` "
           "WHERE `sender_id` LIKE '%%@openim'")}
    wecom_ids = {r["contact_id"] for r in my.query(e, "SELECT `contact_id` FROM `contacts`")} \
        | {r["sender_id"] for r in my.query(e, "SELECT DISTINCT `sender_id` FROM `messages`")}
    c["openim_wecom_id_overlap"] = len(ids & wecom_ids)
    out["C_openim"] = c

    # ---------------- 企微单聊整体 ----------------
    out["wecom_1v1_all"] = {
        "convs": int(my.scalar(e, "SELECT COUNT(*) FROM `chats` "
                                  "WHERE `chat_id` LIKE 'S:%%'") or 0),
        "msgs": int(my.scalar(e, """SELECT COUNT(*) FROM `messages` m JOIN `chats` c
                 ON c.`chat_pk`=m.`chat_pk` WHERE c.`chat_id` LIKE 'S:%%'""") or 0),
        "from_me": int(my.scalar(e, """SELECT COUNT(*) FROM `messages` m JOIN `chats` c
                 ON c.`chat_pk`=m.`chat_pk` WHERE c.`chat_id` LIKE 'S:%%'
                 AND m.`sender_id`=%s""", (SELF_WECOM,)) or 0),
    }
    w.close()
    e.close()
    return out


def main():
    ap = argparse.ArgumentParser(description="微信 ↔ 企微 共友统计")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    d = collect()
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return

    a = d["A_strict"]
    print("【A 严格口径】企微单聊对方 ∩ 微信联系人（姓名一致）：%d 人" % a["count"])
    print("  企微 1v1 消息 %s 条（对方发 %s）｜ 微信 1v1 消息 %s 条（对方发 %s）｜ "
          "他们在微信群里发言 %s 条"
          % (format(a["wecom_1v1_msgs"], ","), format(a["wecom_1v1_from_peer"], ","),
             format(a["wx_1v1_msgs"], ","), format(a["wx_1v1_from_peer"], ","),
             format(a["wx_group_msgs"], ",")))
    print("  %-8s %-18s %8s %8s %8s %8s" % ("姓名", "企微 userid", "企微1v1", "其中对方",
                                            "微信1v1", "微信群发言"))
    for r in a["rows"]:
        print("  %-8s %-18s %8d %8d %8d %8d"
              % (r["name"], r["wecom_id"], r["wecom_msgs"], r["wecom_from_peer"],
                 r["wx_1v1_msgs"], r["wx_group_msgs"]))
    b = d["B_name"]
    print("\n【B 姓名口径】疑似共友 %d 人；这些名字对应的微信账号发出 %s 条消息"
          % (b["count"], format(b.get("wx_msgs_from_them", 0), ",")))
    print("    （含同名商家/客服号，不可当朋友数）")
    c = d["C_openim"]
    print("\n【C 互通消息】微信里的 @openim：%d 个会话 / %s 条消息 / %d 个企微发送者"
          % (c["openim_chats"], format(c["openim_msgs"], ","),
             c["openim_senders_in_groups"]))
    print("    这些 ID 与企微同事 ID 的交集：%d（⇒ 两套 ID 不同命名空间）"
          % c["openim_wecom_id_overlap"])
    v = d["wecom_1v1_all"]
    print("\n【企微单聊整体】%d 个会话 / %s 条消息（我发 %s）"
          % (v["convs"], format(v["msgs"], ","), format(v["from_me"], ",")))


if __name__ == "__main__":
    main()
