#!/usr/bin/env python3
"""第四步：导出。先把「不需要密钥就能拿到」的部分导出来。

当前能导的：

* **会话快照**（``export/wecom/会话快照.csv|json``）——企业微信 CEF 侧写在
  ``Profiles/<id>/conv_snapshot`` 里的明文 protobuf，含 61 个会话的名字、
  最后消息时间、最后一条消息的发送者与正文。**不需要数据库密钥。**

要导完整聊天记录，得先拿到 ``message.db`` 的密钥：

    ./.venv/bin/python scripts/wecom_capture_key.py --yes   # 需要先退出企业微信
    ./.venv/bin/python lib/wecom/extract.py                 # 解密成 data/wecom/plain
    ./.venv/bin/python scripts/wecom_inspect.py             # 认一下真实表结构
    ./.venv/bin/python scripts/wecom_export.py              # 再跑一次本脚本

    ./.venv/bin/python scripts/wecom_export.py --snapshot-only
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from wecom import config, conv, extract  # noqa: E402


def ts(v) -> str:
    if not v:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(v)))
    except (OSError, ValueError):
        return str(v)


# --------------------------------------------------------------------------- #
def export_snapshot(conversations: list, outdir: str) -> dict:
    os.makedirs(outdir, exist_ok=True)
    csv_path = os.path.join(outdir, "会话快照.csv")
    json_path = os.path.join(outdir, "会话快照.json")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["会话ID", "会话名", "最后消息时间", "最后发送者",
                    "发送者ID", "内容类型", "最后消息正文"])
        for c in conversations:
            w.writerow([c.conv_id, c.name, ts(c.last_time),
                        c.last.sender_name if c.last else "",
                        c.last.sender_id if c.last else "",
                        c.last.content_type if c.last else "",
                        c.last.text if c.last else ""])
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump([{
            "conv_id": c.conv_id, "name": c.name, "peer_id": c.peer_id,
            "last_time": c.last_time, "last_time_str": ts(c.last_time),
            "last": (c.last.__dict__ if c.last else None),
            "extra_texts": c.extra_texts[:20],
        } for c in conversations], fh, ensure_ascii=False, indent=2)
    return {"csv": csv_path, "json": json_path, "count": len(conversations)}


def snapshot_html(conversations: list, outdir: str) -> str:
    rows = []
    for c in conversations:
        last = c.last
        rows.append(
            "<tr><td class=k>%s</td><td>%s</td><td class=t>%s</td>"
            "<td>%s</td><td>%s</td></tr>" % (
                html.escape(str(c.conv_id)), html.escape(c.name or ""),
                html.escape(ts(c.last_time)),
                html.escape(last.sender_name if last else ""),
                html.escape((last.text if last else "") or "")))
    doc = """<!doctype html><meta charset="utf-8">
<title>企业微信会话快照</title>
<style>
 body{font:14px/1.6 -apple-system,"PingFang SC",sans-serif;margin:24px;background:#fafafa}
 h1{font-size:20px} .note{background:#fff8e1;border-left:4px solid #f0ad4e;padding:10px 14px;margin:14px 0}
 table{border-collapse:collapse;background:#fff;width:100%%}
 th,td{border:1px solid #e3e3e3;padding:6px 10px;vertical-align:top;text-align:left}
 th{background:#f2f2f2;position:sticky;top:0}
 td.k{font-family:ui-monospace,monospace;font-size:12px;color:#666}
 td.t{white-space:nowrap;color:#666}
</style>
<h1>企业微信会话快照（%d 个会话）</h1>
<div class="note">这份数据来自企业微信 CEF 侧写在 <code>conv_snapshot</code> 里的<b>明文</b> protobuf，
不需要数据库密钥；但每个会话只有<b>最后一条</b>消息。完整聊天记录需要解开
<code>message.db</code>（跑 <code>scripts/wecom_capture_key.py</code>）。</div>
<table><thead><tr><th>会话ID</th><th>会话名</th><th>最后消息时间</th>
<th>发送者</th><th>最后消息</th></tr></thead><tbody>
%s
</tbody></table>
""" % (len(conversations), "\n".join(rows))
    path = os.path.join(outdir, "会话快照.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path


# --------------------------------------------------------------------------- #
def export_messages(outdir: str, with_backups: bool = False,
                    args_raw_root: str | None = None, plain_root=None, with_media=True) -> dict | None:
    """有明文库就导消息；没有就返回 None。"""
    from wecom import messages as msg_mod, render
    args_raw_root = args_raw_root or (os.path.join(os.path.dirname(plain_root),"raw") if plain_root else config.RAW)

    plain_datasets = extract.datasets(plain_root or config.PLAIN)
    if not plain_datasets:
        return None

    # 备份数据集是同一批会话的历史副本，默认只导当前账号那一份，
    # 否则同一批消息会在导出里出现好几遍
    live = [p for p in plain_datasets if "Backup" not in os.path.basename(p)]
    if not with_backups:
        plain_datasets = live or plain_datasets

    hints = msg_mod.name_hints_from_snapshot(args_raw_root)
    datasets = []
    for path in plain_datasets:
        print("   解析 %s …" % os.path.basename(path))
        match=__import__("re").match(r"(\d+)",os.path.basename(path))
        account=match.group(1) if match else os.path.basename(path)
        hints=msg_mod.name_hints_from_snapshot(args_raw_root,account)
        ds = msg_mod.load_dataset(path, name_hints=hints)
        for note in ds.notes:
            print("      · %s" % note)
        datasets.append(ds)

    from localio import atomic_json, task_lock, identity
    from wecom.resources import CacheIndex
    import json
    from pathlib import Path
    manifest_path=os.path.join(outdir,"_manifest.json")
    manifest=json.loads(Path(manifest_path).read_text(encoding="utf-8")) if os.path.exists(manifest_path) else {"version":2,"chats":{}}
    used={v["dir"] for v in manifest["chats"].values()}
    dirnames={};chats=0
    with task_lock(os.path.join(outdir,".export.lock")):
        for ds in datasets:
            account=ds.self_id or os.path.basename(ds.path)
            cache=CacheIndex(args_raw_root,account,allow_single=len(datasets)==1) if with_media else None
            for conv in ds.conversations.values():
                if not conv.messages: continue
                key=identity(account,os.path.basename(ds.path),conv.conv_id)
                old=manifest["chats"].get(key)
                name=old["dir"] if old else render.safe_name(conv.name)+"__"+key[:12]
                if not old and name in used: raise RuntimeError("会话目录冲突")
                used.add(name);dirnames[(ds.path,conv.conv_id)]=name
                render.write_conversation(conv,outdir,dirname=name,media_index=cache)
                manifest["chats"][key]=dict(dir=name,kind=conv.kind,account=account,dataset=os.path.basename(ds.path),conv_id=conv.conv_id,name=conv.name,count=len(conv.messages))
                atomic_json(manifest_path,manifest)
                chats+=1
        index=render.write_index(datasets,outdir,dirnames=dirnames)
    return {"chats":chats,"messages":sum(len(c.messages) for ds in datasets for c in ds.conversations.values()),"index":index}


MEDIA_KINDS = (
    ("Images", "图片"), ("Files", "文件"), ("Videos", "视频"),
    ("Voices", "语音"), ("Emotions", "表情"),
    ("Emotion_Thumbnail", "表情缩略图"), ("Video_Thumbnail", "视频缩略图"),
    ("Link_Thumbnail", "链接缩略图"),
)


def export_media(outdir: str, raw_root=config.RAW):
    from pathlib import Path
    from wecom.resources import CacheIndex,media_html
    profiles=[p for p in Path(raw_root).glob("Profiles/*") if (p/"Caches").is_dir()]
    if not profiles: return None
    items=[]
    for profile in profiles:
        from wecom.accounts import profile_accounts
        account=profile_accounts(raw_root).get(profile.name,profile.name)
        idx=CacheIndex(raw_root,account)
        items.extend(idx.export_item(key,path,kind,outdir) for key,values in idx.items.items() for path,kind in values)
    page=os.path.join(outdir,"媒体.html")
    with open(page,"w",encoding="utf-8") as fh:
        fh.write('<!doctype html><meta charset="utf-8"><title>企微本机媒体</title><h1>本机媒体</h1>'+media_html(items,page))
    manifest=os.path.join(outdir,"媒体清单.csv")
    with open(manifest,"w",encoding="utf-8-sig",newline="") as fh:
        writer=csv.writer(fh);writer.writerow(["类型","相对路径","字节"])
        writer.writerows((i["kind"],os.path.relpath(i["path"],outdir),os.path.getsize(i["path"])) for i in items)
    return dict(files=len(items),manifest=manifest,root=os.path.join(outdir,"媒体"))


def main():
    ap = argparse.ArgumentParser(description="导出企业微信数据")
    ap.add_argument("--raw")
    ap.add_argument("--out", default=config.EXPORT)
    ap.add_argument("--snapshot-only", action="store_true",
                    help="只导会话快照，不碰明文库/附件")
    ap.add_argument("--with-backups", action="store_true",
                    help="连企业微信的历史备份数据集一起导（会和当前数据重复）")
    ap.add_argument("--no-media", action="store_true",
                    help="不搬聊天附件（媒体在 data/wecom/raw 里）")
    ap.add_argument("--plain")
    args = ap.parse_args()
    args.raw=args.raw or (os.path.join(os.path.dirname(args.plain),"raw") if args.plain else config.RAW)
    args.plain=args.plain or config.PLAIN

    os.makedirs(args.out, exist_ok=True)
    print("输出目录：%s" % args.out)

    conversations = conv.load_all(args.raw)
    if conversations:
        info = export_snapshot(conversations, args.out)
        page = snapshot_html(conversations, args.out)
        print("   会话快照：%d 个会话 -> %s" % (info["count"], info["csv"]))
        print("   网页：%s" % page)
        texts = [c.last.text for c in conversations if c.last and c.last.text]
        print("   其中 %d 个会话带最后一条消息正文" % len(texts))
    else:
        print("   没有找到 conv_snapshot（先跑 scripts/wecom_snapshot.py）")

    if not args.snapshot_only and not args.no_media:
        media = export_media(args.out,args.raw)
        if media:
            print("   聊天附件：%d 个文件 -> %s" % (media["files"], media["root"]))
            print("   清单：%s" % media["manifest"])

    if not args.snapshot_only:
        got = export_messages(args.out, with_backups=args.with_backups,
                              args_raw_root=args.raw,plain_root=args.plain,with_media=not args.no_media)
        if got:
            print("   完整消息：%d 个会话 / %d 条 -> %s"
                  % (got["chats"], got["messages"], got["index"]))
        else:
            print("\n提示：完整聊天记录还没导。需要先拿到 message.db 的密钥：")
            print("   1) 退出企业微信")
            print("   2) ./.venv/bin/python scripts/wecom_capture_key.py --yes")
            print("   3) ./.venv/bin/python -m wecom.extract   (或 "
                  "./.venv/bin/python lib/wecom/extract.py)")
            print("   4) ./.venv/bin/python scripts/wecom_inspect.py")
            print("   5) 再跑一次本脚本")


if __name__ == "__main__":
    main()
