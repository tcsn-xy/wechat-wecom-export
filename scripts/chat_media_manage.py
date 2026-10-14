#!/usr/bin/env python3
"""Migrate image/voice BLOBs, verify managed files, query image captions."""
import argparse
import datetime as dt
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'lib'),str(ROOT/'scripts')]
from mysql_store import conn as my,files,captions
from localio import task_lock,atomic_json


def migrate(c,database,limit=0):
    result=dict(migrated=0,bytes=0,failed=0,failures=[])
    ids=my.query(c,"SELECT DISTINCT i.id FROM media_items i LEFT JOIN blob_parts b ON b.row_id=i.id AND b.table_name IN ('media_items','media_items.decoded_data') WHERE i.msg_kind IN ('image','voice') AND (i.data IS NOT NULL OR i.decoded_data IS NOT NULL OR b.id IS NOT NULL) ORDER BY i.id"+(' LIMIT '+str(limit) if limit else ''))
    for entry in ids:
        try:
            item=files.migrate_one(c,entry['id'])
            if item:
                result['migrated']+=1;result['bytes']+=item['bytes']
                with open(ROOT/'.runtime/media-files/migration.jsonl','a') as log:
                    log.write(json.dumps(dict(database=database,at=dt.datetime.now().isoformat(),**item))+'\n')
        except Exception as exc:
            c.rollback();result['failed']+=1
            result['failures'].append(dict(id=entry['id'],error=type(exc).__name__))
    return result


def verify(c):
    result=dict(files=0,bytes=0,failed=0,failures=[])
    for row in my.query(c,"SELECT id,file_path,decoded_path FROM media_items WHERE msg_kind IN ('image','voice') AND file_path IS NOT NULL"):
        try:
            for decoded,col in [(False,'file_path'),(True,'decoded_path')]:
                if row[col]:result['bytes']+=len(files.read(c,row['id'],decoded));result['files']+=1
        except Exception as exc:
            result['failed']+=1;result['failures'].append(dict(id=row['id'],error=type(exc).__name__))
    return result


def status(c):
    return dict(media=my.query(c,"SELECT msg_kind,COUNT(*) rows_count,SUM(file_path IS NOT NULL) files,SUM(unavailable=1) unavailable,SUM(COALESCE(bytes,0)) original_bytes FROM media_items WHERE msg_kind IN ('image','voice') GROUP BY msg_kind"),
        captions=my.query(c,'SELECT status,COUNT(*) n FROM image_captions GROUP BY status'),
        database_bytes=my.scalar(c,'SELECT SUM(data_length+index_length) FROM information_schema.tables WHERE table_schema=DATABASE()'))


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['migrate','verify','status','search','retry'])
    p.add_argument('--database',choices=['wx_chat','wecom_chat']);p.add_argument('--limit',type=int,default=0);p.add_argument('--keyword',default='')
    a=p.parse_args();out={};state=ROOT/'.runtime/media-files';state.mkdir(parents=True,exist_ok=True)
    with task_lock(state/'manage.lock'):
        for db in ([a.database] if a.database else ['wx_chat','wecom_chat']):
            with my.connect(db) as c:
                locking=a.action=='migrate'
                if locking and my.scalar(c,'SELECT GET_LOCK(%s,0)',('chat-import:'+db,))!=1:raise RuntimeError('入库正在执行，下轮重试')
                try:
                    if a.action in ('migrate','retry'):files.prepare(c)
                    if a.action=='migrate':out[db]=migrate(c,db,a.limit);captions.enqueue(c)
                    elif a.action=='verify':out[db]=verify(c)
                    elif a.action=='search':out[db]=captions.search(c,a.keyword,a.limit or 50)
                    elif a.action=='retry':
                        my.execute(c,"UPDATE image_captions SET status='pending',attempts=0,error_code=NULL,next_attempt=NULL WHERE status IN ('failed','paused','retry')");c.commit();out[db]=status(c)
                    else:out[db]=status(c)
                finally:
                    if locking:my.execute(c,'SELECT RELEASE_LOCK(%s)',('chat-import:'+db,))
    out=json.loads(json.dumps(out,default=lambda v:int(v) if isinstance(v,Decimal) else str(v)))
    atomic_json(state/(a.action+'.json'),out)
    print(json.dumps(out,ensure_ascii=False,default=str,indent=2))
    return int(any(isinstance(r,dict) and r.get('failed',0) for r in out.values()))

if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:
        print(json.dumps(dict(status='failed',error=type(exc).__name__)));sys.exit(1)
