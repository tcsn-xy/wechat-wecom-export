"""极简 protobuf wire-format 解码器（不依赖 .proto，也不依赖 protobuf 库）。

企业微信把很多东西直接编码成 protobuf 再落盘/传输：会话快照（明文
``conv_snapshot``）、``local_encrypt`` 配置、以及消息体里的各种结构。
没有 .proto 也能解：wire format 本身就带字段号与类型，解出来按字段号排布即可。

支持的类型：
    0 varint       -> int
    1 fixed64      -> bytes(8)
    2 length-delim -> bytes（能当 UTF-8 解就用字符串，能当 protobuf 解就继续下钻）
    5 fixed32      -> bytes(4)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class PbError(ValueError):
    pass


def read_varint(buf: bytes, pos: int) -> tuple:
    shift = 0
    value = 0
    while True:
        if pos >= len(buf):
            raise PbError("varint 越界")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise PbError("varint 太长")


def looks_like_pb(buf: bytes) -> bool:
    """粗糙判断一段字节是不是 protobuf（用来决定要不要继续下钻）。"""
    if not buf:
        return False
    pos, n = 0, 0
    while pos < len(buf):
        try:
            key, pos = read_varint(buf, pos)
        except PbError:
            return False
        num, wt = key >> 3, key & 7
        if num == 0:
            return False
        if wt == 0:
            try:
                _, pos = read_varint(buf, pos)
            except PbError:
                return False
        elif wt == 1:
            pos += 8
        elif wt == 2:
            try:
                ln, pos = read_varint(buf, pos)
            except PbError:
                return False
            pos += ln
        elif wt == 5:
            pos += 4
        else:
            return False
        if pos > len(buf):
            return False
        n += 1
    return n > 0


def _decode_text(buf: bytes) -> str | None:
    try:
        s = buf.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if s and all(ch.isprintable() or ch in "\n\r\t" for ch in s):
        return s
    return None


@dataclass
class Field:
    number: int
    wire: int
    value: Any            # int | bytes | str | list[Field]

    def as_text(self) -> str:
        return self.value if isinstance(self.value, str) else ""


@dataclass
class Message:
    fields: list = field(default_factory=list)

    def all(self, number: int) -> list:
        return [f for f in self.fields if f.number == number]

    def first(self, number: int):
        got = self.all(number)
        return got[0] if got else None

    def text(self, number: int, default: str = "") -> str:
        f = self.first(number)
        if not f:
            return default
        return f.value if isinstance(f.value, str) else default

    def num(self, number: int, default: int = 0) -> int:
        f = self.first(number)
        return f.value if f and isinstance(f.value, int) else default

    def sub(self, number: int):
        f = self.first(number)
        return f.value if f and isinstance(f.value, Message) else None


def parse(buf: bytes) -> Message:
    msg = Message()
    pos = 0
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise PbError("字段号 0 非法")
        if wire == 0:
            value, pos = read_varint(buf, pos)
        elif wire == 1:
            value, pos = buf[pos:pos + 8], pos + 8
        elif wire == 2:
            length, pos = read_varint(buf, pos)
            raw = buf[pos:pos + length]
            if len(raw) != length:
                raise PbError("长度越界")
            pos += length
            # 嵌套消息的 payload 一定以很小的 field tag 开头（控制字节），
            # 纯文本几乎不会这样。先按嵌套解，解不动再当文本——
            # 否则 "\n<正文" 这种「外壳 + 正文」会被整段当成字符串返回。
            nested = None
            if raw[:1] and raw[0] < 0x20 and looks_like_pb(raw):
                try:
                    nested = parse(raw)
                except PbError:
                    nested = None
            if nested is not None:
                value = nested
            else:
                text = _decode_text(raw)
                if text is not None:
                    value = text
                elif looks_like_pb(raw):
                    try:
                        value = parse(raw)
                    except PbError:
                        value = raw
                else:
                    value = raw
        elif wire == 5:
            value, pos = buf[pos:pos + 4], pos + 4
        else:
            raise PbError("不支持的 wire type %d" % wire)
        msg.fields.append(Field(number, wire, value))
    return msg


def dump(msg: Message, indent: int = 0, max_depth: int = 4,
         max_items: int = 6, width: int = 90) -> str:
    """把解析结果打成可读树，用于人肉认字段。"""
    lines = []
    pad = "  " * indent
    for f in msg.fields[:200]:
        if isinstance(f.value, Message):
            if indent >= max_depth:
                lines.append("%s%d: <message %d 字段>" % (pad, f.number, len(f.value.fields)))
            else:
                lines.append("%s%d: {" % (pad, f.number))
                lines.append(dump(f.value, indent + 1, max_depth, max_items, width))
                lines.append("%s}" % pad)
        elif isinstance(f.value, str):
            s = f.value.replace("\n", "\\n")
            lines.append("%s%d: \"%s\"" % (pad, f.number, s[:width]))
        elif isinstance(f.value, int):
            lines.append("%s%d: %d" % (pad, f.number, f.value))
        else:
            lines.append("%s%d: hex:%s" % (pad, f.number, f.value[:24].hex()))
    return "\n".join(lines)


def walk(msg: Message):
    """深度优先遍历所有 (路径, Field)。"""
    for f in msg.fields:
        yield (), f
        if isinstance(f.value, Message):
            for path, sub in walk(f.value):
                yield (f.number,) + path, sub
