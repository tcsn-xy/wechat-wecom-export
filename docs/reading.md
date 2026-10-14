# 读取信息的方法

两平台容器、加密协议和账号命名空间独立。源数据库只读；克隆、解密、导出写入部署目录下的私密产物。

## 企业微信

日常快照借企业微信自带 WeDrive/wemail Node 子进程的 inspector 通道，在其沙盒内读取，只连接本机 `127.0.0.1:9229`。不是直接从外部进程绕过容器权限；不使用 lldb 附加受限原版客户端。

1. 正常启动并登录企业微信，确保存在其 Node 子进程，先取得加密快照。
2. 第一次获取 raw key：自行退出企业微信，然后运行抓取命令。命令复制并逐组件重签 `.runtime/WeComSigned.app`，启动副本；按屏幕提示登录。
3. `wxdump` 导出副本可写内存，原生扫描器用数据库第一页验证候选 key，成功后保存本机 key。不要使用 `CC_MD5` 断点或 `leaks` 读取受限原版。
4. 恢复正常客户端，解密快照，验证并导出。

```bash
./.venv/bin/python scripts/wecom_snapshot.py --with-media
# 自行退出企业微信之后，确认允许副本重签/启动/登录再执行
./.venv/bin/python scripts/wecom_capture_key.py --yes
./.venv/bin/python lib/wecom/extract.py
./.venv/bin/python scripts/wecom_inspect.py --rows 0
./.venv/bin/python scripts/wecom_export.py
./.venv/bin/python scripts/wecom_recent.py --no-snapshot
```

用 `--app /path/to/企业微信.app` 指定非标准安装位置。初次操作可能需要登录，脚本不会代替你退出正在运行的企业微信。不要删除持久密钥文件，除非已确认不再需要；临时内存镜像由抓取流程清理。

企微数据库采用 wxSQLite3 AES-128 分页加密，不是 SQLCipher；解密会合并已提交 WAL，再验证 SQLite。数据库账号与媒体 Profiles 目录是不同 ID 命名空间，多账号映射不明时需在本地 raw 快照设置 `_profile_accounts.json`，格式 `{数据库账号ID:媒体档案目录名}`；不跨账号合并。

## 个人微信

```bash
# 首次抓密钥需要管理员权限，会退出当前微信并启动重签副本，按提示登录
sudo ./.venv/bin/python scripts/capture_keys.py --keep-copy
# 抓取结束会退出副本；此时可先尝试直读快照
./.venv/bin/python scripts/clone_data_local.py
# 如需日常运行副本，单独启用并按提示登录；此命令不抓密钥
./.venv/bin/python scripts/capture_keys.py --switch-copy
# 当前已登录副本运行时拉取快照
sudo ./.venv/bin/python scripts/clone_data.py
./.venv/bin/python scripts/export_all.py
./.venv/bin/python scripts/doctor.py --links --invariants
./.venv/bin/python scripts/recent.py --list
./.venv/bin/python scripts/wccli_search.py "关键词" --source full
```

微信使用 SQLCipher。所有消息分片均参与枚举，同表名的 `message_0.db` 与 `message_1.db` 可能是不同历史，不能删掉其中一个。显示名优先级：备注 → 每群昵称 → 微信昵称 → 微信号 → wxid。

微信直读失败时保留旧快照，后台退回小时机制，不自动重签、重启或登录。原版 App 不修改。客户端自更新可能使副本恢复官方签名，需人工评估后重新重签。

## 输出与覆盖

微信输出 `export/`，企微输出 `export/wecom/`，支持 HTML/TXT/CSV 与本机可获得的媒体。未缓存图片、附件或失败转换保留缺失标记/原始文件，不声称全部媒体已恢复。表情下载是可选 CDN 请求：`scripts/fetch_stickers.py`。

快照、明文、查询输出和 HTML 含私人信息，不能进入公开仓库、在线调试服务或示例数据。外部插件 `wechat-cli` 若使用 `init` 可能修改原版 App，配置只使用 `scripts/wccli_setup.py`；其明文缓存可用 `doctor.py --clean-cache` 清理。
