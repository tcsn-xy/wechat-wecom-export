"""微信消息 → 媒体引用的补全（在 ``wxexport.parse.describe`` 之上补漏）。

为什么需要补
------------
``parse.describe`` 只认 ``md5="…"`` 一个属性。实测 2026-09-29 的消息里有：

* **视频**（``<videomsg>``）：没有 ``md5`` 属性，md5 在 **``newmd5``** 或
  **``originsourcemd5``** 里（还有 ``aeskey``/``cdnvideourl``）。
  结果 3,722 条里的 376 条视频消息一个媒体行都没落（``refs=[]``）。
* **高清图/原图**（``<img>``）：正文里的 ``md5`` 是缩略图的，原图的 md5 可能在
  ``cdnbigimgurl``/``aeskey`` 里，本机缓存常只有缩略图。

这里只做"补"，不改 ``lib/wxexport`` 里已经验证过的行为：把 ``describe`` 的结果
加上从原始 XML 里额外抠出来的 md5。补出来的引用同样会走
"本机有就存 blob、没有就写 reason" 的同一条路，不制造假数据。
"""
from __future__ import annotations

import re
from typing import List, Tuple

from wxexport import parse

MD5_ATTRS = ("md5", "newmd5", "originsourcemd5", "cdnthumbmd5", "filemd5",
             "attachmentmd5", "aeskey")
_ATTR_RE = {
    name: re.compile(r'\b%s="([0-9a-fA-F]{32})"' % name) for name in MD5_ATTRS
}
_ANY_MD5_RE = re.compile(r'\bmd5="([0-9a-fA-F]{32})"')
_AES_RE = re.compile(r'\baeskey="([0-9a-fA-F]{32})"')


def md5_candidates(payload: str) -> List[str]:
    """按可信度从高到低给出正文里可能的文件 md5（去重、小写）。"""
    out: List[str] = []
    for name in ("md5", "newmd5", "originsourcemd5", "cdnthumbmd5", "filemd5",
                 "attachmentmd5", "aeskey"):
        for m in _ATTR_RE[name].finditer(payload or ""):
            v = m.group(1).lower()
            if v not in out:
                out.append(v)
    return out


def enrich_refs(kind: str, payload: str, refs: List[Tuple[str, str]]
                ) -> List[Tuple[str, str]]:
    """把 ``parse.describe`` 的 refs 补全。

    * 已经有 refs 的（图片/表情/文件）原样返回，不折腾；
    * ``video`` 且 refs 为空 → 用 ``newmd5``/``originsourcemd5`` 补；
    * ``file`` 且 refs 为空 → 用全文里的 ``md5`` 属性补；
    * 其它类型不补（避免把 UUID 之类误当文件 md5）。
    """
    if refs:
        return refs
    if kind not in ("video", "file", "image", "sticker"):
        return refs
    cands = md5_candidates(payload)
    if not cands:
        return refs
    return [(kind if kind != "sticker" else "sticker", cands[0])]
