# 微信与企业微信聊天记录导出

macOS 上读取本人微信 4.x / 企业微信 5.x 的本机快照，导出 HTML、TXT、CSV，导入两个独立 MySQL 库，并维护图片、语音文件及可选图片描述。

## 能做什么

- 本机密钥准备、有效快照、企业微信解密与 WAL 合并。
- 保留正文、原文、链接及来源身份，默认幂等增量，补导旧历史。
- 每小时全来源核验与准实时文字同步；图片/语音文件独立采集。
- 按平台与账号保存并验证媒体，兼容历史 BLOB 的逐项迁移。
- 可选 MiniMax Coding Plan caption/OCR，默认关闭，需授权图片外发。
- 可独立安装的 Codex Skill，打包完整源码与部署操作说明。

没有缓存的媒体不能凭空恢复，未知会话或未完成 caption 会如实保留状态。客户端版本、TCC 权限、重新登录及副本签名会影响可用性；不保证所有版本通用。没有提供任何真实聊天记录、账号密钥或个人验收报告。

## 快速开始

推荐 macOS、Python 3.11、MySQL 8.x、Homebrew、Xcode Command Line Tools。

```bash
git clone https://github.com/tcsn-xy/wechat-wecom-export.git
cd wechat-wecom-export
brew install python@3.11 sqlcipher openssl@3 mysql pkg-config
python3.11 scripts/deploy.py bootstrap
./.venv/bin/python scripts/deploy.py check
./.venv/bin/python scripts/deploy.py configure-mysql
```

先按 [完整部署](docs/deployment.md) 创建本机数据库账号，再按 [读取方法](docs/reading.md) 获取有效密钥与快照。首次密钥获取可能需要退出客户端、重签副本和人工登录；原版 App 不修改。安装依赖不会自动启动同步任务。

- [信息存入与后台同步](docs/storage.md)
- [媒体文件、迁移和 caption](docs/media-caption.md)
- [故障排查与验证](docs/troubleshooting.md)

## 独立 Skill

从 [Releases](https://github.com/tcsn-xy/wechat-wecom-export/releases) 获取 `wechat-wecom-export-skill.zip` 与 `SHA256SUMS`，校验并解压到 `~/.codex/skills/`。完整运行源码位于 Skill 的 `assets/project/`，无需另找脚本。

也可以从源码构建并安装：

```bash
python3 scripts/build_skill.py --install
# 只打包：python3 scripts/build_skill.py
```

调用 `$wechat-wecom-export`，或直接安装随包项目：

```bash
python3 ~/.codex/skills/wechat-wecom-export/scripts/install.py --target "$HOME/wechat-wecom-export"
```

Skill 模板在 `skills/wechat-wecom-export/`，包从 `release-manifest.json` 收集同一份源码生成。安装 Skill 不获取密钥、不安装后台、不上传图片；同版本重复安装保留本机数据。

## 隐私与网络

`keys/`、`data/`、`export/`、`media_store/`、`.runtime/`、个人分析、备份和转储不发布。私密配置权限 0600，数据库无内置口令，后台读取同一配置。提交采用明确文件清单与发布扫描，公开历史从脱敏源码开始。

常规解析和入库在本机执行。可选表情下载访问微信 CDN；可选 caption 发送图片 JPEG 识别版本及固定提示词到获授权的 MiniMax 接口，不附带聊天上下文。caption 未配置或未明确启用时拒绝真实识别请求。

## 开发与验证

```bash
./.venv/bin/python -B -m unittest discover -s tests -v
./.venv/bin/python -B scripts/wecom_selftest.py
./.venv/bin/python -B scripts/isolated_validation.py --report .runtime/validation/report.md
python3 scripts/privacy_scan.py --directory . --git-history
```

测试使用合成数据，集成测试的账号需有 `repairtest_*` 临时库权限。合成验收不证明真实用户历史完整，也不覆盖客户端登录和 TCC 行为。见 [开发约束](AGENTS.md)。

MIT 许可，版权归 `tcsn-xy`；依赖与外部工具说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
