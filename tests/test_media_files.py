import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import uuid
from PIL import Image
from mysql_store import conn as my,schema,files,captions

class FileTests(unittest.TestCase):
    def test_disable_survives_login_and_install_reenables(self):
        import image_caption_worker as worker
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker,'PLIST',Path(tmp)/'agent.plist'), patch.object(worker,'STATE',Path(tmp)/'state/status.json'), patch.object(worker.subprocess,'run') as run:
            worker.manage('disable')
            self.assertEqual(run.call_args_list[0].args[0][1],'disable')
            run.reset_mock();worker.manage('install')
            actions=[call.args[0][1] for call in run.call_args_list]
            self.assertIn('enable',actions);self.assertLess(actions.index('enable'),actions.index('bootstrap'))

    def test_atomic_dedup_and_account_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=files.publish([b'abc'],'wechat','a','png',tmp)
            b=files.publish([b'a',b'bc'],'wechat','a','png',tmp)
            other=files.publish([b'abc'],'wechat','b','png',tmp)
            self.assertEqual(a,b);self.assertNotEqual(a['path'],other['path'])
            files.resolve(a['path'],tmp).write_bytes(b'broken')
            with self.assertRaises(ValueError):files.publish([b'abc'],'wechat','a','png',tmp)
            with self.assertRaises(ValueError):files.resolve('../elsewhere',tmp)
    def test_disk_failure_leaves_no_final_file(self):
        with tempfile.TemporaryDirectory() as tmp,patch('mysql_store.files.os.fsync',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):files.publish([b'abc'],'wechat','a','png',tmp)
            self.assertFalse(list(Path(tmp).rglob('*.png')))
    def test_parse_and_provider_failures(self):
        im=Image.new('RGB',(3,3));blob=io.BytesIO();im.save(blob,format='PNG')
        cfg=dict(api_key='synthetic-key',host='https://api.minimaxi.com')
        class Response(io.BytesIO):pass
        success=lambda *a,**kw:Response(json.dumps(dict(content=json.dumps(dict(caption='红色方块',image_text='ABC')))).encode())
        self.assertEqual(captions.infer(blob.getvalue(),cfg,success)['image_text'],'ABC')
        for content in ['', 'error text', '{"caption":"x"}']:
            with self.assertRaises(captions.APIError):captions.infer(blob.getvalue(),cfg,lambda *a,**kw:Response(json.dumps(dict(content=content)).encode()))
        def denied(*a,**kw):raise urllib.error.HTTPError('https://example.invalid',429,'secret',{},None)
        with self.assertRaises(captions.APIError) as ctx:captions.infer(blob.getvalue(),cfg,denied)
        self.assertTrue(ctx.exception.pause);self.assertNotIn('secret',str(ctx.exception))
        def rejected(*a,**kw):return Response(json.dumps(dict(base_resp=dict(status_code=1026))).encode())
        with self.assertRaises(captions.APIError) as ctx:captions.infer(blob.getvalue(),cfg,rejected)
        self.assertFalse(ctx.exception.pause);self.assertTrue(ctx.exception.permanent)
        def quota(*a,**kw):return Response(json.dumps(dict(base_resp=dict(status_code=2056))).encode())
        with self.assertRaises(captions.APIError) as ctx:captions.infer(blob.getvalue(),cfg,quota)
        self.assertTrue(ctx.exception.pause)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.db='repairtest_media_'+uuid.uuid4().hex[:12]
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,CHAT_MEDIA_ROOT=self.tmp.name);self.env.start()
        c=my.connect();schema.create_all(c,self.db);c.close();self.c=my.connect(self.db);files.prepare(self.c)
    def tearDown(self):
        self.c.close();c=my.connect();my.execute(c,'DROP DATABASE `'+self.db+'`');c.close();self.env.stop();self.tmp.cleanup()
    def add(self,data=b'original',decoded=b'preview',kind='image'):
        row=dict(platform='wechat',account_key='fixture',msg_kind=kind,source_key=uuid.uuid4().hex,data=data,ext='png',decoded_data=decoded,decoded_mime='image/jpeg')
        pk=my.write_media(self.c,row);self.c.commit();return pk
    def test_migration_preserves_ids_preview_and_is_idempotent(self):
        pk=self.add();result=files.migrate_one(self.c,pk)
        self.assertEqual(result['bytes'],15)
        self.assertEqual(files.read(self.c,pk),b'original');self.assertEqual(my.blob_for(self.c,'media_items.decoded_data',pk),b'preview')
        self.assertIsNone(my.scalar(self.c,'SELECT data FROM media_items WHERE id=%s',(pk,)))
        self.assertIsNone(files.migrate_one(self.c,pk))
        files.resolve(my.scalar(self.c,'SELECT file_path FROM media_items WHERE id=%s',(pk,))).unlink()
        with self.assertRaises(FileNotFoundError):files.read(self.c,pk)
    def test_chunked_original_and_preview(self):
        pk=self.add(None,None)
        for table,data in [('media_items',b'abc'),('media_items.decoded_data',b'xyz')]:
            for i,v in enumerate(data):my.execute(self.c,'INSERT INTO blob_parts (table_name,row_id,part_no,data) VALUES (%s,%s,%s,%s)',(table,pk,i,bytes([v])))
        my.execute(self.c,'UPDATE media_items SET bytes=3,sha1=%s,decoded_sha1=%s WHERE id=%s',(hashlib.sha1(b'abc').hexdigest(),hashlib.sha1(b'xyz').hexdigest(),pk));self.c.commit()
        files.migrate_one(self.c,pk);self.assertEqual(files.read(self.c,pk),b'abc');self.assertEqual(files.read(self.c,pk,True),b'xyz')
        self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM blob_parts'),0)
    def test_checksum_failure_and_interrupt_preserve_blobs(self):
        pk=self.add();my.execute(self.c,'UPDATE media_items SET sha1=%s WHERE id=%s',('0'*40,pk));self.c.commit()
        with self.assertRaises(ValueError):files.migrate_one(self.c,pk)
        self.c.rollback();self.assertEqual(my.scalar(self.c,'SELECT data FROM media_items WHERE id=%s',(pk,)),b'original')
        my.execute(self.c,'UPDATE media_items SET sha1=NULL WHERE id=%s',(pk,));self.c.commit()
        original=my.execute
        def interrupt(c,sql,args=()):
            if sql.startswith('DELETE FROM blob_parts'):raise RuntimeError('interrupt')
            return original(c,sql,args)
        with patch.object(my,'execute',side_effect=interrupt):
            with self.assertRaises(RuntimeError):files.migrate_one(self.c,pk)
        self.c.rollback();self.assertEqual(my.scalar(self.c,'SELECT data FROM media_items WHERE id=%s',(pk,)),b'original')
        self.assertIsNotNone(files.migrate_one(self.c,pk))
    def test_missing_then_cached_does_not_replace_existing_media(self):
        row=dict(platform='wechat',account_key='fixture',message_pk=99,chat_id='fake',ts=1,msg_kind='image',source_key='key',media_role='original',unavailable=1,reason='missing')
        pk=files.save(self.c,row);self.c.commit();row.update(unavailable=0,reason=None)
        self.assertEqual(files.save(self.c,row,b'cached'),pk);self.c.commit()
        self.assertEqual(files.read(self.c,pk),b'cached')
        row.update(unavailable=1,reason='missing');files.save(self.c,row);self.c.commit();self.assertEqual(files.read(self.c,pk),b'cached')
    def test_original_upgrade_does_not_keep_thumbnail_caption_source(self):
        row=dict(platform='wechat',account_key='fixture',message_pk=99,chat_id='fake',ts=1,msg_kind='image',source_key='key',media_role='thumb',is_thumb=1,decoded_mime='image/jpeg',unavailable=0)
        pk=files.save(self.c,row,b'thumb-original',b'thumb-preview');self.c.commit()
        captions.enqueue(self.c)
        row.update(media_role='original',is_thumb=0);row.pop('decoded_mime')
        self.assertEqual(files.save(self.c,row,b'full-original'),pk);self.c.commit()
        stored=my.query(self.c,'SELECT * FROM media_items WHERE id=%s',(pk,))[0]
        self.assertIsNone(stored['decoded_path']);self.assertIsNone(stored['decoded_sha256'])
        self.assertEqual(files.read(self.c,pk),b'full-original')
        captions.enqueue(self.c)
        self.assertEqual(captions.next_item(self.c)[0]['image_sha256'],hashlib.sha256(b'full-original').hexdigest())

    def test_caption_reuse_retry_and_non_image_untouched(self):
        pk=self.add();files.migrate_one(self.c,pk);voice=self.add(kind='voice');files.migrate_one(self.c,voice)
        file=self.add(kind='file');self.assertIsNone(files.migrate_one(self.c,file))
        self.assertEqual(my.scalar(self.c,'SELECT data FROM media_items WHERE id=%s',(file,)),b'original')
        captions.enqueue(self.c);captions.enqueue(self.c);self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM image_captions'),1)
        item=captions.next_item(self.c)[0]
        captions.process(self.c,item,lambda data:dict(caption='合成测试',image_text=''))
        self.assertFalse(captions.next_item(self.c))
        my.execute(self.c,"UPDATE image_captions SET status='pending'");self.c.commit();item=captions.next_item(self.c)[0]
        def quota(data):raise captions.APIError('quota',True)
        with self.assertRaises(captions.APIError):captions.process(self.c,item,quota)
        self.assertEqual(my.scalar(self.c,'SELECT status FROM image_captions'),'paused')
        self.assertEqual(files.read(self.c,pk),b'original')

class CollectionTests(unittest.TestCase):
    setUp=DatabaseTests.setUp
    tearDown=DatabaseTests.tearDown
    def test_wechat_collector_retries_missing_cache_and_preserves_ids(self):
        from fixtures import wx_fixture,media_fixture
        from mysql_store import wx_import
        import chat_media_sync
        with tempfile.TemporaryDirectory() as tmp:
            paths,tables=wx_fixture(tmp);media_fixture(paths,tables)
            wx_import.run(paths,database=self.db,skip_media=True,verbose=False)
            first=chat_media_sync.wx_collect(self.c,paths)
            self.assertEqual(first['failed'],0)
            rows=my.query(self.c,'SELECT id,msg_kind,file_path FROM media_items ORDER BY id')
            self.assertTrue(any(r['file_path'] for r in rows))
            chat_media_sync.wx_collect(self.c,paths)
            self.assertEqual(rows,my.query(self.c,'SELECT id,msg_kind,file_path FROM media_items ORDER BY id'))
            self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM media_items WHERE data IS NOT NULL OR decoded_data IS NOT NULL'),0)
    def test_wecom_collector_account_mapping_and_repeat(self):
        from fixtures import wecom_fixture,wx_fixture,media_fixture
        from mysql_store import wecom_import
        import chat_media_sync
        with tempfile.TemporaryDirectory() as tmp:
            paths,tables=wx_fixture(tmp);raw,plain,ds=wecom_fixture(tmp);media_fixture(paths,tables,raw,ds)
            wecom_import.run(database=self.db,raw_root=raw,plain_root=plain,skip_media=True,verbose=False)
            first=chat_media_sync.wecom_collect(self.c,raw)
            self.assertEqual(first['failed'],0)
            rows=my.query(self.c,'SELECT id,msg_kind,file_path,account_key FROM media_items ORDER BY id')
            self.assertTrue(any(r['file_path'] for r in rows))
            chat_media_sync.wecom_collect(self.c,raw)
            self.assertEqual(rows,my.query(self.c,'SELECT id,msg_kind,file_path,account_key FROM media_items ORDER BY id'))
            self.assertEqual(my.scalar(self.c,'SELECT COUNT(*) FROM media_items WHERE data IS NOT NULL OR decoded_data IS NOT NULL'),0)

if __name__=='__main__':unittest.main()
