#!/bin/bash
# 公共前置：定位项目根目录、挑选 Python 解释器。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export WXEXPORT_ROOT="$ROOT"

pick_python() {
  for c in "${WXEXPORT_PYTHON:-}" "$ROOT/.venv/bin/python" \
           "/opt/homebrew/bin/python3.11" \
           "/opt/homebrew/bin/python3" \
           "python3"; do
    [ -x "$c" ] || command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c "import sqlcipher3, zstandard, Crypto" >/dev/null 2>&1; then
      echo "$c"; return 0
    fi
  done
  return 1
}

if ! PY="$(pick_python)"; then
  cat <<'EOF'
找不到带依赖的 Python 环境。

需要这些包：sqlcipher3 / zstandard / pycryptodome （HEVC 图片还需要 av）。
安装方式：

    /opt/homebrew/bin/python3.11 -m venv .venv
    ./.venv/bin/python scripts/deploy.py dependencies

（如果 pip 连不上网，可以复用已有的虚拟环境：设置环境变量
  WXEXPORT_PYTHON=/path/to/venv/bin/python）
EOF
  exit 1
fi
export WXEXPORT_PYTHON="$PY"

banner() {
  echo "════════════════════════════════════════════════════════════"
  echo "  $1"
  echo "  项目目录：$ROOT"
  echo "  Python  ：$PY"
  echo "════════════════════════════════════════════════════════════"
}
