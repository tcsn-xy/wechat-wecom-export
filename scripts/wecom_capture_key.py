#!/usr/bin/env python3
"""第二步：拿到企业微信数据库的 16 字节 raw key。

为什么必须重启一次企业微信
--------------------------
企业微信的库是 wxSQLite3 风格 AES-128 分页加密，key 是**每台机器一份、登录时
由服务端下发/激活**的随机 16 字节，不写在任何配置文件里，只活在主进程内存。
而主进程是 Hardened Runtime 且没有 ``get-task-allow``：

* ``task_for_pid`` 被内核拒绝（``is hardened … not a declared debugger``），
  root 也一样；
* ``lldb`` 附加、``dtrace`` 挂钩都被拒；
* 系统自带的 ``leaks``/``heap`` 属于只读调试器，但**只能看 readonly 内存**，
  拿不到可写堆里的 key（已实测）。

所以只有一条路：**复制一份 App、重签名去掉 Hardened Runtime**
（``bin/_sign_app.sh`` 会逐组件保留 entitlements，沙盒与容器权限都不丢），
启动这个副本，用 wxdump 导出可写内存，再由 native/wecom_keyscan.c 离线扫描。
不要使用 CC_MD5 断点；当前实现使用可写内存扫描。

注意
----
* **原版 App 一个字节都不动**，重签的是 ``.runtime/WeComSigned.app`` 副本；
* 脚本**不会替你退出企业微信**：它会先检查，发现还在运行就停下来，让你自己退；
* 副本会以同一个账号启动，可能需要你扫码登录一次；抓完脚本会关掉副本，
  你再打开原来的企业微信即可。

    ./.venv/bin/python scripts/wecom_capture_key.py --yes      # 全流程
    ./.venv/bin/python scripts/wecom_capture_key.py --sign-only
    ./.venv/bin/python scripts/wecom_capture_key.py --capture-only
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from wecom import config, crypto, keys  # noqa: E402

LLDB_MODULE = os.path.join(ROOT, "lldb", "wxcap.py")
SIGN_SCRIPT = os.path.join(ROOT, "bin", "_sign_app.sh")
RUNTIME = os.path.join(ROOT, ".runtime")
SIGNED_APP = os.path.join(RUNTIME, "WeComSigned.app")
APP_CANDIDATES = ("/Applications/企业微信.app", "/Applications/WeCom.app",
                  os.path.expanduser("~/Applications/企业微信.app"))


# --------------------------------------------------------------------------- #
def find_app() -> str:
    for p in APP_CANDIDATES:
        if os.path.isdir(p):
            return p
    raise SystemExit("找不到企业微信 App，请用 --app 指定")


def running_pids() -> list:
    out = subprocess.run(["pgrep", "-x", "企业微信"], capture_output=True, text=True)
    return [int(x) for x in out.stdout.split()]


def first_dataset():
    """拿一个加密库做校验（第 1 页就够）。优先当前账号，别挑到备份集。"""
    cands = sorted(glob.glob(os.path.join(config.RAW, "acct", "*", "message.db")))
    cands.sort(key=lambda p: ("Backup" in p, p))
    for p in cands:
        with open(p, "rb") as fh:
            page = fh.read(crypto.PAGE_SIZE)
        if len(page) >= crypto.PAGE_SIZE and \
                crypto.database_format(page) == "wecom-aes128":
            return p, page
    raise SystemExit("找不到可校验的加密库；先跑 scripts/wecom_snapshot.py")


def run_lldb(commands, log_path, timeout=900) -> str:
    cmd = ["lldb", "--batch", "-o", "command script import %s" % shlex.quote(LLDB_MODULE)]
    for c in commands:
        cmd += ["-o", c]
    cmd += ["-o", "quit"]
    with open(log_path, "w") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(1)
        if proc.poll() is None:
            proc.kill()
    return open(log_path, errors="replace").read()


# --------------------------------------------------------------------------- #
def do_sign(app: str):
    os.makedirs(RUNTIME, exist_ok=True)
    if os.path.isdir(SIGNED_APP):
        print("   清理旧副本")
        shutil.rmtree(SIGNED_APP, ignore_errors=True)
    print("   复制一份 App（clonefile 写时复制）…")
    rc = subprocess.run(["cp", "-Rc", app, SIGNED_APP]).returncode
    if rc != 0:
        print("   clonefile 不行，退化为普通拷贝（慢一点）…")
        subprocess.run(["cp", "-R", app, SIGNED_APP], check=True)
    print("   重签名（逐组件保留 entitlements，去掉 Hardened Runtime）…")
    log = subprocess.run(["bash", SIGN_SCRIPT, SIGNED_APP],
                         capture_output=True, text=True)
    tail = "\n".join(log.stdout.strip().splitlines()[-4:])
    print("   " + tail.replace("\n", "\n   "))
    info = subprocess.run(["codesign", "-dvvv", SIGNED_APP],
                          capture_output=True, text=True).stderr
    if "flags=" in info:
        flags = [l for l in info.splitlines() if "flags=" in l][0]
        print("   %s" % flags.strip())
        if "runtime" in flags:
            raise SystemExit("副本仍然带 Hardened Runtime，附加会被拒")
    return SIGNED_APP


def launch_copy():
    exe = os.path.join(SIGNED_APP, "Contents", "MacOS", "企业微信")
    if not os.path.isfile(exe):
        for cand in glob.glob(os.path.join(SIGNED_APP, "Contents", "MacOS", "*")):
            exe = cand
            break
    print("   启动副本：%s" % exe)
    subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    for _ in range(60):
        time.sleep(1)
        if running_pids():
            return exe
    raise SystemExit("副本没起来（可能是单实例机制把它顶掉了）")


def build_scanner() -> str:
    """编译 native/wecom_keyscan.c（扫内存镜像找 key 的小工具）。"""
    src = os.path.join(ROOT, "native", "wecom_keyscan.c")
    out = os.path.join(RUNTIME, "wecom_keyscan")
    if os.path.exists(out) and os.path.getmtime(out) > os.path.getmtime(src):
        return out
    print("   编译内存扫描器 …")
    prefixes = [os.environ.get('OPENSSL_PREFIX', '')]
    try:
        prefixes.append(subprocess.check_output(['brew', '--prefix', 'openssl@3'], text=True).strip())
    except (OSError, subprocess.CalledProcessError):
        pass
    alternatives = [["-I" + p + "/include", "-L" + p + "/lib"] for p in prefixes if p]
    alternatives += [["-I/opt/homebrew/include", "-L/opt/homebrew/lib"], [], ["-I/usr/local/include"]]
    for extra in alternatives:
        cmd = ["clang", "-O2", "-o", out, src, "-lcrypto", "-lpthread"] + extra
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            return out
    raise SystemExit("扫描器编译失败：需要 openssl 头文件（brew install openssl）")


def capture(seconds: int, page_one: bytes, page_path: str) -> bytes | None:
    """抓 key：dump 副本的可写内存 → 离线扫。

    **不要改回「断点命中 CC_MD5」那套**：企业微信的 wxSQLite3 页密钥派生用的是
    **OpenSSL 的 MD5_Update**（二进制里静态链了 md5_block_data_order），
    CC_MD5 一条都断不到（实测第一轮就是这么空的）。dump 内存扫一遍最稳，
    而且不用赌「某一刻刚好在解页」。
    """
    log = os.path.join(RUNTIME, "wecom_capture.log")
    dump_dir = os.path.join(RUNTIME, "memdump")
    page1_file = os.path.join(RUNTIME, "page1.bin")
    with open(page1_file, "wb") as fh:
        fh.write(page_one)

    if os.path.isdir(dump_dir):
        shutil.rmtree(dump_dir, ignore_errors=True)
    os.makedirs(dump_dir, exist_ok=True)
    print("   挂 lldb 导出副本的可写内存（最多 2 GB，几十秒）…")
    text = run_lldb(["wxattach 企业微信 60",
                     "wxdump %s 2048" % shlex.quote(dump_dir)],
                    log, timeout=seconds + 600)
    for line in text.splitlines():
        if "DUMP_DONE" in line or "ATTACHED" in line or "error" in line.lower():
            print("     " + line.strip())

    blobs = sorted(glob.glob(os.path.join(dump_dir, "*.bin")))
    total = sum(os.path.getsize(b) for b in blobs)
    print("   镜像 %d 段 / %.1f MB，开始扫描 …" % (len(blobs), total / 1e6))
    if not blobs:
        return None

    scanner = build_scanner()
    res = subprocess.run([scanner, page1_file] + blobs,
                         capture_output=True, text=True)
    cands = []
    for line in res.stdout.splitlines():
        if line.startswith("HIT"):
            try:
                cands.append(bytes.fromhex(line.split("\t")[-1].strip()))
            except ValueError:
                pass
    uniq = sorted(set(cands))
    print("   候选 %d 个（去重后 %d）" % (len(cands), len(uniq)))

    # 内存镜像里有明文 key，扫完立刻删掉
    shutil.rmtree(dump_dir, ignore_errors=True)
    os.remove(page1_file)

    for k in uniq:
        if crypto.verify_key(k, page_one):
            print("   ✅ 找到有效密钥（第一页校验通过；密钥值不输出）")
            return k
    if uniq:
        print("   候选都没通过校验（可能是别的库的 key）")
    return None


# --------------------------------------------------------------------------- #
def main():
    os.umask(0o077)
    ap = argparse.ArgumentParser(description="抓企业微信数据库 raw key")
    ap.add_argument("--app", default="", help="企业微信 App 路径")
    ap.add_argument("--seconds", type=int, default=180, help="抓取时长")
    ap.add_argument("--sign-only", action="store_true")
    ap.add_argument("--capture-only", action="store_true")
    ap.add_argument("--yes", action="store_true",
                    help="确认已了解：需要你先退出企业微信，副本可能要重新扫码登录")
    ap.add_argument("--wait", type=int, default=0,
                    help="企业微信还在运行时，等它退出的秒数（0=直接退出）")
    args = ap.parse_args()

    page_path, page_one = first_dataset()
    print("校验用库：%s" % page_path)

    app = args.app or find_app()

    if not args.capture_only:
        if args.sign_only:
            # 只复制+重签名，不启动、不附加，所以不用退出企业微信
            print("== 只做「复制 + 重签名」（不启动、不附加，企业微信可以继续开着） ==")
            do_sign(app)
            info = subprocess.run(["codesign", "-d", "--entitlements", "-", SIGNED_APP],
                                  capture_output=True, text=True).stdout
            kept = [k for k in ("app-sandbox", "application-groups", "downloads.read-write")
                    if k in info]
            print("   副本：%s" % SIGNED_APP)
            print("   保住的 entitlements：%s" % (", ".join(kept) or "（无）"))
            print("   真正的抓取请在没有企业微信运行时跑（脚本会检查）。")
            return
        pids = running_pids()
        if pids and args.wait > 0:
            print("\n企业微信还在运行（pid %s）。" % ", ".join(map(str, pids)))
            print("请在菜单栏「退出企业微信」（不是关窗口），我在等它退出…")
            deadline = time.time() + args.wait
            while time.time() < deadline and running_pids():
                time.sleep(2)
            pids = running_pids()
            print("   已退出，继续。" if not pids else "   等超时了。")
        if pids:
            print("\n企业微信正在运行（pid %s）。" % ", ".join(map(str, pids)))
            print("请先自己退出企业微信（菜单栏 →「退出」，不是关窗口），然后重跑本脚本。")
            print("本脚本不会替你退出/关闭它。")
            raise SystemExit(2)
        if not args.yes:
            raise SystemExit(
                "这会复制并重签名一份企业微信副本、启动它去抓密钥，可能要求你扫码登录。\n"
                "确认后加 --yes 再跑。原版 App 不会被改动。")
        print("== 1/3 复制并重签名副本 ==")
        do_sign(app)
        print("== 2/3 启动副本并抓 key ==")
        launch_copy()
    else:
        if not running_pids():
            raise SystemExit("没有正在运行的企业微信/副本，无法附加")

    key = capture(args.seconds, page_one, page_path)
    if not key:
        print("\n没抓到 key。可以试：加大 --seconds；在副本里点开几个会话（会触发解密）。")
        raise SystemExit(1)

    dbs = keys.verify_against(os.path.dirname(os.path.dirname(page_path)), key)
    path = keys.save_key(key, dbs, config.KEY_FILE)
    print("   密钥已保存到 %s（0600）" % path)
    print("   能用它打开的库：%d 个" % len(dbs))

    print("\n== 3/3 收尾 ==")
    if not args.capture_only:
        for pid in running_pids():
            print("   关闭副本 pid=%d" % pid)
            subprocess.run(["kill", "-TERM", str(pid)])
        print("   现在可以重新打开原来的企业微信了。")
    print("\n下一步： ./.venv/bin/python lib/wecom/extract.py")
    print("然后： ./.venv/bin/python scripts/wecom_export.py")


if __name__ == "__main__":
    main()
