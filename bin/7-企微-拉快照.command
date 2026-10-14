#!/bin/bash
# 企业微信 ① 拉快照 —— 完全不动你的窗口、不需要任何系统授权
cd "$(dirname "${BASH_SOURCE[0]}")" && source ./_common.sh
banner "企微① 拉取数据快照"
cat <<'TXT'

企业微信的数据在它自己的沙盒里，外部进程（连 root 都不行）读不了。
这里借它自己的 Node 子进程在沙盒内开一个 shell，用 clonefile（写时复制）
把数据原样搬到项目的 data/wecom/raw。

全程只读、不改 App、不重启、不弹授权框、不抢焦点。企业微信要在运行中。

TXT
"$PY" "$ROOT/scripts/wecom_snapshot.py" "$@"
STATUS=$?
echo
[ $STATUS -eq 0 ] && echo "下一步：双击 8-企微-抓密钥.command（需要先退出企业微信）" \
                  || echo "出错了，把上面的输出发我。"
read -r -p "按回车关闭窗口…" _
