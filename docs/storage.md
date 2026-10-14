# 信息存入的方法

`wx_chat` 与 `wecom_chat` 独立，表记录 `platform` 与来源身份。保存消息正文、空正文对应原文、原始二进制字段、链接及可用媒体关联。导入默认幂等增量；同一来源重跑保持消息身份。

```bash
./.venv/bin/python scripts/mysql_import_wx.py --text-only
./.venv/bin/python scripts/mysql_import_wecom.py --text-only
./.venv/bin/python scripts/chat_mysql_sync.py --status
./.venv/bin/python scripts/chat_watch_sync.py --status
./.venv/bin/python scripts/chat_watch_sync.py --reconcile
```

建议先文字入库，再用 `chat_media_sync.py` 把图片和语音写到文件。手动导入器省略 `--text-only` 是兼容的完整媒体 BLOB 模式，可能占用大量空间；不会代替新的文件媒体管线。

`--rebuild` 只允许显式指定非正式库。`wx_chat` / `wecom_chat` 及大小写变体禁止重建；不要为补历史清空库。

准实时写入消息、原文、当前批次链接和游标同事务提交，快照版本防止旧批次覆盖新内容。小时全来源核验不按时间水位截断，因此新迁入的旧历史也能补齐。未知新会话等待真实映射，不猜身份。

来源表不加挤掉不同会话/分片的唯一键，文字结构准备不调用旧媒体合并迁移。媒体锁与文字锁分离，SQLite 连接按批次关闭。实时服务健康时常规导入会交给守护进程；自定义快照或限定会话导入先停用实时服务。

状态和报告均写到本机 `.runtime/`，含账号或来源信息的报告不公开。不能用成功退出代替历史覆盖验收：检查待核验来源、原文与链接缺失、最新全量成功时间和实际延迟样本。
