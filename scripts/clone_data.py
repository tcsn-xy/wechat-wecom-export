#!/usr/bin/env python3
"""第二步：把微信沙盒里的数据原样搬到项目 data/ 目录。

微信容器受 TCC 保护，外部进程读不了；这里同样借微信进程自己的权限，用
``clonefile(2)`` 在 APFS 上做写时复制（几乎瞬间完成，且不占额外空间）。

**前提**：当前运行的微信必须允许调试器附加。如果抓过密钥后微信已恢复成官方
原版，就需要重新走一遍 ``capture_keys.py``（会把微信重启并要你重新登录一次）。

    sudo python3 scripts/clone_data.py
    sudo python3 scripts/clone_data.py --account wxid_xxx_37a7
    sudo python3 scripts/clone_data.py --list           # 只列出容器里的账号
"""
import argparse
import datetime as _dt
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from localio import task_lock, publish_many, recover
from wxexport import config, keys  # noqa: E402

LLDB_MODULE = os.path.join(ROOT, "lldb", "wxcap.py")
RUNTIME = os.path.join(ROOT, ".runtime")

# 要搬出来的子目录：db_storage 和 msg 是必需的，其余有就拿
SUB_DIRS = ["db_storage", "msg", "ilink", "all_users", "resource", "cache"]

# db_storage 的**一级子目录**。实测（2026-09-20）对整个 db_storage 做目录级
# clonefile 会让 lldb 卡死、微信假死；而逐个克隆它的子目录完全正常
# （session 4.8M / contact 19M / message 756M 都是 rc=0，很快）。
# 所以 db_storage 要"往下一层"克隆，别再改回去。
DB_SUBDIRS = [
    "session", "contact", "message", "media", "biz_message", "favorite", "sns",
    "emoticon", "hardlink", "config", "solitaire", "third_app_icon", "sight",
    "sticker", "head_image", "general", "mm", "brand", "fuzz", "oplog",
    "key_value", "favorite_db", "message_biz", "backup", "emoticon_store",
]


def run_lldb(commands, log_path, timeout=900):
    """跑一次 lldb 批处理，返回日志文本。"""
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
    try:
        with open(log_path) as fh:
            return fh.read()
    except OSError:
        return ""


def run_lldb_exprs(pairs, log_path, timeout=900):
    """用**纯 lldb 批处理脚本**执行一串裸 ``expr`` 克隆。

    ⚠️ 为什么不用 lldb/wxcap.py 里的 ``wxclone`` 命令：实测（2026-09-20）
    从 Python 命令回调里再调 ``expr``/``EvaluateExpression`` 会**死锁**
    （同样的表达式直接写在 lldb 脚本里就秒回 rc=0）。所以这里绕开回调，
    直接生成脚本文件让 lldb 自己跑。
    """
    script = os.path.join(RUNTIME, "clone_script.lldb")
    lines = []
    for src, dst in pairs:
        lines.append('expr -l c++ -- (int)clonefile("%s", "%s", 0)' % (src, dst))
    with open(script, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    cmd = ["lldb", "--batch", "-o", "process attach --name WeChat",
           "-o", "process detach" if False else "-o", "quit"]
    cmd = ["lldb", "--batch", "-s", script]
    # 先 attach，再跑克隆
    with open(script, "w") as fh:
        fh.write("process attach --name WeChat\n")
        fh.write("\n".join(lines) + "\n")
        fh.write("process detach\nquit\n")
    with open(log_path, "w") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(1)
        if proc.poll() is None:
            proc.kill()
            print("  ⚠ lldb 超时，已中止")
    return open(log_path, errors="replace").read()


def discover_accounts(container, walk_out, log_path):
    """让微信自己列出容器目录，从中识别账号目录。"""
    print("  让微信列出容器目录（外部进程读不了，只能请它自己来）…")
    for p in (walk_out,):
        if os.path.exists(p):
            os.remove(p)
    run_lldb(["wxattach WeChat 60",
              "wxwalk %s %s" % (container, walk_out)], log_path, timeout=420)
    accts = set()
    if os.path.exists(walk_out):
        with open(walk_out, errors="replace") as fh:
            for line in fh:
                if not line.startswith(container):
                    continue
                rest = line[len(container):].lstrip("/")
                head = rest.split("/", 1)[0]
                if head.startswith("wxid_"):
                    accts.add(head)
    return sorted(accts)


def main():
    ap = argparse.ArgumentParser(description="拉取微信数据快照")
    ap.add_argument("--account", default="", help="账号目录名，如 wxid_xxx_37a7")
    ap.add_argument("--container", default=config.CONTAINER)
    ap.add_argument("--list", action="store_true", help="只列出容器里的账号")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--keep-legacy", action="store_true",
                    help="保留 ~/Downloads/wx_dump 旧快照")
    ap.add_argument("--image-cache", action="store_true", help="数据库、ilink 与图片缓存，不刷新文件/视频")
    ap.add_argument("--db-only", action="store_true",
                    help="只搬数据库（db_storage/ilink），不搬 msg 那几个 GB 的媒体——增量用")
    ap.add_argument("--out",default=config.DATA)
    ap.add_argument("--capture",default=config.CAPTURE_FILE)
    args = ap.parse_args()
    with task_lock(os.path.join(args.out,".snapshot.lock")):
        return clone_snapshot(args)


def clone_snapshot(args):

    if os.geteuid() != 0:
        print("需要管理员权限才能附加调试器，请用：\n  sudo python3 %s" %
              os.path.relpath(__file__, ROOT))
        sys.exit(1)

    os.makedirs(RUNTIME, exist_ok=True)
    snap = config.read_snapshot(os.path.join(args.out,"_snapshot.json"))

    if args.list:
        accts = discover_accounts(args.container,
                                  os.path.join(RUNTIME, "walk_root.txt"),
                                  os.path.join(RUNTIME, "walk.log"))
        print("容器里的账号目录：")
        for a in accts:
            print("   %s" % a)
        if not accts:
            print("   （没识别出来，可能微信没在运行或不允许附加）")
        return

    account = args.account or snap.get("account_dir") or ""
    if not account:
        accts = discover_accounts(args.container,
                                  os.path.join(RUNTIME, "walk_root.txt"),
                                  os.path.join(RUNTIME, "walk.log"))
        if len(accts) == 1:
            account = accts[0]
        elif accts:
            print("发现多个账号，请用 --account 指定一个：")
            for a in accts:
                print("   %s" % a)
            sys.exit(1)
    if not account:
        raise SystemExit(
            "无法确定账号目录。\n"
            "先运行一次 python3 scripts/capture_keys.py（它会顺带识别账号），\n"
            "或者用 --account wxid_xxxx_37a7 手动指定。")

    acct_path = os.path.join(args.container, account)
    wxid = config.wxid_from_account_dir(account)
    if args.db_only and snap.get("wxid") not in (None,wxid):
        raise SystemExit("换账号不能只刷新数据库并复用旧账号媒体，请完整拉取快照")
    print("账号目录 : %s" % account)
    print("wxid     : %s" % wxid)
    print("源       : %s" % acct_path)
    print("目标     : %s" % args.out)
    print("-" * 60)

    subs = ["db_storage", "ilink", "msg/attach"] if getattr(args,"image_cache",False) else (["db_storage", "ilink"] if args.db_only else SUB_DIRS)

    # 关键 1：**不能直接往项目目录（~/Documents）clone**。微信被重签名后，
    # TCC 对「文稿文件夹」的旧授权失效，clonefile 会卡在一个系统弹窗上等你点允许
    # （实测会挂住十几分钟）。微信本来就有 downloads 读写权限，所以先 clone 到
    # ~/Downloads，再原地 mv 进项目（同一个卷，瞬时）。
    # ⚠️ 脚本是 sudo 跑的，`~` 会展开成 /var/root —— 微信的沙盒只对
    # **真实用户**的 ~/Downloads 有写权限，写 /var/root 一律 EPERM（clonefile -1，踩过）。
    # 所以这里取「发起 sudo 的那个用户」的家目录。
    try:
        import pwd
        home = pwd.getpwnam(os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name).pw_dir
    except (KeyError, ImportError):
        home = os.path.expanduser("~")
    os.makedirs(os.path.join(home,"Downloads"),exist_ok=True)
    stage = tempfile.mkdtemp(prefix="wx_clone_stage-",dir=os.path.join(home,"Downloads"))

    def hand_to_user(path):
        """sudo 建的目录属 root，微信（普通用户）写不进去→ clonefile 直接 EPERM。
        所以把 staging 目录交还给发起 sudo 的那个用户。"""
        try:
            uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
            if uid and gid:
                os.chown(path, int(uid), int(gid))
            os.chmod(path, 0o777)
        except OSError:
            pass

    hand_to_user(stage)

    clones = []
    for sub in subs:
        if sub == "db_storage":
            # 关键 2：db_storage 要拆成一级子目录逐个克隆（见 DB_SUBDIRS 的注释）
            for d in DB_SUBDIRS:
                clones.append(("db_storage/%s" % d,
                               os.path.join(acct_path, sub, d),
                               os.path.join(stage, sub, d)))
        else:
            src = os.path.join(acct_path, sub)
            clones.append((sub, src, os.path.join(stage, sub)))

    # clonefile 不会替我们建父目录 —— 缺了就一律返回 -1（踩过）
    for _sub, _src, dst in clones:
        parent = os.path.dirname(dst)
        os.makedirs(parent, exist_ok=True)
        hand_to_user(parent)

    os.makedirs(args.out, exist_ok=True)
    ok = 0
    log = run_lldb_exprs([(src, dst) for _sub, src, dst in clones],
                         os.path.join(RUNTIME, "clone.log"), timeout=args.timeout)
    for line in log.splitlines():
        if "= 0" in line:
            ok += 1

    print("\n----- lldb 输出 -----")
    print("  成功克隆 %d / %d 个子项" % (ok, len(clones)))
    for line in log.splitlines():
        if "error" in line.lower():
            print("  " + line.strip()[:120])

    kr = keys.Keyring(args.capture)
    for rel in ("session/session.db", "contact/contact.db", "message/message_0.db"):
        if not kr.verify(os.path.join(stage,"db_storage",rel)):
            raise SystemExit("新快照不完整或密钥错误，旧快照保留：" + rel)
    if getattr(args,"image_cache",False) and not os.path.isdir(os.path.join(stage,"msg","attach")):
        raise SystemExit("图片缓存克隆失败，旧快照保留")
    if not args.db_only:
        if not os.path.isdir(os.path.join(stage,"msg")):
            raise SystemExit("完整媒体克隆失败，旧快照保留")
        os.makedirs(os.path.join(stage,"ilink"),exist_ok=True)
    uin = config.uin_from_ilink(os.path.join(stage, "ilink")) or (snap.get("uin") if snap.get("wxid")==wxid else None)
    snap.update(account_dir=account,account_path=acct_path,wxid=wxid,uin=uin,
                wechat_app=config.find_wechat_app(),
                cloned_at=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    config.write_snapshot(snap,os.path.join(stage,"_snapshot.json"))
    mapping={os.path.join(args.out,"db_storage"):os.path.join(stage,"db_storage"),
             os.path.join(args.out,"_snapshot.json"):os.path.join(stage,"_snapshot.json")}
    for sub in subs:
        if sub != "db_storage" and os.path.exists(os.path.join(stage,sub)):
            mapping[os.path.join(args.out,sub)]=os.path.join(stage,sub)
    with task_lock(os.path.join(args.out,".snapshot.lock")):
        publish_many(mapping,os.path.join(args.out,".snapshot.switch.json"))
    shutil.rmtree(stage,ignore_errors=True)
    print("密钥校验：通过；快照就绪，上一份有效数据保存在 .previous")

    if not args.keep_legacy and os.path.isdir(config.LEGACY_DUMP):
        print("\n提示：旧快照还留在 %s（约占 2 GB），确认新快照没问题后可以删掉。"
              % config.LEGACY_DUMP)


if __name__ == "__main__":
    main()
