#!/usr/bin/env python3
"""只把微信的 ``msg/file``（收到的文档附件）克隆出来，供导出器打包。

为什么不直接用 clone_data.py
-----------------------------
1. ``clone_data.py`` 会在克隆**之前**把 ``data/db_storage``、``data/msg`` 整个
   ``rmtree`` 掉，克隆失败就等于把现有快照弄丢了（wechat-cli 的 config.json
   还指着 ``data/db_storage``）。
2. 我们要的只是附件，没必要时重搬 756 MB 的库。

它做的事：用和 ``clone_data.py:run_lldb_exprs`` **完全一样**的方式
（裸 ``expr`` + clonefile、写在 lldb 脚本文件里、attach 完就 detach），
在微信自己的进程里把 ``msg/file`` 克隆到 staging，再搬进 ``data/msg/file``。
**不删任何现有数据**。

用法::

    sudo ./.venv/bin/python scripts/clone_files.py
    sudo ./.venv/bin/python scripts/clone_files.py --month 2026-09
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

RUNTIME = os.path.join(ROOT, ".runtime")
DEFAULT_ACCT = ""
CONTAINER = ("Library/Containers/com.tencent.xinWeChat/Data/Documents/"
             "xwechat_files")


def run_lldb_exprs(pairs, log_path, timeout=180):
    """裸 expr 执行 clonefile。原样照搬 clone_data.py 的做法，别改成 wxclone 回调。"""
    script = os.path.join(RUNTIME, "clone_files.lldb")
    os.makedirs(RUNTIME, exist_ok=True)
    with open(script, "w") as fh:
        fh.write("process attach --name WeChat\n")
        for src, dst in pairs:
            fh.write('expr -l c++ -- (int)clonefile("%s", "%s", 0)\n' % (src, dst))
        fh.write("process detach\nquit\n")

    with open(log_path, "w") as fh:
        proc = subprocess.Popen(["lldb", "--batch", "-s", script],
                                stdout=fh, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(1)
        if proc.poll() is None:
            proc.kill()
    with open(log_path, errors="replace") as fh:
        return fh.read()


def hand_to_user(path):
    """sudo 建的目录属 root，微信（普通用户）写不进去 → clonefile 直接 EPERM。"""
    try:
        uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
        if uid and gid:
            os.chown(path, int(uid), int(gid))
        os.chmod(path, 0o777)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description="只克隆 msg/file（文档附件）")
    ap.add_argument("--account", required=True, help="账号目录名，由 clone_data.py --list 获取")
    ap.add_argument("--month", default="", help="只克隆某个月份，如 2026-09")
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("需要管理员权限才能附加调试器，请用 sudo 运行", file=sys.stderr)
        return 1

    # sudo 下 HOME 可能是 /var/root，必须回到真实用户
    home = os.path.expanduser("~")
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        import pwd
        try:
            home = pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            pass

    acct_path = os.path.join(home, CONTAINER, args.account)
    stage = os.path.join(home, "Downloads", "wx_file_stage")
    src_sub = "msg/file" + ("/" + args.month if args.month else "")

    os.makedirs(os.path.join(stage, "msg"), exist_ok=True)
    hand_to_user(stage)
    hand_to_user(os.path.join(stage, "msg"))

    src = os.path.join(acct_path, src_sub)
    dst = os.path.join(stage, src_sub)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    hand_to_user(os.path.dirname(dst))

    print("克隆 %s" % src_sub)
    log = run_lldb_exprs([(src, dst)],
                         os.path.join(RUNTIME, "clone_files.log"),
                         timeout=args.timeout)

    ok = "= 0" in log
    print("  lldb 结果: %s" % ("rc=0 成功" if ok else "失败"))
    for line in log.splitlines():
        low = line.lower()
        if "error" in low or "= -1" in line or "= 0" in line:
            print("    " + line.strip()[:150])

    if not os.path.isdir(dst):
        print("\n[!] 没有克隆出目录 —— 可能是微信已经清掉了这些附件的缓存，"
              "或者路径不对。", file=sys.stderr)
        return 1

    n = sum(len(fs) for _r, _d, fs in os.walk(dst))
    size = sum(os.path.getsize(os.path.join(r, f))
               for r, _d, fs in os.walk(dst) for f in fs
               if os.path.exists(os.path.join(r, f)))
    print("\n  staging 里有 %d 个文件，%.1f MB" % (n, size / 1e6))

    # 搬进项目 data/msg/file（不删旧的，只补）
    real = os.path.join(ROOT, "data", "msg", "file")
    os.makedirs(real, exist_ok=True)
    moved = 0
    for r, _d, fs in os.walk(dst):
        rel = os.path.relpath(r, dst)
        todir = os.path.join(real, rel) if rel != "." else real
        os.makedirs(todir, exist_ok=True)
        for f in fs:
            tgt = os.path.join(todir, f)
            if not os.path.exists(tgt):
                os.replace(os.path.join(r, f), tgt)
                moved += 1
    print("  搬入 data/msg/file：%d 个新文件" % moved)
    print("\n下一步：重新导出这个会话，附件会进 export/.../附件/")
    print("  ./.venv/bin/python scripts/export_all.py --chat 10000000001@chatroom --force")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
