#!/bin/bash
# 第三步：离线导出全部会话
cd "$(dirname "${BASH_SOURCE[0]}")" && source ./_common.sh
banner "③ 导出全部聊天记录"
cat <<'TXT'

这一步完全离线，不需要微信在运行，也不会改动任何原始数据。
输出在 export/ 目录：
    export/index.html          总索引（先看这个）
    export/群聊/<群名>/        群聊记录
    export/私聊/<名字>/        单聊记录
    export/共享资源/表情包/    全局去重的表情包
TXT
echo
"$PY" "$ROOT/scripts/export_all.py" "$@"
STATUS=$?
echo
if [ $STATUS -eq 0 ]; then
  open "$ROOT/export/index.html" 2>/dev/null || true
fi
read -r -p "按回车关闭窗口…" _
