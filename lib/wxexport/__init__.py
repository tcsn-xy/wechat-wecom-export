"""wxexport —— macOS 微信 4.x 聊天记录提取与导出工具包。

整体流程分四步，互相解耦，可独立重跑：

    1. 捕获密钥   scripts/capture_keys.py   （需要重启微信，仅首次）
    2. 拉取数据   scripts/clone_data.py     （把微信沙盒里的数据搬出来）
    3. 导出记录   scripts/export_all.py     （离线解密 + 生成 HTML/TXT/CSV）
    4. 补齐表情   scripts/fetch_stickers.py （从 CDN 取回表情包，可选）

本包只处理使用者本人设备上的数据，全程只读，不修改微信任何文件。
"""

__version__ = "1.0.0"

__all__ = ["config", "keys", "media", "names", "parse", "render", "export"]
