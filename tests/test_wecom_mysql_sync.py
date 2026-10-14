import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid
from fixtures import wecom_fixture
from mysql_store import conn,schema
import wecom_mysql_sync as sync
import chat_mysql_sync as both


def options(root,raw,plain,db):
    return argparse.Namespace(state=str(Path(root)/'status.json'),no_snapshot=True,raw=str(raw),plain=str(plain),media_root=str(raw),key=str(Path(root)/'fixture-key.json'),database=db)


class Failures(unittest.TestCase):
    def test_platform_failure_does_not_skip_other(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(both,'STATE',Path(tmp)/'both.json'):
            seen=[]
            def run(cmd):
                seen.append(Path(cmd[1]).name)
                return argparse.Namespace(returncode=1 if len(seen)==1 else 0)
            self.assertEqual(both.run_all(run),1)
            self.assertEqual(seen,['wecom_mysql_sync.py','wx_mysql_sync.py','chat_media_sync.py'])
            self.assertEqual(json.loads(both.STATE.read_text())['status'],'partial_failure')

    def test_snapshot_failure_never_imports_and_preserves_last_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw,plain,ds=wecom_fixture(tmp);a=options(tmp,raw,plain,'repairtest_unused');a.no_snapshot=False
            Path(a.state).write_text('{"last_success":"previous","accounts":["1001"]}')
            with patch.object(sync,'refresh',side_effect=sync.SyncError('企微读取失败')),patch.object(sync.my,'connect') as connect:
                self.assertEqual(sync.sync(a),1);connect.assert_not_called()
            self.assertEqual(json.loads(Path(a.state).read_text())['last_success'],'previous')

    def test_account_change_stops_before_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw,plain,ds=wecom_fixture(tmp);a=options(tmp,raw,plain,'repairtest_unused')
            Path(a.state).write_text('{"accounts":["other"]}')
            with patch.object(sync.my,'connect') as connect:
                self.assertEqual(sync.sync(a),1);connect.assert_not_called()

    def test_original_service_configuration_keeps_interval(self):
        p=both.configuration();self.assertEqual(p['StartInterval'],3600)
        self.assertEqual(Path(p['ProgramArguments'][1]).name,'chat_mysql_sync.py')


class Integration(unittest.TestCase):
    def test_text_history_idempotence_raw_extra_and_media_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw,plain,ds=wecom_fixture(tmp);name='repairtest_wecomsync_'+uuid.uuid4().hex[:10]
            c=conn.connect();schema.create_all(c,name);c.close()
            try:
                c=conn.connect(name)
                with c.cursor() as cur:cur.execute("INSERT INTO media_items(platform,msg_kind,source_key,data) VALUES ('wecom','file','fixture',%s)",(b'preserved-fixture',))
                c.commit();c.close()
                a=options(tmp,raw,plain,name)
                self.assertEqual(sync.sync(a),0)
                initial=json.loads(Path(a.state).read_text())['after']['messages']
                self.assertEqual(sync.sync(a),0)
                self.assertEqual(json.loads(Path(a.state).read_text())['added_rows'],0)
                d=sqlite3.connect(ds/'message.db')
                d.execute('INSERT INTO message_table VALUES (?,?,?,?,?,?,?,?,?,?)',(99,99,99,'u_a','R:1001',0,1500000000,0,b'historical https://example.com',b'\0extra\xff'));d.commit();d.close()
                self.assertEqual(sync.sync(a),0)
                state=json.loads(Path(a.state).read_text());self.assertEqual(state['added_rows'],1)
                self.assertEqual(state['after']['messages'],initial+1)
                self.assertEqual(state['after']['earliest_ts'],1500000000)
                c=conn.connect(name)
                self.assertEqual(conn.scalar(c,"SELECT data FROM media_items WHERE source_key='fixture'"),b'preserved-fixture')
                rawrow=conn.query(c,'SELECT r.raw_blob,r.extra_blob FROM message_raw r JOIN messages m ON m.id=r.message_pk WHERE m.local_id=99')[0]
                self.assertEqual(rawrow['extra_blob'],b'\0extra\xff')
                self.assertTrue(conn.scalar(c,'SELECT COUNT(*) FROM message_links'))
                self.assertEqual(conn.scalar(c,"SELECT chat_id FROM messages WHERE local_id=99"),"R:1001")
                conn.execute(c,'DELETE FROM message_raw LIMIT 1');c.commit()
                self.assertFalse(sync.audit(c,[ds])['ok']);c.close()
                with patch.object(sync,'audit',side_effect=RuntimeError('private content should not appear')):
                    self.assertEqual(sync.sync(a),1)
                self.assertNotIn('private content',Path(a.state).read_text())
                self.assertEqual(sync.sync(a),0)
            finally:
                c=conn.connect();schema.drop_db(c,name);c.close()


if __name__=='__main__':unittest.main()
