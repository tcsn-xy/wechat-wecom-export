"""拿到企业微信数据库的 16 字节 raw key。

为什么难
--------
企业微信 5.x 的库是 wxSQLite3 风格的 AES-128 分页加密，key 是**每台机器一份的
随机 16 字节**，不写在任何配置文件里——它只活在进程内存里（登录时由服务端下发或
由本机生成）。而企业微信主进程是 Hardened Runtime 且没有 ``get-task-allow``：

* ``task_for_pid`` 被内核拒绝（日志原文：``is hardened, ... doesn't have
  get-task-allow, ... is not a declared debugger``）；
* ``lldb`` 附加同样被拒。

首次获取密钥请使用 scripts/wecom_capture_key.py：重签副本 → 可写内存导出 → 离线扫描。
本模块中的 memgraph 接口仅为旧格式兼容，不是受限原版客户端的可用读取方法。
"""
from __future__ import annotations

import binascii
import json
import os
import re
import subprocess
import time
from pathlib import Path

from Crypto.Cipher import AES

from . import crypto

# memgraph 里的对象内容可能是
#   * 连续的 32 位十六进制（SQL 字面量 x'...'）
#   * 十六进制 dump（"0a 1b 2c ..."）
#   * 也可能是原始字节
_HEX_RUN = re.compile(rb"[0-9a-fA-F]{32,4096}")
_HEX_TOKEN = re.compile(rb"(?<![0-9a-fA-F])([0-9a-fA-F]{2})(?![0-9a-fA-F])")


# --------------------------------------------------------------------------- #
def _matcher(page_one: bytes):
    """返回一个「这个 16 字节是不是 key」的快速判定函数。"""
    frag = page_one[16:24]
    c0 = page_one[8:16] + page_one[24:32]      # 密文流的第一个 AES 块
    iv = crypto.page_iv(1)

    def is_key(k: bytes) -> bool:
        if len(k) != 16:
            return False
        aes = AES.new(crypto.page_key(k, 1), AES.MODE_CBC, iv)
        return aes.decrypt(c0)[:8] == frag

    return is_key


def candidate_keys_from_bytes(blob: bytes, page_one: bytes, want: int = 4) -> list:
    """在任意字节流里找 key：先试十六进制文本，再退化为逐字节窗口。"""
    is_key = _matcher(page_one)
    found = []
    seen = set()

    def take(k: bytes):
        if k not in seen and is_key(k):
            seen.add(k)
            found.append(k)

    # 1) 连续的 32 位 hex（最常见：SQL 里的 x'<key><salt>'）
    for m in _HEX_RUN.finditer(blob):
        h = m.group(0)
        for off in range(0, min(len(h) - 31, 65), 2):
            try:
                take(bytes.fromhex(h[off:off + 32].decode()))
            except ValueError:
                pass
        if len(found) >= want:
            return found

    # 2) 十六进制 dump 文本 -> 还原字节流
    if b" " in blob or b"\n" in blob:
        toks = _HEX_TOKEN.findall(blob)
        if len(toks) > 64:
            stream = binascii.unhexlify(b"".join(toks))
            for i in range(0, len(stream) - 15):
                take(stream[i:i + 16])
            if len(found) >= want:
                return found

    # 3) 原始字节窗口
    for i in range(0, len(blob) - 15):
        take(blob[i:i + 16])
        if len(found) >= want:
            break
    return found


def candidate_keys_from_file(path: str | Path, page_one: bytes,
                             want: int = 4, chunk: int = 1 << 24) -> list:
    """流式扫描大文件（memgraph 可能几百 MB），不整份读进内存。"""
    is_key = _matcher(page_one)
    found, seen = [], set()
    overlap = 4096
    tail = b""

    def take(k: bytes):
        if k not in seen and is_key(k):
            seen.add(k)
            found.append(k)

    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            data = tail + buf
            # 文本形式
            for m in _HEX_RUN.finditer(data):
                h = m.group(0)
                for off in range(0, min(len(h) - 31, 65), 2):
                    try:
                        take(bytes.fromhex(h[off:off + 32].decode()))
                    except ValueError:
                        pass
            # 原始字节
            for i in range(0, len(data) - 15, 8):
                take(data[i:i + 16])
            if len(found) >= want:
                return found
            tail = data[-overlap:]
    return found


# --------------------------------------------------------------------------- #
# 从进程内存图里取 key
# --------------------------------------------------------------------------- #
def process_name_pid(app: str = "企业微信") -> int:
    out = subprocess.run(["pgrep", "-x", app], capture_output=True, text=True).stdout
    pids = [int(x) for x in out.split()]
    if not pids:
        raise RuntimeError("找不到正在运行的「%s」进程" % app)
    return pids[0]


def dump_memgraph(pid: int, out_path: str | Path, full_content: bool = True,
                  timeout: int = 900) -> dict:
    """调 leaks 把目标进程的内存图（含对象内容）写到 out_path。"""
    out_path = Path(out_path)
    if out_path.exists():
        out_path.unlink()
    cmd = ["leaks", str(pid), "--outputGraph=%s" % out_path]
    if full_content:
        cmd.append("--fullContent")
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return {
        "cmd": " ".join(cmd),
        "rc": proc.returncode,
        "seconds": round(time.time() - t0, 1),
        "stdout": proc.stdout[-2000:],
        "stderr": proc.stderr[-2000:],
        "size": out_path.stat().st_size if out_path.exists() else 0,
    }


def find_key(page_one: bytes, memgraph: str | Path, want: int = 4) -> list:
    """从内存图文件里恢复候选 raw key。"""
    return candidate_keys_from_file(memgraph, page_one, want=want)


def verify_against(data_dir: str | Path, key: bytes) -> list:
    """用一把 key 去开这个目录下所有加密库，返回能开的库（相对路径）。"""
    data_dir = Path(data_dir)
    ok = []
    for path in sorted(data_dir.rglob("*.db")):
        if path.name.endswith(("-wal", "-shm")):
            continue
        try:
            with open(path, "rb") as fh:
                page = fh.read(crypto.PAGE_SIZE)
        except OSError:
            continue
        if len(page) < crypto.PAGE_SIZE:
            continue
        if crypto.database_format(page) == "wecom-aes128" and crypto.verify_key(key, page):
            ok.append(str(path.relative_to(data_dir)))
    return ok


def save_key(key: bytes, databases: list, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "algorithm": "wxsqlite3-aes128-cbc",
        "raw_key_hex": key.hex(),
        "validated_databases": databases,
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.chmod(path, 0o600)
    return path


def load_key(path: str | Path) -> bytes:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    key = bytes.fromhex(data["raw_key_hex"])
    if len(key) != 16:
        raise ValueError("密钥文件里的 raw_key_hex 不是 16 字节")
    return key
