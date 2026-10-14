#!/usr/bin/env python3
"""Dynamic, read-only acceptance report; supports isolated databases and sources."""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'lib'))
from mysql_store import conn as my
from wxexport import config,keys
from wecom import extract,messages

REPORT=str(ROOT/'docs/MySQL入库报告.md')


def source_wx(paths):
    kr=keys.Keyring(paths.capture);out={}
    for path in paths.message_dbs()+paths.biz_message_dbs():
        c=kr.connect(path)
        try:
            for (table,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"):
                out[(os.path.basename(path),table)]=c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
        finally:c.close()
    if not out:raise RuntimeError('源快照没有消息表')
    return out


def source_wecom(plain,with_backups=False):
    out={}
    for ds in extract.datasets(plain):
        if 'Backup' in ds.name and not with_backups:continue
        c=messages.open_ro(ds/'message.db')
        try:
            for table in messages.tables(c):
                mapped=messages.mapping(c,table,messages.COLS)
                if mapped.get('content') and mapped.get('conversation'):
                    out[(ds.name,'message.db',table)]=c.execute('SELECT COUNT(*) FROM "'+table+'"').fetchone()[0]
        finally:c.close()
    if not out:raise RuntimeError('企微源快照没有消息表')
    return out


def evaluate(wx_database='wx_chat',wecom_database='wecom_chat',*,snapshot_root=None,capture=None,
             wecom_plain=None,wecom_raw=None):
    from wecom import config as wc
    checks=[];totals={}
    def add(no,name,status,expected=None,actual=None,detail=''):
        checks.append(dict(no=no,name=name,status=status,expected=expected,actual=actual,detail=detail))
    for platform,database in [('wechat',wx_database),('wecom',wecom_database)]:
        if not database:continue
        c=my.connect(database)
        try:
            latest=my.query(c,'SELECT status,scope,media_enabled FROM import_runs ORDER BY id DESC LIMIT 1')
            scope=json.loads(latest[0]['scope']) if latest else {}
            media=bool(latest[0]['media_enabled']) if latest else True
            add('A1' if platform=='wechat' else 'A2',database+'：最近入库任务',
                'pass' if latest and latest[0]['status']=='complete' else 'fail',
                'complete',latest[0]['status'] if latest else '未登记')
            if platform=='wechat':
                paths=config.load(require_snapshot=True,snapshot_root=snapshot_root,capture=capture)
                source=source_wx(paths)
                if scope.get('chat') or scope.get('limit'):
                    # A partial run must identify exactly which source tables were selected.
                    chosen={tuple(v) for v in scope.get('sources',[])}
                    source={k:v for k,v in source.items() if k in chosen}
                    add('A1',database+'：限定导入范围','info',detail=json.dumps(scope,ensure_ascii=False))
                dest={(r['source_db'],r['source_table']):int(r['n']) for r in my.query(c,'SELECT source_db,source_table,COUNT(*) n FROM messages GROUP BY 1,2')}
                if scope.get('chat') or scope.get('limit'):
                    dest={k:v for k,v in dest.items() if k in chosen}
            else:
                source=source_wecom(wecom_plain or wc.PLAIN,scope.get('with_backups',False))
                dest={(r['source_dataset'],r['source_db'],r['source_table']):int(r['n']) for r in my.query(c,'SELECT source_dataset,source_db,source_table,COUNT(*) n FROM messages GROUP BY 1,2,3')}
            mismatches=[(k,n,dest.get(k,0)) for k,n in source.items() if n!=dest.get(k,0)]
            extras=[k for k,n in dest.items() if n and k not in source]
            add('A1' if platform=='wechat' else 'A2',database+'：逐源表行数',
                'fail' if mismatches or extras else 'pass',0,len(mismatches)+len(extras),
                repr(mismatches[:5]+[(k,'非本轮源',dest[k]) for k in extras[:5]]))
            for name,sql in [
                ('原始报文覆盖','SELECT COUNT(*) FROM messages m LEFT JOIN message_raw r ON r.message_pk=m.id WHERE r.message_pk IS NULL'),
                ('原文孤立行','SELECT COUNT(*) FROM message_raw r LEFT JOIN messages m ON m.id=r.message_pk WHERE m.id IS NULL'),
                ('来源登记重复','SELECT COUNT(*) FROM (SELECT platform,account_key,dataset,source_db,source_table,chat_id,COUNT(*) n FROM import_sources GROUP BY 1,2,3,4,5,6 HAVING COUNT(*)>1) t'),
                ('来源字段归属','SELECT COUNT(*) FROM import_sources s LEFT JOIN (SELECT DISTINCT account_key,source_dataset,source_db,source_table,chat_id FROM messages) m ON m.account_key=s.account_key AND m.source_dataset=s.dataset AND m.source_db=s.source_db AND m.source_table=s.source_table AND (s.chat_id IS NULL OR m.chat_id=s.chat_id) WHERE s.source_rows>0 AND m.source_table IS NULL'),
                ('链接覆盖','SELECT COUNT(*) FROM messages m WHERE m.has_link=1 AND NOT EXISTS (SELECT 1 FROM message_links l WHERE l.message_pk=m.id)'),
                ('链接孤立行','SELECT COUNT(*) FROM message_links l LEFT JOIN messages m ON m.id=l.message_pk WHERE m.id IS NULL'),
                ('不可用媒体原因','SELECT COUNT(*) FROM media_items WHERE unavailable=1 AND (reason IS NULL OR reason=\'\')'),
                ('媒体重复身份','SELECT COUNT(*) FROM (SELECT identity_key FROM media_items GROUP BY identity_key HAVING COUNT(*)>1) t'),
                ('分片孤立行',"SELECT COUNT(*) FROM blob_parts b LEFT JOIN media_items m ON m.id=b.row_id WHERE b.table_name IN ('media_items','media_items.decoded_data') AND m.id IS NULL"),
                ('对账错误',"SELECT COUNT(*) FROM import_reconciliation WHERE status IN ('error','warn')"),
            ]:
                n=my.scalar(c,sql)
                add('A3' if '报文' in name or '来源' in name else 'A4',database+'：'+name,'pass' if n==0 else 'fail',0,n)
            if media:
                n=my.scalar(c,'SELECT COUNT(*) FROM messages m WHERE has_media=1 AND NOT EXISTS (SELECT 1 FROM media_items i WHERE i.message_pk=m.id)')
                add('A4',database+'：媒体消息覆盖','pass' if not n else 'fail',0,n)
            else:
                add('A4',database+'：媒体消息覆盖','skip',detail='本次以文字模式导入，媒体未验收')
            # Verify inline and chunked bytes independently, streaming chunked content.
            file_mode=bool(my.query(c,"SHOW COLUMNS FROM media_items LIKE 'file_path'"))
            ids=my.query(c,'SELECT id,bytes,sha1,content_md5,decoded_sha1,decoded_data FROM media_items WHERE unavailable=0 ORDER BY id LIMIT 120')
            bad=0
            for item in ids:
                row=my.query(c,'SELECT data FROM media_items WHERE id=%s',(item['id'],))[0]
                sha=hashlib.sha1();md5=hashlib.md5();length=0
                if file_mode and my.scalar(c,'SELECT file_path FROM media_items WHERE id=%s',(item['id'],)):
                    from mysql_store.files import read
                    blob=read(c,item['id']);sha.update(blob);md5.update(blob);length=len(blob)
                elif row['data'] is not None:
                    blob=bytes(row['data']);sha.update(blob);md5.update(blob);length=len(blob)
                else:
                    parts=my.query(c,'SELECT part_no,LENGTH(data) bytes FROM blob_parts WHERE table_name=%s AND row_id=%s ORDER BY part_no',('media_items',item['id']))
                    for ordinal,part in enumerate(parts):
                        if ordinal!=part['part_no']:bad+=1
                        blob=my.query(c,'SELECT data FROM blob_parts WHERE table_name=%s AND row_id=%s AND part_no=%s',('media_items',item['id'],part['part_no']))[0]['data']
                        sha.update(blob);md5.update(blob);length+=len(blob)
                bad+=int(length!=item['bytes'] or sha.hexdigest()!=item['sha1'] or md5.hexdigest()!=item['content_md5'])
                if item['decoded_sha1']:
                    preview=item['decoded_data']
                    if preview is None:preview=my.blob_for(c,'media_items.decoded_data',item['id'])
                    bad+=int(hashlib.sha1(preview).hexdigest()!=item['decoded_sha1'])
            add('A4',database+'：字节长度和 SHA1 抽检','pass' if not bad else 'fail',0,bad,detail='%d 行，含分片'%len(ids))
            if platform=='wechat':
                n=my.scalar(c,"SELECT COUNT(*) FROM messages WHERE sender_id LIKE '%%@chatroom'")
                add('A7','群系统消息发送者','pass' if not n else 'fail',0,n)
            if media and not (scope.get('chat') or scope.get('limit')):
                cached=set()
                roots=([Path(paths.msg)/'file',Path(paths.msg)/'video'] if platform=='wechat' else
                       list(Path(wecom_raw or wc.RAW).glob('Profiles/*/Caches')))
                for root in roots:
                    for path in root.rglob('*'):
                        if not path.is_file() or path.is_symlink() or path.name.startswith('.') or path.stat().st_size==0:continue
                        digest=hashlib.md5()
                        with path.open('rb') as fh:
                            for block in iter(lambda:fh.read(1024*1024),b''):digest.update(block)
                        cached.add(digest.hexdigest())
                stored={r['content_md5'] for r in my.query(c,'SELECT DISTINCT content_md5 FROM media_items WHERE unavailable=0')}
                missing=len(cached-stored)
                add('A4',database+'：明文媒体缓存覆盖（按内容去重）','pass' if not missing else 'fail',0,missing)
            n=my.scalar(c,'SELECT COUNT(*) FROM messages WHERE platform<>%s',(platform,))
            add('A6',database+'：平台隔离','pass' if not n else 'fail',0,n)
            totals[database]={table:my.scalar(c,'SELECT COUNT(*) FROM `'+table+'`') for table in ['messages','chats','message_raw','media_items','message_links']}
        except Exception as exc:
            add('A1' if platform=='wechat' else 'A2',database+'：检查无法完成','fail',detail=str(exc))
        finally:c.close()
    passed=sum(r['status']=='pass' for r in checks)
    failed=sum(r['status']=='fail' for r in checks)
    skipped=sum(r['status']=='skip' for r in checks)
    return dict(checks=checks,totals=totals,passed=passed,failed=failed,skipped=skipped,
                ok=bool(checks) and not failed and not skipped)


def build_report(**kwargs):
    result=evaluate(**kwargs)
    lines=['# 微信与企业微信入库验收','',
           '生成时间：'+datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),'',
           '- 验收：通过 %d，失败 %d，跳过 %d。'%(result['passed'],result['failed'],result['skipped']),
           '- '+('本次范围内全部检查通过。' if result['ok'] else '验收未完成或存在失败；不能宣称全部通过。'),'',
           '| 库 | 会话 | 消息 | 原文 | 媒体 | 链接 |','|---|---:|---:|---:|---:|---:|']
    for db,s in result['totals'].items():lines.append('| %s | %s | %s | %s | %s | %s |'%(db,s['chats'],s['messages'],s['message_raw'],s['media_items'],s['message_links']))
    lines+=['','| 编号 | 检查 | 状态 | 期望 | 实际 | 说明 |','|---|---|---|---|---|---|']
    for r in result['checks']:
        values=[r['no'],r['name'],r['status'],r['expected'],r['actual'],r['detail']]
        lines.append('| '+' | '.join(str(v if v is not None else '—').replace('|','\\|').replace('\n',' ') for v in values)+' |')
    lines+=['','本报告只验证指定数据库和指定快照。文字模式、限定会话及临时合成数据验收不代表正式账号的全量验收。',
            '缓存缺失保留原因；原始报文与展示文本分开保存；媒体转换产物不替代原始字节。']
    return '\n'.join(lines)+'\n',result


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--wx-database',default='wx_chat')
    ap.add_argument('--wecom-database',default='wecom_chat')
    ap.add_argument('--snapshot-root')
    ap.add_argument('--capture')
    ap.add_argument('--wecom-plain')
    ap.add_argument('--wecom-raw')
    ap.add_argument('--out',default=REPORT)
    args=ap.parse_args()
    text,result=build_report(wx_database=args.wx_database,wecom_database=args.wecom_database,
        snapshot_root=args.snapshot_root,capture=args.capture,wecom_plain=args.wecom_plain,wecom_raw=args.wecom_raw)
    Path(args.out).parent.mkdir(parents=True,exist_ok=True)
    Path(args.out).write_text(text,encoding='utf-8')
    print('通过 %d，失败 %d，跳过 %d；报告：%s'%(result['passed'],result['failed'],result['skipped'],args.out))
    return 0 if result['ok'] else 1

if __name__=='__main__':sys.exit(main())
