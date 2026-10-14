#!/usr/bin/env python3
"""把个人微信的全部聊天记录导入本机 MySQL（库名 ``wx_chat``）。

    ./.venv/bin/python scripts/mysql_import_wx.py                 # 全量
    ./.venv/bin/python scripts/mysql_import_wx.py --limit 3       # 只做消息最多的 3 个会话
    ./.venv/bin/python scripts/mysql_import_wx.py --chat 10000000001@chatroom
    ./.venv/bin/python scripts/mysql_import_wx.py --text-only     # 不落媒体 BLOB
    ./.venv/bin/python scripts/mysql_import_wx.py --no-voice-decode

前置：先跑 ``scripts/clone_data_local.py`` 生成 ``data/`` 快照。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from mysql_store import conn as my  # noqa: E402
from mysql_store import schema, wx_import  # noqa: E402
from wxexport import config  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="微信 → MySQL 全量入库")
    ap.add_argument("--limit", type=int, default=0, help="只做消息最多的前 N 个会话")
    ap.add_argument("--chat", default="", help="只做一个会话 ID")
    ap.add_argument("--text-only", action="store_true", help="不落媒体 BLOB")
    ap.add_argument("--no-voice-decode", action="store_true",
                    help="语音只存原始 SILK，不做 WAV 解码")
    ap.add_argument("--password", default="", help="MySQL 口令（优先使用环境变量或私密配置；避免在命令参数中传入）")
    ap.add_argument("--keep-existing", action="store_true",
                    help="兼容参数：保留现有数据（现在默认如此）")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--database", default="", help="目标库；默认幂等增量")
    ap.add_argument("--rebuild", action="store_true", help="重建显式指定的非正式库")
    ap.add_argument("--snapshot-root")
    ap.add_argument("--capture")
    args = ap.parse_args()
    database = my.database_name(args.database or "wx_chat")
    if args.rebuild and (not args.database or database.casefold() in ("wx_chat", "wecom_chat")):
        ap.error("--rebuild 必须显式指定非正式目标库 --database")
    if args.rebuild and args.keep_existing:
        ap.error("--rebuild 与 --keep-existing 不能同时使用")

    from mysql_store import realtime_bridge
    if database.casefold()=='wx_chat' and realtime_bridge.active('wechat'):
        if args.limit or args.chat or args.snapshot_root or args.capture:
            ap.error('实时服务运行中，限定范围/自定义快照导入请先停用实时服务；常规同步可直接运行本命令')
        realtime_bridge.request('wechat')
        print('已交给实时服务分批补漏；媒体继续由独立文件任务处理，使用 chat_watch_sync.py --status 查看进度')
        return

    if getattr(args,"limit",0)<0:
        ap.error("--limit 不能为负数")
    if args.password:
        os.environ["WX_MYSQL_PASSWORD"] = args.password

    p = config.load(require_snapshot=True, snapshot_root=args.snapshot_root, capture=args.capture)
    if not os.path.isdir(p.db_storage):
        raise SystemExit("没有快照。先跑： ./.venv/bin/python scripts/clone_data_local.py")

    print("账号      : %s" % (p.wxid or "未知"))
    print("快照      : %s（%s）" % (p.db_storage, p.cloned_at or "?"))
    print("目标库    : %s @ %s:%s" % (database, my.DEFAULT_HOST, my.DEFAULT_PORT))
    print("媒体      : %s" % ("关闭" if args.text_only else "写入 BLOB"))
    print("-" * 60)

    c = my.connect()
    if args.rebuild:
        # Rebuild is explicit and is prohibited for the two production names.
        schema.drop_db(c, database)
        print("已重建指定库："+database)
    schema.create_all(c, database)
    c.close()

    t0 = time.time()
    stats = wx_import.run(
        p,
        limit=args.limit,
        chat=args.chat,
        skip_media=args.text_only,
        decode_voice=not args.no_voice_decode,
        database=database,
        verbose=not args.quiet,
    )
    c = my.connect(database)
    total = my.scalar(c, "SELECT COUNT(*) FROM `messages`")
    chats = my.scalar(c, "SELECT COUNT(*) FROM `chats`")
    media = my.scalar(c, "SELECT COUNT(*) FROM `media_items`")
    links = my.scalar(c, "SELECT COUNT(*) FROM `message_links`")
    mb = my.scalar(c, "SELECT ROUND(SUM(COALESCE(`bytes`,0))/1024/1024,1) "
                      "FROM `media_items`")
    c.close()

    print("-" * 60)
    print("会话 %s 个 ｜ 消息 %s 条 ｜ 媒体 %s 行（%s MB）｜ 链接 %s 条"
          % (format(chats, ","), format(total, ","), format(media, ","), mb,
             format(links, ",")))
    print("媒体命中 %s ｜ 缺缓存 %s ｜ 语音解码 %s / 失败 %s ｜ 原始正文字符 "
          "%s" % (format(stats["media_ok"], ","), format(stats["media_missing"], ","),
                  format(stats["voice_decoded"], ","), stats["voice_failed"],
                  "—"))
    if stats["orphan_chats"]:
        print("孤立消息表 %d 个（%d 条消息）已入库"
              % (stats["orphan_chats"], stats["orphan_messages"]))
    print("耗时 %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
