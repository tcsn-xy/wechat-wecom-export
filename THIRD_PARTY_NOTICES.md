# Third-party notices

This repository does not distribute Tencent clients, user database keys, media or private datasets. WeChat and WeCom names belong to their respective owners; this is an independent local export project.

Python dependencies are declared in requirements.txt and installed separately: sqlcipher3 / sqlcipher3-binary (SQLCipher bindings), zstandard, PyCryptodome, Pillow, PyAV, PyMySQL and silk-python. Their upstream licenses continue to apply; package installation does not relicense them under this repository's MIT license. Native builds additionally link locally installed SQLCipher and OpenSSL. PyAV uses FFmpeg under the licensing terms of the installed build.

Optional integration: [huohuoer/wechat-cli](https://github.com/huohuoer/wechat-cli). The tool is installed separately and is not vendored. This project's wccli_setup.py supplies local snapshot configuration; wccli_search.py provides an independent search fallback.

Optional caption provider: MiniMax Coding Plan VLM API. No provider key is included; access requires the user's own subscription and explicit image-upload authorization.

Chinese holiday calendar data cites the public State Council notice linked in lib/mysql_store/cn_calendar.py. Protocol handling is implemented in the project source; no client binary is distributed.
