"""Cooperative per-platform scheduler: fast messages preempt bounded reconciliation."""
from __future__ import annotations
from collections import deque,OrderedDict
import datetime as dt
import hashlib
import json
from pathlib import Path
import time
from . import conn as my,realtime_store as store
from .realtime_sources import row_hash


class Job:
    def __init__(self,desc,iterator,full=False,event_at=None):
        self.desc=desc;self.iterator=iter(iterator);self.full=full;self.event_at=event_at;self.event_cursor=0
        self.rows=0;self.max_id=0;self.pending=None;self.chat_counts={}
    def close(self):
        close=getattr(self.iterator,'close',None)
        if close:close()


class Engine:
    def __init__(self,c,adapter):
        self.c=c;self.adapter=adapter;self.descriptors={};self.fast=OrderedDict();self.full=OrderedDict();self.cache={}
        self.cursors={r['source_key']:r for r in my.query(c,'SELECT * FROM rt_sources')}
        self.results=dict(added=0,updated=0,unchanged=0,stale=0)
        self.latencies=deque(maxlen=2000);self.last_success=None;self.last_full_success=None
        self.full_requested=False;self.full_active=False;self.unresolved=0;self.completed_sources=[];self.started=time.time();self.fast_steps=0
    def schedule(self,paths,event_at=None,force_full=False):
        descriptors,self.unresolved=self.adapter.descriptors(paths,self.c)
        if hasattr(self.adapter,'unresolved_counts'):self.unresolved=sum(self.adapter.unresolved_counts.values())
        descriptors.sort(key=lambda d:d.get('priority_ts',0),reverse=True)
        for desc in descriptors:
            key=store.source_key(desc);old=self.cursors.get(key)
            reset=old and (old['schema_hash']!=desc['schema_hash'] or old['file_identity']!=desc['file_identity'] or desc['max_id']<old['cursor_id'])
            if reset:
                with store.writer_lock(self.c):
                    my.execute(self.c,'UPDATE rt_sources SET cursor_id=0,bootstrap=1 WHERE source_key=%s',(key,));self.c.commit()
                old=dict(old,cursor_id=0,bootstrap=1);self.cursors[key]=old;self.cache.pop(key,None)
                if key in self.full:self.full.pop(key).close()
            self.descriptors[key]=desc
            if key in self.fast:self.fast.pop(key).close()
            # A new/restored table prioritizes its tail, while the complete history remains a durable obligation.
            cursor=old['cursor_id'] if old else desc['max_id']
            def batches(d=desc,start=cursor):
                yield from self.adapter.rows(d,after=start)
                yield from self.adapter.rows(d,tail=True)
            self.fast[key]=Job(desc,batches(),event_at=event_at if old and not reset else None)
            self.fast[key].event_cursor=cursor
            if not old or reset or old.get('bootstrap') or force_full:
                if key not in self.full:self.full[key]=Job(desc,self.adapter.rows(desc,batch_size=100),full=True)
        # Updated/new conversations must not sit behind the initial historical backlog.
        for desc in reversed(descriptors):self.fast.move_to_end(store.source_key(desc),last=False)
        if force_full:self.full_requested=True;self.full_active=True
    def request_full(self):
        self.full_requested=True;self.full_active=True
        for key,desc in self.descriptors.items():
            if key not in self.full:self.full[key]=Job(desc,self.adapter.rows(desc,batch_size=100),full=True)
    def step(self):
        if self.full and (not self.fast or self.fast_steps>=4):jobs=self.full;self.fast_steps=0
        else:jobs=self.fast;self.fast_steps+=1
        if not jobs:
            if self.full_active and not self.unresolved:
                self.last_full_success=dt.datetime.now().isoformat();self.full_active=False;self.full_requested=False
            return False
        key=next(iter(jobs));job=jobs[key];desc=job.desc
        if job.pending is None:
            try:job.pending=next(job.iterator)
            except StopIteration:
                cursor=self.cursors.get(key,{}).get('cursor_id',0)
                if not job.full:cursor=max(cursor,desc['max_id'])
                if job.full:self.register_source(desc,job.rows,job.chat_counts)
                result=store.write_batch(self.c,desc,[],cursor,desc['entry']['revision'],complete=job.full,checked_rows=job.rows)
                self.cursors[key]=my.query(self.c,'SELECT * FROM rt_sources WHERE source_key=%s',(key,))[0]
                jobs.pop(key);job.close();return True
        rows=job.pending
        # Full scans may be old; refresh each bounded batch from the latest validated generation.
        current=self.descriptors.get(key,desc)
        if job.full and current['entry']['revision']>desc['entry']['revision']:
            if current['schema_hash']!=desc['schema_hash']:
                jobs.pop(key);job.close();self.full[key]=Job(current,self.adapter.rows(current,batch_size=100),full=True);return True
            newer={r[0]:r for batch in self.adapter.rows(current,ids=[r[0] for r in rows]) for r in batch}
            if len(newer)==len(rows):rows=[newer[r[0]] for r in rows];desc=current
        memo=self.cache.setdefault(key,OrderedDict());records=[];seen=[]
        for row in rows:
            digest=row_hash(row)
            if job.full or memo.get(row[0])!=digest:
                record=self.adapter.decode(desc,row);record['_source_counter']=row[0];records.append(record)
            seen.append((row[0],digest))
        cursor=self.cursors.get(key,{}).get('cursor_id',0)
        if not job.full:cursor=max(cursor,max((r[0] for r in rows),default=cursor))
        result=store.write_batch(self.c,desc,records,cursor,desc['entry']['revision'],verify_existing=job.full)
        if job.full:
            for record in records:
                cid=record['message']['chat_id'];job.chat_counts[cid]=job.chat_counts.get(cid,0)+1
        for counter,digest in seen:
            memo[counter]=digest;memo.move_to_end(counter)
        while len(memo)>2000:memo.popitem(last=False)
        for name in self.results:self.results[name]+=result[name]
        if result['added'] and not job.full and job.event_at is not None:
            samples=sum(1 for counter in result['added_counters'] if counter>job.event_cursor)
            self.latencies.extend([round(time.monotonic()-job.event_at,3)]*samples)
        self.last_success=dt.datetime.now().isoformat();job.rows+=len(rows);job.pending=None
        self.cursors[key]=my.query(self.c,'SELECT * FROM rt_sources WHERE source_key=%s',(key,))[0]
        # One batch per source before rotating: a large group cannot starve new groups.
        jobs.move_to_end(key)
        return True
    def register_source(self,desc,count,chat_counts):
        with store.writer_lock(self.c):
            my.execute(self.c,'DELETE FROM import_sources WHERE platform=%s AND account_key=%s AND dataset=%s AND source_db=%s AND source_table=%s',
                (desc['platform'],desc['account_key'],desc['dataset'],desc['source_db'],desc['source_table']))
            scope='platform=%s AND account_key=%s AND source_dataset=%s AND source_db=%s AND source_table=%s'
            values=(desc['platform'],desc['account_key'],desc['dataset'],desc['source_db'],desc['source_table'])
            if desc['platform']=='wechat':
                pk=my.scalar(self.c,'SELECT chat_pk FROM chats WHERE platform=%s AND account_key=%s AND chat_id=%s',('wechat',desc['account_key'],desc['chat_id']))
                scope='chat_pk=%s AND '+scope;values=(pk,*values)
            stats=my.query(self.c,'SELECT chat_id,COUNT(*) n,MIN(ts) lo,MAX(ts) hi FROM messages WHERE '+scope+' GROUP BY chat_id',values)
            for stat in stats:
                my.execute(self.c,'INSERT INTO import_sources(platform,account_key,dataset,source_db,source_table,chat_id,source_rows,inserted_rows,min_ts,max_ts) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (desc['platform'],desc['account_key'],desc['dataset'],desc['source_db'],desc['source_table'],stat['chat_id'],chat_counts.get(stat['chat_id'],0),stat['n'],stat['lo'],stat['hi']))
            self.c.commit()
    def status(self):
        values=sorted(self.latencies);n=len(values)
        return dict(**self.results,fast_sources=len(self.fast),full_sources=len(self.full),unresolved_sources=self.unresolved,
            last_success=self.last_success,last_full_success=self.last_full_success,latency_samples=n,
            latency_p95_seconds=values[min(n-1,int(n*.95))] if n>=20 else None,
            latency_validation='measured' if n>=20 else 'insufficient_samples',
            last_latency_seconds=self.latencies[-1] if n else None)
    def collect(self):
        retained=[job.desc['entry']['folder'] for job in list(self.fast.values())+list(self.full.values())]
        self.adapter.snapshots.collect(retained)
    def close(self):
        for job in list(self.fast.values())+list(self.full.values()):job.close()
