#!/bin/bash
# 企业微信 ③ 解密 + 导出
cd "$(dirname "${BASH_SOURCE[0]}")" && source ./_common.sh
banner "企微③ 解密并导出"
cat <<'TXT'

有密钥就先把 data/wecom/raw 里的加密库解密成 data/wecom/plain，
再导出到 export/wecom/。没有密钥时会退化成只导「会话快照」（明文，含每个
会话最后一条消息）。

TXT
if [ -f "$ROOT/keys/wecom_key.json" ]; then
  "$PY" "$ROOT/lib/wecom/extract.py" --quiet || true
  echo
fi
"$PY" "$ROOT/scripts/wecom_export.py"
STATUS=$?
echo
if [ $STATUS -eq 0 ]; then
  open "$ROOT/export/wecom/会话快照.html" 2>/dev/null || true
fi
read -r -p "按回车关闭窗口…" _
