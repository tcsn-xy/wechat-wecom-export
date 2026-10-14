#!/usr/bin/env python3
"""企业微信：只捞「新产生的」聊天记录（增量，全程无感）。

为什么能无感
------------
读企业微信容器靠的是它**自己的 Node 子进程**（WeDrive/wemail）——给进程发
``SIGUSR1`` 开一个本地 inspector 端口，然后借它的沙盒跑 ``/bin/sh``。全程：

* 不 attach 调试器、不注入、不改 App、不重启、不弹任何系统授权框；
* 不点 UI、不动窗口、不抢焦点；
* 只 clonefile（APFS 写时复制）拷一份数据库目录，不碰媒体缓存（那 4.7 GB 不用每次搬）。

密钥早就有了（``keys/wecom_key.json``），所以这一步只是「搬 + 解 + 比对」。

    ./.venv/bin/python scripts/wecom_recent.py                # 上次导出之后的新消息
    ./.venv/bin/python scripts/wecom_recent.py --since 2h
    ./.venv/bin/python scripts/wecom_recent.py --since 2026-09-20
    ./.venv/bin/python scripts/wecom_recent.py --chat 产品群 --merge
    ./.venv/bin/python scripts/wecom_recent.py --no-snapshot  # 用已有快照，不联网也跑

输出：
* 终端直接打印新消息摘要；
* ``export/wecom/增量/<时间戳>/`` 里每个会话一份 TXT + 一份汇总 HTML；
* ``--merge`` 时另外把这些消息追加进主导出 ``export/wecom/<会话>/``（按内容去重）。
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from wecom import config, crypto, extract, keys, messages, render  # noqa: E402

RECENT = os.path.join(config.DATA, "recent")
RECENT_RAW = os.path.join(RECENT, "raw")
RECENT_PLAIN = os.path.join(RECENT, "plain")
WATERMARK = os.path.join(config.DATA, ".watermark.json")
STAGING = os.path.expanduser("~/Downloads/wecom_recent")

# 沙盒里只拷数据库目录（不带 4.7 GB 的附件缓存），秒级完成
STAGE_SCRIPT = r'''
set -e
OUT="$HOME/Downloads/wecom_recent"
rm -rf "$OUT"; mkdir -p "$OUT/acct"
find "$HOME/Library/Application Support/WXWork/Data" -maxdepth 6 -name message.db 2>/dev/null \
| while read -r m; do
    d=$(dirname "$m")
    [ -f "$d/session.db" ] || continue
    [ -f "$d/user.db" ] || continue
    case "$d" in *Backup*) continue;; esac
    name=$(echo "$d" | sed 's|.*/Data/||; s|/|_|g')
    cp -Rc "$d" "$OUT/acct/$name" 2>/dev/null || cp -R "$d" "$OUT/acct/$name"
    echo "DATASET $name"
  done
'''


def parse_since(text: str, watermark: int) -> int:
    """把 --since 解析成 unix 秒。last=上次水位，2h/3d/1w=相对，其它当日期。"""
    text = (text or "last").strip().lower()
    if text in ("last", "", "auto"):
        return watermark or int(time.time()) - 86400
    unit = text[-1]
    if unit in "smhdw" and text[:-1].isdigit():
        n = int(text[:-1])
        return int(time.time()) - n * {"s": 1, "m": 60, "h": 3600,
                                       "d": 86400, "w": 604800}[unit]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d", "%m-%d", "%H:%M"):
        try:
            t = time.strptime(text, fmt)
        except ValueError:
            continue
        now = time.localtime()
        t = time.struct_time((t.tm_year if t.tm_year > 1900 else now.tm_year,
                              t.tm_mon, t.tm_mday,
                              t.tm_hour, t.tm_min, t.tm_sec, 0, 0, -1))
        return int(time.mktime(t))
    raise SystemExit("看不懂的 --since：%s（示例：last / 2h / 3d / 2026-09-20）" % text)


def stage() -> None:
    """借企业微信沙盒，把数据库目录 clonefile 到 ~/Downloads，再搬进项目。"""
    from wecom import access

    pid, port = access.wecom_access()
    print("   沙盒通道：pid=%d port=%d" % (pid, port))
    res = access.sh(STAGE_SCRIPT, port, timeout=300)
    if res["rc"] != 0:
        print(res["out"][-1500:])
        raise SystemExit("沙盒内克隆失败：%s" % res["err"][:300])
    for line in res["out"].splitlines():
        if line.startswith("DATASET"):
            print("   " + line)
    if not os.path.isdir(STAGING):
        raise SystemExit("没拿到中转目录")
    from wecom_snapshot import move_into_project
    os.makedirs(RECENT, exist_ok=True)
    move_into_project(RECENT_RAW,STAGING)


def load_watermark(path=WATERMARK):
    if not os.path.exists(path): return {}
    with open(path,encoding="utf-8") as fh:
        return json.load(fh)


def message_digest(message):
    from localio import identity
    return identity(render.serialize_message(message))


def conversation_key(conv):
    from localio import identity
    m=conv.messages[0]
    return identity(m.account_id,m.source_dataset,conv.conv_id)


def fresh_messages(conv, ledger, since=None):
    if since is not None:
        return [m for m in conv.messages if m.time>=since]
    old=ledger.get("conversations",{}).get(conversation_key(conv),{}).get("messages",{})
    return [m for m in conv.messages if old.get(m.stable_id)!=message_digest(m)]


def migrate_legacy(conv,csv_path):
    """Never guess which stable ID a truncated or ambiguous legacy row meant."""
    candidates={}
    for m in conv.messages:
        key=(render.ts(m.time),m.sender_id,m.ctype,m.text)
        candidates.setdefault(key,[]).append(m)
    result=[]
    with open(csv_path,encoding="utf-8-sig",newline="") as fh:
        for row in csv.DictReader(fh):
            key=(row.get("时间",""),row.get("发送者ID",""),row.get("内容类型",""),row.get("正文",""))
            matches=candidates.get(key,[])
            if len(matches)!=1:
                raise RuntimeError("旧导出不能可靠迁移，原文件保留：%s" % csv_path)
            result.append(matches[0])
    return result


def merge_into_export(convs,outdir,*,raw_root=config.RAW,source_convs=None):
    from dataclasses import replace
    from pathlib import Path
    from localio import atomic_json,task_lock
    from wecom.resources import CacheIndex
    manifest_path=os.path.join(outdir,"_manifest.json")
    os.makedirs(outdir,exist_ok=True)
    with task_lock(os.path.join(outdir,".export.lock")):
        manifest=load_watermark(manifest_path) or {"version":2,"chats":{}}
        added=0
        for conv in convs:
            if not conv.messages: continue
            key=conversation_key(conv);first=conv.messages[0]
            entry=manifest["chats"].get(key)
            dirname=entry["dir"] if entry else render.safe_name(conv.name)+"__"+key[:12]
            target=Path(outdir)/conv.kind/dirname
            records=target/"_messages.json"
            old=[]
            if records.exists():
                old=[render.deserialize_message(row) for row in json.loads(records.read_text())]
            elif entry:
                raise RuntimeError("导出缺少消息身份索引，不能可靠合并：%s" % target)
            else:
                legacy=Path(outdir)/conv.kind/render.safe_name(conv.name)/"聊天记录.csv"
                if legacy.exists():
                    original=next((c for c in (source_convs or []) if c.messages and conversation_key(c)==key),None)
                    if original is None: raise RuntimeError("迁移旧导出需要对应账号的完整快照")
                    old=migrate_legacy(original,legacy)
            by_id={m.stable_id:m for m in old}
            for message in conv.messages:
                added+=message.stable_id not in by_id
                by_id[message.stable_id]=message
            complete=replace(conv,messages=sorted(by_id.values(),key=lambda m:(m.time,m.msg_id)))
            cache=CacheIndex(raw_root,first.account_id,allow_single=len({m.account_id for c in convs for m in c.messages})==1)
            render.write_conversation(complete,outdir,dirname=dirname,media_index=cache)
            manifest["chats"][key]=dict(dir=dirname,kind=conv.kind,name=conv.name,conv_id=conv.conv_id,
                                        account=first.account_id,dataset=first.source_dataset,count=len(complete.messages))
            atomic_json(manifest_path,manifest)
        # Rebuild from every persisted conversation, including unchanged ones.
        from wecom.messages import Conversation,Dataset
        datasets={};dirnames={}
        for key,e in manifest["chats"].items():
            path=Path(outdir)/e["kind"]/e["dir"]/"_messages.json"
            if not path.exists(): raise RuntimeError("完成清单缺少身份索引：%s" % path)
            group=e["account"]+"/"+e["dataset"]
            ds=datasets.setdefault(group,Dataset(path=group))
            conv=Conversation(conv_id=e["conv_id"],name=e["name"],kind=e["kind"],
                              messages=[render.deserialize_message(r) for r in json.loads(path.read_text())])
            ds.conversations[conv.conv_id]=conv;dirnames[(group,conv.conv_id)]=e["dir"]
        legacy_index=Path(outdir)/"index.legacy.html"
        current=Path(outdir)/"index.html"
        if current.exists() and not legacy_index.exists(): shutil.copy2(current,legacy_index)
        extras=[("合并前索引（旧格式记录）","index.legacy.html")] if legacy_index.exists() else None
        render.write_index(list(datasets.values()),outdir,dirnames=dirnames,extra_rows=extras)
        return added


def main():
    from dataclasses import replace
    from pathlib import Path
    import uuid
    from localio import atomic_json,task_lock,staged_directory
    ap=argparse.ArgumentParser(description="企微按稳定消息身份增量导出")
    ap.add_argument("--since",default="last")
    ap.add_argument("--chat",default="")
    ap.add_argument("--limit",type=int,default=40)
    ap.add_argument("--merge",action="store_true")
    ap.add_argument("--no-snapshot",action="store_true")
    ap.add_argument("--raw")
    ap.add_argument("--plain")
    ap.add_argument("--key",default=config.KEY_FILE)
    ap.add_argument("--out",default=config.EXPORT)
    ap.add_argument("--watermark")
    args=ap.parse_args()
    args.watermark=args.watermark or (WATERMARK if args.out==config.EXPORT else os.path.join(args.out,".watermark.json"))
    media_raw=args.raw or (os.path.join(os.path.dirname(args.plain),"raw") if args.plain else config.RAW)
    if args.limit<0: ap.error("--limit 不能为负数")
    raw_root=args.raw or RECENT_RAW
    if not args.raw and not args.plain and not args.no_snapshot: stage()
    plain_root=args.plain or RECENT_PLAIN
    if not args.plain:
        extract.decrypt_all(raw_root,plain_root,keys.load_key(args.key),quiet=True)
    hints=messages.name_hints_from_snapshot(media_raw)
    dirs=[p for p in extract.datasets(plain_root) if "Backup" not in os.path.basename(p)]
    if not dirs: raise SystemExit("没有可用的完整数据集")
    source=[]
    import re
    for path in dirs:
        match=re.match(r"(\d+)",os.path.basename(path))
        account=match.group(1) if match else os.path.basename(path)
        hints=messages.name_hints_from_snapshot(media_raw,account)
        source.extend(c for c in messages.load_dataset(path,name_hints=hints).conversations.values() if c.messages)
    selected=[c for c in source if not args.chat or args.chat in c.name or args.chat==c.conv_id]
    explicit=args.since.lower() not in ("last","auto","")
    with task_lock(args.watermark+".lock"):
        ledger=load_watermark(args.watermark)
        first=not ledger
        legacy=bool(ledger) and ledger.get("version")!=2
        since=parse_since(args.since,0) if explicit or first else None
        hits=[(c,fresh_messages(c,ledger,since)) for c in selected]
        hits=[(c,ms) for c,ms in hits if ms]
        total=sum(len(ms) for c,ms in hits)
        if not total:
            print("没有新消息；已输出身份记录保持不变。")
            return
        stamp=time.strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6]
        target=os.path.join(args.out,"增量",stamp)
        fresh_convs=[replace(c,messages=ms) for c,ms in hits]
        with staged_directory(target) as stage_root:
            from wecom.resources import CacheIndex
            datasets=[];dirnames={}
            for conv in fresh_convs:
                key=conversation_key(conv);name=render.safe_name(conv.name)+"__"+key[:12]
                cache=CacheIndex(media_raw,conv.messages[0].account_id,allow_single=len(dirs)==1)
                render.write_conversation(conv,stage_root,dirname=name,media_index=cache)
                ds=messages.Dataset(path=key,conversations={conv.conv_id:conv});datasets.append(ds)
                dirnames[(key,conv.conv_id)]=name
            render.write_index(datasets,stage_root,dirnames=dirnames)
        if args.merge:
            print("合并新增 %d 条" % merge_into_export(fresh_convs,args.out,raw_root=media_raw,source_convs=source))
        if not explicit:
            ledger.setdefault("conversations",{})
            ledger["version"]=2
            for conv,fresh in hits:
                saved=ledger["conversations"].setdefault(conversation_key(conv),{}).setdefault("messages",{})
                # Initial recent-window exports establish a baseline for older source rows.
                for message in (conv.messages if first else fresh):
                    saved[message.stable_id]=message_digest(message)
            ledger["last_send_time"]=max(int(ledger.get("last_send_time",0)),max(m.time for c,ms in hits for m in ms))
            ledger["updated_at"]=time.strftime("%Y-%m-%d %H:%M:%S")
            atomic_json(args.watermark,ledger)
        print("已输出 %d 个会话 / %d 条：%s" % (len(hits),total,target))
        for c,ms in hits:
            for m in ms[:args.limit]: print("[%s] %s: %s"%(render.ts(m.time),m.sender,m.text[:88]))


if __name__=="__main__":
    main()
