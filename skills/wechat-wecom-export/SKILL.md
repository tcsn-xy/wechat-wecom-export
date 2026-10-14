---
name: wechat-wecom-export
description: 在 macOS 本机部署、读取和查询本人微信及企业微信记录，导入独立 MySQL 库，维护图片语音文件和可选 MiniMax caption。使用完整随包脚本，适用于首次部署、历史补齐、后台同步及故障排查。
---

# 微信与企业微信本机读取、入库和部署

完整运行源码位于 `assets/project/`。先确认用户需要首次部署、读取/查询、入库/同步还是媒体/caption；沿用已有部署和当前会话授权。两平台数据和密钥不能外发，caption 需当前用户明确允许图片外发。

## 安装与定位

本 Skill 的源码模板在仓库；独立完整包由 `scripts/build_skill.py` 构建。使用独立包时：

1. 若已有部署，定位其 `scripts/deploy.py`、`.venv` 和本机私密配置，检查版本及状态，不重复抓密钥或安装任务。
2. 首次部署用 `python3 <本Skill>/scripts/install.py --target <用户选择的部署目录>`。缺少位置偏好时默认 `~/wechat-wecom-export`，执行前说明该目录。安装会校验随包源码，并拒绝覆盖不同版本文件；重复安装同一版本保留私密数据。
3. 读取部署目录的 `docs/deployment.md`，按照真实环境安装依赖和配置。安装 Skill 或复制源码不等于安装后台，不会自动获取密钥、启动同步或上传图片。
4. 操作可使用 `python3 <本Skill>/scripts/run.py --project <部署目录> <脚本名> [参数...]`，或部署目录 `.venv/bin/python` 直接运行实际脚本。依赖尚未安装时先用 Python 3.11 执行 `scripts/deploy.py bootstrap`。

## 按任务读取文档

- 首次部署/更新环境/后台管理：`assets/project/docs/deployment.md`。
- 获取密钥、快照、企微解密/WAL、查询或 HTML/TXT/CSV：`assets/project/docs/reading.md`。
- MySQL 正文、原文、链接、幂等导入与历史补齐：`assets/project/docs/storage.md`。
- 图片/语音文件、BLOB 迁移、caption/OCR、并发/失败重试：`assets/project/docs/media-caption.md`。
- 异常与验证：`assets/project/docs/troubleshooting.md`。

部署后优先读取实际部署目录对应文档，避免 Skill 版本与运行源码不一致。

## 必须保留的协议约束

- 微信 SQLCipher 与企微 wxSQLite3 AES-128 不互换；企微日常快照用已有 Node inspector 本机沙盒通道。初次 key 用重签副本可写内存扫描，不能退回 lldb 附加原版、leaks 或 CC_MD5 断点。
- 首次 key 捕获涉及客户端退出/副本启动/人工登录，提前说明具体动作，按现有用户授权执行；不自动重启日常运行客户端，不修改原版 App。
- 微信历史分片全部枚举；按每群保存昵称，显示名为备注 → 群昵称 → 微信昵称 → 微信号 → wxid；引用 XML 清洗，系统发送者不假冒群成员。
- 默认幂等入库，正式库禁止重建。完整来源身份包含平台、账号、数据集、数据库、表、会话和消息 ID。原文含空正文也保留；大 BLOB 分片；文字准备不调用旧媒体合并迁移。
- 媒体逐项文件校验成功后才清除对应 BLOB/分片，保留消息关联与备份；未缓存图片和未完成 caption 如实报告。
- caption 默认关闭，只向获授权的 MiniMax Coding Plan 发送图片识别版本和固定提示词，密钥 0600；不发送聊天上下文，不切换收费接口。并发 1–64 自适应限流；新部署入口默认 4。

## 完成标准

读取任务报告实际会话/时间范围及缺失项；部署任务核对依赖、配置、快照、入库和所选服务状态。用合成数据回归；不把合成验收当成真实全量覆盖。准实时延迟至少 20 个有效新消息样本才报告 P95，历史追赶不计入新增延迟。
