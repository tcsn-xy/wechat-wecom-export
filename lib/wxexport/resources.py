"""Resolve local voice/video/file media for offline exports."""
from __future__ import annotations

import os
import shutil
from . import media_store, refs
import audio_codec


def resolve_media(paths, keyring, chat_id, local_id, ts, kind, payload, outdir):
    root = os.path.join(outdir, "附件")
    os.makedirs(root, exist_ok=True)
    if kind == "voice":
        source = os.path.join(paths.db_storage, "message", "media_0.db")
        blob = None
        if os.path.exists(source):
            c = keyring.connect(source)
            try:
                row = c.execute("SELECT voice_data FROM VoiceInfo WHERE local_id=? AND create_time=?",
                                (local_id, ts)).fetchone()
                blob = row[0] if row else None
            finally:
                c.close()
        if not blob:
            return [dict(kind=kind, reason="VoiceInfo 里没有这条语音")]
        name = "%s_%s" % (ts, local_id)
        original = os.path.join(root, name + ".silk")
        with open(original, "wb") as fh:
            fh.write(blob)
        wav = audio_codec.decode_to_wav(blob)
        if not wav:
            return [dict(kind=kind, path=original, mime="audio/x-silk",
                         reason="SILK 未能解码，保留原始文件")]
        dst = os.path.join(root, name + ".wav")
        with open(dst, "wb") as fh:
            fh.write(wav[0])
        return [dict(kind=kind, path=dst, mime="audio/wav"),
                dict(kind="file", path=original, mime="audio/x-silk")]
    resolver = getattr(paths, "_resource_resolver", None)
    if resolver is None:
        resolver = media_store.MediaRoot(paths.msg, os.path.join(paths.db_storage, "hardlink", "hardlink.db"), paths.capture)
        paths._resource_resolver = resolver
    hardlinks = resolver.load_hardlink()
    candidates = refs.md5_candidates(payload)
    for md5 in candidates:
        source = resolver.find(kind, md5, hardlinks.get(md5, "")) or resolver.find_any(md5)
        if source:
            dst = os.path.join(root, md5 + "_" + os.path.basename(source))
            if not os.path.exists(dst):
                shutil.copy2(source, dst)
            return [dict(kind=kind, path=dst, mime=audio_codec.mime_for(os.path.splitext(dst)[1]),
                         name=os.path.basename(source))]
    return [dict(kind=kind, reason="本机无该%s缓存" % kind if candidates else "空壳媒体消息：正文没有文件引用")]
