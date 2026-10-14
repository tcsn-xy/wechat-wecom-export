"""Hourly sync: synthetic fixtures only; temporary databases cleaned after tests."""
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
from fixtures import wx_fixture, media_fixture
import sqlcipher3
from localio import task_lock
from mysql_store import conn, schema
import wx_mysql_sync as sync


def args(tmp, paths=None, database='repairtest_unused'):
    return argparse.Namespace(state=str(Path(tmp)/'status.json'), no_snapshot=True,
        snapshot_root=paths.data if paths else None,
        capture=paths.capture if paths else None, database=database)


class Failures(unittest.TestCase):
    def test_snapshot_failure_never_imports_or_advances_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=args(tmp);a.no_snapshot=False
            Path(a.state).write_text(json.dumps({'last_success':'previous','account_key':'fixture'}))
            with patch.object(sync,'refresh',side_effect=sync.SyncError('快照失败')), patch.object(sync.my,'connect') as connect:
                self.assertEqual(sync.sync(a),1)
                connect.assert_not_called()
            s=sync.read_state(a.state)
            self.assertEqual(s['last_success'],'previous');self.assertEqual(s['status'],'failed')

    def test_overlap_preserves_running_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=args(tmp);Path(a.state).write_text('{"status":"running"}')
            with task_lock(Path(a.state).with_suffix('.lock')):
                import subprocess,sys
                result=subprocess.run([sys.executable,str(sync.ROOT/'scripts/wx_mysql_sync.py'),
                    '--state',a.state,'--no-snapshot'],capture_output=True,text=True)
                self.assertEqual(result.returncode,0)
                self.assertIn('本次跳过',result.stdout)
            self.assertEqual(sync.read_state(a.state)['status'],'running')

    def test_launchd_interval_and_no_shell(self):
        p=sync.launch_config()
        self.assertEqual(p['StartInterval'],3600);self.assertTrue(p['RunAtLoad'])
        self.assertEqual(p['ProgramArguments'][0],str(sync.ROOT/'.venv/bin/python'))

    def test_original_running_blocks_snapshot(self):
        with patch.object(sync,'running_wechats',return_value=[(1,'/Applications/WeChat.app/Contents/MacOS/WeChat')]),patch.object(sync,'child') as child:
            with self.assertRaises(sync.SyncError):sync.refresh()
            child.assert_not_called()

    def test_database_error_is_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths,_=wx_fixture(tmp);a=args(tmp,paths)
            with patch.object(sync.my,'connect',side_effect=RuntimeError('SECRET BODY PASSWORD')):
                self.assertEqual(sync.sync(a),1)
            self.assertNotIn('SECRET',Path(a.state).read_text())

    def test_mysql_connection_refused_reports_code_without_secrets(self):
        import pymysql
        with tempfile.TemporaryDirectory() as tmp:
            paths,_=wx_fixture(tmp);a=args(tmp,paths)
            with patch.object(sync.my,'connect',side_effect=pymysql.err.OperationalError(2003,'SECRET')):
                self.assertEqual(sync.sync(a),1)
            state=sync.read_state(a.state)
            self.assertEqual(state['mysql_errno'],2003)
            self.assertNotIn('SECRET',Path(a.state).read_text())

    def test_foreign_account_blocks_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths,_=wx_fixture(tmp);a=args(tmp,paths)
            Path(a.state).write_text('{"account_key":"another"}')
            with patch.object(sync.my,'connect') as connect:
                self.assertEqual(sync.sync(a),1);connect.assert_not_called()


class Integration(unittest.TestCase):
    def test_legacy_text_upgrade_preserves_media_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths,tables=wx_fixture(tmp);media_fixture(paths,tables)
            name='repairtest_sync_'+uuid.uuid4().hex[:12]
            c=conn.connect();schema.create_all(c,name);c.close()
            try:
                from mysql_store import wx_import
                wx_import.run(paths,database=name,verbose=False)
                c=conn.connect(name)
                before=conn.query(c,'SELECT id,data,decoded_data FROM media_items ORDER BY id')
                conn.execute(c,'ALTER TABLE message_raw DROP COLUMN raw_blob, DROP COLUMN extra_blob')
                conn.execute(c,'ALTER TABLE messages DROP INDEX uk_msg, DROP COLUMN source_identity, ADD UNIQUE KEY uk_msg (chat_pk,source_db(60),source_table(60),local_id)')
                conn.execute(c,'ALTER TABLE media_items DROP INDEX uk_media_identity, DROP COLUMN identity_key, DROP COLUMN account_key, DROP COLUMN content_md5, DROP COLUMN decoded_sha1')
                c.commit();c.close()
                self.assertEqual(sync.sync(args(tmp,paths,name)),0)
                c=conn.connect(name)
                self.assertEqual(conn.query(c,'SELECT id,data,decoded_data FROM media_items ORDER BY id'),before)
                self.assertNotIn('identity_key',{r['Field'] for r in conn.query(c,'SHOW COLUMNS FROM media_items')})
                c.close()
            finally:
                c=conn.connect();schema.drop_db(c,name);c.close()

    def test_backfill_raw_links_media_and_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths,tables=wx_fixture(tmp);media_fixture(paths,tables)
            name='repairtest_sync_'+uuid.uuid4().hex[:12]
            c=conn.connect();schema.create_all(c,name);c.close()
            try:
                # Existing media must survive text-only hourly sync.
                from mysql_store import wx_import
                wx_import.run(paths,database=name,verbose=False)
                c=conn.connect(name)
                media=conn.query(c,'SELECT id,content_md5,sha1 FROM media_items ORDER BY id');c.close()
                a=args(tmp,paths,name)
                self.assertEqual(sync.sync(a),0)
                self.assertEqual(sync.read_state(a.state)['added_rows'],0)
                d=sqlcipher3.connect(str(Path(paths.db_storage)/'message/message_1.db'))
                d.execute("PRAGMA key='fixture-only'")
                d.execute('INSERT INTO %s VALUES (9,1,1,1500000000,?,99,9,0)'%tables[1],('wxid_friend:\nhistorical https://example.com',))
                d.execute('INSERT INTO %s VALUES (10,1,1,1890000000,?,100,10,0)'%tables[1],('wxid_friend:\nnew',))
                d.commit();d.close()
                self.assertEqual(sync.sync(a),0)
                self.assertEqual(sync.read_state(a.state)['added_rows'],2)
                # Abort audit after successful writes; next run must recover idempotently.
                with patch.object(sync,'audit',side_effect=RuntimeError('interrupted')):
                    self.assertEqual(sync.sync(a),1)
                self.assertEqual(sync.sync(a),0)
                state=sync.read_state(a.state)
                self.assertEqual(state['after']['earliest_ts'],1500000000)
                self.assertEqual(state['added_rows'],0)
                report=sync.read_state(Path(tmp)/'audit.json');self.assertTrue(report['ok'])
                self.assertTrue(any(s['source_db']=='message_1.db' for s in report['sources']))
                c=conn.connect(name)
                self.assertEqual(conn.scalar(c,'SELECT COUNT(*) FROM message_links WHERE url=%s',('https://example.com',)),1)
                self.assertEqual(conn.query(c,'SELECT id,content_md5,sha1 FROM media_items ORDER BY id'),media)
                c.close()
                self.assertEqual(sync.sync(a),0)
                # Audit catches deleted raw rows even for an empty message body.
                c=conn.connect(name);conn.execute(c,'DELETE FROM message_raw LIMIT 1');c.commit()
                from wxexport import export
                self.assertFalse(sync.audit(c,paths,export.Exporter(paths,with_media=False,verbose=False).enumerate_chats())['ok'])
                c.close()
                self.assertEqual(sync.sync(a),0)
                # Failed key validation must not touch existing DB records.
                Path(paths.capture).write_text('')
                self.assertEqual(sync.sync(a),1)
                self.assertIsNotNone(sync.read_state(a.state)['last_success'])
            finally:
                c=conn.connect();schema.drop_db(c,name);c.close()


if __name__=='__main__':unittest.main()
