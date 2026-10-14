"""WeCom's own plaintext cache resolver (independent of wxexport)."""
import hashlib
import os
from pathlib import Path
import re
import shutil
from urllib.parse import quote
import audio_codec

MEDIA_DIRS = {"Images":"image", "Files":"file", "Videos":"video", "Voices":"voice",
              "Emotions":"sticker", "Video_Thumbnail":"thumb", "Emotion_Thumbnail":"thumb",
              "Link_Thumbnail":"thumb"}


class CacheIndex:
    def __init__(self, raw_root, account, allow_single=False):
        from .accounts import profile_for
        profile=profile_for(raw_root,account)
        matched=[profile] if profile else []
        self.items={};self.account=account
        for profile in matched:
            for folder,kind in MEDIA_DIRS.items():
                for path in sorted((profile/"Caches"/folder).rglob("*")):
                    if not path.is_file(): continue
                    key=path.parent.name.lower()
                    if not re.fullmatch(r"[a-f0-9]{32}",key): key=path.stem.lower()
                    self.items.setdefault(key,[]).append((path,kind))

    def references(self,message):
        candidates=set()
        for blob in (message.raw_content,message.raw_extra,message.text):
            text=blob.decode("latin-1") if isinstance(blob,bytes) else str(blob or "")
            candidates.update(v.lower() for v in re.findall(r"[0-9a-fA-F]{32}",text))
        return [(key,path,kind) for key in sorted(candidates) for path,kind in self.items.get(key,[])]

    def export_item(self,key,path,kind,root):
        target=Path(root)/"媒体"/self.account/(key+"_"+path.name)
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists(): shutil.copy2(path,target)
        item=dict(kind=kind,path=str(target),name=path.name,mime=audio_codec.mime_for(path.suffix))
        if kind=="voice" and path.suffix.lower() != ".wav":
            got=audio_codec.decode_to_wav(path.read_bytes())
            if got:
                wav=target.with_suffix(target.suffix+".wav");wav.write_bytes(got[0])
                item.update(path=str(wav),mime="audio/wav",original=str(target))
            else: item["reason"]="SILK 未能解码；可下载原始文件"
        return item


def media_html(items,page):
    import html
    out=[]
    for item in items:
        src=html.escape(quote(os.path.relpath(item["path"],Path(page).parent)))
        name=html.escape(item["name"])
        if item["kind"] in ("image","sticker","thumb"):
            out.append('<a href="%s"><img loading="lazy" style="max-width:330px" src="%s"></a>'%(src,src))
        elif item["kind"]=="voice" and item["mime"]=="audio/wav":
            out.append('<audio controls preload="none" src="%s"></audio>'%src)
        elif item["kind"]=="video":
            out.append('<video controls preload="metadata" style="max-width:100%%" src="%s"></video>'%src)
        out.append('<a download href="%s">下载 %s</a>'%(src,name))
        if item.get("original"):
            original=html.escape(quote(os.path.relpath(item["original"],Path(page).parent)))
            out.append('<a download href="%s">原始语音</a>'%original)
        if item.get("reason"): out.append('<p>%s</p>'%html.escape(item["reason"]))
    return ''.join(out)
