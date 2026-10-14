#!/usr/bin/env python3
"""Independent MiniMax image caption worker and launchd management."""
import argparse
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED,CancelledError
import signal
import threading
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'lib'),str(ROOT/'scripts')]
from mysql_store import conn as my,files,captions
from localio import task_lock,atomic_json
LABEL='local.wxexport.image-caption'
STATE=ROOT/'.runtime/image-captions/status.json'
PLIST=Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')
MAX_CONCURRENCY=64
DATABASES=('wx_chat','wecom_chat')


def manage(action,max_concurrency=MAX_CONCURRENCY):
    target='gui/'+str(os.getuid());PLIST.parent.mkdir(parents=True,exist_ok=True)
    if action in ('disable','uninstall'):
        if action=='disable':subprocess.run(['launchctl','disable',target+'/'+LABEL],check=True,capture_output=True)
        subprocess.run(['launchctl','bootout',target+'/'+LABEL],capture_output=True)
        if action=='uninstall':PLIST.unlink(missing_ok=True)
        return
    STATE.parent.mkdir(parents=True,exist_ok=True)
    configuration=dict(Label=LABEL,ProgramArguments=[str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/image_caption_worker.py'),'--concurrency',str(max_concurrency)],
        EnvironmentVariables=__import__('local_config').launch_environment(),Umask=0o077,WorkingDirectory=str(ROOT),RunAtLoad=True,StartInterval=300,ProcessType='Background',ExitTimeOut=120,
        StandardOutPath=str(STATE.parent/'launchd.log'),StandardErrorPath=str(STATE.parent/'error.log'))
    PLIST.write_bytes(plistlib.dumps(configuration));os.chmod(PLIST,0o600)
    subprocess.run(['launchctl','bootout',target+'/'+LABEL],capture_output=True)
    subprocess.run(['launchctl','enable',target+'/'+LABEL],check=True,capture_output=True)
    subprocess.run(['launchctl','bootstrap',target,str(PLIST)],check=True,capture_output=True)


class AdaptiveConcurrency:
    def __init__(self,maximum,now=0):
        self.maximum=maximum;self.target=maximum
        self.cooldown_until=0;self.next_growth=now+60;self.successes=0

    def throttle(self,now,seconds):
        # A wave of simultaneous 429s lowers concurrency once, not once per response.
        if now>=self.cooldown_until:self.target=max(1,self.target//2)
        self.cooldown_until=max(self.cooldown_until,now+seconds)
        self.next_growth=self.cooldown_until+60;self.successes=0

    def succeeded(self,now):
        self.successes+=1
        if now>=self.next_growth and self.successes>=self.target:
            self.target=min(self.maximum,self.target+max(1,self.target//4))
            self.successes=0;self.next_growth=now+60


def run_item(database,item,processor,stop_event):
    # A PyMySQL connection must never be shared between worker threads.
    try:
        if stop_event.is_set():return dict(cancelled=True)
        with my.connect(database) as c:
            if processor is captions.process:processor(c,item,stop_event=stop_event)
            else:processor(c,item)
        return dict(ok=True)
    except CancelledError:return dict(cancelled=True)
    except captions.APIError as exc:
        return dict(ok=False,code=exc.code,rate_limited=exc.rate_limited,
                    pause=exc.pause and not exc.rate_limited,retry_after=exc.retry_after)
    except Exception as exc:
        return dict(ok=False,code=type(exc).__name__,unexpected=True)


def release_claim(c,item,code=None):
    my.execute(c,"UPDATE image_captions SET status=%s,error_code=%s,next_attempt=%s WHERE id=%s AND status='processing'",
        ('retry' if code else 'pending',code,dt.datetime.now()+dt.timedelta(seconds=60) if code else None,item['id']))
    c.commit()


def work(limit=0,processor=captions.process,*,max_concurrency=MAX_CONCURRENCY,
         databases=DATABASES,state_path=None,stop_event=None):
    if not 1<=max_concurrency<=MAX_CONCURRENCY:raise ValueError('并发必须为 1–64')
    if processor is captions.process:captions.load_config()
    state_path=Path(state_path or STATE);stop_event=stop_event or threading.Event()
    started=time.monotonic();control=AdaptiveConcurrency(max_concurrency,started)
    result=dict(status='running',started_at=dt.datetime.now().isoformat(),completed=0,failed=0,
                submitted=0,cancelled=0,max_concurrency=max_concurrency,peak_inflight=0,error_counts={})
    connections={};futures={};pool=None

    def update():
        result.update(updated_at=dt.datetime.now().isoformat(),inflight=len(futures),
                      target_concurrency=control.target,cooldown_seconds=max(0,round(control.cooldown_until-time.monotonic())),
                      elapsed_seconds=round(time.monotonic()-started,1),**captions.network_stats())
        result['peak_inflight']=max(result['peak_inflight'],len(futures))
        result['completed_per_minute']=round(60*result['completed']/max(1,time.monotonic()-started),1)
        atomic_json(state_path,result)

    with task_lock(state_path.with_suffix('.lock')):
        update()
        try:
            for db in databases:
                c=my.connect(db);connections[db]=c
                if my.scalar(c,'SELECT GET_LOCK(%s,0)',('image-caption:'+db,))!=1:
                    raise RuntimeError('caption 任务重叠')
                # Exclusive scheduler ownership makes abandoned claims safe to recover.
                my.execute(c,"UPDATE image_captions SET status='pending' WHERE status='processing'")
                my.execute(c,"UPDATE image_captions SET status='retry' WHERE status='paused' AND error_code IN ('provider_1002','provider_1041','provider_2045','http_429')")
                my.execute(c,"UPDATE image_captions SET status='pending',attempts=0,error_code=NULL,next_attempt=NULL WHERE status='paused' AND error_code='provider_2056' AND next_attempt<=NOW()")
                c.commit();captions.enqueue(c)
                delay=my.scalar(c,"SELECT COALESCE(TIMESTAMPDIFF(SECOND,NOW(),MAX(next_attempt)),0) FROM image_captions WHERE status='retry' AND error_code IN ('provider_1002','provider_1041','provider_2045','http_429')")
                if delay>0:control.throttle(time.monotonic(),delay)
            if any(my.scalar(c,"SELECT COUNT(*) FROM image_captions WHERE status='paused'") for c in connections.values()):
                result.update(status='paused',error_code='provider_paused');return result
            pool=ThreadPoolExecutor(max_workers=max_concurrency,thread_name_prefix='caption')
            stopping=False;last_update=0
            while True:
                for future in [f for f in futures if f.done()]:
                    db,item=futures.pop(future);c=connections[db]
                    if future.cancelled():
                        release_claim(c,item);result['cancelled']+=1;continue
                    outcome=future.result()
                    if outcome.get('cancelled'):
                        release_claim(c,item);result['cancelled']+=1;continue
                    if outcome['ok']:
                        result['completed']+=1;control.succeeded(time.monotonic())
                    else:
                        code=outcome['code'];result['failed']+=1;result['error_code']=code
                        result['error_counts'][code]=result['error_counts'].get(code,0)+1
                        if outcome.get('rate_limited'):
                            control.throttle(time.monotonic(),outcome['retry_after'])
                        elif outcome.get('pause'):
                            result['status']='paused';result['pause_reason']=code;stopping=True;stop_event.set()
                        elif outcome.get('unexpected'):
                            release_claim(c,item,'worker_'+code)
                            result['status']='failed';stopping=True;stop_event.set()
                if stop_event.is_set():
                    stopping=True
                    if result['status']=='running':result['status']='stopped'
                if stopping:
                    for future in futures:future.cancel()
                    if not futures:break
                elif limit and result['submitted']>=limit:
                    if not futures:result['status']='complete';break
                elif time.monotonic()>=control.cooldown_until:
                    room=max(0,control.target-len(futures))
                    if limit:room=min(room,limit-result['submitted'])
                    if room:
                        candidates=[]
                        for db,c in connections.items():
                            c.commit()
                            candidates.extend((db,item) for item in captions.next_items(c,room))
                        candidates.sort(key=lambda pair:pair[1]['priority_ts'] or 0,reverse=True)
                        if not candidates and not futures:result['status']='idle';break
                        for db,item in candidates[:room]:
                            if stop_event.is_set():break
                            c=connections[db]
                            claimed=my.execute(c,"UPDATE image_captions SET status='processing' WHERE id=%s AND status IN ('pending','retry') AND (next_attempt IS NULL OR next_attempt<=NOW())",(item['id'],)).rowcount
                            c.commit()
                            if not claimed:continue
                            future=pool.submit(run_item,db,item,processor,stop_event)
                            futures[future]=(db,item);result['submitted']+=1
                if time.monotonic()-last_update>=1:
                    update();last_update=time.monotonic()
                if futures:wait(futures,timeout=0.5,return_when=FIRST_COMPLETED)
                else:stop_event.wait(min(1,max(0.05,control.cooldown_until-time.monotonic())))
            return result
        except Exception as exc:
            result.update(status='failed',error_code=type(exc).__name__)
            raise
        finally:
            # Drain in-flight responses before giving another scheduler ownership.
            if pool:pool.shutdown(wait=True,cancel_futures=True)
            for db,c in connections.items():
                try:
                    if my.scalar(c,'SELECT IS_USED_LOCK(%s)=CONNECTION_ID()',('image-caption:'+db,)):
                        my.execute(c,"UPDATE image_captions SET status='pending' WHERE status='processing'")
                        c.commit();my.execute(c,'SELECT RELEASE_LOCK(%s)',('image-caption:'+db,))
                finally:c.close()
            futures.clear();result['finished_at']=dt.datetime.now().isoformat();update()


def main():
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__);g=p.add_mutually_exclusive_group()
    for action in ['install','disable','uninstall','status']:g.add_argument('--'+action,action='store_true')
    p.add_argument('--limit',type=int,default=0)
    p.add_argument('--concurrency',type=int,default=MAX_CONCURRENCY,choices=range(1,MAX_CONCURRENCY+1))
    a=p.parse_args()
    for action in ['install','disable','uninstall']:
        if getattr(a,action):
            if action=='install':captions.load_config()
            manage(action,a.concurrency);return 0
    if a.status:print(STATE.read_text() if STATE.exists() else '{}');return 0
    captions.load_config()
    stop=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    try:result=work(a.limit,max_concurrency=a.concurrency,stop_event=stop)
    except RuntimeError:return 0
    except Exception as exc:
        result=dict(status='failed',error_code=type(exc).__name__);atomic_json(STATE,result)
    print(json.dumps(result,ensure_ascii=False));return int(result['status']=='failed')

if __name__=='__main__':sys.exit(main())
