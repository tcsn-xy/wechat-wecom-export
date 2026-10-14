#!/bin/bash
# 第一步：抓取微信数据库密钥（只在第一次、或换账号/重新登录后需要）
cd "$(dirname "${BASH_SOURCE[0]}")" && source ./_common.sh
banner "① 抓取密钥"
cat <<'TXT'

这一步会：
  · 复制一份 WeChat.app 到 .runtime/，只给副本重新签名（原版不动）
  · 退出当前微信，用副本启动，并让你重新登录一次
  · 在后台断点监听，把账号口令抓出来存到 keys/capture.jsonl
  · 结束后退出副本，恢复原状

需要输入管理员密码。完成后重签名副本会被删除。
TXT
read -r -p "回车开始，Ctrl-C 取消… " _
sudo "$PY" "$ROOT/scripts/capture_keys.py" "$@"
echo
read -r -p "按回车关闭窗口…" _
