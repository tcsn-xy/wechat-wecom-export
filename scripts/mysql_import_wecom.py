#!/usr/bin/env python3
"""把企业微信的全部聊天记录导入本机 MySQL（库名 ``wecom_chat``）。

    ./.venv/bin/python scripts/mysql_import_wecom.py                 # 全量
    ./.venv/bin/python scripts/mysql_import_wecom.py --with-backups  # 连 Backup 快照一起
    ./.venv/bin/python scripts/mysql_import_wecom.py --text-only     # 不落媒体 BLOB
    ./.venv/bin/python scripts/mysql_import_wecom.py --no-voice-decode

前置：
    ./.venv/bin/python scripts/wecom_snapshot.py --with-media
    ./.venv/bin/python lib/wecom/extract.py
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from mysql_store import conn as my  # noqa: E402
from mysql_store import schema, wecom_import  # noqa: E402
from wecom import config as wconfig  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="企业微信 → MySQL 全量入库")
    ap.add_argument("--with-backups", action="store_true",
                    help="连 Backup 快照一起导（会与主库重复，默认只取最新一份）")
    ap.add_argument("--text-only", action="store_true", help="不落媒体 BLOB")
    ap.add_argument("--no-voice-decode", action="store_true",
                    help="语音只存原始字节，不做 WAV 解码")
    ap.add_argument("--password", default="", help="MySQL 口令")
    ap.add_argument("--keep-existing", action="store_true", help="不重建库")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--database", default="", help="目标库；默认幂等增量")
    ap.add_argument("--rebuild", action="store_true", help="重建显式指定的非正式库")
    ap.add_argument("--raw", default=wconfig.RAW)
    ap.add_argument("--plain", default=wconfig.PLAIN)
    args = ap.parse_args()
    database = my.database_name(args.database or "wecom_chat")
    if args.rebuild and (not args.database or database.casefold() in ("wx_chat", "wecom_chat")):
        ap.error("--rebuild 必须显式指定非正式目标库 --database")
    if args.rebuild and args.keep_existing:
        ap.error("--rebuild 与 --keep-existing 不能同时使用")

    from mysql_store import realtime_bridge
    if database.casefold()=='wecom_chat' and realtime_bridge.active('wecom'):
        if args.with_backups or args.raw!=wconfig.RAW or args.plain!=wconfig.PLAIN:
            ap.error('实时服务运行中，限定范围/自定义快照导入请先停用实时服务；常规同步可直接运行本命令')
        realtime_bridge.request('wecom')
        print('已交给实时服务分批补漏；媒体继续由独立文件任务处理，使用 chat_watch_sync.py --status 查看进度')
        return

    if getattr(args,"limit",0)<0:
        ap.error("--limit 不能为负数")
    if args.password:
        os.environ["WX_MYSQL_PASSWORD"] = args.password

    if not os.path.isdir(args.plain):
        raise SystemExit(
            "没有解密后的库：%s\n先跑：\n"
            "  ./.venv/bin/python scripts/wecom_snapshot.py --with-media\n"
            "  ./.venv/bin/python lib/wecom/extract.py" % args.plain)

    print("明文库    : %s" % args.plain)
    print("原始快照  : %s" % wconfig.RAW)
    print("目标库    : %s @ %s:%s" % (database, my.DEFAULT_HOST, my.DEFAULT_PORT))
    print("Backup    : %s" % ("包含" if args.with_backups else "排除（只取最新一份）"))
    print("媒体      : %s" % ("关闭" if args.text_only else "写入 BLOB"))
    print("-" * 60)

    c = my.connect()
    if args.rebuild:
        schema.drop_db(c, database)
        print("已重建指定库："+database)
    schema.create_all(c, database)
    c.close()

    t0 = time.time()
    stats = wecom_import.run(
        database=database, raw_root=args.raw, plain_root=args.plain,
        verbose=not args.quiet,
        with_backups=args.with_backups,
        decode_voice=not args.no_voice_decode,
        skip_media=args.text_only,
    )
    c = my.connect(database)
    msgs = my.scalar(c, "SELECT COUNT(*) FROM `messages`")
    chats = my.scalar(c, "SELECT COUNT(*) FROM `chats`")
    media = my.scalar(c, "SELECT COUNT(*) FROM `media_items`")
    links = my.scalar(c, "SELECT COUNT(*) FROM `message_links`")
    mb = my.scalar(c, "SELECT ROUND(SUM(COALESCE(`bytes`,0))/1024/1024,1) "
                      "FROM `media_items`")
    c.close()
    print("-" * 60)
    print("会话 %s ｜ 消息 %s ｜ 媒体 %s 行（%s MB）｜ 链接 %s ｜ 联系人 %s"
          % (format(chats, ","), format(msgs, ","), format(media, ","), mb,
             format(links, ","), format(stats["contacts"], ",")))
    print("媒体命中 %s ｜ 不可用 %s ｜ 大文件分片 %s ｜ 语音解码 %s / 失败 %s"
          % (format(stats["media_ok"], ","), format(stats["media_unavailable"], ","),
             stats["blobs_chunked"], stats["voice_decoded"], stats["voice_failed"]))
    print("耗时 %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
