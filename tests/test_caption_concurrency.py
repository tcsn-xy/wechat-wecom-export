"""Concurrency and recovery against synthetic repairtest_* queues only."""
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
import fixtures  # Establish project library paths for unittest discovery.
from mysql_store import conn as my,schema,files,captions
import image_caption_worker as worker


class ControlTests(unittest.TestCase):
    def test_throttle_wave_halves_once_and_gradually_recovers(self):
        ctl=worker.AdaptiveConcurrency(64,0)
        ctl.throttle(1,60);self.assertEqual(ctl.target,32)
        ctl.throttle(2,60);self.assertEqual(ctl.target,32)
        for _ in range(100):ctl.succeeded(10)
        self.assertEqual(ctl.target,32)
        ctl.succeeded(123);self.assertEqual(ctl.target,40)
        ctl.throttle(124,60);self.assertEqual(ctl.target,20)
    def test_retry_after_is_bounded(self):
        self.assertEqual(captions.retry_after_seconds('90'),90)
        self.assertEqual(captions.retry_after_seconds('not a header'),60)
        self.assertEqual(captions.retry_after_seconds('999999'),3600)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.db='repairtest_parallel_'+uuid.uuid4().hex[:10]
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,CHAT_MEDIA_ROOT=self.tmp.name);self.env.start()
        with my.connect() as c:schema.create_all(c,self.db)
        with my.connect(self.db) as c:
            files.prepare(c)
            for i in range(24):
                row=dict(platform='wechat',account_key='synthetic',message_pk=i+1,chat_id='fixture',ts=i,
                    msg_kind='image',source_key=str(i),media_role='original',unavailable=0)
                files.save(c,row,('synthetic-image-'+str(i)).encode())
            c.commit();captions.enqueue(c)
        self.state=Path(self.tmp.name)/'state.json'
    def tearDown(self):
        with my.connect() as c:my.execute(c,'DROP DATABASE `'+self.db+'`')
        self.env.stop();self.tmp.cleanup()
    def run_worker(self,processor,**kw):
        return worker.work(processor=processor,databases=[self.db],state_path=self.state,**kw)
    def test_parallel_claims_no_duplicates_and_limit(self):
        lock=threading.Lock();active=0;peak=0;ids=[];connection_ids=[]
        gate=threading.Barrier(4)
        def process(c,item):
            nonlocal active,peak
            with lock:
                active+=1;peak=max(peak,active);ids.append(item['id'])
                connection_ids.append(my.scalar(c,'SELECT CONNECTION_ID()'))
                first=len(ids)<=4
            if first:gate.wait(timeout=10)
            time.sleep(0.03)
            my.execute(c,"UPDATE image_captions SET status='complete',caption='synthetic',image_text='' WHERE id=%s",(item['id'],));c.commit()
            with lock:active-=1
        result=self.run_worker(process,max_concurrency=4,limit=17)
        self.assertEqual(result['completed'],17);self.assertEqual(result['submitted'],17)
        self.assertEqual(peak,4);self.assertEqual(len(set(ids)),17)
        self.assertEqual(len(set(connection_ids)),17)
        again=self.run_worker(process,max_concurrency=4)
        self.assertEqual(again['completed'],7);self.assertEqual(len(set(ids)),24)
        with my.connect(self.db) as c:self.assertEqual(my.scalar(c,"SELECT COUNT(*) FROM image_captions WHERE status='processing'"),0)
    def test_quota_stops_dispatch_and_drains_successful_inflight(self):
        gate=threading.Barrier(2);lock=threading.Lock();order=[]
        def process(c,item):
            with lock:order.append(item['id']);first=len(order)==1
            gate.wait(timeout=10)
            if first:
                def quota(data):raise captions.APIError('provider_2056',True)
                captions.process(c,item,quota)
            else:
                time.sleep(0.1)
                my.execute(c,"UPDATE image_captions SET status='complete' WHERE id=%s",(item['id'],));c.commit()
        result=self.run_worker(process,max_concurrency=2)
        self.assertEqual(result['status'],'paused');self.assertEqual(result['submitted'],2)
        self.assertEqual(result['completed'],1)
        with my.connect(self.db) as c:
            self.assertEqual(my.scalar(c,"SELECT COUNT(*) FROM image_captions WHERE status='processing'"),0)
            self.assertEqual(my.scalar(c,"SELECT COUNT(*) FROM image_captions WHERE status='pending'"),22)
    def test_rate_errors_are_retryable_without_exhausting_image_attempts(self):
        with my.connect(self.db) as c:
            item=captions.next_item(c)[0]
            def limited(data):raise captions.APIError('http_429',True,retry_after=3)
            with self.assertRaises(captions.APIError):captions.process(c,item,limited)
            row=my.query(c,'SELECT status,attempts,next_attempt FROM image_captions WHERE id=%s',(item['id'],))[0]
            self.assertEqual(row['status'],'retry');self.assertEqual(row['attempts'],0)
            self.assertIsNotNone(row['next_attempt'])
    def test_abandoned_claim_is_recovered(self):
        with my.connect(self.db) as c:
            my.execute(c,"UPDATE image_captions SET status='processing'");c.commit()
        def process(c,item):
            my.execute(c,"UPDATE image_captions SET status='complete' WHERE id=%s",(item['id'],));c.commit()
        result=self.run_worker(process,max_concurrency=8)
        self.assertEqual(result['completed'],24)
    def test_database_failure_requeues_claims_and_stops(self):
        def broken(c,item):raise OSError('do not log sensitive fixture')
        result=self.run_worker(broken,max_concurrency=2)
        self.assertEqual(result['status'],'failed')
        self.assertNotIn('sensitive',self.state.read_text())
        with my.connect(self.db) as c:self.assertEqual(my.scalar(c,"SELECT COUNT(*) FROM image_captions WHERE status='processing'"),0)
    def test_graceful_stop_drains_inflight(self):
        stop=threading.Event()
        def process(c,item):
            stop.set()
            my.execute(c,"UPDATE image_captions SET status='complete' WHERE id=%s",(item['id'],));c.commit()
        result=self.run_worker(process,max_concurrency=2,stop_event=stop)
        self.assertEqual(result['status'],'stopped');self.assertGreaterEqual(result['completed'],1)
        with my.connect(self.db) as c:self.assertEqual(my.scalar(c,"SELECT COUNT(*) FROM image_captions WHERE status='processing'"),0)

if __name__=='__main__':unittest.main()
