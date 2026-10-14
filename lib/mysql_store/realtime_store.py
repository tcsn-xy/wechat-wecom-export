"""Versioned, bounded transactions for both realtime platform adapters."""
from __future__ import annotations
import contextlib
import datetime as dt
import hashlib
import json
import time
from . import conn as my
from localio import identity

WX_COLUMNS=('platform account_key chat_pk chat_id local_id server_id sort_seq local_type kind sender_id sender_name real_sender_id ts msg_time text content_type source_dataset source_db source_table has_media has_link').split()
IDENTITY_COLUMNS='platform account_key chat_id source_dataset source_db source_table local_id'.split()


def prepare(c):
    unique={r['Key_name'] for r in my.query(c,'SHOW INDEX FROM import_sources') if not r['Non_unique'] and r['Key_name']!='PRIMARY'}
    for name in unique:my.execute(c,'ALTER TABLE import_sources DROP INDEX `'+name.replace('`','``')+'`')
    for table,additions in [('messages',{'rt_revision':'BIGINT NOT NULL DEFAULT 0','rt_hash':'CHAR(64)'}),('chats',{'rt_revision':'BIGINT NOT NULL DEFAULT 0'})]:
        columns={r['Field'] for r in my.query(c,'SHOW COLUMNS FROM '+table)}
        for name,typ in additions.items():
            if name not in columns:my.execute(c,'ALTER TABLE '+table+' ADD COLUMN '+name+' '+typ)
    my.execute(c,"""CREATE TABLE IF NOT EXISTS rt_snapshots (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,platform VARCHAR(16) NOT NULL,
        started_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)) ENGINE=InnoDB""")
    my.execute(c,"""CREATE TABLE IF NOT EXISTS rt_sources (
        source_key CHAR(64) PRIMARY KEY,platform VARCHAR(16) NOT NULL,account_key VARCHAR(191) NOT NULL,
        dataset VARCHAR(255) NOT NULL,source_db VARCHAR(255) NOT NULL,source_table VARCHAR(255) NOT NULL,
        cursor_id BIGINT NOT NULL DEFAULT 0,schema_hash CHAR(64),file_identity VARCHAR(191),
        bootstrap BOOL NOT NULL DEFAULT 1,last_revision BIGINT NOT NULL DEFAULT 0,
        checked_rows BIGINT NOT NULL DEFAULT 0,last_full_at DATETIME,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    c.commit()


def revision(c,platform):
    cur=my.execute(c,'INSERT INTO rt_snapshots(platform) VALUES (%s)',(platform,));value=cur.lastrowid;c.commit();return value


def source_key(source):
    return identity(*[source[k] for k in ('platform','account_key','dataset','source_db','source_table')])


def canonical_links(links):
    # The established schema is unique by (message_pk,url_hash), not by origin.
    unique={}
    for url,title,des,app_type,origin in links:
        value=[url,(title or '')[:512],(des or '')[:1024],(app_type or '')[:32],origin or 'raw']
        if url not in unique:unique[url]=value
        else:
            for i in (1,2,3):unique[url][i]=unique[url][i] or value[i]
    return list(unique.values())


def fingerprint(record):
    h=hashlib.sha256()
    for mapping in (record['message'],record['raw']):
        for name,value in sorted(mapping.items()):
            if name in ('chat_pk','message_pk'):continue
            if name=='extra' and isinstance(value,str):
                try:value=json.loads(value)
                except ValueError:pass
            if isinstance(value,dt.datetime):value=value.strftime('%Y-%m-%d %H:%M:%S')
            if isinstance(value,(bytes,bytearray)):blob=bytes(value)
            else:blob=json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()
            h.update(name.encode()+b'\0'+str(len(blob)).encode()+b':'+blob)
    h.update(json.dumps(sorted(canonical_links(record['links'])),ensure_ascii=False,default=str).encode())
    return h.hexdigest()


@contextlib.contextmanager
def writer_lock(c):
    lock='chat-import:'+my.scalar(c,'SELECT DATABASE()')
    if my.scalar(c,'SELECT GET_LOCK(%s,0)',(lock,))!=1:raise BlockingIOError('writer_busy')
    try:yield
    finally:my.execute(c,'SELECT RELEASE_LOCK(%s)',(lock,))


def ensure_chat(c,record,rev):
    m=record['message'];chat=record['chat'];cid=m['chat_id']
    row=dict(platform=m['platform'],account_key=m['account_key'],chat_id=cid,is_room=int(chat.get('is_room',False)),
        kind=chat.get('kind') or ('群聊' if chat.get('is_room') else '单聊'),title=(chat.get('title') or cid)[:512],
        owner_id=chat.get('owner_id') or chat.get('owner'),member_count=chat.get('member_count',chat.get('members')),is_orphan=int(chat.get('is_orphan',False)),rt_revision=rev)
    columns=list(row)
    updates=','.join('`%s`=IF(rt_revision<=VALUES(rt_revision),VALUES(`%s`),`%s`)'%(k,k,k) for k in ['title','is_room','kind','owner_id','member_count','is_orphan'])
    my.execute(c,'INSERT INTO chats ('+','.join(columns)+') VALUES ('+','.join(['%s']*len(row))+') ON DUPLICATE KEY UPDATE '+updates+',rt_revision=GREATEST(rt_revision,VALUES(rt_revision))',tuple(row.values()))
    return my.scalar(c,'SELECT chat_pk FROM chats WHERE platform=%s AND account_key=%s AND chat_id=%s',(m['platform'],m['account_key'],cid))


def checkpoint(c,source,cursor,rev,*,complete=False,checked_rows=0):
    key=source_key(source)
    my.execute(c,"""INSERT INTO rt_sources (source_key,platform,account_key,dataset,source_db,source_table,cursor_id,schema_hash,file_identity,bootstrap,last_revision,checked_rows,last_full_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,IF(%s,NOW(),NULL))
        ON DUPLICATE KEY UPDATE cursor_id=IF(last_revision<=VALUES(last_revision),GREATEST(cursor_id,VALUES(cursor_id)),cursor_id),
        schema_hash=IF(last_revision<=VALUES(last_revision),VALUES(schema_hash),schema_hash),
        file_identity=IF(last_revision<=VALUES(last_revision),VALUES(file_identity),file_identity),
        bootstrap=IF(%s,0,bootstrap),last_full_at=IF(%s,NOW(),last_full_at),checked_rows=IF(%s,VALUES(checked_rows),checked_rows),
        last_revision=GREATEST(last_revision,VALUES(last_revision))""",
        (key,source['platform'],source['account_key'],source['dataset'],source['source_db'],source['source_table'],cursor,source['schema_hash'],source['file_identity'],int(not complete),rev,checked_rows,complete,complete,complete,complete))


def write_batch(c,source,records,cursor,rev,*,complete=False,checked_rows=0,failpoint=None,verify_existing=False):
    if len(records)>500:raise ValueError('batch exceeds 500')
    result=dict(added=0,updated=0,unchanged=0,stale=0,added_counters=[])
    with writer_lock(c):
        c.begin()
        try:
            pks={}
            for record in records:
                m=record['message'];key=(m['account_key'],m['chat_id'])
                if key not in pks:pks[key]=ensure_chat(c,record,rev)
                m['chat_pk']=pks[key]
            if records:
                expression='SHA2(CAST(JSON_ARRAY('+','.join(['%s']*7)+') AS CHAR),256)'
                params=tuple(value for r in records for value in [r['message'].get(k) for k in IDENTITY_COLUMNS])
                found=my.query(c,'SELECT id,chat_id,local_id,rt_revision,rt_hash,sender_id,ts FROM messages WHERE source_identity IN ('+','.join([expression]*len(records))+') FOR UPDATE',params)
                existing={(r['chat_id'],int(r['local_id'])):r for r in found}
            else:existing={}
            previous={};previous_links={}
            unknown=[r['id'] for r in existing.values() if verify_existing or r['rt_hash'] is None]
            if unknown:
                placeholders=','.join(['%s']*len(unknown))
                previous={r['id']:r for r in my.query(c,'SELECT m.*,r.message_pk raw_pk,r.raw_content,r.raw_blob,r.extra_blob,r.compress_type,r.raw_bytes,r.decoded_from FROM messages m LEFT JOIN message_raw r ON r.message_pk=m.id WHERE m.id IN ('+placeholders+')',tuple(unknown))}
                for link in my.query(c,'SELECT message_pk,url,title,des,app_type,origin FROM message_links WHERE message_pk IN ('+placeholders+')',tuple(unknown)):
                    previous_links.setdefault(link['message_pk'],[]).append(tuple(link[k] for k in ('url','title','des','app_type','origin')))
            affected=set();profiles={}
            for record in records:
                m=record['message'];profiles[(m['chat_pk'],m.get('sender_id') or '系统')]=record.get('member_profile',{})
                old=existing.get((m['chat_id'],int(m['local_id'])))
                digest=fingerprint(record)
                if old and old['rt_revision']>rev:result['stale']+=1;continue
                if old and old['id'] in previous and previous[old['id']].get('raw_pk') is None:old['rt_hash']=None
                if old and old['id'] in previous and previous[old['id']].get('raw_pk') is not None:
                    prior=previous[old['id']]
                    old['rt_hash']=fingerprint(dict(message={k:prior.get(k) for k in m},raw={k:prior.get(k) for k in record['raw']},links=previous_links.get(old['id'],[])))
                if old and old['rt_hash']==digest:
                    my.execute(c,'UPDATE messages SET rt_hash=%s,rt_revision=GREATEST(rt_revision,%s) WHERE id=%s',(digest,rev,old['id']))
                    result['unchanged']+=1;continue
                row=dict(m,rt_hash=digest,rt_revision=rev)
                if old:
                    my.execute(c,'UPDATE messages SET '+','.join('`%s`=%%s'%k for k in row)+' WHERE id=%s',(*row.values(),old['id']));pk=old['id'];result['updated']+=1
                else:
                    cur=my.execute(c,'INSERT INTO messages ('+','.join('`%s`'%k for k in row)+') VALUES ('+','.join(['%s']*len(row))+')',tuple(row.values()));pk=cur.lastrowid;result['added']+=1;result['added_counters'].append(record.get('_source_counter',int(m['local_id'])))
                raw=dict(message_pk=pk,platform=m['platform'],**record['raw'])
                my.execute(c,'INSERT INTO message_raw ('+','.join('`%s`'%k for k in raw)+') VALUES ('+','.join(['%s']*len(raw))+') ON DUPLICATE KEY UPDATE '+','.join('`%s`=VALUES(`%s`)'%(k,k) for k in raw if k!='message_pk'),tuple(raw.values()))
                my.execute(c,'DELETE FROM message_links WHERE message_pk=%s',(pk,))
                for url,title,des,app_type,origin in canonical_links(record['links']):
                    my.execute(c,'INSERT IGNORE INTO message_links (platform,message_pk,chat_id,ts,url,url_hash,title,des,app_type,origin) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        (m['platform'],pk,m['chat_id'],m['ts'],url,hashlib.sha1(url.encode('utf-8','replace')).hexdigest(),(title or '')[:512] or None,(des or '')[:1024] or None,(app_type or '')[:32] or None,origin))
                if not old or old['sender_id']!=m.get('sender_id') or old['ts']!=m['ts']:
                    affected.add((m['chat_pk'],m['platform'],m['account_key'],m['chat_id'],m.get('sender_id') or '系统'))
                if old and old['sender_id'] and old['sender_id']!=m.get('sender_id'):affected.add((m['chat_pk'],m['platform'],m['account_key'],m['chat_id'],old['sender_id']))
            # Derive totals from retained messages, never overwrite them with batch sizes.
            for pk in {v[0] for v in affected}:
                my.execute(c,'UPDATE chats SET message_count=(SELECT COUNT(*) FROM messages WHERE chat_pk=%s),first_ts=(SELECT MIN(ts) FROM messages WHERE chat_pk=%s),last_ts=(SELECT MAX(ts) FROM messages WHERE chat_pk=%s) WHERE chat_pk=%s',(pk,pk,pk,pk))
            for pk,platform,account,cid,sender in affected:
                stats=my.query(c,'SELECT COUNT(*) n,MIN(ts) lo,MAX(ts) hi,MAX(sender_name) shown FROM messages WHERE chat_pk=%s AND sender_id=%s',(pk,sender))[0]
                prof=profiles.get((pk,sender),{})
                my.execute(c,'INSERT INTO chat_members(platform,chat_pk,chat_id,contact_id,shown,message_count,first_ts,last_ts,room_nick,nick_name,remark,alias) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE shown=VALUES(shown),message_count=VALUES(message_count),first_ts=VALUES(first_ts),last_ts=VALUES(last_ts),room_nick=COALESCE(VALUES(room_nick),room_nick),nick_name=COALESCE(VALUES(nick_name),nick_name),remark=COALESCE(VALUES(remark),remark),alias=COALESCE(VALUES(alias),alias)',(platform,pk,cid,sender,stats['shown'],stats['n'],stats['lo'],stats['hi'],(prof.get('room_nick') or '')[:255] or None,(prof.get('nick') or '')[:255] or None,(prof.get('remark') or '')[:255] or None,(prof.get('alias') or '')[:191] or None))
            checkpoint(c,source,cursor,rev,complete=complete,checked_rows=checked_rows)
            if failpoint:failpoint()
            c.commit();return result
        except BaseException:c.rollback();raise
