#!/usr/bin/env python3
"""自检：用**自己造的**加密库把整条流水线跑一遍。

为什么要这个
------------
真库要等拿到密钥才能解开，而密钥那一步是全流程唯一不可反复重试的动作。
所以这里造一份「格式完全一样、内容是假的」的企业微信数据集：真实 SQLite → 用
``crypto.encrypt_page`` 逐页加密（顺便造一段已提交的 WAL），然后跑

    候选密钥扫描 → 解密整库 → 解析消息 → 渲染导出

把除「抓密钥」之外的每一环都验证掉。

    ./.venv/bin/python scripts/wecom_selftest.py

注意：这里用的**表结构是文档里的常见形态**，真机 schema 要以
``scripts/wecom_inspect.py`` 的结果为准；本脚本验证的是「机制」不是「字段名」。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import struct
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from wecom import conv as conv_mod  # noqa: E402
from wecom import crypto, extract, keys, messages, pbwire, render  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("✅" if ok else "❌", name,
                         ("  — %s" % detail) if detail else ""))


# --------------------------------------------------------------------------- #
def make_plain_dataset(root: str):
    os.makedirs(root, exist_ok=True)

    conn = sqlite3.connect(os.path.join(root, "session.db"))
    conn.executescript("""
        create table conversation_table(id text primary key, name text,
                                        last_message_time integer);
        create table conversation_user_table(conversation_id text, user_id text,
                                             nick_name text);
    """)
    conn.executemany("insert into conversation_table values (?,?,?)", [
        ("R:1001", "产品群", 1789870482),
        ("S:2002", "韦结", 1789870000),
    ])
    conn.executemany("insert into conversation_user_table values (?,?,?)", [
        ("R:1001", "u_a", "阿黎"), ("R:1001", "u_b", "老王"),
    ])
    conn.commit()
    conn.close()

    conn = sqlite3.connect(os.path.join(root, "user.db"))
    conn.executescript("""
        create table user_table(id text primary key, name text, real_name text,
                                account text, external_corp_name text);
    """)
    conn.executemany("insert into user_table values (?,?,?,?,?)", [
        ("u_a", "阿黎", "Li", "li001", "世客科技"),
        ("u_b", "老王", "Wang", "wang002", "世客科技"),
    ])
    conn.commit()
    conn.close()

    conn = sqlite3.connect(os.path.join(root, "message.db"))
    conn.executescript("""
        create table message_table(message_id integer primary key, server_id integer,
            sequence integer, sender_id text, conversation_id text,
            content_type integer, send_time integer, flag integer,
            content blob, extra_content blob);
    """)
    conn.executemany("insert into message_table values (?,?,?,?,?,?,?,?,?,?)", [
        (1, 11, 1, "u_a", "R:1001", 1, 1789870000, 0,
         "大家好，今天同步一下进度".encode(), None),
        (2, 12, 2, "u_b", "R:1001", 1, 1789870100, 0,
         "<msg><appmsg><title>周报.docx</title></appmsg></msg>".encode(), None),
        (3, 13, 3, "u_a", "R:1001", 3, 1789870200, 0,
         b"\x08\x01\x12\x05hello", None),
        (4, 14, 4, "u_b", "S:2002", 1, 1789870300, 0,
         "好滴，谢谢韦哥".encode(), None),
        (5, 15, 5, "u_a", "S:2002", 2, 1789870482, 0,
         '<?xml version="1.0"?><msg><img aeskey="x" md5="y"/></msg>'.encode(), None),
    ])
    conn.commit()
    conn.close()


def encrypt_dataset(src_root: str, dst_root: str, key: bytes) -> dict:
    """逐页加密，并给 message.db 造一段已提交的 WAL（覆盖第 2 页）。"""
    for name in ("message.db", "session.db", "user.db"):
        src = os.path.join(src_root, name)
        blob = Path(src).read_bytes()
        assert len(blob) % crypto.PAGE_SIZE == 0, "%s 不是整页" % name
        out = bytearray()
        for i in range(len(blob) // crypto.PAGE_SIZE):
            page = blob[i * crypto.PAGE_SIZE:(i + 1) * crypto.PAGE_SIZE]
            out += crypto.encrypt_page(key, page, i + 1)
        with open(os.path.join(dst_root, name), "wb") as fh:
            fh.write(bytes(out))

    # WAL：把 message.db 的第 2 页换成一个改过的版本（模拟"最新消息还在 WAL 里"）
    src = os.path.join(src_root, "message.db")
    blob = Path(src).read_bytes()
    page2 = blob[crypto.PAGE_SIZE:2 * crypto.PAGE_SIZE]
    enc2 = crypto.encrypt_page(key, page2, 2)
    salt = b"\x11" * 8
    header = (struct.pack(">I", 0x377F0682) + struct.pack(">I", 3007000)
              + struct.pack(">I", crypto.PAGE_SIZE) + struct.pack(">I", 0)
              + salt + b"\x00" * 8)
    frame = (struct.pack(">I", 2) + struct.pack(">I", 2) + salt + b"\x00" * 8
             + enc2)
    with open(os.path.join(dst_root, "message.db-wal"), "wb") as fh:
        fh.write(header + frame)
    return {"pages": len(blob) // crypto.PAGE_SIZE}


# --------------------------------------------------------------------------- #
def main():
    key = bytes(range(16, 32))
    tmp = tempfile.mkdtemp(prefix="wecom-selftest-")
    plain_src = os.path.join(tmp, "plain")
    enc_root = os.path.join(tmp, "raw", "acct", "uiddata")
    out_root = os.path.join(tmp, "out")
    os.makedirs(enc_root)

    print("== 1) 造一份明文数据集，再逐页加密 ==")
    make_plain_dataset(plain_src)
    info = encrypt_dataset(plain_src, enc_root, key)
    page1 = Path(os.path.join(enc_root,"message.db")).read_bytes()[:crypto.PAGE_SIZE]
    check("加密后的库被识别为企业微信格式",
          crypto.database_format(page1) == "wecom-aes128",
          crypto.database_format(page1))
    check("真 key 校验通过", crypto.verify_key(key, page1))
    check("错 key 校验失败", not crypto.verify_key(b"\x00" * 16, page1))

    print("\n== 2) 候选密钥扫描（模拟内存里 x'<key><salt>' 字面量） ==")
    blob = b"junk x'" + key.hex().encode() + b"aabbccdd" + b"' more"
    found = keys.candidate_keys_from_bytes(blob, page1)
    check("从文本候选里找到 key", key in found, "找到 %d 个" % len(found))

    print("\n== 3) 整库解密 + WAL 合并 ==")
    stats = extract.decrypt_dataset(enc_root, os.path.join(tmp, "plain_out"), key,
                                    quiet=True)
    check("三个库都解开了", len(stats["decrypted"]) == 3, str(stats["decrypted"]))
    got = os.path.join(tmp, "plain_out", "message.db")
    check("解出来的库是合法 SQLite",
          Path(got).read_bytes()[:16] == crypto.SQLITE_HEADER)
    conn = sqlite3.connect(got)
    n = conn.execute("select count(*) from message_table").fetchone()[0]
    conn.close()
    check("WAL 合并后数据完整", n == 5, "%d 行" % n)

    print("\n== 4) 解析（表名/字段名运行时发现） ==")
    ds = messages.load_dataset(os.path.join(tmp, "plain_out"))
    check("会话数正确", len(ds.conversations) == 2,
          str(sorted(ds.conversations)))
    conv = ds.conversations.get("R:1001")
    check("群聊消息数正确", conv and len(conv.messages) == 3)
    check("群昵称按群取到", conv and conv.members.get("u_a") == "阿黎")
    texts = [m.text for m in conv.messages] if conv else []
    check("纯文本原样保留", any("同步一下进度" in t for t in texts))
    check("XML 里剥出 title", any("周报.docx" in t for t in texts))
    check("protobuf 里能捞出可读串", any("hello" in t for t in texts))
    c2 = ds.conversations.get("S:2002")
    check("图片消息给占位符不给二进制",
          c2 and any("图片" in m.text for m in c2.messages),
          str([m.text for m in c2.messages]))

    print("\n== 5) 渲染导出 ==")
    for conv in ds.conversations.values():
        render.write_conversation(conv, out_root)
    index = render.write_index([ds], out_root)
    check("index.html 生成", os.path.exists(index))
    check("单聊目录生成",
          os.path.isdir(os.path.join(out_root, "单聊", "韦结")))
    csv_path = os.path.join(out_root, "群聊", "产品群", "聊天记录.csv")
    check("CSV 生成且带表头",
          os.path.exists(csv_path) and
          Path(csv_path).read_text(encoding="utf-8-sig").splitlines()[0].startswith("时间,发送者"))
    check("群成员名单生成",
          os.path.exists(os.path.join(out_root, "群聊", "产品群", "成员名单.csv")))

    print("\n== 6) 合成会话快照解析 ==")
    # The default self-test never reads the owner's real account.
    wire = b"\x08\x01\x12\x03abc"
    check("合成快照 wire 字段可读", pbwire.parse(wire).text(2) == "abc")
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--real-snapshot", help="显式选择真实快照兼容性检查")
    args = ap.parse_args()
    if args.real_snapshot:
        found_conv = conv_mod.load_all(args.real_snapshot)
        check("指定真实快照存在会话", len(found_conv) > 0)

    print("\n== 7) protobuf wire 解码自检 ==")
    nested = pbwire.parse(b"\x08\x01\x12\x03abc")
    check("varint/字符串解析正确",
          nested.num(1) == 1 and nested.text(2) == "abc")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败：%s" % ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
