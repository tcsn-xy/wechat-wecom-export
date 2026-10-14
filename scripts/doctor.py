#!/usr/bin/env python3
"""体检：确认环境、密钥、快照和导出成品都没问题。

    python3 scripts/doctor.py            # 全部检查
    python3 scripts/doctor.py --links    # 顺带逐个校验导出页里的资源引用
"""
import argparse
import os
import re
import sys
import tempfile
import time
import html as _html
from pathlib import Path
from urllib.parse import unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

OK = "  \033[32m✓\033[0m "
NO = "  \033[31m✗\033[0m "
WARN = "  \033[33m!\033[0m "


def check_deps():
    print("\n【1】Python 依赖")
    mods = [("sqlcipher3", True), ("zstandard", True), ("Crypto", True),
            ("PIL", False), ("av", False)]
    good = True
    for name, required in mods:
        try:
            __import__(name)
            print(OK + name)
        except ImportError:
            if required:
                good = False
                print(NO + "%s（必需，缺失）" % name)
            else:
                print(WARN + "%s（可选，缺失；HEVC 高效图会解不开）" % name)
    print("  Python %s" % sys.version.split()[0])
    return good


def check_config(p):
    print("\n【2】路径与账号")
    items = [
        ("项目根目录", p.root),
        ("数据快照", p.db_storage),
        ("密钥文件", p.capture),
    ]
    for label, path in items:
        exists = os.path.exists(path)
        print((OK if exists else NO) + "%-10s %s" % (label, path))
    print("     wxid      %s" % (p.wxid or "（未知）"))
    print("     uin       %s" % (p.uin or "（未知）"))
    print("     微信 App  %s" % (p.wechat_app or "（未找到）"))
    print("     快照时间  %s" % (p.cloned_at or "（未知）"))


def check_keys(p):
    print("\n【3】密钥")
    from wxexport import keys
    if not os.path.exists(p.capture):
        print(NO + "没有 %s，需要先跑 scripts/capture_keys.py" % p.capture)
        return False
    recs = keys.load_capture(p.capture)
    n = sum(1 for r in recs if r.get("t") == "pbkdf"
            and r.get("rounds") == keys.PBKDF2_ROUNDS)
    print(OK + "capture.jsonl 共 %d 条记录，账号口令 %d 条" % (len(recs), n))
    if not n:
        print(NO + "没有 256000 轮的 PBKDF 记录")
        return False
    kr = keys.Keyring(p.capture)
    ok = True
    for rel in ("session/session.db", "contact/contact.db",
                "message/message_0.db"):
        path = os.path.join(p.db_storage, rel)
        if not os.path.exists(path):
            print(WARN + "%s 不存在，跳过" % rel)
            continue
        v = kr.verify(path)
        ok = ok and v
        print((OK if v else NO) + rel)
    return ok


def check_snapshot(p):
    print("\n【4】数据快照")
    from wxexport import keys
    if not os.path.isdir(p.db_storage):
        print(NO + "没有数据快照，需要跑 scripts/clone_data.py")
        return False
    kr = keys.Keyring(p.capture)
    total = 0
    try:
        c = kr.connect(p.session_db())
        sessions = c.execute("SELECT COUNT(*) FROM SessionTable").fetchone()[0]
        print(OK + "SessionTable：%d 个会话" % sessions)
    except Exception as e:
        print(NO + "读不了 session.db：%s" % e)
        return False

    from wxexport import export as _export
    ex = _export.Exporter(p, with_media=False, verbose=False)
    chats = ex.enumerate_chats()
    for c in chats:
        total += c["count"]
    print(OK + "有消息的会话：%d 个，共 %s 条消息"
          % (len(chats), format(total, ",")))
    room = sum(1 for c in chats if c["is_room"])
    print("     群聊 %d 个 ｜ 私聊 %d 个" % (room, len(chats) - room))

    import glob
    dats = 0
    for _r, _d, fs in os.walk(p.attach):
        dats += sum(1 for x in fs if x.endswith(".dat"))
    print(OK + "媒体缓存：%s 个 .dat 文件（%.1f GB）"
          % (format(dats, ","), _dirsize(p.attach) / 1e9))
    return True


def _dirsize(path):
    total = 0
    for r, _d, fs in os.walk(path):
        for x in fs:
            try:
                total += os.path.getsize(os.path.join(r, x))
            except OSError:
                pass
    return total


def check_export(p, deep_links=False):
    print("\n【5】导出成品")
    if not os.path.isdir(p.export):
        print(NO + "还没有 export/，需要跑 scripts/export_all.py")
        return False
    idx = os.path.join(p.export, "index.html")
    if not os.path.exists(idx):
        print(NO + "缺少 export/index.html")
        return False

    htmls = []
    for r, _d, fs in os.walk(p.export):
        _d[:] = [d for d in _d if not d.startswith(".") and not d.endswith(".previous")]
        for x in fs:
            if x.endswith(".html"):
                htmls.append(os.path.join(r, x))
    chats = len(re.findall(r'<tr data-k=', Path(idx).read_text(encoding="utf-8")))
    print(OK + "索引里有 %d 个会话" % chats)
    print(OK + "共 %d 个 HTML 页面" % len(htmls))
    print(OK + "表情库 %d 个文件（%.1f GB）"
          % (len(os.listdir(os.path.join(p.export, "共享资源", "表情包")))
             if os.path.isdir(os.path.join(p.export, "共享资源", "表情包")) else 0,
             _dirsize(os.path.join(p.export, "共享资源", "表情包")) / 1e9))
    print("     export 总大小 %.2f GB" % (_dirsize(p.export) / 1e9))

    if not deep_links:
        print("  （加 --links 可以逐个校验页面里的图片/表情引用是否都存在）")
        return True

    print("\n  校验资源引用…")
    total_refs = missing = 0
    bad = []
    for i, h in enumerate(htmls, 1):
        try:
            s = Path(h).read_text(encoding="utf-8",errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'(?:src|href)="([^"]+)"', s):
            ref = _html.unescape(m.group(1))
            if ref.startswith(("http:","https:","mailto:","#","data:")):
                continue
            target = os.path.normpath(os.path.join(os.path.dirname(h), unquote(ref)))
            total_refs += 1
            if not os.path.exists(target):
                missing += 1
                if len(bad) < 5:
                    bad.append((os.path.relpath(h, p.export), ref))
        if i % 200 == 0:
            print("     …%d/%d" % (i, len(htmls)))
    pct = (100.0 * (total_refs - missing) / total_refs) if total_refs else 100.0
    print((OK if missing == 0 else WARN)
          + "引用 %s 个资源，缺失 %d 个（完整度 %.2f%%）"
          % (format(total_refs, ","), missing, pct))
    for h, r in bad:
        print("       缺 %s → %s" % (h, r))
    return missing == 0


def check_invariants(p):
    """把 AGENTS.md 第 8 节列的不变量变成机器检查。

    这些每一条都对应一个踩过的坑，破坏任何一条都说明老 bug 回来了。
    """
    print("\n【6】导出不变量（对应公开开发约束）")
    import csv
    E = p.export
    rooms = sorted(os.listdir(os.path.join(E, "群聊"))) \
        if os.path.isdir(os.path.join(E, "群聊")) else []
    privs = sorted(os.listdir(os.path.join(E, "私聊"))) \
        if os.path.isdir(os.path.join(E, "私聊")) else []
    rooms = [d for d in rooms if not d.startswith(".") and not d.endswith(".previous")]
    privs = [d for d in privs if not d.startswith(".") and not d.endswith(".previous")]
    results = []

    # 1) 每条记录都有 发送者wxid
    bad = []
    for kind, ds in (("群聊", rooms), ("私聊", privs)):
        for d in ds:
            f = os.path.join(E, kind, d, "聊天记录.csv")
            if not os.path.exists(f):
                bad.append(d + "（缺文件）")
                continue
            with open(f, encoding="utf-8-sig") as fh:
                r = csv.DictReader(fh)
                if "发送者wxid" not in (r.fieldnames or []):
                    bad.append(d + "（无列）")
                    continue
                for row in r:
                    if not (row.get("发送者wxid") or "").strip():
                        bad.append(d + "（有空值）")
                        break
    results.append(("每条记录都有 发送者wxid", not bad, "%d 个会话有问题" % len(bad), bad[:3]))

    # 2) 群聊都有成员名单
    miss = [d for d in rooms
            if not os.path.exists(os.path.join(E, "群聊", d, "成员名单.csv"))
            or not os.path.exists(os.path.join(E, "群聊", d, "成员名单.html"))]
    results.append(("群聊都有成员名单", not miss, "缺 %d 个" % len(miss), miss[:3]))

    # 3) 会话 ID 不会被当成发送者
    bad3 = []
    for d in rooms:
        f = os.path.join(E, "群聊", d, "聊天记录.csv")
        if not os.path.exists(f):
            continue
        for row in csv.DictReader(Path(f).read_text(encoding="utf-8-sig").splitlines()):
            if (row.get("发送者wxid") or "").endswith("@chatroom"):
                bad3.append((d, row["发送者wxid"]))
                break
    results.append(("会话ID没被当成发送者", not bad3,
                    "%d 个会话有问题" % len(bad3), bad3[:3]))

    # 4) 引用里没有原始 XML
    bad4 = []
    for kind, ds in (("群聊", rooms), ("私聊", privs)):
        for d in ds:
            f = os.path.join(E, kind, d, "聊天记录.txt")
            if not os.path.exists(f):
                continue
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "[引用" in line and ("<msg>" in line or "<appmsg" in line):
                        bad4.append(d)
                        break
    results.append(("引用里没有原始 XML", not bad4,
                    "%d 个会话有问题" % len(bad4), bad4[:3]))

    # 5) 成员名单数字能对上
    bad5 = []
    for d in rooms:
        f = os.path.join(E, "群聊", d, "成员名单.html")
        if not os.path.exists(f):
            continue
        s = Path(f).read_text(encoding="utf-8",errors="replace")
        m = re.search(r"成员发言合计 <b>([\d,]+)</b> 条 ＋ 系统消息/无法定位发送者 "
                      r"<b>([\d,]+)</b> 条 ＝ 本群消息总数 <b>([\d,]+)</b> 条", s)
        if not m:
            bad5.append(d + "（缺核对行）")
            continue
        a, b, c = (int(x.replace(",", "")) for x in m.groups())
        if a + b != c:
            bad5.append("%s（%d+%d≠%d）" % (d, a, b, c))
    results.append(("成员名单数字对得上", not bad5,
                    "%d 个会话有问题" % len(bad5), bad5[:3]))

    # 6) 私聊不该有成员名单（只是提示，不算错）
    extra = [d for d in privs
             if os.path.exists(os.path.join(E, "私聊", d, "成员名单.csv"))]
    if extra:
        results.append(("私聊没有多余成员名单", False,
                        "%d 个私聊有名单" % len(extra), extra[:3]))

    allok = True
    for name, good, detail, sample in results:
        allok = allok and good
        print((OK if good else NO) + "%-26s %s" % (name, detail if not good else "通过"))
        for s in sample:
            print("       %s" % (s,))
    return allok


# --------------------------------------------------------------------------- #
# wechat-cli（第三方查询工具）的明文缓存
#
# 它为了「透明解密」会把整个库解密成明文写到 $TMPDIR/wechat_cli_cache/*.db，
# 而且自己从不清理。本机实测能堆到 500 MB 以上，含完整的 message_0.db。
#
# 我们的快照是加密的、并且一律 PRAGMA query_only 只读打开；这份明文是
# wechat-cli 自己引入的暴露面（目录权限 700，不算裸奔，但确实是落盘明文）。
# 所以纳入体检，并给一个一键清理。删掉之后它下次查询会重新解一遍，不影响我们。
WCCLI_CACHE = os.path.join(tempfile.gettempdir(), "wechat_cli_cache")


def _wccli_cache_files():
    """返回 [(路径, 大小, mtime), ...]。"""
    if not os.path.isdir(WCCLI_CACHE):
        return []
    out = []
    for r, _d, fs in os.walk(WCCLI_CACHE):
        for x in fs:
            path = os.path.join(r, x)
            try:
                out.append((path, os.path.getsize(path), os.path.getmtime(path)))
            except OSError:
                pass
    return out


def clean_wccli_cache():
    """删掉 wechat-cli 写的明文库。"""
    files = _wccli_cache_files()
    total = sum(f[1] for f in files)
    removed = 0
    for path, _sz, _mt in files:
        try:
            os.remove(path)
            removed += 1
        except OSError as e:
            print(WARN + "删不掉 %s: %s" % (path, e))
    try:
        os.rmdir(WCCLI_CACHE)          # 只在空目录时成功，非空就算了
    except OSError:
        pass
    print(OK + "已清理 wechat-cli 明文缓存：%d 个文件，回收 %.1f MB"
          % (removed, total / 1e6))
    return True


def check_wccli_cache(clean=False):
    print("\n【7】wechat-cli 明文缓存")
    if clean:
        return clean_wccli_cache()

    files = _wccli_cache_files()
    if not files:
        print(OK + "没有明文缓存（%s 不存在）" % WCCLI_CACHE)
        return True

    total = sum(f[1] for f in files)
    newest = max(f[2] for f in files)
    print(WARN + "%s 下有 %d 个**明文**数据库，共 %.1f MB"
          % (WCCLI_CACHE, len(files), total / 1e6))
    print("     最近写入：%s"
          % time.strftime("%Y-%m-%d %H:%M", time.localtime(newest)))
    print("     wechat-cli 透明解密时会把整库解开写在这里，且自己不清。")
    print("     清理：./.venv/bin/python scripts/doctor.py --clean-cache")
    return True                        # 是提醒，不算体检失败


def main():
    ap = argparse.ArgumentParser(description="体检")
    ap.add_argument("--links", action="store_true", help="逐个校验导出页的资源引用")
    ap.add_argument("--invariants", action="store_true",
                    help="只跑导出不变量检查（快）")
    ap.add_argument("--clean-cache", action="store_true",
                    help="清掉 wechat-cli 留在 $TMPDIR 的明文数据库")
    ap.add_argument("--snapshot-root")
    ap.add_argument("--capture")
    ap.add_argument("--out")
    args = ap.parse_args()

    from wxexport import config
    print("=" * 64)
    print("  微信聊天记录导出 · 自检")
    print("=" * 64)

    if args.invariants:
        p = config.load(snapshot_root=args.snapshot_root,capture=args.capture,export_root=args.out)
        ok = check_invariants(p)
        print("\n" + "=" * 64)
        print("  全部不变量通过。" if ok else "  有不变量被破坏，见 AGENTS.md 第 8 节。")
        print("=" * 64)
        return 0 if ok else 1

    ok = check_deps()
    p = config.load(snapshot_root=args.snapshot_root,capture=args.capture,export_root=args.out)
    check_config(p)
    ok = check_keys(p) and ok
    snap = check_snapshot(p)
    exp = check_export(p, args.links)
    inv = check_invariants(p)
    check_wccli_cache(args.clean_cache)

    print("\n" + "=" * 64)
    if ok and snap and exp and inv:
        print("  一切正常。打开 %s 查看结果。" % os.path.join(p.export, "index.html"))
    else:
        print("  有问题需要处理，见 docs/troubleshooting.md 与 AGENTS.md")
    print("=" * 64)
    return 0 if ok and snap and exp and inv else 1


if __name__ == "__main__":
    sys.exit(main())
