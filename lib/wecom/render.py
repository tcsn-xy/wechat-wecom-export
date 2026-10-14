"""把会话记录写成 TXT / CSV / HTML。"""
from __future__ import annotations

import csv
import html
import os
import time
import base64
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote
from localio import atomic_json, staged_directory


def ts(v) -> str:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    if v > 10 ** 12:            # 毫秒
        v //= 1000
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))
    except (OSError, ValueError):
        return str(v)


def safe_name(name: str) -> str:
    name = (name or "未命名").strip()
    for ch in '/\\:*?"<>|\n\r\t':
        name = name.replace(ch, "_")
    name = name.replace("..", "_").strip(". ")
    return (name or "未命名")[:80]


def write_conversation(conv, root: str, dirname: str | None = None, media_index=None) -> dict:
    target=os.path.join(root,conv.kind,dirname or safe_name(conv.name))
    with staged_directory(target) as staging:
        result=_write_conversation(conv,root,dirname,media_index,outdir=staging)
    return {key: (str(Path(target)/Path(value).name) if key in ("txt","csv","html","members") else value)
            for key,value in result.items()}


def serialize_message(message):
    row=asdict(message)
    for key in ("raw_content","raw_extra"):
        if isinstance(row[key],bytes): row[key]={"base64":base64.b64encode(row[key]).decode()}
    return row


def deserialize_message(row):
    from .messages import Message
    row=dict(row)
    for key in ("raw_content","raw_extra"):
        if isinstance(row.get(key),dict): row[key]=base64.b64decode(row[key]["base64"])
    return Message(**row)


def _write_conversation(conv, root, dirname, media_index, outdir):
    """写一个会话。``dirname`` 用于消解重名（同名会话不能互相覆盖）。"""
    os.makedirs(outdir, exist_ok=True)

    txt_path = os.path.join(outdir, "聊天记录.txt")
    csv_path = os.path.join(outdir, "聊天记录.csv")
    with open(txt_path, "w", encoding="utf-8") as txt, \
            open(csv_path, "w", newline="", encoding="utf-8-sig") as csvf:
        w = csv.writer(csvf)
        w.writerow(["时间", "发送者", "发送者ID", "内容类型", "正文", "消息身份"])
        txt.write("%s（%s）\n会话ID：%s\n消息数：%d\n%s\n\n" % (
            conv.name, conv.kind, conv.conv_id, len(conv.messages), "=" * 60))
        for m in conv.messages:
            when = ts(m.time)
            txt.write("[%s] %s\n%s\n\n" % (when, m.sender, m.text))
            w.writerow([when, m.sender, m.sender_id, m.ctype, m.text, m.stable_id])

    made = {"txt": txt_path, "csv": csv_path, "messages": len(conv.messages)}
    atomic_json(os.path.join(outdir,"_messages.json"),[serialize_message(m) for m in conv.messages])
    page=os.path.join(outdir,"聊天记录.html")
    with open(page,"w",encoding="utf-8") as fh:
        fh.write('<!doctype html><meta charset="utf-8"><title>%s</title><style>body{font:14px/1.7 sans-serif;margin:24px}.msg{border-bottom:1px solid #ddd;padding:8px}a{margin-right:10px}</style><h1>%s</h1>'%(html.escape(conv.name),html.escape(conv.name)))
        from .resources import media_html
        for message in conv.messages:
            fh.write('<div class="msg"><small>%s</small> <b>%s</b><p>%s</p>'%(html.escape(ts(message.time)),html.escape(message.sender),html.escape(message.text).replace('\n','<br>')))
            items=[media_index.export_item(key,path,kind,outdir) for key,path,kind in media_index.references(message)] if media_index else []
            fh.write(media_html(items,page))
            fh.write('</div>')
    made["html"]=page
    if conv.members:
        mem_path = os.path.join(outdir, "成员名单.csv")
        with open(mem_path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["成员ID", "群昵称"])
            for uid, nick in sorted(conv.members.items(), key=lambda kv: kv[1]):
                w.writerow([uid, nick])
        made["members"] = mem_path
    return made


def write_index(datasets, root: str, extra_rows=None, dirnames=None) -> str:
    rows = []
    total_msgs = 0
    for ds in datasets:
        for conv in sorted(ds.conversations.values(),
                           key=lambda c: -(c.messages[-1].time if c.messages else 0)):
            if not conv.messages:
                continue
            total_msgs += len(conv.messages)
            last = ts(conv.messages[-1].time)
            d = (dirnames or {}).get((ds.path,conv.conv_id)) or (dirnames or {}).get(conv.conv_id) or safe_name(conv.name)
            link = quote("%s/%s/聊天记录.html" % (conv.kind, d))
            rows.append(
                "<tr><td>%s</td><td><a href=\"%s\">%s</a></td><td>%d</td>"
                "<td class=t>%s</td><td>%s</td></tr>" % (
                    html.escape(conv.kind), html.escape(link),
                    html.escape(conv.name), len(conv.messages),
                    html.escape(last),
                    html.escape(conv.messages[-1].text[:80])))
    extra = ""
    if extra_rows:
        extra = "<h2>另外</h2><ul>%s</ul>" % "".join(
            "<li><a href=\"%s\">%s</a></li>" % (html.escape(h), html.escape(t))
            for t, h in extra_rows)
    doc = """<!doctype html><meta charset="utf-8"><title>企业微信导出</title>
<style>
 body{font:14px/1.6 -apple-system,"PingFang SC",sans-serif;margin:24px;background:#fafafa}
 h1{font-size:20px} table{border-collapse:collapse;background:#fff;width:100%%}
 th,td{border:1px solid #e3e3e3;padding:6px 10px;text-align:left}
 th{background:#f2f2f2} td.t{white-space:nowrap;color:#666}
</style>
<h1>企业微信导出 · %d 个会话 / %d 条消息</h1>
%s
<table><thead><tr><th>类型</th><th>会话</th><th>消息数</th><th>最后时间</th>
<th>最后一条</th></tr></thead><tbody>
%s
</tbody></table>
""" % (len(rows), total_msgs, extra, "\n".join(rows))
    path = os.path.join(root, "index.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path
