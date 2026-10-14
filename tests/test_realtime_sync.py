"""Realtime source/transaction verification: synthetic data only."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
import sqlcipher3
from fixtures import wx_fixture,wecom_fixture,media_fixture
from mysql_store import conn as my,schema,realtime_store as store
from mysql_store.realtime_sources import Wechat,Wecom
from mysql_store.realtime_engine import Engine
from mysql_store.realtime_snapshot import SnapshotStore,SnapshotChanged,Debounce,signature,clone


class SnapshotTests(unittest.TestCase):
    def test_copy_race_keeps_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/'source.db';src.write_bytes(b'original')
            snapshots=SnapshotStore(Path(tmp)/'copies',lambda p:p)
            old=snapshots.capture(src,1)
            def racing(a,b):clone(a,b);src.write_bytes(b'changed')
            snapshots.copier=racing
            with self.assertRaises(SnapshotChanged):snapshots.capture(src,2)
            self.assertEqual(snapshots.current[str(src)],old)
            self.assertEqual(old['path'].read_bytes(),b'original')
    def test_debounce_has_maximum_under_continuous_writes(self):
        d=Debounce();d.mark(0)
        for tick in range(1,10):d.mark(tick);self.assertFalse(d.due(tick))
        d.mark(10);self.assertTrue(d.due(10));d.clear();self.assertFalse(d.due(20))
    def test_wal_rotation_and_same_size_writes_change_signatures(self):
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/'s.db';src.write_bytes(b'x'*64);wal=Path(str(src)+'-wal');wal.write_bytes(b'a'*64)
            old=signature(src);wal.write_bytes(b'b'*64);self.assertNotEqual(old,signature(src))
            old=signature(src);wal.unlink();wal.write_bytes(b'c');self.assertNotEqual(old,signature(src))
    def test_invalid_snapshot_is_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/'s.db';src.write_bytes(b'bad')
            def invalid(p):raise ValueError('invalid')
            s=SnapshotStore(Path(tmp)/'copies',invalid)
            with self.assertRaises(ValueError):s.capture(src,1)
            self.assertEqual(s.current,{})


class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db='repairtest_live_'+uuid.uuid4().hex[:12]
        with my.connect() as c:schema.create_all(c,self.db)
        self.c=my.connect(self.db);store.prepare(self.c)
        self.paths,self.tables=wx_fixture(self.root)
        self.a=Wechat(Path(self.paths.db_storage),'wxid_self',self.paths.capture,self.root/'live')
        self.engine=Engine(self.c,self.a)
    def tearDown(self):
        self.engine.close();self.c.close()
        with my.connect() as c:my.execute(c,'DROP DATABASE `'+self.db+'`')
        self.tmp.cleanup()
    def capture(self,full=False):
        paths=self.a.discover();rev=store.revision(self.c,self.a.platform)
        for p in paths:self.a.snapshots.capture(p,rev)
        self.engine.schedule(paths,event_at=time.monotonic(),force_full=full)
    def drain(self):
        for _ in range(1000):
            if not self.engine.step():return
        self.fail('scheduler did not drain')
    def insert(self,lid,ts=1790000010,text='new text',shard=0):
        path=Path(self.paths.db_storage)/('message/message_%s.db'%shard)
        c=sqlcipher3.connect(str(path));c.execute("PRAGMA key='fixture-only'")
        c.execute('INSERT INTO '+self.tables[shard]+' VALUES (?,1,1,?,?,?, ?,0)',(lid,ts,'wxid_friend:\n'+text,lid,lid));c.commit();c.close()
    def test_normal_incremental_history_and_duplicate_events(self):
        self.capture(full=True);self.drain();before=my.scalar(self.c,'SELECT COUNT(*) FROM messages')
        self.insert(100,ts=1500000000,text='https://example.invalid/new')
        self.capture();self.drain()
        self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM messages'),before+1)
        self.assertGreater(my.scalar(self.c,'SELECT COUNT(*) FROM message_links'),0)
        self.capture();self.drain();self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM messages'),before+1)
        self.assertEqual(my.scalar(self.c,"SELECT COUNT(*) FROM message_raw WHERE raw_content LIKE '%%compressed needle%%'"),1)
    def test_batch_rollback_does_not_advance_cursor_or_drop_links(self):
        self.capture();desc=next(d for d in self.engine.descriptors.values() if d['source_db']=='message_0.db')
        record=self.a.decode(desc,next(self.a.rows(desc))[0]);key=store.source_key(desc)
        def crash():raise RuntimeError('interrupted')
        with self.assertRaises(RuntimeError):store.write_batch(self.c,desc,[record],999,desc['entry']['revision'],failpoint=crash)
        self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM messages'),0)
        self.assertFalse(my.query(self.c,'SELECT * FROM rt_sources WHERE source_key=%s',(key,)))
        store.write_batch(self.c,desc,[record],1,desc['entry']['revision'])
        self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM message_raw'),1)
    def test_old_snapshot_cannot_overwrite_new_row_and_links(self):
        self.capture();desc=next(d for d in self.engine.descriptors.values() if d['source_db']=='message_0.db')
        record=self.a.decode(desc,next(self.a.rows(desc))[0])
        import copy
        newer=copy.deepcopy(record);newer['message']['text']='updated';newer['raw']['raw_content']='updated';newer['links']=[('https://example.invalid/new','','','','text')]
        store.write_batch(self.c,desc,[newer],1,10)
        store.write_batch(self.c,desc,[record],1,5)
        self.assertEqual(my.scalar(self.c,'SELECT text FROM messages'),'updated')
        self.assertEqual(my.scalar(self.c,'SELECT url FROM message_links'),'https://example.invalid/new')
    def test_hourly_scan_repairs_deleted_raw_even_with_cached_hash(self):
        self.capture(full=True);self.drain()
        my.execute(self.c,'DELETE FROM message_raw LIMIT 1');self.c.commit()
        self.capture(full=True);self.drain()
        self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM messages'),my.scalar(self.c,'SELECT COUNT(*) FROM message_raw'))
    def test_unresolved_new_group_waits_for_mapping(self):
        self.capture();self.drain();before=my.scalar(self.c,'SELECT COUNT(*) FROM messages')
        cid='300@chatroom';table='Msg_'+hashlib.md5(cid.encode()).hexdigest()
        path=Path(self.paths.db_storage)/'message/message_0.db'
        c=sqlcipher3.connect(str(path));c.execute("PRAGMA key='fixture-only'")
        sql=c.execute('SELECT sql FROM sqlite_master WHERE name=?',(self.tables[0],)).fetchone()[0]
        c.execute(sql.replace(self.tables[0],table));c.execute('INSERT INTO '+table+" VALUES (1,1,1,1790000099,'new group',88,88,0)");c.commit();c.close()
        self.capture();self.drain();self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM messages'),before)
        c=sqlcipher3.connect(str(Path(self.paths.db_storage)/'session/session.db'));c.execute("PRAGMA key='fixture-only'");c.execute('INSERT INTO SessionTable VALUES (?,?)',(cid,1790000099));c.commit();c.close()
        self.capture();self.drain();self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM messages'),before+1)
    def test_media_lock_does_not_block_text_transaction(self):
        other=my.connect(self.db)
        try:
            my.scalar(other,'SELECT GET_LOCK(%s,0)',('chat-media:'+self.db,))
            self.capture();self.drain();self.assertGreater(my.scalar(self.c,'SELECT COUNT(*) FROM messages'),0)
        finally:other.close()
    def test_low_id_backfill_outside_overlap_is_found_by_full_scan(self):
        path=Path(self.paths.db_storage)/'message/message_0.db'
        c=sqlcipher3.connect(str(path));c.execute("PRAGMA key='fixture-only'")
        c.executemany('INSERT INTO '+self.tables[0]+" VALUES (?,1,1,1790000010,'bulk',?,?,0)",[(i,i,i) for i in range(10,3010)])
        c.commit();c.close();self.capture(full=True);self.drain()
        before=my.scalar(self.c,'SELECT COUNT(*) FROM messages');self.insert(5,ts=1400000000,text='late historical row')
        self.capture();self.drain();self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM messages'),before)
        self.capture(full=True);self.drain();self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM messages'),before+1)
    def test_schema_change_rescans_without_duplicates(self):
        self.capture(full=True);self.drain();before=my.scalar(self.c,'SELECT COUNT(*) FROM messages')
        path=Path(self.paths.db_storage)/'message/message_0.db'
        c=sqlcipher3.connect(str(path));c.execute("PRAGMA key='fixture-only'");c.execute('ALTER TABLE '+self.tables[0]+' ADD COLUMN new_column TEXT');c.commit();c.close()
        self.capture();self.drain();self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM messages'),before)
    def test_source_replacement_and_cursor_reversal_trigger_full_scan(self):
        self.insert(100);self.capture(full=True);self.drain()
        path=Path(self.paths.db_storage)/'message/message_0.db'
        replacement=path.with_suffix('.replacement');clone(path,replacement);os.replace(replacement,path)
        c=sqlcipher3.connect(str(path));c.execute("PRAGMA key='fixture-only'");c.execute('DELETE FROM '+self.tables[0]+' WHERE local_id=100');c.commit();c.close()
        self.capture();self.assertTrue(self.engine.full);self.drain()
        self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM messages WHERE local_id=100'),1)
    def test_wecom_live_wal_is_included(self):
        self.engine.close();raw,plain,ds=wecom_fixture(self.root)
        self.a=Wecom(plain,['1001'],bytes(16),self.root/'wal-live');self.engine=Engine(self.c,self.a)
        c=sqlite3.connect(ds/'message.db');c.execute('PRAGMA journal_mode=WAL')
        try:
            c.execute("INSERT INTO message_table VALUES (777,777,777,'u_a','R:new',0,1790000999,0,?,?)",(b'wal-only',b'raw-extra'));c.commit()
            self.assertTrue(Path(str(ds/'message.db')+'-wal').exists())
            self.capture();self.drain();self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM messages WHERE local_id=777'),1)
        finally:c.close()

    def test_many_suspended_jobs_do_not_hold_sqlite_connections(self):
        self.capture();desc=next(d for d in self.engine.descriptors.values() if d['source_db']=='message_0.db')
        original=self.a.kr.connect;active=[0]
        class Proxy:
            def __init__(self,c):self.c=c;active[0]+=1
            def execute(self,*a,**kw):return self.c.execute(*a,**kw)
            def close(self):self.c.close();active[0]-=1
        jobs=[]
        try:
            with patch.object(self.a.kr,'connect',side_effect=lambda path:Proxy(original(path))):
                for _ in range(300):
                    job=self.a.rows(desc);next(job);jobs.append(job)
                    self.assertEqual(active[0],0)
        finally:
            for job in jobs:job.close()
    def test_legacy_source_unique_index_removed_without_deleting_rows(self):
        my.execute(self.c,'ALTER TABLE import_sources ADD UNIQUE KEY legacy_source_unique(platform,account_key,source_db(60),source_table(60))')
        my.execute(self.c,"INSERT INTO import_sources(platform,account_key,dataset,source_db,source_table,chat_id) VALUES ('wechat','fixture','dataset','message.db','table','chatA')");self.c.commit()
        store.prepare(self.c)
        self.assertFalse(my.query(self.c,"SHOW INDEX FROM import_sources WHERE Key_name='legacy_source_unique'"))
        self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM import_sources'),1)
        my.execute(self.c,"INSERT INTO import_sources(platform,account_key,dataset,source_db,source_table,chat_id) VALUES ('wechat','fixture','dataset','message.db','table','chatB')");self.c.commit()
        self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM import_sources'),2)

    def test_repeated_full_scan_does_not_rewrite_duplicate_url_origins(self):
        self.insert(10,text='https://example.invalid/repeated')
        self.capture(full=True);self.drain()
        before=self.engine.results['updated']
        self.capture(full=True);self.drain()
        self.assertEqual(self.engine.results['updated'],before)
        self.assertEqual(my.scalar(self.c,"SELECT COUNT(*) FROM message_links WHERE url='https://example.invalid/repeated'"),1)

    def test_wecom_raw_extra_and_rowid_cursor(self):
        self.engine.close();raw,plain,ds=wecom_fixture(self.root)
        self.a=Wecom(plain,['1001'],bytes(16),self.root/'wc-live');self.engine=Engine(self.c,self.a)
        self.capture(full=True);self.drain();before=my.scalar(self.c,'SELECT COUNT(*) FROM messages')
        with sqlite3.connect(ds/'message.db') as c:
            c.execute("INSERT INTO message_table VALUES (999,999,999,'u_a','R:new',0,1500000000,0,?,?)",(b'old history',b'raw-extra'))
        self.capture();self.drain()
        self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM messages'),before+1)
        self.assertEqual(my.scalar(self.c,'SELECT extra_blob FROM message_raw r JOIN messages m ON m.id=r.message_pk WHERE m.local_id=999'),b'raw-extra')


class RunnerFailures(unittest.TestCase):
    def test_permission_failure_is_sanitized_and_never_writes_mysql(self):
        import chat_watch_sync as watch
        import threading
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp,patch.object(watch,'STATE',Path(tmp)):
            adapter=SimpleNamespace(mode='direct',snapshots=SimpleNamespace(root=Path(tmp)))
            adapter.discover=lambda:(_ for _ in ()).throw(PermissionError('SECRET'))
            with patch.object(watch,'adapter_for',return_value=adapter),patch.object(watch.my,'connect') as connect:
                r=watch.Runner('wechat',threading.Event(),once=True);r.run()
                connect.assert_not_called()
                self.assertEqual(r.state['mode'],'degraded_hourly');self.assertNotIn('SECRET',(Path(tmp)/'wechat.json').read_text())
    def test_mysql_unavailable_does_not_publish_source_checkpoint(self):
        import chat_watch_sync as watch
        import threading
        import pymysql
        with tempfile.TemporaryDirectory() as tmp,patch.object(watch,'STATE',Path(tmp)):
            paths,_=wx_fixture(tmp);adapter=Wechat(Path(paths.db_storage),'wxid_self',paths.capture,Path(tmp)/'snapshots')
            with patch.object(watch,'adapter_for',return_value=adapter),patch.object(watch.my,'connect',side_effect=pymysql.OperationalError(2003,'SECRET')):
                r=watch.Runner('wechat',threading.Event(),once=True);r.run()
                self.assertEqual(adapter.snapshots.current,{})
                self.assertEqual(r.state['mysql_errno'],2003);self.assertFalse(r.state['ready'])
    def test_wecom_permission_failure_uses_existing_sandbox_adapter(self):
        import chat_watch_sync as watch
        import threading
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp,patch.object(watch,'STATE',Path(tmp)):
            adapter=SimpleNamespace(mode='direct')
            adapter.discover=lambda:(_ for _ in ()).throw(PermissionError())
            adapter.enable_inspector=lambda:setattr(adapter,'mode','inspector')
            adapter.remote_discover=lambda:[Path(tmp)/'message.db']
            r=watch.Runner('wecom',threading.Event(),once=True);r.adapter=adapter
            try:r.discover();self.assertEqual(r.adapter.mode,'inspector')
            finally:r.watch.close()

if __name__=='__main__':unittest.main()
