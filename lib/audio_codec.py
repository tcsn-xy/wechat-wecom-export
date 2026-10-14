"""微信/企业微信语音的离线解码（SILK → PCM WAV）。

背景
----
微信 4.x 的语音正文是 **SILK v3** 码流（``VoiceInfo.voice_data`` 以
``\\x02#!SILK_V3`` 开头，约 3,645 条 / 70 MB），企业微信 Caches/Voices 里
也是 ``.dat`` 包着的 SILK。

本机 ffmpeg 8.1.2 **没有编译 silk 解码器**（实测 ``-decoders`` 里没有，
``-f silk`` 报 Unknown input format），所以走 Python 绑定的官方 SILK SDK：
``pysilk``（PyPI ``silk-python``，cffi 编译，纯本地、无外部服务）。

实测（2026-09-29）：
* ``pysilk.decode(BytesIO(整个 blob), out, 24000)`` 直接成功，**不要**剥掉
  那 10 字节头（剥了反而 INVALID）；24 kHz 输出约 5.3 秒 / 254 KB PCM。
* 采样率优先 24000，失败退 16000。

产物：WAV（PCM s16le 单声道）。要 mp3 的话本地 ffmpeg 的 ``libmp3lame`` 可用，
但 WAV 已经是「能听的音频」，且不依赖外部二进制，所以默认只产 WAV。
"""
from __future__ import annotations

import io
import struct
import wave
from typing import Optional, Tuple

SILK_MAGIC = b"\x02#!SILK_V3"
_SAMPLE_RATES = (24000, 16000, 12000, 8000)


def is_silk(data: bytes) -> bool:
    if not data:
        return False
    if data[:10] == SILK_MAGIC:
        return True
    # 企业微信的 .dat 可能在前面包一层
    return SILK_MAGIC in data[:64]


def strip_container(data: bytes) -> bytes:
    """企业微信的语音 .dat 可能带自己的包头，把 SILK 魔数之前的字节去掉。"""
    i = data.find(SILK_MAGIC)
    if i > 0:
        return data[i:]
    return data


def decode_pcm(data: bytes, sample_rates=_SAMPLE_RATES
               ) -> Optional[Tuple[bytes, int]]:
    """SILK → ``(pcm_s16le, sample_rate)``；解不了返回 None。"""
    try:
        import pysilk
    except Exception:
        return None
    raw = strip_container(data)
    for sr in sample_rates:
        try:
            out = io.BytesIO()
            pysilk.decode(io.BytesIO(raw), out, sr)
            pcm = out.getvalue()
            if pcm:
                return pcm, sr
        except Exception:
            continue
    return None


def to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def decode_to_wav(data: bytes) -> Optional[Tuple[bytes, int, int]]:
    """SILK → ``(wav_bytes, sample_rate, duration_ms)``；解不了返回 None。"""
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(data), "rb") as w:
                sr = w.getframerate()
                return data, sr, int(w.getnframes() / sr * 1000)
        except (wave.Error, EOFError):
            return None
    got = decode_pcm(data)
    if not got:
        return None
    pcm, sr = got
    dur_ms = int(len(pcm) / 2 / sr * 1000)
    return to_wav(pcm, sr), sr, dur_ms


def pcm_duration_ms(pcm_len: int, sample_rate: int) -> int:
    return int(pcm_len / 2 / sample_rate * 1000)


def wav_duration_ms(wav: bytes) -> Optional[int]:
    """从 WAV 头读时长（兜底，用于已经解好的数据）。"""
    try:
        if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
            return None
        i, n = 12, len(wav)
        byte_rate, data_len = None, None
        while i + 8 <= n:
            cid = wav[i:i + 4]
            size = struct.unpack("<I", wav[i + 4:i + 8])[0]
            if cid == b"fmt " and size >= 16:
                byte_rate = struct.unpack("<I", wav[i + 8 + 8:i + 8 + 12])[0]
            elif cid == b"data":
                data_len = size
            i += 8 + size + (size & 1)
        if byte_rate and data_len:
            return int(data_len / byte_rate * 1000)
    except Exception:
        return None
    return None


def mime_for(ext: str) -> str:
    return {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
        "mp4": "video/mp4", "mov": "video/quicktime", "m4a": "audio/mp4",
        "mp3": "audio/mpeg", "wav": "audio/wav", "amr": "audio/amr",
        "pdf": "application/pdf", "zip": "application/zip",
        "doc": "application/msword", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xls": "application/vnd.ms-excel",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "ppt": "application/vnd.ms-powerpoint",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "silk": "audio/x-silk", "txt": "text/plain", "dat": "application/octet-stream",
    }.get((ext or "").lower().lstrip("."), "application/octet-stream")
