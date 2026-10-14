#!/usr/bin/env python3
"""导出全部微信会话（离线，不需要微信在运行）。

    python3 scripts/export_all.py                 # 全量导出（文字+图片+表情）
    python3 scripts/export_all.py --text-only     # 只要文字，不碰媒体
    python3 scripts/export_all.py --force         # 忽略进度缓存，全部重做
    python3 scripts/export_all.py --limit 10      # 只做消息最多的 10 个会话
    python3 scripts/export_all.py --chat 10000000001@chatroom
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from wxexport import config, export, keys  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="导出全部微信聊天记录")
    ap.add_argument("--force", action="store_true", help="忽略进度缓存，全部重做")
    ap.add_argument("--text-only", action="store_true", help="不导出图片/表情/附件")
    ap.add_argument("--limit", type=int, default=0, help="只导出消息最多的前 N 个会话")
    ap.add_argument("--chat", default="", help="只导出指定会话 ID")
    ap.add_argument("--min-messages", type=int, default=1, help="消息数下限")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--snapshot-root")
    ap.add_argument("--capture")
    ap.add_argument("--out")
    args = ap.parse_args()

    p = config.load(require_snapshot=True, snapshot_root=args.snapshot_root,
                    capture=args.capture, export_root=args.out)

    kr = keys.Keyring(p.capture)
    if not os.path.exists(p.capture):
        raise SystemExit("找不到密钥文件：%s\n请先运行 python3 scripts/capture_keys.py"
                         % p.capture)
    if not kr.verify(p.session_db()):
        raise SystemExit("密钥打不开数据库，可能微信换过账号或重新登录过。\n"
                         "请重新运行 python3 scripts/capture_keys.py")

    print("账号        : %s" % (p.wxid or "未知"))
    print("数据快照    : %s" % p.db_storage)
    if p.cloned_at:
        print("快照时间    : %s" % p.cloned_at)
    print("输出目录    : %s" % p.export)
    print("媒体        : %s" % ("关闭（仅文字）" if args.text_only else "开启"))
    print("-" * 60)

    ex = export.Exporter(p, force=args.force, with_media=not args.text_only,
                         min_messages=args.min_messages, verbose=not args.quiet)
    if args.chat:
        chats = [c for c in ex.enumerate_chats() if c["chat_id"] == args.chat]
        if not chats:
            raise SystemExit("没有找到会话 %s" % args.chat)
        ex.enumerate_chats = lambda: chats
        ex.run()
        print("完成：%s" % chats[0]["title"])
        print("总索引： %s" % os.path.join(p.export, "index.html"))
        return
    if args.limit:
        keep = sorted(ex.enumerate_chats(), key=lambda c: -c["count"])[:args.limit]
        ex.enumerate_chats = lambda: keep

    out = ex.run()
    s = out["stats"]
    print("-" * 60)
    print("会话 %d 个 ｜ 消息 %s 条 ｜ 图片 %d 张（%d 张只有缩略图）｜ 表情 %d 个"
          % (s["chats"], format(s["messages"], ","), s["images"], s["thumbs"],
             s["stickers"]))
    print("图片缺缓存 %d 条 ｜ 表情未取回 %d 条 ｜ 附件 %d 个"
          % (s["image_missing"], s["sticker_missing"], s["attachments"]))
    print("耗时 %.1fs" % out["elapsed"])
    print("\n打开总索引： %s" % os.path.join(p.export, "index.html"))


if __name__ == "__main__":
    main()
