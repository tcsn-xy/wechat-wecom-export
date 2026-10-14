"""把本项目的密钥与快照喂给 wechat-cli（huohuoer/wechat-cli）。

为什么需要这个
--------------
wechat-cli 自带的 `init` 走的是「扫微信进程内存找 key」这条路，在本机**走不通**：

1. 它的 C 扫描器 `task_for_pid` 附加微信进程，再用 `nftw` 遍历
   `~/Library/Containers/com.tencent.xinWeChat/.../xwechat_files/*/db_storage`
   来收集各库的 salt —— 而这个容器对本机任何外部进程（**含 root**）都是
   `EPERM`（TCC / vnode 级，和企微那条线同一个机制）。
2. 它失败后会自动去 `codesign` **原版 `/Applications/WeChat.app` 本体**，
   而且路径写死 `/Applications/WeChat.app`、`~/Applications/WeChat.app`，
   找不到本机的 `/Applications/未命名文件夹 3/WeChat.app`。

而这两件事我们都已经解决了：密钥在 `keys/capture.jsonl`（PBKDF2 口令推导），
快照在 `data/db_storage`（lldb 克隆，**在微信自己的沙盒里**执行所以能读到）。

所以本脚本只做桥接：把每个库的 salt 和我们推导出的 raw key 写成 wechat-cli
认识的 `all_keys.json`，并把 `config.json` 指向我们的快照。

密钥格式（由 wechat-cli 的 `core/key_utils.py` + `find_all_keys_macos.c` 确定）::

    all_keys.json = {
      "message/message_0.db": {"salt": "<32 hex>", "enc_key": "<64 hex>"},
      ...
    }

`enc_key` 必须是 SQLCipher 的 **raw key**（不是口令）。本项目的
`lib/wxexport/keys.db_key()` 算出的正是它：`PBKDF2-SHA512(口令, salt, 256000, 32)`，
和 wechat-cli 内存里扫到的 `x'<key><salt>'` 里那 64 个 hex 是同一个值。

用法::

    ./.venv/bin/python scripts/wccli_setup.py            # 生成配置
    ./.venv/bin/python scripts/wccli_setup.py --check    # 只体检，不写文件
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from wxexport.keys import Keyring  # noqa: E402

PAGE_SZ = 4096
SALT_LEN = 16
SQLITE_HDR = b"SQLite format 3\x00"
DEFAULT_STATE_DIR = Path.home() / ".wechat-cli"


def collect_db_keys(db_dir: Path, keyring: Keyring) -> tuple[dict, list, list]:
    """遍历快照里的每个 .db，算出 salt 和 raw key。

    返回 (keys, skipped_plain, failed)。

    跳过规则和 wechat-cli 保持一致：非 `.db`、`-wal`/`-shm`、小于一页的、
    以及**已经是明文**的（表头为 SQLite format 3，说明没加密）。
    """
    keys: dict = {}
    skipped_plain: list = []
    failed: list = []

    for path in sorted(db_dir.rglob("*.db")):
        name = path.name
        if name.endswith("-wal") or name.endswith("-shm"):
            continue
        try:
            size = path.stat().st_size
        except OSError as e:
            failed.append((str(path), f"stat 失败: {e}"))
            continue
        if size < PAGE_SZ:
            failed.append((str(path), f"小于一页（{size} 字节）"))
            continue

        try:
            with open(path, "rb") as f:
                header = f.read(SALT_LEN)
        except OSError as e:
            failed.append((str(path), f"读取失败: {e}"))
            continue

        if header.startswith(SQLITE_HDR[:SALT_LEN]):
            skipped_plain.append(str(path))
            continue

        rel = path.relative_to(db_dir).as_posix()  # 正斜杠，wechat-cli 按这个匹配
        try:
            keys[rel] = {
                "salt": header.hex(),
                "enc_key": keyring.key_for(str(path)),
            }
        except Exception as e:  # 口令缺失等
            failed.append((str(path), f"推导密钥失败: {e}"))

    return keys, skipped_plain, failed


def verify_samples(db_dir: Path, keys: dict, keyring: Keyring, n: int = 3) -> list:
    """抽查几个库能否真的用这些 key 打开，避免写出一份看似成功实则错的配置。"""
    import sqlcipher3

    results = []
    picks = [k for k in keys if k.startswith("message/message_")]
    picks += [k for k in keys if k.startswith(("contact/", "session/"))]
    picks = picks[:n] or list(keys)[:n]

    for rel in picks:
        path = db_dir / rel
        try:
            conn = sqlcipher3.connect(str(path))
            conn.execute('PRAGMA key = "x\'%s\'"' % keys[rel]["enc_key"])
            conn.execute("PRAGMA cipher_page_size = 4096")
            conn.execute("PRAGMA query_only = ON")
            n_tables = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
            conn.close()
            results.append((rel, True, f"{n_tables} 张表"))
        except Exception as e:
            results.append((rel, False, str(e)))
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="把本项目的密钥/快照桥接给 wechat-cli")
    ap.add_argument("--db-dir", default=str(ROOT / "data" / "db_storage"),
                    help="快照的 db_storage 目录（默认 data/db_storage）")
    ap.add_argument("--capture", default=str(ROOT / "keys" / "capture.jsonl"),
                    help="密钥捕获文件（默认 keys/capture.jsonl）")
    ap.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR),
                    help="wechat-cli 状态目录（默认 ~/.wechat-cli）")
    ap.add_argument("--check", action="store_true", help="只体检，不写任何文件")
    args = ap.parse_args()

    db_dir = Path(args.db_dir).resolve()
    state_dir = Path(args.state_dir).expanduser()
    capture = Path(args.capture)

    if not db_dir.is_dir():
        print(f"[!] 快照目录不存在: {db_dir}", file=sys.stderr)
        print("    先跑: sudo ./.venv/bin/python scripts/clone_data.py", file=sys.stderr)
        return 1
    if not capture.exists():
        print(f"[!] 密钥文件不存在: {capture}", file=sys.stderr)
        print("    先跑: sudo ./.venv/bin/python scripts/capture_keys.py", file=sys.stderr)
        return 1

    keyring = Keyring(str(capture))
    keys, skipped_plain, failed = collect_db_keys(db_dir, keyring)

    print(f"快照:   {db_dir}")
    print(f"状态:   {state_dir}")
    print(f"密钥:   {len(keys)} 个库")

    if skipped_plain:
        print(f"跳过明文库 {len(skipped_plain)} 个: "
              + ", ".join(Path(p).name for p in skipped_plain[:5]))
    if failed:
        print(f"跳过失败 {len(failed)} 个:")
        for path, why in failed[:8]:
            print(f"    {Path(path).name}: {why}")

    if not keys:
        print("[!] 一个密钥都没生成，中止", file=sys.stderr)
        return 1

    print("\n抽查能否用这些 key 打开:")
    ok = True
    for rel, good, detail in verify_samples(db_dir, keys, keyring):
        print(f"    {'OK ' if good else 'FAIL'} {rel}: {detail}")
        ok = ok and good
    if not ok:
        print("[!] 抽查失败，写出去的配置很可能不可用", file=sys.stderr)
        return 1

    msg_db = [k for k in keys if k.startswith("message/")]
    if not msg_db:
        print("[!] 没有 message/*.db 的密钥，wechat-cli 查不到任何消息", file=sys.stderr)
        return 1

    if args.check:
        print("\n--check：未写文件")
        return 0

    state_dir.mkdir(parents=True, exist_ok=True)
    keys_file = state_dir / "all_keys.json"
    cfg_file = state_dir / "config.json"

    keys_file.write_text(json.dumps(keys, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    cfg_file.write_text(json.dumps({"db_dir": str(db_dir)}, indent=2) + "\n",
                       encoding="utf-8")

    print(f"\n已写入 {keys_file}")
    print(f"已写入 {cfg_file}")
    print("\n试一下:")
    print("    wechat-cli sessions --limit 5")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
