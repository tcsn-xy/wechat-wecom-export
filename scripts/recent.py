#!/usr/bin/env python3
"""只看某个会话最近一段时间的记录。

按天分页生成 HTML，方便快速翻看「最近一周都聊了什么」。

    python3 scripts/recent.py --chat 10000000001@chatroom --days 7
    python3 scripts/recent.py --chat 示例好友 --days 3
    python3 scripts/recent.py --chat 示例群B --days 7
    python3 scripts/recent.py --list                     # 看看有哪些会话可挑

输出：export/最近记录/<会话名>_最近N天/
        index.html     按天目录（含每天条数、发言排行）
        2026-09-13.html …
        全部.txt
        图片/
"""
import argparse
import collections
import datetime as _dt
import html
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from wxexport import config, export, keys, render  # noqa: E402


def pick_chat(chats, needle):
    """按 ID、完整名、或名字片段挑一个会话。"""
    if not needle:
        return None
    exact = [c for c in chats if c["chat_id"] == needle]
    if exact:
        return exact[0]
    sub = [c for c in chats if needle.lower() in c["title"].lower()
           or needle in c["chat_id"]]
    if not sub:
        return None
    if len(sub) > 1:
        sub.sort(key=lambda c: -c["count"])
        print("匹配到多个会话，取消息最多的一个：")
        for c in sub[:6]:
            print("   %-30s %s  %d 条" % (c["title"][:28], c["chat_id"], c["count"]))
    return sub[0]


def main():
    ap = argparse.ArgumentParser(description="导出某会话最近一段时间的记录")
    ap.add_argument("--chat", default="", help="会话 ID 或名字（可以是片段）")
    ap.add_argument("--days", type=float, default=7, help="最近多少天，默认 7")
    ap.add_argument("--since", default="", help="起始日期 YYYY-MM-DD（优先于 --days）")
    ap.add_argument("--until", default="", help="结束日期 YYYY-MM-DD")
    ap.add_argument("--text-only", action="store_true", help="不导出图片/表情")
    ap.add_argument("--list", action="store_true", help="列出所有会话")
    ap.add_argument("--snapshot-root")
    ap.add_argument("--capture")
    ap.add_argument("--out")
    args = ap.parse_args()

    p = config.load(require_snapshot=True,snapshot_root=args.snapshot_root,
                    capture=args.capture,export_root=args.out)
    ex = export.Exporter(p, with_media=not args.text_only, verbose=False)
    chats = ex.enumerate_chats()

    if args.list:
        for c in sorted(chats, key=lambda x: -x["count"])[:60]:
            print("%-34s %-26s %8d 条  %s" %
                  (c["title"][:32], c["chat_id"], c["count"],
                   "群" if c["is_room"] else "聊"))
        return

    chat = pick_chat(chats, args.chat)
    if not chat:
        raise SystemExit("没找到会话 %r，用 --list 看看有哪些" % args.chat)
    print("会话：%s（%s）" % (chat["title"], chat["chat_id"]))

    # 时间窗
    if args.since:
        start = _dt.datetime.strptime(args.since, "%Y-%m-%d")
        since = int(start.timestamp())
    else:
        # 以该会话最后一条消息为基准，而不是「现在」
        last = chat.get("hi") or chat.get("last") or 0
        since = int(last) - int(args.days * 86400)
    until = None
    if args.until:
        until = int((_dt.datetime.strptime(args.until, "%Y-%m-%d")
                     + _dt.timedelta(days=1)).timestamp())

    d0 = _dt.datetime.fromtimestamp(since).strftime("%Y-%m-%d %H:%M")
    d1 = (_dt.datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M")
          if until else "最新")
    print("时间窗：%s ~ %s" % (d0, d1))

    outroot = os.path.join(p.export, "最近记录")
    safe = render.safe_name(chat["title"], 40)
    label = ("最近%s天" % (int(args.days) if args.days == int(args.days)
                          else args.days)) if not args.since else args.since
    outdir = os.path.join(outroot, "%s_%s" % (safe, label))
    imgdir = os.path.join(outdir, "图片")
    os.makedirs(outdir, exist_ok=True)
    if not args.text_only:
        os.makedirs(imgdir, exist_ok=True)

    idx = ex._media_for_chat(chat["chat_id"]) if not args.text_only else None
    records, missing = ex._collect_records(chat, idx, imgdir,
                                           since_ts=since, until_ts=until)
    if not records:
        raise SystemExit("这个时间窗里没有消息")
    print("取出 %d 条消息" % len(records))

    # ---------------- 统计 ---------------- #
    per_day = collections.OrderedDict()
    speaker = collections.Counter()
    kinds = collections.Counter()
    for r in records:
        day = r["time"][:10]
        per_day.setdefault(day, []).append(r)
        kinds[r["kind"]] += 1
        if r["kind"] != "system":
            speaker[r["name"]] += 1

    # ---------------- 纯文本 ---------------- #
    txt = os.path.join(outdir, "全部.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write("%s ｜ %s ~ %s ｜ 共 %d 条\n" % (chat["title"], d0, d1, len(records)))
        f.write("=" * 70 + "\n")
        for r in records:
            tag = " <%s>" % os.path.basename(r["img"]) if r.get("img") else ""
            f.write("[%s] %s: %s%s\n" % (r["time"], r["name"], r["text"], tag))

    # ---------------- HTML：按天分页 ---------------- #
    page_of = {}
    for day in per_day:
        page_of[day] = os.path.join(outdir, "%s.html" % day)

    index = os.path.join(outdir, "index.html")
    with open(index, "w", encoding="utf-8") as f:
        f.write(render.HEAD + "<title>%s 最近记录</title>" % html.escape(chat["title"]))
        f.write('<div class=crumb><a href="%s">← 全部会话</a></div>'
                % html.escape(ex.layout.rel(index, os.path.join(p.export, "index.html"))))
        f.write("<h1>%s · %s</h1>" % (html.escape(chat["title"]), html.escape(label)))
        f.write('<div class=meta>%s ~ %s ｜ %d 条消息 ｜ 群成员 %d 人</div>'
                % (d0, d1, len(records), chat.get("members", 0)))
        f.write("<h2>类型构成</h2><table><tr><th>类型</th><th class=num>条数</th></tr>")
        for k, v in kinds.most_common():
            f.write("<tr><td>%s</td><td class=num>%d</td></tr>" % (html.escape(k), v))
        f.write("</table>")
        f.write("<h2>发言最多</h2><table><tr><th>成员</th><th class=num>条数</th></tr>")
        for n, v in speaker.most_common(20):
            f.write("<tr><td>%s</td><td class=num>%d</td></tr>"
                    % (html.escape(n), v))
        f.write("</table>")
        f.write("<h2>按天浏览</h2><table><tr><th>日期</th><th class=num>条数</th>"
                "<th class=num>图片</th><th class=num>表情</th><th></th></tr>")
        for day, recs in per_day.items():
            ni = sum(1 for r in recs if r.get("img") and r["kind"] == "image")
            ns = sum(1 for r in recs if r.get("img") and r["kind"] == "sticker")
            f.write("<tr><td>%s</td><td class=num>%d</td><td class=num>%d</td>"
                    "<td class=num>%d</td><td><a href='%s'>打开 →</a></td></tr>"
                    % (day, len(recs), ni, ns,
                       html.escape(ex.layout.rel(index, page_of[day]))))
        f.write("</table></html>")

    for day, recs in per_day.items():
        page = page_of[day]
        with open(page, "w", encoding="utf-8") as f:
            f.write(render.HEAD + "<title>%s %s</title>"
                    % (html.escape(chat["title"]), day))
            f.write('<div class=crumb><a href="%s">← %s</a></div>'
                    % (html.escape(ex.layout.rel(page, index)),
                       html.escape(chat["title"])))
            f.write("<h1>%s · %s</h1>" % (html.escape(chat["title"]), day))
            f.write('<div class=meta>%d 条 ｜ %s ~ %s</div>'
                    % (len(recs), recs[0]["time"][11:], recs[-1]["time"][11:]))
            f.write("<input class=search placeholder='在这一天里筛选…' "
                    "oninput=\"for(const d of document.querySelectorAll('.msg'))"
                    "d.style.display=d.textContent.includes(this.value)?'':'none'\">")
            for r in recs:
                f.write(render._msg_html(r, page, ex.layout, ex.self_name))
            f.write("</html>")

    print("\n输出目录：%s" % outdir)
    print("  入口： %s" % index)
    print("  纯文本：%s" % txt)
    if missing["image"]:
        print("  提示：%d 条图片消息本机没有缓存" % missing["image"])
    try:
        import subprocess
        subprocess.run(["open", index], check=False)
    except Exception:
        pass


if __name__ == "__main__":
    main()
