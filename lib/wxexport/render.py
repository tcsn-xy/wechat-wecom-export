"""把一个会话的消息渲染成 HTML / TXT / CSV，并生成总索引页。

目录形态::

    export/
    ├── index.html                  总索引
    ├── 总览.csv                    所有会话一行
    ├── 群聊/<群名>/
    │   ├── 聊天记录.html            （消息过多时是分月目录页）
    │   ├── 聊天记录.txt
    │   ├── 聊天记录.csv
    │   ├── 图片/
    │   ├── 附件/
    │   └── 分月/2026-01.html …
    ├── 私聊/<名字>/
    └── 共享资源/表情包/<md5>.gif   全局去重，各会话按相对路径引用
"""
from __future__ import annotations

import csv
import html
import os
import re
from urllib.parse import quote, unquote
from typing import Dict, Iterable, List, Optional

SPLIT_THRESHOLD = 20000  # 超过这么多条就按月份拆分

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_DUP = re.compile(r"\s+")


def safe_name(name: str, maxlen: int = 60) -> str:
    """把会话名变成安全的目录名。"""
    s = _ILLEGAL.sub("_", (name or "").strip())
    s = _DUP.sub(" ", s).strip(" .")
    if not s:
        s = "未命名"
    if len(s) > maxlen:
        s = s[:maxlen].rstrip()
    # 以点开头会变隐藏目录
    if s.startswith("."):
        s = "_" + s[1:]
    return s


class Layout:
    """负责分配输出目录，并保证重名不冲突。"""

    def __init__(self, root: str, mapping=None):
        self.root = root
        self.rooms_dir = os.path.join(root, "群聊")
        self.private_dir = os.path.join(root, "私聊")
        self.shared_dir = os.path.join(root, "共享资源")
        self.sticker_dir = os.path.join(self.shared_dir, "表情包")
        self._used: Dict[str, set] = {}
        for d in (self.rooms_dir, self.private_dir, self.sticker_dir):
            os.makedirs(d, exist_ok=True)
        self._assigned = {}
        for base in (self.rooms_dir, self.private_dir):
            self._used[base] = set(os.listdir(base))
        for cid, entry in (mapping or {}).items():
            relative = entry.get("relative_dir") or os.path.dirname(unquote(entry.get("href", "")))
            path = os.path.abspath(os.path.join(root,relative)) if relative else os.path.abspath(entry["dir"])
            if os.path.dirname(path) not in (self.rooms_dir, self.private_dir):
                raise ValueError("完成清单中的输出路径不属于当前输出目录")
            self._assigned[cid] = path

    def chat_dir(self, is_room: bool, title: str, chat_id: str) -> str:
        if chat_id in self._assigned:
            return self._assigned[chat_id]
        base = self.rooms_dir if is_room else self.private_dir
        used = self._used.setdefault(base, set())
        name = safe_name(title)
        suffix = re.sub(r"[^A-Za-z0-9]", "", chat_id)[-8:] or "x"
        if name in used:
            name = "%s__%s" % (name, suffix)
        i = 2
        while name in used:
            name = "%s__%s_%d" % (safe_name(title), suffix, i)
            i += 1
        used.add(name)
        path = os.path.join(base, name)
        self._assigned[chat_id] = path
        return path

    def rel(self, from_file: str, to_path: str) -> str:
        """从某个文件出发到目标的相对路径（POSIX 分隔符）。"""
        return quote(os.path.relpath(to_path, os.path.dirname(from_file)).replace(os.sep, "/"))


# --------------------------------------------------------------------------- #
CSS = """
:root{color-scheme:dark}
body{background:#0f1113;color:#dfe3e6;font:14px/1.7 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;margin:0;padding:20px 26px 60px}
a{color:#6aa9ff}
h1{font-size:20px;color:#6fd08c;margin:0 0 4px}
h2{font-size:15px;color:#9ad0b0;margin:26px 0 8px;border-bottom:1px solid #262b2f;padding-bottom:5px}
.meta{color:#8a8f94;font-size:12.5px;margin-bottom:14px}
.crumb{font-size:12.5px;color:#8a8f94;margin-bottom:12px}
.msg{margin:4px 0;padding:6px 10px;border-radius:8px;background:#171a1d}
.msg:nth-child(even){background:#141719}
.t{color:#6b7176;font-size:12px}
.n{color:#6fd08c;font-weight:600}
.sys{color:#8b9095;font-style:italic}
.med{color:#c9a227}
.me .n{color:#ffb454}
img{max-width:330px;max-height:330px;border-radius:8px;display:block;margin-top:6px;cursor:zoom-in;background:#000}
.thumb{outline:2px dashed #55452a}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid #23282c;padding:6px 9px;text-align:left;vertical-align:top}
th{color:#9ad0b0;position:sticky;top:0;background:#0f1113}
tr:hover td{background:#171a1d}
.num{text-align:right;font-variant-numeric:tabular-nums;color:#b9c0c6}
.search{width:100%;padding:8px 11px;margin:10px 0 16px;background:#171a1d;border:1px solid #2a3036;border-radius:8px;color:#dfe3e6;font-size:13px}
.hint{color:#8a8f94;font-size:12.5px;background:#15181b;border-left:3px solid #2f6b45;padding:8px 12px;border-radius:0 6px 6px 0;margin:12px 0}
"""

HEAD = ("<!doctype html><html lang=zh-CN><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<style>%s</style>" % CSS)


def _esc(s) -> str:
    return html.escape("" if s is None else str(s))


# --------------------------------------------------------------------------- #
def write_txt(path: str, meta: Dict, records: List[Dict]):
    with open(path, "w", encoding="utf-8") as f:
        f.write("%s\n" % meta["title"])
        f.write("=" * 68 + "\n")
        f.write("会话 ID ：%s\n" % meta["chat_id"])
        if meta.get("is_room"):
            f.write("群主    ：%s\n" % (meta.get("owner_name") or meta.get("owner") or "—"))
            f.write("群成员  ：%d 人\n" % meta.get("members", 0))
        f.write("消息总数：%d\n" % len(records))
        if records:
            f.write("时间范围：%s ~ %s\n" % (records[0]["time"], records[-1]["time"]))
        f.write("=" * 68 + "\n\n")
        for r in records:
            tag = ""
            if r.get("img"):
                tag = "  <%s>" % os.path.basename(r["img"])
            f.write("[%s] %s: %s%s\n" % (r["time"], r["name"], r["text"], tag))


def write_csv(path: str, records: List[Dict]):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["时间", "发送者", "发送者wxid", "类型", "内容", "媒体文件"])
        for r in records:
            w.writerow([r["time"], r["name"], r.get("sender", ""), r["kind"],
                        r["text"],
                        os.path.basename(r["img"]) if r.get("img") else ""])


# --------------------------------------------------------------------------- #
MEMBER_COLS = ["群内显示名", "群昵称(显式设置)", "备注", "微信昵称", "微信号",
               "wxid", "本群发言数", "身份"]


def write_members(csv_path: str, html_path: str, rows: List[Dict],
                  meta: Dict, layout: "Layout", page: Optional[str] = None):
    """成员名单：名字怎么来的、ID 是什么，一张表说清。"""
    page = page or html_path

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(MEMBER_COLS)
        for m in rows:
            w.writerow([m["shown"], m["room_nick"], m["remark"], m["nick"],
                        m["alias"], m["wxid"], m["count"], m["role"]])

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(HEAD + "<title>%s 成员名单</title>" % _esc(meta.get("title", "")))
        f.write('<div class=crumb><a href="%s">← %s</a></div>'
                % (_esc(layout.rel(page, os.path.join(layout.root, "index.html"))),
                   _esc(meta.get("title", ""))))
        f.write("<h1>%s · 成员名单</h1>" % _esc(meta.get("title", "")))
        f.write('<div class=meta>群 ID <code>%s</code> ｜ 群成员 %d 人 ｜ '
                '本群消息 %s 条 ｜ 导出时间 %s</div>'
                % (_esc(meta.get("chat_id", "")), len(rows),
                   format(meta.get("total", 0), ","), _esc(meta.get("exported_at", ""))))
        f.write('<div class=hint><b>名字怎么来的</b>：群里显示的名字按 '
                '<code>备注 → 群昵称 → 微信昵称 → 微信号</code> 取第一个非空的。'
                '依据是微信自己生成的系统消息（「谁邀请谁入群」）里用的就是备注，'
                '群昵称一次都没出现过——你给人设了备注，备注就盖过群昵称。'
                '<br><b>两个 ID 别搞混</b>：<code>wxid</code> 是微信内部 ID，人人都有；'
                '<code>微信号</code> 是自己设的登录 ID，<b>只有你好友才有，'
                '陌生人微信根本不提供</b>。</div>')
        if meta.get("total") is not None:
            un = int(meta.get("unattributed") or 0)
            f.write('<div class=hint>数字对得上：成员发言合计 <b>%s</b> 条 ＋ '
                    '系统消息/无法定位发送者 <b>%s</b> 条 ＝ 本群消息总数 '
                    '<b>%s</b> 条。</div>'
                    % (format(meta["total"] - un, ","), format(un, ","),
                       format(meta["total"], ",")))
        f.write("<input class=search placeholder='筛选名字 / wxid / 微信号…' "
                "oninput=\"for(const r of document.querySelectorAll('tbody tr'))"
                "r.style.display=r.dataset.k.includes(this.value.toLowerCase())?'':'none'\">")
        f.write("<h2>成员</h2><table><tr>")
        for c in MEMBER_COLS:
            cls = " class=num" if c in ("本群发言数",) else ""
            f.write("<th%s>%s</th>" % (cls, _esc(c)))
        f.write("</tr>")
        for m in rows:
            key = _esc((m["shown"] + m["wxid"] + m["alias"] + m["nick"]
                        + m["room_nick"]).lower())
            me = ' style="background:#242013"' if m["role"] == "我" else ""
            f.write('<tr data-k="%s"%s><td>%s</td><td>%s</td><td>%s</td>'
                    '<td>%s</td><td>%s</td><td><code>%s</code></td>'
                    '<td class=num>%d</td><td>%s</td></tr>'
                    % (key, me, _esc(m["shown"]),
                       _esc(m["room_nick"]) or '<span style="color:#5c6165">—</span>',
                       _esc(m["remark"]) or '<span style="color:#5c6165">—</span>',
                       _esc(m["nick"]) or '<span style="color:#5c6165">—</span>',
                       _esc(m["alias"]) or '<span style="color:#5c6165">—</span>',
                       _esc(m["wxid"]), m["count"], _esc(m["role"])))
        f.write("</table>")
        if meta.get("self_wxid"):
            f.write('<div class=hint>「我」= <code>%s</code></div>'
                    % _esc(meta["self_wxid"]))
        f.write("</html>")
    return csv_path, html_path


def _msg_html(r: Dict, page_path: str, layout: Optional[Layout],
              self_name: str) -> str:
    cls = "sys" if r["kind"] == "system" else (
        "med" if r["kind"] in ("sticker", "voice", "video", "file", "link",
                               "transfer", "hongbao", "app", "miniprogram",
                               "videochannel", "live", "quote", "chatrecord",
                               "location", "card", "voip", "announce") else "")
    if r["name"] == self_name:
        cls += " me"
    out = ['<div class="msg %s" data-wxid="%s"><span class=t>%s</span> '
           '<span class=n title="%s">%s</span>: %s'
           % (cls, _esc(r.get("sender", "")), _esc(r["time"]),
              _esc(r.get("sender", "")), _esc(r["name"]), _esc(r["text"]))]
    img = r.get("img")
    if img and layout is not None:
        src = layout.rel(page_path, img)
        thumb = ' class=thumb title="本机只缓存了缩略图"' if r.get("thumb") else ""
        out.append('<a href="%s" target=_blank><img loading=lazy%s src="%s"></a>'
                   % (_esc(src), thumb, _esc(src)))
    for item in r.get("media", []):
        path = item.get("path")
        if not path:
            out.append('<div class=hint>%s</div>' % _esc(item.get("reason", "本机无缓存")))
            continue
        src = _esc(layout.rel(page_path, path))
        if item["kind"] == "voice" and item.get("mime") == "audio/wav":
            out.append('<audio controls preload="none" src="%s"></audio>' % src)
        elif item["kind"] == "video":
            out.append('<video controls preload="metadata" style="max-width:100%%" src="%s"></video>' % src)
        out.append('<div><a download href="%s">下载 %s</a></div>' %
                   (src, _esc(item.get("name") or os.path.basename(path))))
        if item.get("reason"):
            out.append('<div class=hint>%s</div>' % _esc(item["reason"]))
    out.append("</div>")
    return "".join(out)


def _month_of(r: Dict) -> str:
    return r["time"][:7]


def write_html(path: str, meta: Dict, records: List[Dict],
               layout: Layout, self_name: str) -> List[str]:
    """写主 HTML。返回需要额外生成的月份列表（空表示单文件搞定）。"""
    rel_root = layout.rel(path, layout.root)
    home = rel_root + "/index.html"

    def head(title: str) -> str:
        return (HEAD + "<title>%s</title>" % _esc(title)
                + '<div class=crumb><a href="%s">← 全部会话</a></div>' % _esc(home)
                + "<h1>%s</h1>" % _esc(meta["title"])
                + '<div class=meta>%s</div>' % _esc(meta["summary"])
                + "<input class=search placeholder='在这个会话里筛选…（输入关键词）' "
                  "oninput=\"for(const d of document.querySelectorAll('.msg'))"
                  "d.style.display=d.textContent.includes(this.value)?'':'none'\">")

    months: List[str] = []
    if len(records) > SPLIT_THRESHOLD:
        months = sorted({_month_of(r) for r in records})

    if not months:
        with open(path, "w", encoding="utf-8") as f:
            f.write(head(meta["title"]))
            for r in records:
                f.write(_msg_html(r, path, layout, self_name))
            f.write("</html>")
        return []

    # 分月：主文件当目录页
    page_dir = os.path.join(os.path.dirname(path), "分月")
    os.makedirs(page_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(head(meta["title"]))
        f.write('<div class=hint>这个会话共 %d 条消息，为避免浏览器卡死，'
                '已按月份拆分成 %d 个页面。</div>' % (len(records), len(months)))
        f.write("<h2>按月份浏览</h2><table><tr><th>月份</th><th class=num>条数</th>"
                "<th>区间</th><th></th></tr>")
        for m in months:
            sub = [r for r in records if _month_of(r) == m]
            page = os.path.join(page_dir, "%s.html" % m)
            f.write("<tr><td>%s</td><td class=num>%d</td><td>%s ~ %s</td>"
                    "<td><a href='%s'>打开 →</a></td></tr>"
                    % (_esc(m), len(sub), _esc(sub[0]["time"]),
                       _esc(sub[-1]["time"][11:]), _esc(layout.rel(path, page))))
        f.write("</table></html>")

    for m in months:
        sub = [r for r in records if _month_of(r) == m]
        page = os.path.join(page_dir, "%s.html" % m)
        back = layout.rel(page, path)
        with open(page, "w", encoding="utf-8") as f:
            f.write(HEAD + "<title>%s %s</title>" % (_esc(meta["title"]), m))
            f.write('<div class=crumb><a href="%s">← %s</a></div>'
                    % (_esc(back), _esc(meta["title"])))
            f.write("<h1>%s · %s</h1>" % (_esc(meta["title"]), m))
            f.write('<div class=meta>%d 条 ｜ %s ~ %s</div>'
                    % (len(sub), _esc(sub[0]["time"]), _esc(sub[-1]["time"])))
            for r in sub:
                f.write(_msg_html(r, page, layout, self_name))
            f.write("</html>")
    return months


# --------------------------------------------------------------------------- #
def write_index(layout: Layout, chats: List[Dict], meta_global: Dict) -> str:
    """总索引页。chats: 每项含 title/kind/chat_id/count/first/last/href/..."""
    path = os.path.join(layout.root, "index.html")

    total_msgs = sum(c["count"] for c in chats)
    with open(path, "w", encoding="utf-8") as f:
        f.write(HEAD + "<title>微信聊天记录总览</title>")
        f.write("<h1>微信聊天记录总览</h1>")
        f.write('<div class=meta>共 %d 个会话 ｜ %s 条消息 ｜ 导出时间 %s ｜ '
                '账号 %s</div>'
                % (len(chats), format(total_msgs, ","),
                   _esc(meta_global.get("exported_at", "")),
                   _esc(meta_global.get("wxid", ""))))
        f.write('<div class=hint>%s</div>' % _esc(meta_global.get("note", "")))
        f.write("<input class=search placeholder='筛选会话名…' "
                "oninput=\"for(const r of document.querySelectorAll('tbody tr'))"
                "r.style.display=r.dataset.k.includes(this.value)?'':'none'\">")

        for kind, label in (("群聊", "群聊"), ("私聊", "私聊")):
            rows = [c for c in chats if c["kind"] == kind]
            if not rows:
                continue
            rows.sort(key=lambda c: -c["count"])
            f.write("<h2>%s（%d 个）</h2>" % (label, len(rows)))
            f.write("<table><tr><th>会话</th><th class=num>消息数</th>"
                    "<th class=num>图片</th><th class=num>表情</th>"
                    "<th>时间范围</th><th>ID</th></tr>")
            for c in rows:
                key = _esc((c["title"] + c["chat_id"]).lower())
                f.write('<tr data-k="%s"><td><a href="%s">%s</a></td>'
                        '<td class=num>%s</td><td class=num>%d</td>'
                        '<td class=num>%d</td><td>%s ~ %s</td>'
                        '<td><code>%s</code></td></tr>'
                        % (key, _esc(c["href"]),
                           _esc(c["title"]), format(c["count"], ","),
                           c["images"], c["stickers"],
                           _esc(c["first"][:16]), _esc(c["last"][:16]),
                           _esc(c["chat_id"])))
            f.write("</table>")
        f.write("</html>")
    return path


def write_overview_csv(layout: Layout, chats: List[Dict]) -> str:
    path = os.path.join(layout.root, "总览.csv")
    fields = ["类型", "会话名", "会话ID", "消息数", "图片数", "表情数",
              "起始时间", "结束时间", "群主", "成员数", "目录"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for c in sorted(chats, key=lambda x: (x["kind"], -x["count"])):
            w.writerow([c["kind"], c["title"], c["chat_id"], c["count"],
                        c["images"], c["stickers"], c["first"], c["last"],
                        c.get("owner", ""), c.get("members", 0),
                        os.path.relpath(c["dir"], layout.root)])
    return path
