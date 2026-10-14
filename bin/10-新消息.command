#!/bin/bash
cd "$(dirname "${BASH_SOURCE[0]}")" && source ./_common.sh
banner "⑩ 新消息（增量导出）"
# Each platform owns its arguments; a failure must not suppress the other one.
set +e
"$PY" "$ROOT/scripts/new_messages.py" "$@"
status=$?
if [ -t 0 ]; then read -r -p "按回车关闭窗口…" _; fi
exit "$status"
