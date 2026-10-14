import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from fixtures import wx_fixture, wecom_fixture, media_fixture
from localio import staged_directory, task_lock, publish_many
from wxexport import export, keys
from wecom import extract
from mysql_store import conn, schema, wx_import, wecom_import
from wccli_search import search


class LocalRepairs(unittest.TestCase):
    def test_removed_conversation_retains_previous_history(self):
        from doctor import check_export,check_invariants
        with tempfile.TemporaryDirectory() as tmp:
            paths,_=wx_fixture(tmp);ex=export.Exporter(paths,with_media=False,verbose=False);ex.run()
            old=Path(ex.manifest['chats']['100@chatroom']['dir']);content=(old/'聊天记录.csv').read_bytes()
            tombstone=ex.dir.describe('100@chatroom',ex.self_wxid)
            tombstone.update(tables=[],count=0,hi=0,source_missing=True)
            ex.enumerate_chats=lambda:[tombstone];ex.force=True;ex.run()
            self.assertEqual(ex.manifest['chats']['100@chatroom']['count'],0)
            self.assertEqual((Path(str(old)+'.previous')/'聊天记录.csv').read_bytes(),content)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(check_export(paths,True));self.assertTrue(check_invariants(paths))
    def test_quote_cleaning_keeps_title_without_xml(self):
        from wxexport.parse import clean_quote
        result=clean_quote('<msg><appmsg><title>合成文件</title></appmsg></msg>')
        self.assertIn('合成文件',result);self.assertNotIn('<msg>',result);self.assertNotIn('<appmsg',result)
    def test_case_insensitive_rebuild_guard(self):
        import subprocess,sys
        root=Path(__file__).resolve().parents[1]
        for script in ('mysql_import_wx.py','mysql_import_wecom.py'):
            result=subprocess.run([sys.executable,str(root/'scripts'/script),'--database','WX_CHAT','--rebuild'],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('非正式',result.stderr)

    def test_corrupt_or_foreign_manifest_preserves_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths,_=wx_fixture(tmp);ex=export.Exporter(paths,with_media=False,verbose=False);ex.run()
            index=Path(paths.export)/'index.html';before=index.read_bytes()
            Path(ex.manifest_path).write_text('{invalid')
            with self.assertRaises(ValueError):export.Exporter(paths,verbose=False)
            self.assertEqual(index.read_bytes(),before)

    def test_profile_namespace_mapping_is_anchored_to_raw_snapshot(self):
        from wecom.accounts import profile_for
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            raw,plain,ds=wecom_fixture(tmp)
            profile=raw/'Profiles'/'different-namespace'/'Caches';profile.mkdir(parents=True)
            with self.assertRaises(ValueError):profile_for(raw,'1001')
            shutil.copytree(ds,raw/'acct'/'1001_Data')
            self.assertEqual(profile_for(raw,'1001'),profile.parent)
            with self.assertRaises(ValueError):profile_for(raw,'1002')
    def test_bad_zstd_does_not_become_empty_success(self):
        from wxexport.parse import decode_content
        with self.assertRaises(ValueError):decode_content(b'not-zstd',4)
        self.assertEqual(decode_content(b'',4),'')

    def test_image_index_hashes_original_not_preview(self):
        from wxexport.media_store import ChatMediaIndex
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'image.dat';path.write_bytes(b'ciphertext')
            original=b'wxgf-original';preview=b'JPEG-preview'
            with patch('wxexport.media.decrypt_dat',return_value=original),patch('wxexport.media.unwrap_wxgf',return_value=(preview,'jpg')):
                index=ChatMediaIndex(tmp,bytes(16),xorkey=0x70).build()
                hit=index.get(hashlib.md5(original).hexdigest())
                self.assertEqual(hit[0],original)
                self.assertIsNone(index.get(hashlib.md5(preview).hexdigest()))

    def test_name_priority_and_room_member_isolation(self):
        from wxexport.names import Directory,_parse_ext_buffer
        names_map={};ids=set()
        _parse_ext_buffer(b'\x13wxid_a\x00\x13wxid_b\x12\x01X\x00',names_map,ids)
        self.assertEqual(ids,{'wxid_a','wxid_b'});self.assertEqual(names_map,{'wxid_b':'X'})
        with tempfile.TemporaryDirectory() as tmp:
            paths,_=wx_fixture(tmp);directory=Directory(paths.contact_db(),paths.capture)
            directory.room_names={'100@chatroom':{'wxid_friend':'群A'},'200@chatroom':{'wxid_friend':'群B'}}
            self.assertEqual(directory.display('wxid_friend','100@chatroom'),'备注')
            directory.remarks.clear()
            self.assertEqual(directory.display('wxid_friend','100@chatroom'),'群A')
            self.assertEqual(directory.display('wxid_friend','200@chatroom'),'群B')

    def test_failed_decrypt_preserves_plain_generation(self):
        from wecom_selftest import encrypt_dataset
        with tempfile.TemporaryDirectory() as tmp:
            raw,plain,ds=wecom_fixture(tmp)
            enc=raw/'acct'/'1001_Data';enc.mkdir(parents=True)
            encrypt_dataset(str(ds),str(enc),bytes(range(16,32)))
            marker=plain/'good-generation';marker.write_text('keep')
            with self.assertRaises(RuntimeError):extract.decrypt_all(raw,plain,bytes(16),quiet=True)
            self.assertEqual(marker.read_text(),'keep')
            extract.decrypt_all(raw,Path(tmp)/'decoded',bytes(range(16,32)),quiet=True)
            check=sqlite3.connect(Path(tmp)/'decoded/1001_Data/message.db')
            self.assertEqual(check.execute('SELECT COUNT(*) FROM message_table').fetchone()[0],5)
            check.close()

    def test_failed_check_cli_has_nonzero_status(self):
        import subprocess,sys
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'bad';room=out/'群聊'/'bad';room.mkdir(parents=True)
            (room/'聊天记录.csv').write_text('时间,正文\nnow,missing sender\n')
            script=Path(__file__).resolve().parents[1]/'scripts/doctor.py'
            result=subprocess.run([sys.executable,str(script),'--invariants','--snapshot-root',str(Path(tmp)/'none'),
                                   '--capture',str(Path(tmp)/'no-key'),'--out',str(out)],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)

    def test_media_pages_both_platforms(self):
        from wecom_export import export_messages,export_media
        from doctor import check_export
        with tempfile.TemporaryDirectory() as tmp:
            paths,tables=wx_fixture(tmp);raw,plain,ds=wecom_fixture(tmp)
            media_fixture(paths,tables,raw,ds)
            ex=export.Exporter(paths,verbose=False);ex.run()
            page=Path(ex.manifest['chats']['100@chatroom']['dir'])/'聊天记录.html'
            html=page.read_text()
            self.assertIn('<audio',html);self.assertIn('<video',html);self.assertIn('<img',html);self.assertIn('download',html)
            with contextlib.redirect_stdout(io.StringIO()):self.assertTrue(check_export(paths,True))
            out=Path(tmp)/'wecom-out';out.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                export_messages(str(out),args_raw_root=str(raw),plain_root=str(plain))
                export_media(str(out),str(raw))
            pages=list(out.rglob('聊天记录.html'))
            html=''.join(p.read_text() for p in pages)
            self.assertIn('<audio',html);self.assertIn('<video',html);self.assertIn('<img',html)
            import re,html as html_module
            from urllib.parse import unquote
            for page in pages+[out/'index.html',out/'媒体.html']:
                for ref in re.findall(r'(?:src|href)="([^"]+)"',page.read_text()):
                    self.assertTrue((page.parent/unquote(html_module.unescape(ref))).exists(),ref)

    def test_fts_partial_and_full_fallback_scope(self):
        import sqlcipher3
        with tempfile.TemporaryDirectory() as tmp:
            paths,_=wx_fixture(tmp)
            db=sqlcipher3.connect(str(Path(paths.db_storage)/'message/message_fts.db'))
            db.execute("PRAGMA key='fixture-only'")
            db.executescript("CREATE TABLE name2id(username TEXT PRIMARY KEY); INSERT INTO name2id VALUES ('wxid_friend'),('100@chatroom'); CREATE TABLE message_fts_v4_0_content(c0 TEXT,c1 INTEGER,c2 INTEGER,c3 INTEGER,c4 INTEGER,c5 INTEGER,c6 INTEGER); INSERT INTO message_fts_v4_0_content VALUES ('partial needle',1,1,1,2,1,1790000000);")
            db.commit();db.close()
            self.assertEqual(search(paths,'needle')['source'],'fts')
            result=search(paths,'needle',chats=['200@chatroom'])
            self.assertEqual(result['source'],'full');self.assertEqual(result['count'],1)
            self.assertEqual(search(paths,'needle',source='fts',offset=100)['count'],0)
            self.assertEqual(search(paths,'compressed')['source'],'full')

    def test_fingerprint_detects_equal_count_edits(self):
        import sqlcipher3
        with tempfile.TemporaryDirectory() as tmp:
            paths,tables=wx_fixture(tmp);ex=export.Exporter(paths,with_media=False,verbose=False)
            chat=next(c for c in ex.enumerate_chats() if c['chat_id']=='100@chatroom')
            before=ex.fingerprint(chat)
            c=sqlcipher3.connect(str(Path(paths.db_storage)/'message/message_0.db'));c.execute("PRAGMA key='fixture-only'")
            c.execute('UPDATE %s SET message_content=\'changed needle\',create_time=create_time+1 WHERE local_id=1'%tables[0]);c.commit();c.close()
            self.assertNotEqual(before,ex.fingerprint(chat))

    def test_checkpoint_same_second_late_and_edit(self):
        from wecom.messages import Message,Conversation
        from wecom_recent import fresh_messages,conversation_key,message_digest
        a=Message('R:1','1','sender','同名',100,'0','same',account_id='a',source_dataset='a_Data',source_table='message_table')
        b=Message('R:1','2','sender','同名',100,'0','same',account_id='a',source_dataset='a_Data',source_table='message_table')
        late=Message('R:1','3','sender','同名',90,'0','late',account_id='a',source_dataset='a_Data',source_table='message_table')
        conv=Conversation('R:1','群',messages=[a,b,late])
        ledger={'version':2,'conversations':{conversation_key(conv):{'messages':{a.stable_id:message_digest(a)}}}}
        self.assertEqual([m.msg_id for m in fresh_messages(conv,ledger)],['2','3'])
        a.text='edited'
        self.assertEqual([m.msg_id for m in fresh_messages(conv,ledger)],['1','2','3'])

    def test_merge_identity_and_ambiguous_legacy(self):
        from wecom.messages import Message,Conversation
        from wecom_recent import merge_into_export,migrate_legacy
        a=Message('R:1','1','s','sender',100,'0','x'*200+'one',account_id='a',source_dataset='a_Data',source_table='message_table')
        b=Message('R:1','2','s','sender',100,'0','x'*200+'two',account_id='a',source_dataset='a_Data',source_table='message_table')
        conv=Conversation('R:1','group',messages=[a,b])
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'out';raw=Path(tmp)/'raw';raw.mkdir()
            self.assertEqual(merge_into_export([conv],str(out),raw_root=str(raw)),2)
            self.assertEqual(merge_into_export([conv],str(out),raw_root=str(raw)),0)
            indexed=list(out.rglob('_messages.json'))
            indexed=[p for p in indexed if not any(part.endswith('.previous') for part in p.parts)]
            self.assertEqual(len(json.loads(indexed[0].read_text())),2)
            b.text=a.text
            legacy=Path(tmp)/'old.csv';legacy.write_text('时间,发送者ID,内容类型,正文\n1970-01-01 08:01:40,s,0,'+a.text+'\n')
            with self.assertRaises(RuntimeError):migrate_legacy(conv,legacy)

    def test_platform_dispatch_runs_after_failure(self):
        import argparse
        from new_messages import commands,run_all
        args=argparse.Namespace(chat='群',no_snapshot=True,since='2h',merge=True,text_only=False,with_media=False)
        tasks=commands(args)
        self.assertNotIn('--merge',tasks[1][1]);self.assertNotIn('--since',tasks[1][1])
        called=[]
        from types import SimpleNamespace
        with contextlib.redirect_stdout(io.StringIO()):
            code=run_all(tasks,lambda cmd:(called.append(cmd) or SimpleNamespace(returncode=1 if len(called)==1 else 0)))
        self.assertEqual(len(called),2);self.assertEqual(code,1)

    def test_switch_failure_rolls_back_complete_set(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name,text in [('a','old-a'),('b','old-b'),('stage-a','new-a'),('stage-b','new-b')]:
                (root/name).write_text(text)
            original=os.replace
            def failure(src,dst):
                if Path(src).name=='stage-b':raise OSError('simulated switch failure')
                return original(src,dst)
            with patch('localio.os.replace',side_effect=failure):
                with self.assertRaises(OSError):publish_many({root/'a':root/'stage-a',root/'b':root/'stage-b'},root/'journal.json')
            self.assertEqual((root/'a').read_text(),'old-a');self.assertEqual((root/'b').read_text(),'old-b')

    def test_full_search_and_unmatched_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths,_=wx_fixture(tmp)
            out=search(paths,'needle',source='full')
            self.assertEqual(out['count'],4)
            self.assertTrue(any('compressed' in r['text'] for r in out['results']))
            self.assertEqual(search(paths,'needle',source='auto')['source'],'full')
            self.assertEqual(search(paths,'needle',chats=['不存在的群'])['count'],0)
            limited=search(paths,'needle',chats=['200@chatroom'],source='full')
            self.assertEqual([r['chat_id'] for r in limited['results']],['200@chatroom'])

    def test_export_paths_incremental_and_invariants(self):
        from doctor import check_invariants, check_export
        with tempfile.TemporaryDirectory() as tmp:
            paths,_=wx_fixture(tmp)
            ex=export.Exporter(paths,with_media=False,verbose=False)
            ex.run()
            saved=json.loads(Path(ex.manifest_path).read_text())['chats']
            a,b=saved['100@chatroom']['dir'],saved['200@chatroom']['dir']
            self.assertNotEqual(a,b)
            original=(Path(a)/'聊天记录.csv').read_bytes()
            ex=export.Exporter(paths,force=True,with_media=False,verbose=False)
            chats=[c for c in ex.enumerate_chats() if c['chat_id']=='200@chatroom']
            ex.enumerate_chats=lambda:chats
            ex.run()
            self.assertEqual((Path(a)/'聊天记录.csv').read_bytes(),original)
            self.assertEqual(ex.manifest['chats']['200@chatroom']['dir'],b)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(check_invariants(paths))
                self.assertTrue(check_export(paths,True))

    def test_plain_wal(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source';source.mkdir()
            db=sqlite3.connect(source/'message.db')
            db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute('CREATE TABLE sample(value TEXT)');db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute("INSERT INTO sample VALUES ('wal-value')");db.commit()
            extract.decrypt_dataset(source,Path(tmp)/'out',bytes(16),quiet=True)
            check=sqlite3.connect('file:'+str(Path(tmp)/'out/message.db')+'?mode=ro&immutable=1',uri=True)
            self.assertEqual(check.execute('SELECT COUNT(*) FROM sample').fetchone()[0],1)
            check.close();db.close()

    def test_failed_output_preserves_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'output';target.mkdir();(target/'good').write_text('old')
            with self.assertRaises(RuntimeError):
                with staged_directory(target) as stage:
                    (Path(stage)/'good').write_text('bad')
                    raise RuntimeError('interrupted')
            self.assertEqual((target/'good').read_text(),'old')
            with staged_directory(target) as stage:
                (Path(stage)/'good').write_text('new')
            self.assertEqual((target/'good').read_text(),'new')
            self.assertEqual((Path(str(target)+'.previous')/'good').read_text(),'old')


class MysqlRepairs(unittest.TestCase):
    def test_unreferenced_file_is_preserved_once(self):
        paths,_=wx_fixture(self.tmp.name);db=self.database('unreferenced')
        extra=Path(paths.msg)/'file'/'unreferenced.pdf';extra.parent.mkdir(parents=True);extra.write_bytes(b'%PDF synthetic unreferenced')
        for _ in range(2):wx_import.run(paths,database=db,verbose=False)
        c=conn.connect(db)
        self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM media_items'),1)
        self.assertEqual(conn.scalar(c,'SELECT data FROM media_items'),extra.read_bytes());c.close()
    def test_decoded_large_blob_is_chunked_and_removed_on_update(self):
        db=self.database('preview');c=conn.connect(db)
        preview=b'fake-decoded-'*(800000)
        row=dict(platform='wechat',account_key='dummy',msg_kind='voice',source_key='voice',data=b'raw',decoded_data=preview,decoded_mime='audio/wav')
        pk=conn.write_media(c,row)
        self.assertIsNone(conn.scalar(c,'SELECT decoded_data FROM media_items WHERE id=%s',(pk,)))
        self.assertEqual(conn.blob_for(c,'media_items.decoded_data',pk),preview)
        row['decoded_data']=b'small'
        self.assertEqual(conn.write_media(c,row),pk)
        self.assertEqual(conn.blob_for(c,'media_items.decoded_data',pk),b'')
        c.rollback();c.close()

    def test_voice_lookup_requires_matching_timestamp(self):
        from types import SimpleNamespace
        c=sqlite3.connect(':memory:');c.execute('CREATE TABLE VoiceInfo(local_id INTEGER,create_time INTEGER,voice_data BLOB)')
        c.execute("INSERT INTO VoiceInfo VALUES (1,10,x'616263')")
        obj=wx_import.WxImporter.__new__(wx_import.WxImporter);obj.voice_conn=c;obj.log=lambda msg:None
        self.assertIsNone(obj._voice_for(1,11));self.assertEqual(obj._voice_for(1,10),b'abc');c.close()

    def test_mult_account_media_and_message_isolation(self):
        import shutil
        paths,tables=wx_fixture(self.tmp.name);raw,plain,ds=wecom_fixture(self.tmp.name)
        media_fixture(paths,tables,raw,ds)
        shutil.copytree(ds,plain/'1002_Data')
        shutil.copytree(raw/'Profiles'/'1001',raw/'Profiles'/'1002')
        db=self.database('accounts')
        wecom_import.run(database=db,raw_root=str(raw),plain_root=str(plain),verbose=False)
        c=conn.connect(db)
        self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM messages'),20)
        self.assertEqual(conn.scalar(c,'SELECT COUNT(DISTINCT account_key) FROM media_items'),2)
        self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM media_items'),10);c.close()
    def test_additive_migration_repeat_and_long_ids(self):
        paths,_=wx_fixture(self.tmp.name);db=self.database('migration')
        wx_import.run(paths,database=db,skip_media=True,verbose=False)
        c=conn.connect(db)
        mpk=conn.scalar(c,'SELECT MIN(id) FROM messages')
        conn.execute(c,'ALTER TABLE media_items DROP INDEX uk_media_identity, DROP COLUMN identity_key, DROP COLUMN account_key')
        conn.execute(c,'ALTER TABLE message_raw DROP COLUMN raw_blob, DROP COLUMN extra_blob')
        conn.execute(c,'INSERT INTO media_items(platform,message_pk,msg_kind,source_key,unavailable,reason) VALUES (%s,%s,%s,%s,1,%s)',('wechat',mpk,'image','legacy','missing'))
        c.commit();schema.create_all(c,db);schema.create_all(c,db)
        self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM media_items WHERE identity_key IS NOT NULL'),1)
        shared='v3_'+'a'*200
        for suffix in ('X','Y'):
            conn.execute(c,'INSERT INTO contacts(platform,account_key,contact_id) VALUES (%s,%s,%s)',('wechat','dummy',shared+suffix))
        c.commit()
        self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM contacts WHERE account_key=%s',('dummy',)),2)
        conn.execute(c,"INSERT INTO import_sources(platform,account_key,dataset,source_db,source_table,chat_id,source_rows) VALUES (%s,%s,%s,%s,%s,%s,1)",
                     ('wechat','dummy','message_0.db','Msg_'+'a'*32,'wxid_peer','wechat-snapshot'))
        c.commit();schema.migrate(c)
        repaired=conn.query(c,'SELECT dataset,source_db,source_table,chat_id FROM import_sources WHERE account_key=%s',('dummy',))[0]
        self.assertEqual(repaired,dict(dataset='wechat-snapshot',source_db='message_0.db',source_table='Msg_'+'a'*32,chat_id='wxid_peer'))
        c.close()

    def test_missing_cache_upgrades_in_place(self):
        paths,tables=wx_fixture(self.tmp.name);db=self.database('upgrade')
        blobs=media_fixture(paths,tables)
        image=Path(paths.chat_attach_dir('100@chatroom'))/'image.dat';encrypted=image.read_bytes();image.unlink()
        wx_import.run(paths,database=db,verbose=False)
        c=conn.connect(db)
        first=conn.query(c,"SELECT id,unavailable FROM media_items WHERE msg_kind='image'")[0]
        self.assertEqual(first['unavailable'],1);c.close()
        image.write_bytes(encrypted)
        wx_import.run(paths,database=db,verbose=False)
        c=conn.connect(db);later=conn.query(c,"SELECT id,unavailable FROM media_items WHERE msg_kind='image'")[0]
        self.assertEqual(later['id'],first['id']);self.assertEqual(later['unavailable'],0);c.close()

    def test_batch_exception_never_flushes(self):
        db=self.database('batch');c=conn.connect(db)
        with self.assertRaises(RuntimeError):
            with conn.BatchLoader(c) as loader:
                loader.add('accounts',dict(platform='wechat',account_key='buffer-only'))
                raise RuntimeError('abort')
        self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM accounts'),0);c.close()
    def test_media_repeat_upgrade_and_binary_roundtrip(self):
        paths,tables=wx_fixture(self.tmp.name);raw,plain,ds=wecom_fixture(self.tmp.name)
        blobs=media_fixture(paths,tables,raw,ds)
        wx=self.database('wx');wc=self.database('wc')
        for _ in range(2):
            wx_import.run(paths,database=wx,verbose=False)
            wecom_import.run(database=wc,raw_root=str(raw),plain_root=str(plain),verbose=False)
        for database in (wx,wc):
            c=conn.connect(database)
            self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM media_items'),5)
            self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM media_items WHERE unavailable=0'),5)
            self.assertEqual(conn.scalar(c,"SELECT COUNT(*) FROM import_reconciliation WHERE status<>'ok'"),0)
            for row in conn.query(c,'SELECT data,sha1,bytes FROM media_items'):
                import hashlib
                self.assertEqual(hashlib.sha1(row['data']).hexdigest(),row['sha1']);self.assertEqual(len(row['data']),row['bytes'])
            if database==wc:
                self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM message_raw WHERE extra_blob=%s',(b'\x00extra\xff',)),5)
            c.close()

    def test_backups_have_distinct_source_identity(self):
        import shutil
        raw,plain,ds=wecom_fixture(self.tmp.name)
        shutil.copytree(ds,plain/'1001_Backup')
        db=self.database('backup')
        wecom_import.run(database=db,raw_root=str(raw),plain_root=str(plain),skip_media=True,with_backups=True,verbose=False)
        c=conn.connect(db)
        self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM messages'),10)
        c.close()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.databases=[]

    def tearDown(self):
        c=conn.connect()
        for name in self.databases:
            assert name.startswith('repairtest_')
            schema.drop_db(c,name)
        c.close();self.tmp.cleanup()

    def database(self,suffix):
        name='repairtest_'+suffix+'_'+uuid.uuid4().hex[:12]
        self.databases.append(name)
        c=conn.connect();schema.create_all(c,name);c.close()
        return name

    def test_wx_text_only_scope_and_sources(self):
        paths,tables=wx_fixture(self.tmp.name)
        name=self.database('wx')
        wx_import.run(paths,database=name,chat='100@chatroom',skip_media=True,verbose=False)
        c=conn.connect(name)
        self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM messages'),3)
        self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM message_raw'),3)
        sources=conn.query(c,'SELECT source_db,source_table,chat_id,dataset FROM import_sources')
        self.assertEqual({r['source_db'] for r in sources},{'message_0.db','message_1.db'})
        self.assertTrue(all(r['chat_id']=='100@chatroom' and r['dataset']=='wechat-snapshot' for r in sources))
        c.close()
        wx_import.run(paths,database=name,chat='100@chatroom',skip_media=True,verbose=False)
        c=conn.connect(name)
        self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM messages'),3)
        self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM import_sources'),2)
        c.close()

    def test_blob_atomic_idempotent_and_failure(self):
        name=self.database('blob');c=conn.connect(name)
        row=dict(platform='wechat',account_key='dummy',msg_kind='file',source_key='same')
        pk=conn.insert_blob_parts(c,'media_items',row,'data',b'a'*100,chunk=32,commit=False)
        c.commit()
        self.assertEqual(conn.blob_for(c,'media_items',pk),b'a'*100)
        repeated=conn.insert_blob_parts(c,'media_items',row,'data',b'b'*50,chunk=32,commit=False)
        self.assertEqual(pk,repeated)
        self.assertEqual(conn.blob_for(c,'media_items',pk),b'b'*50)
        with patch.object(conn,'_write_parts',side_effect=RuntimeError('fail')):
            with self.assertRaises(RuntimeError):
                conn.insert_blob_parts(c,'media_items',row,'data',b'broken',commit=False)
        self.assertEqual(conn.blob_for(c,'media_items',pk),b'b'*50)
        self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM media_items'),1)
        c.rollback();c.close()

    def test_wecom_raw_and_repeat(self):
        raw,plain,ds=wecom_fixture(self.tmp.name)
        name=self.database('wecom')
        for _ in range(2):
            wecom_import.run(database=name,raw_root=str(raw),plain_root=str(plain),skip_media=True,verbose=False)
        c=conn.connect(name)
        self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM messages'),5)
        self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM message_raw'),5)
        self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM import_sources'),1)
        self.assertEqual(conn.scalar(c,"SELECT COUNT(*) FROM import_reconciliation WHERE status<>'ok'"),0)
        c.close()

    def test_full_report_and_failed_checks_are_not_pass(self):
        from mysql_report import evaluate
        paths,_=wx_fixture(self.tmp.name)
        raw,plain,ds=wecom_fixture(self.tmp.name)
        wx=self.database('wx');wc=self.database('wc')
        wx_import.run(paths,database=wx,verbose=False)
        wecom_import.run(database=wc,raw_root=str(raw),plain_root=str(plain),verbose=False)
        args=dict(wx_database=wx,wecom_database=wc,snapshot_root=paths.data,capture=paths.capture,wecom_plain=str(plain),wecom_raw=str(raw))
        result=evaluate(**args)
        self.assertTrue(result['ok'],[r for r in result['checks'] if r['status']=='fail'])
        c=conn.connect(wx);conn.execute(c,'DELETE FROM message_raw LIMIT 1');c.commit();c.close()
        self.assertFalse(evaluate(**args)['ok'])


if __name__=='__main__':
    unittest.main()
