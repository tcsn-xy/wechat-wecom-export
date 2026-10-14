#!/bin/bash
# 第二步：把微信沙盒里的数据搬到 data/
cd "$(dirname "${BASH_SOURCE[0]}")" && source ./_common.sh
banner "② 拉取数据快照"
cat <<'TXT'

把微信自己沙盒里的 db_storage / msg / ilink / resource 用 clonefile
写时复制到项目的 data/ 目录。几乎瞬间完成，不占额外磁盘。

前提：当前运行的微信允许调试器附加。
      如果刚跑完 ①，微信副本还在运行，可以直接继续；
      如果微信已经恢复成官方原版，请先重新跑 ①。
TXT
read -r -p "回车开始，Ctrl-C 取消… " _
sudo "$PY" "$ROOT/scripts/clone_data.py" "$@"
echo
read -r -p "按回车关闭窗口…" _
