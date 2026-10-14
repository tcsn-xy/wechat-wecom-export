#!/usr/bin/env python3
"""第一步：借企业微信自己的沙盒，把它的数据快照搬进项目 ``data/wecom/raw``。

为什么不能直接读
----------------
企业微信 5.x 是**沙盒应用**，数据在
``~/Library/Containers/com.tencent.WeWorkMac/Data``；macOS 对「别的 App 的容器」
加了一层 TCC 保护（``kTCCServiceSystemPolicyAppDataDetailed``），外部进程——
**包括 root**——直接读就是 EPERM，连软链接、``/System/Volumes/Data`` 绕路都没用。
官方签名又带 Hardened Runtime，``lldb`` 也附加不上。

怎么读到的
----------
企业微信会拉起两个 Node.js 子进程（WeDrive 的 node 与 wemail 的 node），它们是
主进程的孩子，**继承了企业微信的沙盒与容器授权**。Node 有个官方开发特性：
给进程发 ``SIGUSR1`` 就会在 ``127.0.0.1:9229`` 打开 inspector。于是我们能在
**企业微信自己的沙盒里**执行 ``/bin/sh``，用 ``cp -Rc``（clonefile 写时复制，
几乎瞬时）把容器里的目录拷到 ``~/Downloads``，再原地 ``mv`` 进项目。

全程不 attach、不注入、不改 App、不重启、不弹任何系统授权框。

    ./.venv/bin/python scripts/wecom_snapshot.py            # 搬快照
    ./.venv/bin/python scripts/wecom_snapshot.py --list     # 只看有哪些数据集
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from wecom import access, config  # noqa: E402
from localio import task_lock, publish_many


FIND_DATASETS = r'''
find "$HOME/Library/Application Support/WXWork/Data" \
     "$HOME/Library/Group Containers" -maxdepth 6 -name message.db 2>/dev/null \
| while read -r m; do
    d=$(dirname "$m")
    if [ -f "$d/session.db" ] && [ -f "$d/user.db" ]; then
        echo "$d"
    fi
  done
'''


def list_datasets(port: int) -> list:
    res = access.sh(FIND_DATASETS, port)
    return [line.strip() for line in res["out"].splitlines() if line.strip()]


def stage(port: int, with_media: bool = False) -> dict:
    """在沙盒里把快照克隆到 ~/Downloads，返回统计信息。

    ``with_media``：连 ``Profiles/<id>/Caches/`` 里的聊天附件一起克隆。那些是
    **明文**（PNG/JPEG/DOCX/MOV/GIF/SILK 语音），本机约 4.6 GB；clonefile 写时
    复制所以几乎不占空间，但源缓存之后被清掉时块会被我们钉住。
    """
    keep_media = "1" if with_media else "0"
    script = r'''
set -e
OUT="$HOME/Downloads/wecom_snapshot"
rm -rf "$OUT"
mkdir -p "$OUT/acct"

find "$HOME/Library/Application Support/WXWork/Data" -maxdepth 6 -name message.db 2>/dev/null \
| while read -r m; do
    d=$(dirname "$m")
    [ -f "$d/session.db" ] || continue
    [ -f "$d/user.db" ] || continue
    name=$(echo "$d" | sed 's|.*/Data/||; s|/|_|g')
    cp -Rc "$d" "$OUT/acct/$name" 2>/dev/null || cp -R "$d" "$OUT/acct/$name"
    echo "DATASET $name"
  done

for rel in "Library/Application Support/WXWork/Data/Global" \
           "Documents/local_storage_index.db" "Documents/local_en" \
           "Documents/Network" "Documents/Profiles"; do
  src="$HOME/$rel"
  [ -e "$src" ] || continue
  dst="$OUT/$(basename "$rel")"
  cp -Rc "$src" "$dst" 2>/dev/null || cp -R "$src" "$dst" 2>/dev/null || true
  echo "EXTRA $rel"
done

# Caches 里是聊天附件缓存（明文）。默认不搬；--with-media 时整个留下。
if [ "$MEDIA_FLAG" != "1" ]; then
  rm -rf "$OUT"/Profiles/*/Caches 2>/dev/null || true
else
  echo "EXTRA Caches(媒体)"
fi

du -sh "$OUT" 2>/dev/null || true
'''
    script = script.replace("$MEDIA_FLAG", keep_media)
    res = access.sh(script, port, timeout=1800)
    return res


def move_into_project(destination=config.RAW, staging=config.STAGING):
    from wecom.extract import datasets
    found=datasets(staging)
    if not found:
        raise RuntimeError("新快照没有完整数据集，旧快照保留")
    for ds in found:
        if any(os.path.getsize(os.path.join(ds,f))<4096 for f in config.CORE_DBS):
            raise RuntimeError("新快照核心库不完整，旧快照保留")
    os.makedirs(os.path.dirname(destination),exist_ok=True)
    with task_lock(os.path.join(os.path.dirname(destination),".snapshot.lock")):
        publish_many({destination:staging},os.path.join(os.path.dirname(destination),".snapshot.switch.json"))
    return {"raw":destination,"datasets":datasets(destination)}


def main():
    ap = argparse.ArgumentParser(description="把企业微信容器里的数据搬进项目")
    ap.add_argument("--list", action="store_true", help="只列出数据集，不搬")
    ap.add_argument("--keep-staging", action="store_true",
                    help="不清理 ~/Downloads/wecom_snapshot")
    ap.add_argument("--with-media", action="store_true",
                    help="连聊天附件缓存一起搬（明文，约 4.6 GB，clonefile 写时复制）")
    ap.add_argument("--out", default=config.RAW)
    args = ap.parse_args()
    with task_lock(config.STAGING+".lock"):
        return snapshot(args)


def snapshot(args):

    pid, port = access.wecom_access()
    print("沙盒通道：企业微信子进程 pid=%d，inspector 端口 %d" % (pid, port))

    if args.list:
        found = list_datasets(port)
        print("发现 %d 个数据集：" % len(found))
        for d in found:
            res = access.sh('du -sh "%s" 2>/dev/null' % d, port, timeout=60)
            print("   %s  %s" % (res["out"].strip(), d))
        return

    print("在企业微信沙盒里克隆快照（clonefile 写时复制）…")
    res = stage(port, with_media=args.with_media)
    if res["rc"] != 0:
        print(res["out"][-3000:])
        raise SystemExit("沙盒内克隆失败：%s" % res["err"][:500])
    for line in res["out"].splitlines():
        if line.startswith(("DATASET", "EXTRA", "DATASETS_TOTAL")) or "G" in line[:6]:
            print("   " + line)

    print("\n搬进项目 …")
    info = move_into_project(args.out)
    print("   %s" % info["raw"])
    for d in info["datasets"]:
        size = sum(os.path.getsize(os.path.join(dp, f))
                   for dp, _dn, fn in os.walk(d) for f in fn)
        print("   数据集 %-24s %.1f MB" % (os.path.basename(d), size / 1e6))
    if not info["datasets"]:
        raise SystemExit("没搬出任何数据集（message.db/session.db/user.db）")
    print("\n下一步： ./.venv/bin/python scripts/wecom_capture_key.py")


if __name__ == "__main__":
    main()
