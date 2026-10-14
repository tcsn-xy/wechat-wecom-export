"""把加密的企业微信数据库解密成明文 SQLite，供后面解析/导出使用。

输入是 :mod:`wecom.snapshot` 搬出来的原始快照（``data/wecom/raw``），
输出是 ``data/wecom/plain``。**只读原始快照，绝不写回容器。**

明文库里还会把 ``-wal`` 里已提交的事务合并进去（企业微信退出前的最新消息
往往还在 WAL 里），所以解析明文库不需要再管 WAL。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# 既支持 `python -m wecom.extract`，也支持 `python lib/wecom/extract.py`
# （bin/9-企微-导出.command 就是后者），所以要自己把 lib 放进 sys.path。
_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from wecom import config, crypto  # noqa: E402
from localio import staged_directory, task_lock


def _plain_path(src: Path, src_root: Path, dst_root: Path) -> Path:
    return dst_root / src.relative_to(src_root)


def decrypt_dataset(src_root, dst_root, key: bytes, quiet: bool = False) -> dict:
    """把一个数据集目录（含 message.db/session.db/user.db）解密到 dst_root。"""
    src_root, dst_root = Path(src_root), Path(dst_root)
    stats = {"decrypted": [], "copied_plain": [], "skipped": [], "failed": []}
    for src in sorted(src_root.rglob("*")):
        if not src.is_file() or src.suffix != ".db":
            continue
        dst = _plain_path(src, src_root, dst_root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            blob = src.read_bytes()
        except OSError as exc:
            stats["failed"].append((str(src), str(exc)))
            continue
        if len(blob) < crypto.PAGE_SIZE:
            stats["skipped"].append(str(src))
            continue

        fmt = crypto.database_format(blob[:crypto.PAGE_SIZE])
        if fmt == "unknown":
            stats["skipped"].append(str(src))
            continue
        if fmt == "sqlite":
            stats["copied_plain"].append(str(src.relative_to(src_root)))

        wal_path = Path(str(src) + "-wal")
        wal = wal_path.read_bytes() if wal_path.exists() else None
        try:
            plain, info = crypto.decrypt_database_bytes(blob, key, wal=wal)
        except ValueError as exc:
            stats["failed"].append((str(src), str(exc)))
            continue
        dst.write_bytes(plain)
        if not quiet:
            print("   %-46s %5.2f MB -> %5.2f MB  (WAL +%d)"
                  % (src.relative_to(src_root), len(blob) / 1e6,
                     len(plain) / 1e6, info["wal_frames"]))
        if fmt != "sqlite":
            stats["decrypted"].append(str(src.relative_to(src_root)))
    return stats


def datasets(raw_root=None) -> list:
    """列出快照里所有数据集（同时含三个核心库的目录）。

    raw 快照的层级是 ``<root>/acct/<数据集>``，解密输出是 ``<root>/<数据集>``，
    两种都认。
    """
    root = Path(raw_root or config.RAW)
    if not root.is_dir():
        return []
    acct = root / "acct"
    base = acct if acct.is_dir() else root
    out = []
    for path in sorted(base.iterdir()):
        if path.is_dir() and all((path / f).is_file() for f in config.CORE_DBS):
            out.append(path)
    return out


def decrypt_all(raw_root=None, plain_root=None, key: bytes | None = None,
                quiet: bool = False) -> dict:
    from wecom import keys as keys_mod

    raw_root = Path(raw_root or config.RAW)
    plain_root = Path(plain_root or config.PLAIN)
    if key is None:
        key = keys_mod.load_key(config.KEY_FILE)

    results = {}
    found = datasets(raw_root)
    if not found:
        raise ValueError("原始快照没有完整数据集")
    with task_lock(raw_root.parent / ".snapshot.lock"), \
         task_lock(plain_root.parent / ".extract.lock"), staged_directory(plain_root) as staging:
        for ds in found:
            dst = Path(staging) / ds.name
            if not quiet:
                print("== 数据集 %s ==" % ds.name)
            st = results[ds.name] = decrypt_dataset(ds, dst, key, quiet=quiet)
            if st["failed"] or not all((dst / core).exists() for core in config.CORE_DBS):
                raise RuntimeError("数据集解密不完整：%s；%s" % (ds.name, st["failed"]))
            import sqlite3
            for core in config.CORE_DBS:
                with sqlite3.connect("file:%s?mode=ro&immutable=1" % (dst / core), uri=True) as c:
                    if c.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("解密库完整性检查失败：%s" % core)
    return results


def main():
    import argparse

    ap = argparse.ArgumentParser(description="解密企业微信数据库快照")
    ap.add_argument("--key", default=config.KEY_FILE, help="密钥 json")
    ap.add_argument("--raw", default=config.RAW)
    ap.add_argument("--out", default=config.PLAIN)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    from wecom import keys as keys_mod

    key = keys_mod.load_key(args.key)
    res = decrypt_all(args.raw, args.out, key, quiet=args.quiet)
    total = sum(len(v["decrypted"]) for v in res.values())
    print("\n解密完成：%d 个库 -> %s" % (total, args.out))
    for name, st in res.items():
        if st["failed"]:
            print("  ⚠ %s 有 %d 个库失败" % (name, len(st["failed"])))
            for path, err in st["failed"][:3]:
                print("     %s: %s" % (path, err))


if __name__ == "__main__":
    main()
