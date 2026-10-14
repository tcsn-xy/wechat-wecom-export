"""企业微信导出用的路径与常量。"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA = os.path.join(ROOT, "data", "wecom")
RAW = os.path.join(DATA, "raw")          # 从容器里搬出来的原始（加密）快照
PLAIN = os.path.join(DATA, "plain")      # 解密后的明文库
EXPORT = os.path.join(ROOT, "export", "wecom")
KEYS = os.path.join(ROOT, "keys")
KEY_FILE = os.path.join(KEYS, "wecom_key.json")
KEYSALT_LOG = os.path.join(KEYS, "wecom_saltkeys.jsonl")

# 容器真实路径（外部进程读不了，只有沙盒里那双手能读）
CONTAINER_DATA = os.path.expanduser(
    "~/Library/Containers/com.tencent.WeWorkMac/Data")
# 沙盒内的中转站：企业微信自己有 downloads 读写权限
STAGING = os.path.expanduser("~/Downloads/wecom_snapshot")

# 沙盒里 $HOME 就是容器 Data，所以下面这些都是相对 $HOME 的路径
WXWORK = "Library/Application Support/WXWork"
WXWORK_DATA = WXWORK + "/Data"
GROUP_CONTAINER = "Library/Group Containers/88L2Q4487Q.com.tencent.WeWorkMac"

CORE_DBS = ("message.db", "session.db", "user.db")

# 值得一并搬出来的东西（相对 $HOME）
EXTRA_PATHS = (
    "Library/Application Support/WXWork/Data/Global",
    "Documents/local_storage_index.db",
    "Documents/local_en",
    "Documents/Network",
    "Documents/Profiles",
)


def ensure_dirs():
    for d in (DATA, RAW, PLAIN, EXPORT):
        os.makedirs(d, exist_ok=True)
    return DATA
