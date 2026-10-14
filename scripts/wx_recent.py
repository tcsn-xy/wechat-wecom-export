#!/usr/bin/env python3
"""微信：只导出「新产生的」聊天记录（增量）。

和企微那条线的区别（**这是硬约束，不是偷懒**）
--------------------------------------------
微信容器同样被 TCC 保护（外部进程连 root 都读不了），官方签名又带 Hardened Runtime，
所以「读它的数据」只有一条路：**附加调试器**，而只有重签名过的副本才允许附加。

因此：

* 微信正在跑 **重签名副本**（``.runtime/WeChatRun.app``，`bin/1-抓取密钥.command` 起的那个）
  → 本脚本可以**全自动、无窗口、不抢焦点**地刷新快照并只导出有变化的会话；
* 微信跑的是**原版** → 读不了，脚本会明确告诉你怎么办，不会偷偷动你的微信。

    ./.venv/bin/python scripts/wx_recent.py                # 刷新快照 + 只导出有变化的会话
    ./.venv/bin/python scripts/wx_recent.py --no-snapshot  # 不刷新，只比对现有快照
    ./.venv/bin/python scripts/wx_recent.py --list         # 只列出哪些会话有新消息
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from wxexport import config, export  # noqa: E402


def running_wechats() -> list:
    """返回正在运行的微信进程 [(pid, 可执行文件路径), ...]。"""
    out = subprocess.run(["ps", "-axo", "pid=,comm="],
                         capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        pid, _, comm = line.strip().partition(" ")
        if not pid.isdigit():
            continue
        if comm.endswith("/WeChat") or comm.endswith("/WeChat.app/Contents/MacOS/WeChat"):
            found.append((int(pid), comm))
    return found


def debuggable_running() -> bool:
    """跑的是不是可附加的副本？"""
    return any("WeChatRun" in comm or ".runtime/" in comm
               for _pid, comm in running_wechats())


def refresh_snapshot(with_media: bool = False) -> bool:
    label = "（含媒体）" if with_media else "（只搬数据库，最快）"
    print("   刷新数据快照%s：clonefile 写时复制…" % label)
    cmd = ["sudo", "-n", sys.executable,
           os.path.join(ROOT, "scripts", "clone_data.py")]
    if not with_media:
        cmd.append("--db-only")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = (r.stdout or "")[-600:] + (r.stderr or "")[-400:]
        print("   ⚠ 刷新失败：%s" % tail.strip().splitlines()[-1] if tail.strip() else "未知错误")
        return False
    for line in (r.stdout or "").splitlines():
        if "实际拿到" in line or "密钥校验" in line or "快照就绪" in line:
            print("   " + line.strip())
    return True


def main():
    ap = argparse.ArgumentParser(description="微信增量导出")
    ap.add_argument("--no-snapshot", action="store_true", help="不刷新快照，只用现有的")
    ap.add_argument("--list", action="store_true", help="只列出有新消息的会话")
    ap.add_argument("--text-only", action="store_true", help="不导图片/表情")
    ap.add_argument("--with-media", action="store_true",
                    help="刷新快照时连 msg 那几个 GB 的媒体一起搬（默认只搬数据库）")
    ap.add_argument("--snapshot-root")
    ap.add_argument("--capture")
    ap.add_argument("--out")
    ap.add_argument("--chat", default="")
    ap.add_argument("--since", default="", help="兼容双平台入口；微信按会话指纹重导")
    args = ap.parse_args()
    if args.snapshot_root:
        args.no_snapshot = True

    procs = running_wechats()
    if not procs:
        print("   ⚠ 微信当前没在运行 —— 只能比对上一次的快照，拿不到刚产生的消息。")
        if not args.no_snapshot:
            raise SystemExit("微信未运行，未刷新快照；可使用 --no-snapshot 离线比对")
    elif not debuggable_running():
        print("""
   ⚠ 现在跑的是**原版**微信，读不了它的容器（TCC + Hardened Runtime 双重拦）。

   两个选择：
     A. 想让增量导出变成「随时一句命令」：用重签名副本当日常微信（功能一样）
          open "$ROOT/.runtime/WeChatRun.app"     # 首次可能要扫码登录一次
        之后本脚本就能全自动、无窗口、不抢焦点地刷新。
     B. 不想换：那每次刷新都要走一次「退出微信 → 重签副本 → 扫码」，
        这就不可能"无感"了。
""".replace("$ROOT", ROOT))
        if not args.no_snapshot:
            raise SystemExit(2)

    # 先刷新，再要求快照存在（第一次跑的时候 data/db_storage 可能还没建）
    if not args.no_snapshot and debuggable_running():
        if not refresh_snapshot(with_media=args.with_media):
            raise SystemExit("刷新失败，旧快照保留；可显式使用 --no-snapshot 离线比对")

    p = config.load(require_snapshot=True, snapshot_root=args.snapshot_root,
                    capture=args.capture, export_root=args.out)
    print("账号：%s ｜ 快照：%s" % (p.wxid or "?", p.cloned_at or p.db_storage))

    ex = export.Exporter(p, force=True, with_media=not args.text_only, verbose=False)
    chats = ex.enumerate_chats()
    done = ex.manifest.get("chats", {})

    changed = []
    present={c['chat_id'] for c in chats}
    for cid,old in done.items():
        if cid in present or old.get('source_missing'):
            continue
        if args.chat and args.chat!=cid and args.chat not in old['title']:
            continue
        info=ex.dir.describe(cid,ex.self_wxid)
        info.update(title=old['title'],tables=[],count=0,hi=0,last=0,source_missing=True)
        changed.append((info,-int(old.get('count',0)),0))
    for c in chats:
        if args.chat and args.chat not in (c["chat_id"],) and args.chat not in c["title"]:
            continue
        old = done.get(c["chat_id"])
        if not old:
            changed.append((c, c["count"], c["last"]))
            continue
        if old.get("fingerprint") != ex.fingerprint(c):
            changed.append((c, c["count"] - int(old.get("count", 0)), c["last"]))

    if not changed:
        print("\n没有新消息（%d 个会话都和上次导出一致）。" % len(chats))
        return

    changed.sort(key=lambda t: str(t[2] or ""), reverse=True)
    print("\n有 %d 个会话有变化：\n" % len(changed))
    for c, delta, last in changed[:25]:
        print("   %-28s %s  +%s 条（最新 %s）"
              % (c["title"][:26], c.get("kind", ""),
                 delta if isinstance(delta, int) else "?", last))
    if len(changed) > 25:
        print("   … 还有 %d 个" % (len(changed) - 25))
    if args.list:
        return

    print("\n只重导这些会话（其余的走缓存，不动）…")
    keep = [c for c, _d, _l in changed]
    ex.enumerate_chats = lambda: keep
    t0 = time.time()
    out = ex.run()
    s = out["stats"]
    print("-" * 60)
    print("重导 %d 个会话 ｜ 消息 %s 条 ｜ 耗时 %.1fs"
          % (s["chats"], format(s["messages"], ","), out["elapsed"]))
    print("总索引： %s" % os.path.join(p.export, "index.html"))


if __name__ == "__main__":
    main()
