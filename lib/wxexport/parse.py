"""消息内容解码与类型归一化。

微信 4.x 的消息正文有两层包装：

1. ``WCDB_CT_message_content`` 表明正文是不是 zstd 压缩的（群聊大消息基本都压缩）；
2. 群聊消息正文最前面会带 ``<发送者wxid>:\\n`` 前缀。

解出来之后通常是一段 XML，按 ``local_type`` 区分语义。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple

import zstandard

_D = zstandard.ZstdDecompressor()

# local_type 在低位，高位是标志位
TYPE_MASK = 0xFFFFFFFF

SENDER_RE = re.compile(r"^([A-Za-z0-9_\-@\.]+):\n")

MD5_RE = re.compile(r'\bmd5="([0-9a-f]{32})"')
AESKEY_RE = re.compile(r'\baeskey="([0-9a-fA-F]{32})"')
CDNURL_RE = re.compile(r'\bcdnurl="([^"]+)"')
URL_RE = re.compile(r'\burl="([^"]+)"')

APP_SUBTYPE_LABEL = {
    "4": "链接", "5": "链接", "6": "文件", "8": "表情", "17": "实时位置",
    "19": "合并转发的聊天记录", "24": "笔记", "33": "小程序", "36": "小程序",
    "44": "商品", "48": "音乐", "51": "视频号", "53": "接龙", "57": "引用",
    "62": "直播", "63": "视频号", "87": "群公告", "2000": "转账", "2001": "红包",
}


def decode_content(raw, ctype=None) -> str:
    """把 message_content 解成字符串。"""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        if not raw:
            return ""
        head = raw[:4]
        if ctype == 4 or head == b"\x28\xb5\x2f\xfd":
            try:
                raw = _D.decompress(raw, max_output_size=32 << 20)
            except Exception:
                try:
                    raw = _D.decompress(raw)
                except Exception as exc:
                    raise ValueError("zstd 消息正文无法解压") from exc
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
    return raw or ""


def split_sender(payload: str) -> Tuple[Optional[str], str]:
    """群聊正文形如 ``wxid_abc:\\n真正的正文``。"""
    m = SENDER_RE.match(payload or "")
    if m and ("wxid_" in m.group(1) or "@" in m.group(1)):
        return m.group(1), payload[m.end():]
    return None, payload


def xml_root(payload: str):
    try:
        return ET.fromstring((payload or "").strip())
    except Exception:
        return None


def xml_text(payload: str, *tags) -> Optional[str]:
    root = xml_root(payload)
    if root is None:
        return None
    for t in tags:
        el = root.find(".//" + t)
        if el is not None and (el.text or "").strip():
            return el.text.strip()
    return None


def _first(pattern: re.Pattern, text: str) -> Optional[str]:
    m = pattern.search(text or "")
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
def describe(ltype: int, payload: str) -> Tuple[str, str, List[Tuple[str, str]]]:
    """返回 ``(kind, 展示文本, [(媒体类型, md5), ...])``。"""
    media: List[Tuple[str, str]] = []
    payload = payload or ""

    if ltype == 1:
        return "text", payload, media

    if ltype == 3:
        md5 = _first(MD5_RE, payload)
        if md5:
            media.append(("image", md5))
        return "image", "[图片]", media

    if ltype == 47:
        md5 = _first(MD5_RE, payload)
        if md5:
            media.append(("sticker", md5))
        return "sticker", "[表情包]", media

    if ltype == 34:
        return "voice", "[语音]", media

    if ltype == 43:
        md5 = _first(MD5_RE, payload)
        if md5:
            media.append(("video", md5))
        return "video", "[视频]", media

    if ltype == 48:
        label = xml_text(payload, "label", "poiname")
        return "location", "[位置] %s" % (label or ""), media

    if ltype == 42:
        return "card", "[名片] %s" % (xml_text(payload, "nickname") or ""), media

    if ltype == 50:
        return "voip", "[语音/视频通话]", media

    if ltype == 10000:
        return "system", _system_text(payload), media

    if ltype in (37, 66):
        return "system", "[%s] %s" % (
            "好友请求" if ltype == 37 else "系统",
            xml_text(payload, "content", "msg") or payload[:120]), media

    if ltype == 49:
        return _app_message(payload, media)

    if ltype == 11000:
        return "system", "[群公告/系统]", media

    return "other", "[类型%s]" % ltype, media


def _system_text(payload: str) -> str:
    if "<sysmsg" not in payload:
        return re.sub(r"\s+", " ", payload).strip() or "[系统消息]"
    m = re.search(r"<replacemsg><!\[CDATA\[(.*?)\]\]></replacemsg>", payload, re.S)
    if m:
        return m.group(1).strip()
    m = re.search(r"<template><!\[CDATA\[(.*?)\]\]></template>", payload, re.S)
    if m:
        tpl = m.group(1)
        for lm in re.finditer(r'<link name="([^"]+)"[^>]*>(.*?)</link>',
                              payload, re.S):
            members = re.findall(r"<nickname><!\[CDATA\[(.*?)\]\]></nickname>",
                                 lm.group(2), re.S)
            if members:
                tpl = tpl.replace("$%s$" % lm.group(1), "、".join(members))
        return re.sub(r"\s+", " ", tpl).strip()
    return "[系统消息]"


def _app_message(payload: str, media) -> Tuple[str, str, List]:
    root = xml_root(payload)
    if root is None:
        return "app", "[应用消息]", media
    app = root.find(".//appmsg")
    if app is None:
        return "app", "[应用消息]", media

    sub = (app.findtext("type") or "").strip()
    title = (app.findtext("title") or "").strip()
    des = (app.findtext("des") or "").strip()
    url = (app.findtext("url") or "").strip()

    if sub == "57":
        ref = app.find(".//refermsg")
        q = ""
        if ref is not None:
            qn = (ref.findtext("displayname") or "").strip()
            qt = clean_quote(ref.findtext("content") or "")
            if qn or qt:
                q = "「%s: %s」 " % (qn, qt)
        return "quote", "[引用 %s] %s" % (q, title), media

    if sub == "19":
        return "chatrecord", "[合并转发的聊天记录] %s" % title, media

    if sub == "6":
        md5 = (app.findtext("md5") or "").strip()
        if md5:
            media.append(("file", md5))
        size = (app.findtext("appattach/appattachtotallen") or "").strip()
        extra = " (%s 字节)" % size if size else ""
        return "file", "[文件] %s%s" % (title, extra), media

    if sub == "5":
        return "link", "[链接] %s %s" % (title, url), media

    if sub in ("2000", "2001"):
        memo = ""
        el = app.find(".//wcpayinfo/pay_memo")
        if el is not None:
            memo = (el.text or "").strip()
        kind = "transfer" if sub == "2000" else "hongbao"
        return kind, "[%s] %s" % ("转账" if sub == "2000" else "红包",
                                  memo or title), media

    if sub in ("33", "36"):
        return "miniprogram", "[小程序] %s" % title, media
    if sub in ("51", "63"):
        return "videochannel", "[视频号] %s" % title, media
    if sub == "62":
        return "live", "[直播] %s" % title, media
    if sub == "87":
        return "announce", "[群公告] %s" % (des or title), media

    label = APP_SUBTYPE_LABEL.get(sub) or ("应用消息(%s)" % sub if sub else "应用消息")
    body = " ".join(x for x in (title, des) if x)
    return "app", "[%s] %s" % (label, body), media


# --------------------------------------------------------------------------- #
_XML_HINT = re.compile(r"<\s*(msg|appmsg|sysmsg|emoji|img|wcpayinfo)\b")


def clean_quote(content: str) -> str:
    """被引用的那条消息的文本。

    ``refermsg/content`` 有时候是纯文本，有时候又是一整段 XML（对方引用的是
    转账、红包、文件这类应用消息）。不处理的话会把原始 XML 直接摊到记录里。
    """
    s = (content or "").strip()
    if not s or not _XML_HINT.search(s):
        return s
    root = xml_root(s)
    if root is not None:
        for tag in ("title", "des", "content"):
            el = root.find(".//" + tag)
            if el is not None and (el.text or "").strip():
                return el.text.strip()
        el = root.find(".//emoji")
        if el is not None:
            return "[表情]"
        el = root.find(".//img")
        if el is not None:
            return "[图片]"
    m = re.search(r"<type>(\d+)</type>", s)
    sub = m.group(1) if m else ""
    if sub in ("2000", "2001"):
        return "[转账/红包]"
    if sub == "6":
        return "[文件]"
    if sub == "19":
        return "[合并转发的聊天记录]"
    if sub == "5":
        return "[链接]"
    return "[消息]"


# --------------------------------------------------------------------------- #
def message_media(payload: str, ltype: int):
    """取出一条消息引用的媒体信息（图片/表情/视频的文件 md5）。"""
    _kind, _text, media = describe(ltype, payload)
    return media
