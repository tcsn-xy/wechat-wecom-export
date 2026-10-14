#!/bin/bash
# 企业微信 ② 抓数据库密钥 —— 唯一需要动一次企业微信的步骤
cd "$(dirname "${BASH_SOURCE[0]}")" && source ./_common.sh
banner "企微② 抓数据库密钥（需要先退出企业微信）"
cat <<'TXT'

为什么要动一次：
  企业微信的库是 wxSQLite3 AES-128 加密，那把 16 字节 key 是登录时服务端下发的，
  只活在主进程内存里；而主进程带 Hardened Runtime，task_for_pid / lldb / dtrace
  全部被系统拒绝（root 也一样），leaks 之类的只读调试器又只能看 readonly 内存。
  所以只能：复制一份 App → 逐组件重签名（去掉 Hardened Runtime，保留沙盒权限）
  → 启动副本 → 挂 lldb 断在 CC_MD5 上抓 key。

  * 原版 /Applications/企业微信.app 一个字节都不动，动的是 .runtime/ 里的副本；
  * 副本第一次起来可能要你扫码登录一次；
  * 抓完脚本会自动关掉副本，你再打开原来的企业微信即可。

TXT
"$PY" "$ROOT/scripts/wecom_capture_key.py" --yes --wait 600 "$@"
STATUS=$?
echo
[ $STATUS -eq 0 ] && echo "下一步：双击 9-企微-导出.command" \
                  || echo "没成功的话把输出发我，我再想办法。"
read -r -p "按回车关闭窗口…" _
