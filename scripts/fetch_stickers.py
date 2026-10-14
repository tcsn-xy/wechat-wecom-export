#!/usr/bin/env python3
"""把所有会话里出现过的表情包从微信 CDN 取回本地。

微信不在本地缓存「收到的表情」，只在消息 XML 里给了 ``cdnurl``。这个脚本把
全部会话的表情链接汇总去重后下载到 ``export/共享资源/表情包/<md5>.<ext>``，
所有会话共用同一份，靠相对路径引用，不会重复占空间。

    python3 scripts/fetch_stickers.py              # 下载（可反复跑，已下载的跳过）
    python3 scripts/fetch_stickers.py --workers 12 # 提高并发
    python3 scripts/fetch_stickers.py --list-only  # 只列出清单，不下载
"""
import argparse
import collections
import hashlib
import html
import json
import os
import re
import sys
import threading
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from wxexport import config, keys, parse  # noqa: E402


def collect(p, kr):
    """扫描所有消息库，返回 {md5: (url, 出现次数)}。"""
    urls = {}
    counts = collections.Counter()
    dbs = p.message_dbs() + p.biz_message_dbs()
    for db in dbs:
        try:
            c = kr.connect(db)
        except Exception:
            continue
        tbls = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
        for t in tbls:
            try:
                cur = c.execute(
                    "SELECT message_content, WCDB_CT_message_content FROM %s "
                    "WHERE (local_type & 4294967295)=47" % t)
            except Exception:
                try:
                    cur = c.execute(
                        "SELECT message_content FROM %s "
                        "WHERE (local_type & 4294967295)=47" % t)
                except Exception:
                    continue
            for row in cur:
                raw = row[0]
                ct = row[1] if len(row) > 1 else None
                s = parse.decode_content(raw, ct)
                if not s:
                    continue
                md5 = parse._first(parse.MD5_RE, s)
                url = parse._first(parse.CDNURL_RE, s)
                if not (md5 and url):
                    continue
                counts[md5] += 1
                urls.setdefault(md5, html.unescape(url))
    return urls, counts


def captions(p, kr):
    out = {}
    try:
        c = kr.connect(p.emoticon_db())
        for tbl, a, b in (("kNonStoreEmoticonTable", "md5", "caption"),
                          ("kStoreEmoticonCaptionsTable", "md5_", "caption_")):
            try:
                for x, y in c.execute("SELECT %s,%s FROM %s" % (a, b, tbl)):
                    if x and y and str(y).strip():
                        out.setdefault(str(x), str(y).strip())
            except Exception:
                pass
    except Exception:
        pass
    return out


def main():
    ap = argparse.ArgumentParser(description="下载表情包")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--list-only", action="store_true")
    args = ap.parse_args()

    p = config.load(require_snapshot=True)
    kr = keys.Keyring(p.capture)
    outdir = os.path.join(p.export, "共享资源", "表情包")
    os.makedirs(outdir, exist_ok=True)

    print("扫描消息库里的表情链接…")
    urls, counts = collect(p, kr)
    caps = captions(p, kr)
    items = sorted(urls.items(), key=lambda kv: -counts[kv[0]])
    if args.limit:
        items = items[:args.limit]
    print("表情消息 %d 条，唯一表情 %d 个" % (sum(counts.values()), len(urls)))

    with open(os.path.join(outdir, "_index.txt"), "w", encoding="utf-8") as f:
        f.write("md5\t出现次数\t描述\tCDN\n")
        for md5, n in counts.most_common():
            f.write("%s\t%d\t%s\t%s\n" % (md5, n, caps.get(md5, ""),
                                          urls.get(md5, "")))

    if args.list_only:
        for md5, url in items[:40]:
            print("  %s  x%-4d %s" % (md5, counts[md5], caps.get(md5, "")))
        print("\n清单已写到 %s/_index.txt" % outdir)
        return

    lock = threading.Lock()
    stat = collections.Counter()

    def fetch(item):
        md5, url = item
        for ext in ("gif", "jpg", "png", "webp", "bmp"):
            cand = os.path.join(outdir, "%s.%s" % (md5, ext))
            if os.path.exists(cand) and os.path.getsize(cand) > 0:
                with lock:
                    stat["cached"] += 1
                return
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=args.timeout) as r:
                data = r.read()
        except Exception as e:
            with lock:
                stat["err"] += 1
                stat["err_" + type(e).__name__] += 1
            return
        if not data:
            with lock:
                stat["empty"] += 1
            return
        ext = "jpg"
        if data[:4] == b"GIF8":
            ext = "gif"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = "png"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = "webp"
        with open(os.path.join(outdir, "%s.%s" % (md5, ext)), "wb") as f:
            f.write(data)
        with lock:
            stat["ok"] += 1

    import concurrent.futures as cf
    done = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for _ in pool.map(fetch, items):
            done += 1
            if done % 300 == 0:
                print("  进度 %d/%d  ok=%d cached=%d err=%d"
                      % (done, len(items), stat["ok"], stat["cached"], stat["err"]),
                      flush=True)

    print("下载完成：新下载 %d，已存在 %d，失败 %d"
          % (stat["ok"], stat["cached"], stat["err"]))
    if stat["err"]:
        print("  失败原因：%s" % {k[4:]: v for k, v in stat.items()
                                 if k.startswith("err_")})
        print("  多为 CDN 签名过期——微信只在短时间内保留表情的下载链接。")
    print("表情库：%s" % outdir)


if __name__ == "__main__":
    main()
