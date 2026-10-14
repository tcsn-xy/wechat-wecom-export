#!/bin/bash
# 体检：确认依赖、密钥、快照、导出成品都没问题
cd "$(dirname "${BASH_SOURCE[0]}")" && source ./_common.sh
banner "⑤ 自检"
echo
"$PY" "$ROOT/scripts/doctor.py" "$@"
echo
read -r -p "按回车关闭窗口…" _
