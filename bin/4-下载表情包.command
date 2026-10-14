#!/bin/bash
# 可选：把消息里的表情包从 CDN 取回（可反复跑，已下载的会跳过）
cd "$(dirname "${BASH_SOURCE[0]}")" && source ./_common.sh
banner "④ 下载表情包"
echo
echo "微信不把收到的表情存本地，只能按消息里的链接去 CDN 取。"
echo "链接有有效期，越早跑取回得越完整。"
echo
"$PY" "$ROOT/scripts/fetch_stickers.py" "$@"
echo
read -r -p "按回车关闭窗口…" _
