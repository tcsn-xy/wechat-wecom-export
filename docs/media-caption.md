# 媒体、caption 和其他处理

## 图片与语音文件

图片和语音保存到 `media_store/`，按平台、账号与 SHA256 隔离；MySQL 保存相对路径、哈希和关联。原始语音及成功解码的 WAV 分开保存。视频、附件和表情保持兼容方式；不做语音转写。

```bash
./.venv/bin/python scripts/chat_media_sync.py
./.venv/bin/python scripts/chat_media_manage.py status
./.venv/bin/python scripts/chat_media_manage.py verify
# 仅已有 BLOB 需要迁移时使用；此操作会清除已验证项的库内 BLOB
./.venv/bin/python scripts/chat_media_manage.py migrate --limit 10
./.venv/bin/python scripts/chat_media_manage.py migrate
./.venv/bin/python scripts/chat_media_manage.py verify
```

迁移逐项写文件、重新读取验证，再事务清除对应 BLOB/分片；保留行 ID、消息关联及来源。验证失败保留原 BLOB。不会删除恢复备份，也不会执行 OPTIMIZE。电脑无缓存的图片无法凭空补出；小时任务持续检查历史缺失缓存。

更换根目录时先复制并验证整套文件，再修改私密配置 `media_root` 或 `CHAT_MEDIA_ROOT`；所有前台与后台进程必须使用同一根目录。部署入口会把显式的非敏感路径传给后台。

## 图片描述与 OCR（默认关闭）

可选使用 MiniMax Coding Plan `/v1/coding_plan/vlm`。服务收到的是图片 JPEG 识别版本和固定提示词，不附带联系人、聊天上下文或账号。图片本身可能含隐私，必须在当前部署得到用户的外发授权后启用。不要把原项目曾获的授权当成新用户授权。

```bash
# 只保存配置，保持关闭；密钥交互隐藏输入，文件 0600，存在时拒绝覆盖
./.venv/bin/python scripts/deploy.py configure-caption
# 若用户明确允许发送图片，可首次配置时加 --allow-image-upload
# 已存在配置时，人工确认并编辑 enabled 与 image_external_consent 为 true
./.venv/bin/python scripts/deploy.py check --caption
# 一次少量验证（同样要求配置中的两个授权开关）
./.venv/bin/python scripts/image_caption_worker.py --limit 2 --concurrency 2
./.venv/bin/python scripts/deploy.py service caption install --allow-image-upload --concurrency 4
./.venv/bin/python scripts/deploy.py service caption status
./.venv/bin/python scripts/chat_media_manage.py search --keyword '会议'
./.venv/bin/python scripts/chat_media_manage.py retry
./.venv/bin/python scripts/deploy.py service caption disable
./.venv/bin/python scripts/deploy.py service caption uninstall
```

密钥文件 `keys/minimax.json`：`{"host":"https://api.minimaxi.com","api_key":"<local-secret>","enabled":false,"image_external_consent":false}`。选择与套餐区域匹配的 host；支持 `api.minimaxi.com`、`api.minimax.cn`、`api.minimax.io`。配置缺失、权限不安全或未启用时，真实识别请求被拒绝。

部署入口默认 4 并发，支持 1–64；原工作器的直接入口最大默认值仍为 64。限频/连接数限制减半并按 Retry-After 退避，稳定后恢复；额度耗尽暂停队列，权限/余额问题修复后重试，不自动更换接口或收费模型。

caption 队列采用 processing 领取、独立连接、进程中断回收，同平台/账号/图片/提示词版本复用。描述保存到 `image_captions`，不覆盖正文。入队不等于完成，检查状态、失败码与已完成数。
