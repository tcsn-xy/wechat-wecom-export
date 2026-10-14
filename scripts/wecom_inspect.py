#!/usr/bin/env python3
"""第三步：把解密后的明文库体检一遍——表结构、行数、样例行。

导出器必须照着**真实 schema** 写，不能照抄别处的字段名猜。这个脚本把
``data/wecom/plain`` 里每个库的表、列、行数和几条样例打出来（内容会截断），
用来确定 message.db / session.db / user.db 到底长什么样。

    ./.venv/bin/python scripts/wecom_inspect.py
    ./.venv/bin/python scripts/wecom_inspect.py --db message.db --rows 3
    ./.venv/bin/python scripts/wecom_inspect.py --dump 会话表 --limit 20
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from wecom import config  # noqa: E402


def dbs(plain_root: str) -> list:
    return sorted(glob.glob(os.path.join(plain_root, "**", "*.db"), recursive=True))


def open_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect("file:%s?mode=ro&immutable=1" % path, uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    return conn


def describe(path: str, rows: int, width: int):
    print("=" * 78)
    print("%s  (%.2f MB)" % (path, os.path.getsize(path) / 1e6))
    try:
        conn = open_ro(path)
    except sqlite3.Error as exc:
        print("   打不开：%s" % exc)
        return
    tables = [r[0] for r in conn.execute(
        "select name from sqlite_master where type='table' order by name")]
    print("   表 %d 个：%s" % (len(tables), ", ".join(tables[:20])))
    for t in tables:
        try:
            count = conn.execute('select count(*) from "%s"' % t).fetchone()[0]
        except sqlite3.Error as exc:
            print("   -- %s: %s" % (t, exc))
            continue
        cols = [r[1] for r in conn.execute('pragma table_info("%s")' % t)]
        print("\n   [%s]  %d 行，%d 列" % (t, count, len(cols)))
        print("      列：%s" % ", ".join(cols))
        if rows <= 0 or count == 0:
            continue
        try:
            cur = conn.execute('select * from "%s" limit %d' % (t, rows))
            for rec in cur:
                cells = []
                for c, v in zip(cols, rec):
                    s = "NULL" if v is None else str(v).replace("\n", "\\n")
                    if len(s) > width:
                        s = s[:width] + "…"
                    cells.append("%s=%s" % (c, s))
                print("      · " + "  ".join(cells)[:1500])
        except sqlite3.Error as exc:
            print("      取样例失败：%s" % exc)
    conn.close()


def dump_table(path: str, table: str, limit: int):
    conn = open_ro(path)
    cols = [r[1] for r in conn.execute('pragma table_info("%s")' % table)]
    print("| " + " | ".join(cols) + " |")
    for rec in conn.execute('select * from "%s" limit %d' % (table, limit)):
        print("| " + " | ".join(
            "" if v is None else str(v).replace("\n", "\\n")[:200] for v in rec) + " |")
    conn.close()


def main():
    ap = argparse.ArgumentParser(description="体检解密后的企业微信库")
    ap.add_argument("--plain", default=config.PLAIN)
    ap.add_argument("--db", default="", help="只体检名字匹配的库")
    ap.add_argument("--rows", type=int, default=2, help="每个表打几条样例")
    ap.add_argument("--width", type=int, default=120, help="单元格截断宽度")
    ap.add_argument("--dump", default="", help="整表打印（配合 --db）")
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()

    found = dbs(args.plain)
    if not found:
        raise SystemExit("没有明文库；先跑 scripts/wecom_capture_key.py 再解密")
    if args.db:
        found = [p for p in found if args.db in p]
    if args.dump:
        if not args.db or len(found) != 1:
            raise SystemExit("--dump 需要配合 --db 精确指定一个库")
        dump_table(found[0], args.dump, args.limit)
        return
    for p in found:
        describe(p, args.rows, args.width)


if __name__ == "__main__":
    main()
