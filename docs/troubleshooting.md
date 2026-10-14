# 排查与验证

- 配置报错：确认私密 JSON 对象有效、文件权限为 0600、口令非空；使用 `deploy.py check --mysql`，检查结果只显示错误类别。后台不读取交互 shell 的临时口令。
- SQLCipher 编译失败：确认 Homebrew `sqlcipher` 与 `pkg-config` 已安装，使用同一架构的 Python，再运行 `deploy.py dependencies`。
- 企微扫描器编译失败：安装 `openssl@3`、clang，或设置 `OPENSSL_PREFIX`；不得退回 lldb 附加受限原版或 CC_MD5 断点。
- 企微没有 Node 子进程：正常启动登录并检查客户端相关模块；不要自动重启或使用其他用户账号。inspector 仅监听本机，不对外开放。
- 快照失败：保留上一份有效快照，核对客户端版本、权限、有效 key 与 WAL；不删除源数据。
- 图片缺失：确认本机缓存和账号映射，检查媒体文件 hash；不要把缩略图/未完成 caption 宣称为原图已补齐。
- 后台无数据：先完成首次密钥准备和快照，检查 `.runtime/` 状态、旧历史积压和全量核验结果。

## 可复现的合成验收

使用虚构账号、消息和媒体。MySQL 集成测试会创建并删除独立 `repairtest_*` 临时库，测试账号需要对应权限；不重建正式库。

```bash
./.venv/bin/python -B -m unittest discover -s tests -v
./.venv/bin/python -B scripts/wecom_selftest.py
./.venv/bin/python -B scripts/isolated_validation.py --report .runtime/validation/report.md
```

隔离验收内部包含微信全量导出、资源引用和不变量检查、压缩搜索回退、企微加密/WAL/解析/渲染，以及重复 MySQL 导入。真实全量导出需使用本机私密快照和独立输出位置；结果不公开。

对公开发布运行 `scripts/privacy_scan.py --directory .`；提交后再运行 `--git-history`。额外的个人敏感词清单通过 `--deny-file /private/path/list.json` 传入，不能提交到仓库。

自动测试无法保证所有客户端版本和新机器的 TCC/登录流程。GitHub CI 用合成数据验证离线链路；macOS 原生首次获取密钥必须人工完成。
