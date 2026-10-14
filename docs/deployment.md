# 完整部署（macOS）

面向本人设备、本人账号。读取模块适配微信 4.x / 企业微信 5.x；不同客户端版本、TCC 授权和客户端自更新会影响读取，首次获取密钥需要人在电脑前操作。本项目不是腾讯官方 SDK。

## 1. 环境

推荐 Apple Silicon macOS、Python 3.11、MySQL 8.x、Homebrew 和 Xcode Command Line Tools。`launchd`、APFS 克隆与客户端密钥获取只支持 macOS；其他平台可运行部分离线解析与合成测试。

```bash
xcode-select --install
brew install python@3.11 sqlcipher openssl@3 mysql pkg-config
brew services start mysql
cd /path/to/wechat-wecom-export
python3.11 scripts/deploy.py bootstrap
./.venv/bin/python scripts/deploy.py check
```

`bootstrap` 创建本目录 `.venv` 并安装 `requirements.txt`，可重复执行。已有虚拟环境不会被删除。macOS 的 `sqlcipher3` 需要 SQLCipher 头文件及库，部署脚本会从 Homebrew 查找编译路径。企微扫描器会使用 Homebrew `openssl@3` 或 `OPENSSL_PREFIX`。

## 2. MySQL 账号与私密配置

先用 MySQL 管理员交互创建本机专用账号，并授予 `wx_chat.*` 与 `wecom_chat.*` 的建表、读写和结构升级权限。可在交互式 `mysql -u root -p` 会话中执行下例；把占位口令改为你生成的强口令。避免把口令放入命令参数或保存进源码。

```sql
CREATE USER 'wxexport'@'127.0.0.1' IDENTIFIED BY '<choose-a-local-password>';
GRANT ALL PRIVILEGES ON wx_chat.* TO 'wxexport'@'127.0.0.1';
GRANT ALL PRIVILEGES ON wecom_chat.* TO 'wxexport'@'127.0.0.1';
```

```bash
./.venv/bin/python scripts/deploy.py configure-mysql
./.venv/bin/python scripts/deploy.py check --mysql
./.venv/bin/python scripts/deploy.py initialize-databases
```

配置交互输入口令，默认保存 `keys/local.json`（0600）；已存在时拒绝覆盖。配置内容为 `{"mysql":{"host":"127.0.0.1","port":3306,"user":"wxexport","password":"<local-secret>"}}`。可用 `--media-root /path/to/private/media` 设置媒体根目录。

`WXEXPORT_CONFIG` 可指定另一份私密配置。`WX_MYSQL_HOST/PORT/USER/PASSWORD` 覆盖对应字段，`MYSQL_PWD` 为口令兼容来源；不提供默认口令。后台任务重新读取同一私密配置文件，plist 只保存配置路径，不保存口令。环境变量口令不会自动持久化；安装后台前必须有文件配置。

初始化只创建缺失的两个库；已有库保留，由导入器做必要的增量结构准备。隔离测试额外需要能创建及删除 `repairtest_*` 的测试账号，不给日常账号额外权限。

## 3. 首次读取

按照 [读取方法](reading.md) 先拿密钥与有效快照，再确认离线导出能运行。首次部署不要直接安装后台来代替密钥准备。

## 4. 入库和后台

```bash
./.venv/bin/python scripts/mysql_import_wx.py --text-only
./.venv/bin/python scripts/mysql_import_wecom.py --text-only
./.venv/bin/python scripts/chat_media_sync.py --no-snapshot
./.venv/bin/python scripts/deploy.py service hourly install
./.venv/bin/python scripts/deploy.py service realtime install --observe
./.venv/bin/python scripts/deploy.py service realtime status
# 观察模式能读有效快照后，再启用实际入库
./.venv/bin/python scripts/deploy.py service realtime install
```

`hourly` 每小时顺序核验企微、微信和媒体，各阶段独立报错；`realtime` 文本目标 5–15 秒、媒体仍每小时。初次历史追赶需要时间；目标延迟不是所有客户端版本的保证。查看状态确认全来源核验结束；新增样本不足 20 条不宣称 P95 达标。

每种服务使用固定的 launchd 标签，同一用户只支持一份对应部署。部署入口发现服务属于其他目录时拒绝覆盖。不要在安装后移动部署目录；先从原目录卸载，再迁移并重装。

```bash
./.venv/bin/python scripts/deploy.py service hourly status
./.venv/bin/python scripts/deploy.py service realtime disable
./.venv/bin/python scripts/deploy.py service hourly disable
./.venv/bin/python scripts/deploy.py service realtime uninstall
./.venv/bin/python scripts/deploy.py service hourly uninstall
```

停用/卸载不删除数据、密钥或恢复备份。图片识别单独配置，见 [媒体与 caption](media-caption.md)。
