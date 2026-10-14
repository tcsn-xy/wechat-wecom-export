#!/usr/bin/env python3
"""Collect cached images/voices independently of text import; never write media BLOBs."""
import argparse
import datetime as dt
import hashlib
import gc
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'lib'),str(ROOT/'scripts')]
from localio import task_lock,atomic_json
from mysql_store import conn as my,files,captions
from wxexport import config,media,media_store,parse,refs,keys
import audio_codec
from wx_mysql_sync import running_wechats,child,SyncError
STATE=ROOT/'.runtime/media-files/sync.json'


def wx_refresh():
    procs=running_wechats()
    if procs:
        if len(procs)!=1 or procs[0][1]!=str(ROOT/'.runtime/WeChatRun.app/Contents/MacOS/WeChat'):raise SyncError('媒体快照需要项目微信副本')
        child(['sudo','-n',sys.executable,str(ROOT/'scripts/clone_data.py'),'--image-cache'])
    else:
        child([sys.executable,str(ROOT/'scripts/clone_data_local.py'),'--image-cache'])


def metadata(m,kind,key,ext='',thumb=False):
    return dict(platform=m['platform'],account_key=m['account_key'],message_pk=m['id'],chat_id=m['chat_id'],ts=m['ts'],msg_kind=kind,
        media_role='thumb' if thumb else 'original',source_key=key,source_kind='managed-cache',ext=ext or None,
        mime=audio_codec.mime_for(ext),is_thumb=int(thumb),unavailable=1,reason='本机无缓存')


def message_pages(c,query,params):
    last=2**64-1
    while True:
        page=my.query(c,query+' AND m.id<%s ORDER BY m.id DESC LIMIT 500',(*params,last))
        if not page:return
        for row in page:yield row
        last=page[-1]['id']
        del page
        gc.collect()


def wx_collect(c,paths):
    kr=keys.Keyring(paths.capture);v2key=config.media_v2_key(paths)
    index=media_store.ChatMediaIndex(str(Path(paths.msg)/'attach'),v2key).build() if v2key else None
    # Use raw message content already imported, avoiding a second source-table identity implementation.
    items=message_pages(c,"SELECT m.id,m.platform,m.account_key,m.chat_id,m.ts,m.local_id,m.kind,r.raw_content FROM messages m JOIN message_raw r ON r.message_pk=m.id WHERE m.account_key=%s AND m.kind IN ('image','voice')",(paths.wxid,))
    voice_path=Path(paths.db_storage)/'message/media_0.db';vc=kr.connect(str(voice_path)) if voice_path.exists() else None
    result=dict(messages=0,saved=0,missing=0,failed=0)
    try:
        for m in items:
            result['messages']+=1
            try:
                if m['kind']=='voice':
                    key='%s@%s'%(m['local_id'],m['ts']);row=metadata(m,'voice',key,'silk')
                    old=my.query(c,"SELECT id,file_path,unavailable FROM media_items WHERE message_pk=%s AND msg_kind='voice' AND source_key=%s",(m['id'],key))
                    if old and old[0]['file_path'] and files.resolve(old[0]['file_path']).exists():continue
                    hit=vc.execute('SELECT voice_data FROM VoiceInfo WHERE local_id=? AND create_time=? LIMIT 1',(m['local_id'],m['ts'])).fetchone() if vc else None
                    data=bytes(hit[0]) if hit and hit[0] else None;decoded=None
                    if data:
                        got=audio_codec.decode_to_wav(data)
                        if got:decoded=got[0];row.update(decoded_mime='audio/wav',duration_ms=got[2],reason=None)
                        else:row.update(needs_decode=1,reason='SILK 解码失败，保留原始文件')
                    files.save(c,row,data,decoded);result['saved' if data else 'missing']+=1
                else:
                    _,payload=parse.split_sender(m['raw_content'] or '')
                    _,_,ref=parse.describe(3,payload);ref=refs.enrich_refs('image',payload,ref)
                    candidates=[key for _,key in ref] or ['']
                    for key in candidates:
                        old=my.query(c,"SELECT file_path,media_role FROM media_items WHERE message_pk=%s AND msg_kind='image' AND COALESCE(source_key,'')=%s",(m['id'],key))
                        if old and old[0]['file_path'] and old[0]['media_role']=='original' and files.resolve(old[0]['file_path']).exists():continue
                        hit=index.get(key) if index and key else None
                        if hit:
                            _,ext,thumb,path=hit
                            # Preserve original bytes, including wxgf rather than just its JPEG preview.
                            data=index.original(path) if hasattr(index,'original') else media.decrypt_dat(Path(path).read_bytes(),v2key,index.xorkey)
                            if data.startswith(b'wxgf'):ext='wxgf'
                            row=metadata(m,'image',key,ext,thumb);row.update(source_path=path,reason='仅有缩略图' if thumb else None)
                            decoded=None
                            if data.startswith(b'wxgf'):
                                bitstream,fmt=media.unwrap_wxgf(data);decoded=media.hevc_to_jpeg(bitstream) if fmt=='wxgf' else bitstream
                                if decoded:row['decoded_mime']='image/jpeg'
                                else:row.update(needs_decode=1,reason='wxgf 未能解码')
                            files.save(c,row,data,decoded);result['saved']+=1
                        else:
                            files.save(c,metadata(m,'image',key));result['missing']+=1
                c.commit()
            except Exception:
                c.rollback();result['failed']+=1
    finally:
        if vc:vc.close()
    captions.enqueue(c);return result


def wecom_collect(c,raw):
    from wecom.resources import CacheIndex
    result=dict(saved=0,missing=0,failed=0)
    for owner in my.query(c,"SELECT DISTINCT account_key FROM messages WHERE platform='wecom'"):
        account=owner['account_key'];index=CacheIndex(raw,account)
        items=message_pages(c,"SELECT m.id,m.platform,m.account_key,m.chat_id,m.ts,m.kind,m.text,r.raw_blob,r.extra_blob FROM messages m JOIN message_raw r ON r.message_pk=m.id WHERE m.account_key=%s",(account,))
        for m in items:
            fake=SimpleNamespace(raw_content=m['raw_blob'],raw_extra=m['extra_blob'],text=m['text'])
            matched=[(key,path,kind) for key,path,kind in index.references(fake) if kind in files.KINDS]
            for key,path,kind in matched:
                try:
                    old=my.query(c,'SELECT file_path FROM media_items WHERE message_pk=%s AND msg_kind=%s AND source_key=%s',(m['id'],kind,key))
                    if old and old[0]['file_path'] and files.resolve(old[0]['file_path']).exists():continue
                    row=metadata(m,kind,key,path.suffix.lstrip('.'));row.update(source_path=str(path),file_name=path.name,reason=None)
                    data=path.read_bytes();decoded=None
                    if kind=='voice' and not data.startswith(b'RIFF'):
                        got=audio_codec.decode_to_wav(data)
                        if got:decoded=got[0];row.update(decoded_mime='audio/wav',duration_ms=got[2])
                        else:row.update(needs_decode=1,reason='语音转换失败，保留原始文件')
                    files.save(c,row,data,decoded);result['saved']+=1;c.commit()
                except Exception:c.rollback();result['failed']+=1
            if not matched and m['kind'] in files.KINDS:
                row=metadata(m,m['kind'],'missing')
                # Placeholder never replaces an available row with another source key.
                if not my.scalar(c,'SELECT COUNT(*) FROM media_items WHERE message_pk=%s AND msg_kind=%s',(m['id'],m['kind'])):
                    files.save(c,row);c.commit()
                result['missing']+=1
    captions.enqueue(c);return result


def main():
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__);p.add_argument('--no-snapshot',action='store_true');p.add_argument('--platform',choices=['wechat','wecom']);a=p.parse_args()
    results={}
    try:
        with task_lock(STATE.with_suffix('.lock')):
            for platform,db in [('wecom','wecom_chat'),('wechat','wx_chat')]:
                if a.platform and a.platform!=platform:continue
                atomic_json(STATE,dict(at=dt.datetime.now().isoformat(),status='running',stage=platform,platforms=results))
                try:
                    if platform=='wechat':
                        if not a.no_snapshot:wx_refresh()
                        paths=config.load(require_snapshot=True);snapshot_lock=Path(paths.data)/'.snapshot.lock'
                        from wx_mysql_sync import read_state,STATE as wx_state
                        if read_state(wx_state).get('account_key') not in (None,paths.wxid):raise SyncError('媒体快照账号与后台绑定不符')
                    else:
                        import wecom_mysql_sync as wc
                        from wecom import config as cfg
                        raw=wc.DATA/'raw';plain=wc.DATA/'plain'
                        if not a.no_snapshot:wc.refresh(raw,plain,Path(cfg.KEY_FILE),wc.accounts(wc.primary(raw)),with_media=True)
                        snapshot_lock=raw.parent/'.snapshot.lock'
                    with task_lock(snapshot_lock),my.connect(db) as c:
                        if my.scalar(c,'SELECT GET_LOCK(%s,0)',('chat-media:'+db,))!=1:raise SyncError('正在入库，下轮重试')
                        try:
                            files.prepare(c)
                            result=wx_collect(c,paths) if platform=='wechat' else wecom_collect(c,raw)
                            results[platform]=dict(status='success' if not result['failed'] else 'partial_failure',**result)
                        finally:my.execute(c,'SELECT RELEASE_LOCK(%s)',('chat-media:'+db,))
                except Exception as exc:results[platform]=dict(status='failed',error=type(exc).__name__)
                atomic_json(STATE,dict(at=dt.datetime.now().isoformat(),platforms=results))
    except RuntimeError:return 0
    print(json.dumps(results,ensure_ascii=False));return int(any(r['status']!='success' for r in results.values()))

if __name__=='__main__':sys.exit(main())
