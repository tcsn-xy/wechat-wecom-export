#!/bin/bash
# 只看某个会话最近几天的记录（按天分页）
cd "$(dirname "${BASH_SOURCE[0]}")" && source ./_common.sh
banner "⑥ 看最近记录"
echo
read -r -p "会话名或ID（可只写片段，留空则列出全部）: " WHO
read -r -p "最近几天 [7]: " DAYS
DAYS="${DAYS:-7}"
echo
if [ -z "$WHO" ]; then
  "$PY" "$ROOT/scripts/recent.py" --list
else
  "$PY" "$ROOT/scripts/recent.py" --chat "$WHO" --days "$DAYS"
fi
echo
read -r -p "按回车关闭窗口…" _
