#!/usr/bin/env python3
"""Hourly WeCom text/raw/link ingestion, retaining existing media and history."""
from __future__ import annotations
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'lib'),str(ROOT/'scripts')]
from localio import task_lock,atomic_json,publish_many
from mysql_store import conn as my,wecom_import
from wecom import config,extract,messages,keys
from wx_mysql_sync import SyncError,now,read_state,prepare_text_schema

DATA=Path(config.DATA)/'mysql-sync'
STATE=ROOT/'.runtime/wecom-mysql-sync/status.json'


def accounts(datasets):
    return sorted({re.match(r'\d+',d.name).group(0) if re.match(r'\d+',d.name) else d.name for d in datasets})


def primary(root):
    return [d for d in extract.datasets(root) if 'Backup' not in d.name]


def refresh(raw,plain,key_path,expected,with_media=False):
    from wecom import access
    downloads=Path.home()/'Downloads';downloads.mkdir(exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix='wecom_mysql_stage-',dir=downloads))
    plain.parent.mkdir(parents=True,exist_ok=True)
    decoded=Path(tempfile.mkdtemp(prefix='.wecom-decrypt-',dir=plain.parent))/'plain'
    try:
        pid,port=access.wecom_access()
        script='OUT='+shlex.quote(str(staging))+r'''
set -e
mkdir -p "$OUT/acct"
find "$HOME/Library/Application Support/WXWork/Data" -maxdepth 6 -name message.db 2>/dev/null |
while IFS= read -r m; do
 d=$(dirname "$m")
 [ -f "$d/session.db" ] && [ -f "$d/user.db" ] || continue
 case "$d" in *Backup*) continue;; esac
 name=$(printf '%s' "$d" | sed 's|.*/Data/||; s|/|_|g')
 cp -Rc "$d" "$OUT/acct/$name" 2>/dev/null || cp -R "$d" "$OUT/acct/$name"
done
for p in "$HOME"/Documents/Profiles/*/conv_snapshot; do
 [ -f "$p" ] || continue
 profile=$(basename "$(dirname "$p")")
 mkdir -p "$OUT/Profiles/$profile"
 cp "$p" "$OUT/Profiles/$profile/conv_snapshot"
done
'''
        if with_media:
            script += r'''
for p in "$HOME"/Documents/Profiles/*; do
 [ -d "$p" ] || continue
 profile=$(basename "$p")
 for folder in Images Voices; do
  [ -d "$p/Caches/$folder" ] || continue
  mkdir -p "$OUT/Profiles/$profile/Caches"
  cp -Rc "$p/Caches/$folder" "$OUT/Profiles/$profile/Caches/$folder" 2>/dev/null || cp -R "$p/Caches/$folder" "$OUT/Profiles/$profile/Caches/$folder"
 done
done
'''
            mapping=Path(config.RAW)/'_profile_accounts.json'
            if mapping.exists():shutil.copy2(mapping,staging/'_profile_accounts.json')
        result=access.sh(script,port,timeout=300)
        if result['rc']:
            raise SyncError('企微数据库快照复制失败；旧快照保留')
        found=primary(staging)
        if not found:raise SyncError('企微没有可读的主数据集；请确认已登录')
        if expected and accounts(found)!=expected:raise SyncError('企微账号发生变化；拒绝覆盖已有同步绑定')
        key=keys.load_key(str(key_path))
        extract.decrypt_all(str(staging),str(decoded),key=key,quiet=True)
        atomic_json(staging/'_snapshot.json',dict(cloned_at=now(),accounts=accounts(found)))
        raw.parent.mkdir(parents=True,exist_ok=True)
        with task_lock(raw.parent/'.snapshot.lock'),task_lock(plain.parent/'.extract.lock'):
            publish_many({raw:staging,plain:decoded},raw.parent/'.snapshot.switch.json')
    finally:
        shutil.rmtree(staging,ignore_errors=True)
        shutil.rmtree(decoded.parent,ignore_errors=True)


class TextImporter(wecom_import.WecomImporter):
    def __init__(self,*,media_root,**kwargs):
        self.media_root=media_root
        super().__init__(**kwargs)

    def _cache_root(self):
        # Existing media stays bound to its captured database account/profile namespace.
        from wecom.accounts import profile_for
        profile=profile_for(self.media_root,self.current_account)
        return str(profile/'Caches') if profile else None


def totals(c,owners):
    return my.query(c,'SELECT COUNT(*) messages,MIN(ts) earliest_ts,MAX(ts) latest_ts FROM messages '
        'WHERE platform=%s AND account_key IN ('+','.join(['%s']*len(owners))+')',('wecom',*owners))[0]


def blob(value):
    return bytes(value) if isinstance(value,(bytes,bytearray)) else (str(value).encode() if value is not None else None)


def audit(c,dirs):
    count=missing=wrong=0;details=[]
    for ds in dirs:
        owner=accounts([ds])[0];db=messages.open_ro(ds/'message.db')
        try:
            for table in messages.tables(db):
                if 'message' not in table.lower() or table.startswith('sqlite_'):continue
                mapping=messages.mapping(db,table,messages.COLS)
                if not mapping.get('content') or not mapping.get('conversation'):continue
                fields=[(k,v) for k,v in mapping.items() if v]
                query='SELECT rowid,'+','.join('"'+v.replace('"','""')+'"' for _,v in fields)+' FROM "'+table.replace('"','""')+'"'
                dest=my.query(c,'SELECT m.chat_id,m.local_id,r.message_pk,r.raw_blob,r.extra_blob FROM messages m '
                    'LEFT JOIN message_raw r ON r.message_pk=m.id WHERE m.platform=%s AND m.account_key=%s '
                    'AND m.source_dataset=%s AND m.source_db=%s AND m.source_table=%s',
                    ('wecom',owner,ds.name,'message.db',table))
                saved={(r['chat_id'],str(r['local_id'])):r for r in dest};n=absent=bad=0
                for row in db.execute(query):
                    rec=dict(zip(['_rowid']+[k for k,_ in fields],row))
                    cid=str(rec.get('conversation') or 'ORPHAN_'+table)
                    lid=str(rec.get('msg_id') or rec['_rowid']);got=saved.get((cid,lid));n+=1
                    if got is None:absent+=1
                    elif got['message_pk'] is None or got['raw_blob']!=blob(rec.get('content')) or got['extra_blob']!=blob(rec.get('extra')):bad+=1
                count+=n;missing+=absent;wrong+=bad
                details.append(dict(account_key=owner,dataset=ds.name,table=table,source_rows=n,missing=absent,raw_mismatch=bad))
        finally:db.close()
    missing_links=my.scalar(c,"SELECT COUNT(*) FROM messages m WHERE platform='wecom' AND has_link=1 AND NOT EXISTS (SELECT 1 FROM message_links l WHERE l.message_pk=m.id)")
    return dict(ok=not(missing or wrong or missing_links),source_rows=count,missing_messages=missing,raw_mismatch=wrong,missing_links=missing_links,sources=details)


def sync(args):
    from mysql_store import realtime_bridge
    if args.database=='wecom_chat' and Path(args.state).resolve()==STATE.resolve() and realtime_bridge.request('wecom'):
        print('实时服务已接收补漏请求；通过 chat_watch_sync.py --status 查看完成状态')
        return 0

    state_path=Path(args.state);started=time.monotonic()
    try:
        with task_lock(state_path.with_suffix('.lock')):
            state=read_state(state_path);success=state.get('last_success')
            for name in ('before','after','added_rows','source_rows','elapsed_seconds'):state.pop(name,None)
            expected=state.get('accounts')
            if not expected and not args.no_snapshot:
                expected=accounts(primary(config.PLAIN))
            state.update(status='running',stage='snapshot',last_attempt=now(),last_success=success,error=None)
            atomic_json(state_path,state)
            try:
                raw=Path(args.raw);plain=Path(args.plain)
                with open(os.devnull,'w') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):
                    if not args.no_snapshot:refresh(raw,plain,Path(args.key),expected)
                dirs=primary(plain);owners=accounts(dirs)
                if not owners:raise SyncError('企微主数据集缺失，拒绝入库')
                if expected and expected!=owners:raise SyncError('企微账号与后台绑定不一致')
                state.update(accounts=owners,snapshot_time=read_state(raw/'_snapshot.json').get('cloned_at'),stage='database')
                atomic_json(state_path,state)
                with task_lock(raw.parent/'.snapshot.lock'),task_lock(plain.parent/'.extract.lock'):
                    c=my.connect();lock='chat-import:'+args.database
                    try:
                        if my.scalar(c,'SELECT GET_LOCK(%s,0)',(lock,))!=1:raise SyncError('企微目标库正在入库，下轮重试')
                        prepare_text_schema(c,args.database)
                        # Fix only legacy prefix identity; never migrate or merge media rows.
                        idx=my.query(c,"SHOW INDEX FROM snapshot_conversations WHERE Key_name='uk_snapshot_conv'")
                        if any(r['Sub_part'] is not None for r in idx):
                            my.execute(c,'ALTER TABLE snapshot_conversations DROP INDEX uk_snapshot_conv, ADD UNIQUE KEY uk_snapshot_conv (platform,account_key,conv_id)')
                        c.commit();before=totals(c,owners);media_before=my.scalar(c,'SELECT COUNT(*) FROM media_items');c.commit()
                        state.update(stage='import',before=before);atomic_json(state_path,state)
                        imp=TextImporter(database=args.database,raw_root=str(raw),plain_root=str(plain),media_root=args.media_root,skip_media=True,verbose=False)
                        imp.db.close();imp.db=c
                        with open(os.devnull,'w') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):imp.run()
                        state['stage']='audit';atomic_json(state_path,state)
                        result=audit(c,dirs);after=totals(c,owners)
                        result.update(media_rows_before=media_before,media_rows_after=my.scalar(c,'SELECT COUNT(*) FROM media_items'))
                        result['ok']=result['ok'] and result['media_rows_before']==result['media_rows_after']
                        atomic_json(state_path.with_name('audit.json'),dict(at=now(),before=before,after=after,**result))
                        state.update(after=after,added_rows=after['messages']-before['messages'],source_rows=result['source_rows'])
                        if not result['ok']:raise SyncError('企微原文、消息或链接对账失败，查看本机 audit.json')
                    finally:
                        try:my.execute(c,'SELECT RELEASE_LOCK(%s)',(lock,))
                        finally:c.close()
                state.update(status='success',stage='complete',last_success=now());code=0
            except (Exception,SystemExit) as exc:
                errno=exc.args[0] if exc.args and isinstance(exc.args[0],int) else None
                state.update(status='failed',error=str(exc) if isinstance(exc,SyncError) else '企微阶段 %s 失败（%s）；下轮重试'%(state['stage'],type(exc).__name__),mysql_errno=errno);code=1
            state['elapsed_seconds']=round(time.monotonic()-started,1);atomic_json(state_path,state)
            print(json.dumps(state,ensure_ascii=False));return code
    except RuntimeError:
        print('企微同步任务正在运行，本次跳过');return 0


def main():
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--status',action='store_true');p.add_argument('--no-snapshot',action='store_true')
    p.add_argument('--raw',default=str(DATA/'raw'));p.add_argument('--plain',default=str(DATA/'plain'))
    p.add_argument('--media-root',default=config.RAW);p.add_argument('--key',default=config.KEY_FILE)
    p.add_argument('--database',default='wecom_chat');p.add_argument('--state',default=str(STATE))
    args=p.parse_args();args.database=my.database_name(args.database)
    if args.status:print(json.dumps(read_state(args.state),ensure_ascii=False,indent=2));return 0
    return sync(args)


if __name__=='__main__':sys.exit(main())
