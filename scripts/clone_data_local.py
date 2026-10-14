#!/usr/bin/env python3
"""把微信容器里的数据搬到项目 ``data/``（本地直读版，不需要 sudo / lldb）。

与 ``scripts/clone_data.py`` 的区别
-----------------------------------
旧脚本借微信进程自己的权限跑 ``clonefile(2)``，前提是当前微信是**重签名副本**
（官方原版带 Hardened Runtime，附加不了调试器）。

实测（2026-09-29）**微信退出时容器对普通用户直接可读**，于是完全不需要调试器：
直接用 ``cp -Rc``（APFS clonefile，写时复制、秒级、不占额外空间）。

    ./.venv/bin/python scripts/clone_data_local.py
    ./.venv/bin/python scripts/clone_data_local.py --account wxid_xxx_37a7
    ./.venv/bin/python scripts/clone_data_local.py --list
    ./.venv/bin/python scripts/clone_data_local.py --no-media   # 不搬 msg/（几个 GB）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import shutil
import subprocess
import tempfile
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from wxexport import config, keys  # noqa: E402
from localio import task_lock, publish_many, recover

# db_storage 的一级子目录（与 clone_data.py 保持一致）
DB_SUBDIRS = [
    "session", "contact", "message", "media", "biz_message", "favorite", "sns",
    "emoticon", "hardlink", "config", "solitaire", "third_app_icon", "sight",
    "sticker", "head_image", "general", "mm", "brand", "fuzz", "oplog",
    "key_value", "favorite_db", "message_biz", "backup", "emoticon_store",
]


def copy_tree(src: str, dst: str) -> bool:
    """``cp -Rc`` 优先（写时复制），失败退回 ``cp -R``。

    注意：macOS 的 cp **不支持** GNU 的 ``--no-preserve=ownership``（会报
    illegal option），所以这里不加；拷贝完统一 chown 给当前用户即可。
    """
    if not os.path.exists(src):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        shutil.rmtree(dst, ignore_errors=True)
    for args in (["cp", "-Rc", src, dst], ["cp", "-R", src, dst]):
        if os.path.exists(dst):
            shutil.rmtree(dst)
        if subprocess.run(args, capture_output=True).returncode == 0:
            try:
                uid = os.getuid()
                for r, dirs, files in os.walk(dst):
                    os.chown(r, uid, -1)
                    for f in files:
                        os.chown(os.path.join(r, f), uid, -1)
            except OSError:
                pass
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description="把微信数据快照搬到项目 data/")
    ap.add_argument("--account", default="", help="账号目录名，如 wxid_xxx_37a7")
    ap.add_argument("--container", default=config.CONTAINER)
    ap.add_argument("--list", action="store_true", help="只列出容器里的账号")
    ap.add_argument("--image-cache", action="store_true", help="只复制图片缓存，不刷新附件/视频")
    ap.add_argument("--no-media", action="store_true", help="不搬 msg/（附件与图片缓存）")
    ap.add_argument("--out")
    ap.add_argument("--capture")
    args = ap.parse_args()

    if args.list:
        accts = config.container_accounts(args.container)
        print("容器里的账号目录：")
        for a in accts:
            print("   %s" % os.path.basename(a))
        if not accts:
            print("   （读不到，可能微信正在运行或容器权限未恢复）")
        return

    accts = config.container_accounts(args.container)
    account = args.account
    if not account:
        if len(accts) == 1:
            account = os.path.basename(accts[0])
        elif accts:
            print("发现多个账号，请用 --account 指定：")
            for a in accts:
                print("   %s" % os.path.basename(a))
            sys.exit(1)
    if not account:
        raise SystemExit(
            "读不到账号目录（%s）。\n"
            "如果微信正在运行，先退出微信再试；实在读不到就走老路：\n"
            "  sudo ./.venv/bin/python scripts/clone_data.py" % args.container)

    acct_path = os.path.join(args.container, account)
    wxid = config.wxid_from_account_dir(account)
    print("账号目录 : %s" % account)
    print("wxid     : %s" % wxid)
    print("源       : %s" % acct_path)
    print("目标     : %s" % config.DATA)
    print("-" * 60)

    subs = ["db_storage", "ilink", "msg/attach"] if getattr(args,"image_cache",False) else (["db_storage", "ilink"] if args.no_media else ["db_storage", "msg", "ilink"])
    destination = os.path.abspath(args.out or config.DATA)
    os.makedirs(destination, exist_ok=True)
    with task_lock(os.path.join(destination, ".snapshot.lock")):
        journal = os.path.join(destination, ".snapshot.switch.json")
        recover(journal)
        with tempfile.TemporaryDirectory(prefix=".wx-snapshot-", dir=destination) as staging:
            ok, fail = [], []
            for d in DB_SUBDIRS:
                src = os.path.join(acct_path, "db_storage", d)
                if not os.path.exists(src):
                    continue
                label = "db_storage/" + d
                (ok if copy_tree(src, os.path.join(staging, label)) else fail).append(label)
            for sub in [s for s in subs if s != "db_storage"]:
                src = os.path.join(acct_path, sub)
                if os.path.exists(src):
                    (ok if copy_tree(src, os.path.join(staging, sub)) else fail).append(sub)
            required = {"db_storage/session", "db_storage/contact", "db_storage/message"}
            if fail or not required.issubset(ok):
                raise SystemExit("快照不完整，旧快照保留：%s" % (fail or required-set(ok)))
            previous = config.read_snapshot(os.path.join(destination,"_snapshot.json"))
            if args.no_media and previous.get("wxid") not in (None, wxid):
                raise SystemExit("换账号需要带媒体刷新，不能复用另一账号的媒体")
            if not args.no_media:
                os.makedirs(os.path.join(staging,"msg"),exist_ok=True)
                os.makedirs(os.path.join(staging,"ilink"),exist_ok=True)
            snap = dict(previous, account_dir=account, account_path=acct_path, wxid=wxid,
                        uin=config.uin_from_ilink(os.path.join(staging,"ilink")) or
                            (previous.get("uin") if previous.get("wxid")==wxid else None),
                        wechat_app=config.find_wechat_app(),
                        cloned_at=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), method="local-cp-Rc")
            kr = keys.Keyring(args.capture or config.CAPTURE_FILE)
            for rel in ("session/session.db", "contact/contact.db", "message/message_0.db"):
                if not kr.verify(os.path.join(staging,"db_storage",rel)):
                    raise SystemExit("密钥或数据库验证失败，旧快照保留："+rel)
            config.write_snapshot(snap,os.path.join(staging,"_snapshot.json"))
            mapping={os.path.join(destination,"db_storage"):os.path.join(staging,"db_storage"),
                     os.path.join(destination,"_snapshot.json"):os.path.join(staging,"_snapshot.json")}
            for sub in (["msg/attach","ilink"] if getattr(args,"image_cache",False) else ["msg","ilink"]):
                if os.path.exists(os.path.join(staging,sub)):
                    mapping[os.path.join(destination,sub)]=os.path.join(staging,sub)
            publish_many(mapping,journal)
            print("快照验证与切换通过；上一份保存在 .previous")
    print("\n快照就绪：%s" % os.path.join(destination,"_snapshot.json"))


if __name__ == "__main__":
    main()
