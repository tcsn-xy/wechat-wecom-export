#!/usr/bin/env python3
"""第一步：从正在运行的微信进程里抓取 SQLCipher 口令。

为什么需要这么做
----------------
账号级口令是 32 字节随机数，加密存在微信进程内存里，磁盘上算不出来。
唯一可行的办法是让微信**自己**在做 PBKDF 派生时把参数交出来——
我们在 ``CCKeyDerivationPBKDF`` 下断点，读它当次的入参。

为什么需要重签名
----------------
腾讯官方签名带 Hardened Runtime，macOS 会拒绝任何调试器附加（root 也不行）。
所以流程是：复制一份 WeChat.app → 只给**副本**去掉 Hardened Runtime 重新签名
→ 用副本启动微信 → 附加 → 抓完再全部退出。**原版 App 一个字节都不动。**

代价：副本的签名和官方不一致，微信会要求重新登录一次（扫码）。
抓到的口令与登录态无关，保存后以后都不需要再抓。

    sudo python3 scripts/capture_keys.py
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from wxexport import config, keys  # noqa: E402

RUNTIME = os.path.join(ROOT, ".runtime")
RUN_APP = os.path.join(RUNTIME, "WeChatRun.app")
SIGN_SH = os.path.join(ROOT, "bin", "_sign_app.sh")
LLDB_MODULE = os.path.join(ROOT, "lldb", "wxcap.py")


def sh(cmd, **kw):
    print("  $ %s" % (cmd if isinstance(cmd, str) else " ".join(cmd)), flush=True)
    return subprocess.run(cmd, shell=isinstance(cmd, str), **kw)


def wechat_pids():
    try:
        out = subprocess.run(["pgrep", "-x", "WeChat"], capture_output=True,
                             text=True).stdout.split()
        return [int(x) for x in out]
    except Exception:
        return []


def quit_wechat(timeout=30):
    if not wechat_pids():
        return True
    print("  退出正在运行的微信…")
    subprocess.run(["osascript", "-e", 'tell application "WeChat" to quit'],
                   capture_output=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not wechat_pids():
            return True
        time.sleep(0.5)
    subprocess.run(["pkill", "-TERM", "-x", "WeChat"], capture_output=True)
    deadline = time.time() + 10
    while time.time() < deadline:
        if not wechat_pids():
            return True
        time.sleep(0.5)
    return not wechat_pids()


def switch_copy(app: str, force_resign: bool = False, lock: bool = True) -> int:
    """把「日常微信」换成重签名副本：复制 → 重签名 → 退出原版 → 启动副本。

    只需要一次扫码；之后微信就是可附加的了，`10-新消息.command` 才能无感增量。
    不做任何 lldb 相关的事，所以**不需要 root**。
    """
    os.makedirs(RUNTIME, exist_ok=True)
    if os.path.isdir(RUN_APP) and not force_resign:
        print("[1/3] 副本已存在，直接复用：%s" % RUN_APP)
        print("[2/3] 跳过复制与签名")
    elif os.path.isdir(RUN_APP) and force_resign:
        # 微信会**自动更新**，更新会把我们的 ad-hoc 签名换回官方签名，
        # 于是一下子又附加不上了。这里就地重签（不用重新 ditto 1.4 GB）。
        print("[1/3] 副本已存在，就地重签名（不重新复制）")
        subprocess.run(["chmod", "-R", "u+w", RUN_APP], check=False)
        if sh(["bash", SIGN_SH, RUN_APP]).returncode != 0:
            raise SystemExit("签名失败")
        print("[2/3] 重签完成")
    else:
        print("[1/3] 复制一份微信（原版一个字节都不动）…")
        if os.path.exists(RUN_APP):
            shutil.rmtree(RUN_APP, ignore_errors=True)
        if sh(["ditto", app, RUN_APP]).returncode != 0:
            raise SystemExit("复制失败")
        print("[2/3] 重签名（保留各组件 entitlements，去掉 Hardened Runtime）…")
        if sh(["bash", SIGN_SH, RUN_APP]).returncode != 0:
            raise SystemExit("签名失败")

    if lock:
        print("     把副本设为只读，防止微信自动更新把签名换回去…")
        subprocess.run(["chmod", "-R", "a-w", RUN_APP], check=False)

    print("[3/3] 退出原版微信，然后启动副本…")
    if not quit_wechat():
        raise SystemExit("退不掉微信，请手动退出（菜单栏 → 退出）后再跑一次")
    time.sleep(1)
    subprocess.run(["open", RUN_APP], check=False)
    print("""
副本已经启动了（Dock 里看起来和微信一模一样，数据也是同一份）。

  接下来只做一件事：**扫码登录一次**。
  登录进去以后就正常用，跟以前没区别；以后跑 10-新消息.command 就是全自动无感。

  想回到原版：退出副本，然后打开 /Applications 里原来的微信即可
  （数据同一份，不会丢东西；只是原版读不了，增量功能会停）。
""")
    return 0


def main():
    os.umask(0o077)
    ap = argparse.ArgumentParser(description="抓取微信数据库密钥")
    ap.add_argument("--seconds", type=int, default=180,
                    help="监听的时长；期间需要在微信里点几下（登录/进聊天）")
    ap.add_argument("--keep-copy", action="store_true", help="保留重签名的副本")
    ap.add_argument("--app", default="", help="手动指定 WeChat.app 路径")
    ap.add_argument("--switch-copy", action="store_true",
                    help="只做「生成副本 + 退出原版 + 启动副本」（不抓密钥，不需要 root）")
    ap.add_argument("--resign", action="store_true",
                    help="配合 --switch-copy：副本还在，但被微信自动更新换回官方签名了，就地重签")
    ap.add_argument("--no-lock", action="store_true",
                    help="配合 --switch-copy：不把副本设为只读（默认会锁，防自动更新）")
    args = ap.parse_args()

    p = config.load()
    app = args.app or p.wechat_app or config.find_wechat_app()
    if not app or not os.path.isdir(app):
        raise SystemExit("找不到 WeChat.app，请用 --app 指定路径")
    print("原版微信      : %s" % app)

    if args.switch_copy:
        return switch_copy(app, force_resign=args.resign, lock=not args.no_lock)

    if os.geteuid() != 0:
        print("需要管理员权限才能附加调试器，请用：\n  sudo python3 %s" %
              os.path.relpath(__file__, ROOT))
        sys.exit(1)

    os.makedirs(RUNTIME, exist_ok=True)
    os.makedirs(config.KEYS, exist_ok=True)
    capture = os.path.join(config.KEYS, "capture.jsonl")
    if os.path.exists(capture):
        os.chmod(capture, 0o600)

    # ---------------------------------------------------------------- #
    print("\n[1/5] 复制一份微信（原版不动）")
    if os.path.exists(RUN_APP):
        shutil.rmtree(RUN_APP, ignore_errors=True)
    sh(["ditto", app, RUN_APP], check=True)

    print("\n[2/5] 给副本重新签名（保留各组件原有 entitlements，去掉 Hardened Runtime）")
    r = sh(["bash", SIGN_SH, RUN_APP])
    if r.returncode != 0:
        raise SystemExit("签名失败")

    print("\n[3/5] 退出当前微信")
    if not quit_wechat():
        raise SystemExit("无法退出微信，请手动退出后重试")
    time.sleep(1)

    print("\n[4/5] 启动 lldb 监听，然后用副本启动微信")
    print("      >>> 接下来请在弹出的微信里完成登录，并点开任意几个聊天 <<<")
    log = os.path.join(RUNTIME, "lldb.log")
    lldb_cmd = [
        "lldb", "--batch",
        "-o", "command script import %s" % shlex.quote(LLDB_MODULE),
        "-o", "wxattach WeChat 180",
        "-o", "wxcapture %d %s" % (args.seconds, shlex.quote(capture)),
        "-o", "quit",
    ]
    with open(log, "w") as fh:
        proc = subprocess.Popen(lldb_cmd, stdout=fh, stderr=subprocess.STDOUT)

    time.sleep(3)
    # -g 不抢焦点，-n 新开一个实例
    subprocess.run(["open", "-g", "-n", "-a", RUN_APP])
    print("      副本已启动（后台，不抢焦点）")

    print("      等待抓取完成（最多 %ds）…" % (args.seconds + 60))
    deadline = time.time() + args.seconds + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(2)
    if proc.poll() is None:
        proc.terminate()
    time.sleep(1)

    print("\n[5/5] 收尾")
    quit_wechat()
    if not args.keep_copy:
        shutil.rmtree(RUN_APP, ignore_errors=True)
        print("      已删除重签名副本")

    print("\n----- lldb 输出 -----")
    try:
        with open(log) as fh:
            print(fh.read()[-3000:])
    except OSError:
        pass

    recs = keys.load_capture(capture)
    n_pw = sum(1 for r in recs if r.get("t") == "pbkdf"
               and r.get("rounds") == keys.PBKDF2_ROUNDS)
    print("\n抓到 %d 条记录，其中账号口令 %d 条 -> %s" % (len(recs), n_pw, capture))
    if n_pw:
        print("成功。接下来运行：python3 scripts/clone_data.py")
    else:
        print("没有抓到账号口令。可以加大 --seconds，并在监听期间多操作微信。")


if __name__ == "__main__":
    main()
