#!/usr/bin/env python3
"""Search local encrypted WeChat shards with FTS-to-full-scan fallback.

The upstream wechat-cli LIKE search can miss compressed message bodies. This tool
uses the plaintext FTS content tables when available and can scan all message
shards with --source full. FTS coverage is partial; its session/sender rowids
belong to the FTS database name2id table, not message database Name2Id.

    python scripts/wccli_search.py "example" --source full
    python scripts/wccli_search.py "example" --chat 10000000001@chatroom
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from wxexport import config, names  # noqa: E402

# 和 wechat-cli 的 --type 取值保持一致，方便互换使用。
# 每项是 (base_type, sub_type 或 None)
TYPE_FILTERS = {
    "text": (1, None),
    "image": (3, None),
    "voice": (34, None),
    "video": (43, None),
    "sticker": (47, None),
    "location": (48, None),
    "link": (49, None),
    "file": (49, 6),
    "call": (50, None),
    "system": (10000, None),
}

TYPE_NAMES = {
    1: "文本", 3: "图片", 34: "语音", 42: "名片", 43: "视频", 47: "表情",
    48: "位置", 49: "链接/文件", 50: "通话", 10000: "系统", 10002: "撤回",
}


# --------------------------------------------------------------------------- #
def fts_shards(conn):
    """发现所有 ``message_fts_v4_<n>_content`` 分片表。"""
    out = []
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'message_fts_v4_%_content'"):
        if re.fullmatch(r"message_fts_v4_\d+_content", name):
            out.append(name)
    return sorted(out, key=lambda s: int(re.findall(r"\d+", s)[0]))


def like_pattern(keyword: str) -> str:
    """把关键词转成 LIKE 模式，转义 ``%`` / ``_`` / 反斜杠。"""
    esc = (keyword.replace("\\", "\\\\")
                  .replace("%", "\\%")
                  .replace("_", "\\_"))
    return f"%{esc}%"


def parse_when(value: str, *, is_end: bool) -> int | None:
    """解析 ``YYYY-MM-DD[ HH:MM[:SS]]``；只给日期时，结束端补到当天 23:59:59。"""
    value = (value or "").strip()
    if not value:
        return None
    for fmt, date_only in (("%Y-%m-%d %H:%M:%S", False),
                           ("%Y-%m-%d %H:%M", False),
                           ("%Y-%m-%d", True)):
        try:
            dt = _dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
        if date_only and is_end:
            dt = dt.replace(hour=23, minute=59, second=59)
        return int(dt.timestamp())
    raise SystemExit(f"时间格式无效: {value!r}（用 YYYY-MM-DD 或 'YYYY-MM-DD HH:MM'）")


def resolve_chats(directory: names.Directory, needles) -> tuple[set, list]:
    """把 --chat 的名字片段/ID 解析成 username 集合。返回 (命中, 没命中)。"""
    hit: set = set()
    miss: list = []
    for needle in needles:
        n = (needle or "").strip()
        if not n:
            continue
        low = n.lower()
        before = len(hit)
        # 直接给 ID
        if n.endswith("@chatroom") or n.startswith("wxid_") or n.startswith("gh_"):
            hit.add(n)
        # 群名 / 联系人名片段
        for wxid, title in directory.names.items():
            if low in (title or "").lower():
                hit.add(wxid)
        for room in directory.rooms:
            if low in (directory.rooms.get(room) or "").lower():
                hit.add(room)
        if len(hit) == before:
            miss.append(n)
    return hit, miss


def build_query(shards, pattern, session_ids, base, sub, t_from, t_to):
    """拼一条 UNION ALL，尽量把过滤下推到每个分片。

    ``session_ids`` 必须是**数字** session_id（即 name2id 的 rowid），
    不是 username —— 索引里的 ``c4`` 存的是 rowid。
    """
    parts, params = [], []
    for t in shards:
        where = ["c0 LIKE ? ESCAPE '\\'"]
        p = [pattern]
        if base is not None:
            where.append("(c3 & 0xFFFFFFFF) = ?")
            p.append(base)
            if sub is not None:
                where.append("((c3 >> 32) & 0xFFFFFFFF) = ?")
                p.append(sub)
        if t_from is not None:
            where.append("c6 >= ?")
            p.append(t_from)
        if t_to is not None:
            where.append("c6 <= ?")
            p.append(t_to)
        if session_ids:
            where.append("c4 IN (%s)" % ",".join("?" * len(session_ids)))
            p.extend(sorted(session_ids))
        parts.append(
            f"SELECT c1 AS local_id, c3 AS local_type, c4 AS session_id, "
            f"c5 AS sender_id, c6 AS create_time, c0 AS text "
            f"FROM [{t}] WHERE {' AND '.join(where)}")
        params.extend(p)
    sql = ("SELECT * FROM (" + " UNION ALL ".join(parts) + ") "
           "ORDER BY create_time DESC LIMIT ? OFFSET ?")
    return sql, params


def search(paths, keyword, *, chats=(), limit=20, offset=0, source="auto",
           start_time="", end_time="", msg_type=None):
    """Read-only search. Auto's FTS hits remain an explicitly partial result."""
    import hashlib
    import heapq
    from wxexport import keys, parse
    from localio import iter_rows
    if not keyword.strip() or limit < 1 or offset < 0:
        raise ValueError("关键词不能为空；limit 必须为正数；offset 不能为负")
    kr = keys.Keyring(paths.capture)
    directory = names.Directory(paths.contact_db(), paths.capture)
    chat_set, misses = resolve_chats(directory, chats)
    # IDs from every source namespace can be supplied directly.
    for needle in chats:
        if "@" in needle or needle.startswith(("ORPHAN_", "v3_")):
            chat_set.add(needle)
            if needle in misses:
                misses.remove(needle)
    base, sub = TYPE_FILTERS.get(msg_type, (None, None))
    lo, hi = parse_when(start_time, is_end=False), parse_when(end_time, is_end=True)
    result = dict(scope=", ".join(chats) if chats else "全部会话", keyword=keyword,
                  count=0, offset=offset, limit=limit, has_more=False,
                  start_time=start_time or None, end_time=end_time or None,
                  type=msg_type, results=[], unmatched_chats=misses)
    if chats and not chat_set:
        result.update(source="none", coverage="指定会话未匹配，未扩大搜索范围")
        return result

    def record(cid, sender, ts, lt, lid, text, **origin):
        return dict(chat_id=cid, chat=directory.room_title(cid) or cid,
                    sender_id=sender, sender=("我" if sender == paths.wxid else
                                             directory.display(sender, cid) if sender else "系统"),
                    time=_dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                    create_time=ts, local_type=lt, local_id=lid, text=text, **origin)

    if source != "full":
        fts_path = os.path.join(paths.db_storage, "message", "message_fts.db")
        try:
            c = kr.connect(fts_path)
            try:
                id2user = dict(c.execute("SELECT rowid,username FROM name2id"))
                ids = {i for i,u in id2user.items() if u in chat_set}
                rows = []
                exists = False
                if not chats or ids:
                    shards = fts_shards(c)
                    if not shards:
                        raise ValueError("全文索引没有分片")
                    sql, params = build_query(shards, like_pattern(keyword), ids, base, sub, lo, hi)
                    exists = c.execute(sql, (*params, 1, 0)).fetchone() is not None
                    rows = c.execute(sql, (*params, limit+1, offset)).fetchall()
                if exists or source == "fts":
                    result["has_more"] = len(rows) > limit
                    result["results"] = [record(id2user.get(sid,""), id2user.get(sender,""),
                                                 int(ts),lt,lid,text) for lid,lt,sid,sender,ts,text in rows[:limit]]
                    result.update(source="fts", coverage="微信维护的部分索引，不保证覆盖全部历史")
                    result["count"] = len(result["results"])
                    return result
            finally:
                c.close()
        except Exception as exc:
            if source == "fts":
                raise RuntimeError("全文索引不可用：%s" % exc) from exc
            result["fallback_reason"] = str(exc)
    candidates = set(directory.names) | set(directory.rooms) | set(directory.nicks)
    c = kr.connect(paths.session_db())
    try:
        for table,col in (("SessionTable","username"),("Name2Id","user_name")):
            if c.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():
                candidates.update(r[0] for r in c.execute("SELECT %s FROM %s" % (col,table)) if r[0])
    finally:
        c.close()
    by_table = {"Msg_"+hashlib.md5(cid.encode()).hexdigest():cid for cid in candidates}
    heap=[]; capacity=limit+offset+1; serial=0
    needle=keyword.casefold()
    for path in paths.message_dbs()+paths.biz_message_dbs():
        c=kr.connect(path)
        try:
            columns_by_table = {}
            senders={}
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='Name2Id'").fetchone():
                senders=dict(c.execute("SELECT rowid,user_name FROM Name2Id"))
            tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    if re.fullmatch(r"Msg_[0-9a-fA-F]{32}",r[0])]
            for table in tables:
                cid=by_table.get(table) or "ORPHAN_%s_%s" % (os.path.basename(path).replace(".db",""),table[4:28])
                if chats and cid not in chat_set:
                    continue
                columns={r[1] for r in c.execute("PRAGMA table_info(%s)" % table)}
                ct="WCDB_CT_message_content" if "WCDB_CT_message_content" in columns else "NULL"
                where=[];params=[]
                if lo is not None: where.append("create_time>=?");params.append(lo)
                if hi is not None: where.append("create_time<=?");params.append(hi)
                if base is not None: where.append("(local_type & 4294967295)=?");params.append(base)
                sql="SELECT local_id,local_type,real_sender_id,create_time,message_content,%s FROM %s %s" % (
                    ct,table,("WHERE "+" AND ".join(where)) if where else "")
                for lid,lt,rsid,ts,raw,ct in iter_rows(c.execute(sql,params)):
                    payload=parse.decode_content(raw,ct)
                    sender,payload=parse.split_sender(payload)
                    sender=sender or senders.get(rsid,"")
                    if sender==cid or sender.endswith("@chatroom"): sender=""
                    if sub is not None:
                        app_sub=parse.xml_text(payload,"type")
                        if str(sub)!=str(app_sub or (int(lt)>>32)):
                            continue
                    kind,text,refs=parse.describe(int(lt)&parse.TYPE_MASK,payload)
                    if needle not in text.casefold(): continue
                    serial+=1
                    item=record(cid,sender,int(ts or 0),lt,lid,text,
                                source_db=os.path.basename(path),source_table=table)
                    key=(int(ts or 0),os.path.basename(path),table,int(lid),serial,item)
                    if len(heap)<capacity: heapq.heappush(heap,key)
                    elif key[:5]>heap[0][:5]: heapq.heapreplace(heap,key)
        finally:
            c.close()
    ordered=sorted(heap,key=lambda x:x[:5],reverse=True)
    result["has_more"]=len(ordered)>offset+limit
    result["results"]=[v[-1] for v in ordered[offset:offset+limit]]
    result.update(count=len(result["results"]),source="full",coverage="全部可读消息分片，含压缩消息及孤立表")
    return result


def main():
    ap=argparse.ArgumentParser(description="微信本机消息搜索：索引与全量回退")
    ap.add_argument("keyword")
    ap.add_argument("--chat",action="append",default=[])
    ap.add_argument("--limit",type=int,default=20)
    ap.add_argument("--offset",type=int,default=0)
    ap.add_argument("--source",choices=["auto","fts","full"],default="auto")
    ap.add_argument("--start-time",default="")
    ap.add_argument("--end-time",default="")
    ap.add_argument("--type",choices=sorted(TYPE_FILTERS))
    ap.add_argument("--format",choices=["json","text"],default="json")
    ap.add_argument("--snapshot-root")
    ap.add_argument("--capture")
    args=ap.parse_args()
    p=config.load(require_snapshot=True,snapshot_root=args.snapshot_root,capture=args.capture)
    out=search(p,args.keyword,chats=args.chat,limit=args.limit,offset=args.offset,
               source=args.source,start_time=args.start_time,end_time=args.end_time,msg_type=args.type)
    if args.format=="json":
        json.dump(out,sys.stdout,ensure_ascii=False,indent=2);print()
    else:
        print("%s ｜ %s ｜ %d 条" % (out["source"],out["coverage"],out["count"]))
        for r in out["results"]:
            print("[%s] [%s] %s: %s" % (r["time"],r["chat"],r["sender"],r["text"]))


if __name__=="__main__":
    main()
