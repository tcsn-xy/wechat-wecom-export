#!/usr/bin/env python3
"""Local WeChat/WeCom near-realtime text/raw/link ingestion."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import signal
import shutil
import subprocess
import sys
import threading
import traceback
import time
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'lib'),str(ROOT/'scripts')]
from localio import atomic_json,task_lock
from mysql_store import conn as my,realtime_store as store,realtime_bridge as bridge
from mysql_store.realtime_sources import Wechat,Wecom
from mysql_store.realtime_snapshot import Changes,Debounce,SnapshotChanged
from mysql_store.realtime_engine import Engine
from wxexport import config as wxconfig
from wecom import config as wcconfig,keys as wckeys,extract
LABEL='local.wxexport.realtime-sync'
PLIST=Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')
STATE=bridge.STATE


def adapter_for(platform):
    if platform=='wechat':
        snap=wxconfig.read_snapshot(str(Path(wxconfig.DATA)/'_snapshot.json'))
        account=snap.get('wxid');root=snap.get('account_path')
        from wx_mysql_sync import read_state,STATE as old_state
        if not root or not account or read_state(old_state).get('account_key') not in (None,account):raise ValueError('bound_wechat_account_missing')
        return Wechat(Path(root)/'db_storage',account,wxconfig.CAPTURE_FILE,STATE/'wechat-snapshots')
    old=ROOT/'.runtime/wecom-mysql-sync/status.json'
    accounts=json.loads(old.read_text()).get('accounts',[]) if old.exists() else []
    if not accounts:raise ValueError('bound_wecom_account_missing')
    return Wecom(Path(wcconfig.CONTAINER_DATA)/wcconfig.WXWORK_DATA,accounts,wckeys.load_key(wcconfig.KEY_FILE),STATE/'wecom-snapshots',media_root=wcconfig.RAW)


class Runner:
    def __init__(self,platform,stop,observe=False,once=False):
        self.platform=platform;self.database='wx_chat' if platform=='wechat' else 'wecom_chat'
        self.stop=stop;self.observe=observe;self.once=once;self.lock=threading.Lock()
        self.state=dict(enabled=not observe,ready=False,mode='starting',pid=os.getpid(),started_at=dt.datetime.now().isoformat())
        self.adapter=None;self.engine=None;self.c=None;self.watch=Changes();self.paths=[];self.last_publish=0
        self.seen={};self.dirty=set();self.event_at=None;self.debounce=Debounce()
        self.last_poll=0;self.last_discovery=0;self.last_probe=0;self.next_full=time.monotonic()+3600
        self.force_full=True;self.request_token=None;self.failures=0;self.first_capture=True
        self.last_wall=time.time();self.last_mono=time.monotonic()
    def publish(self,**fields):
        with self.lock:
            self.state.update(fields,heartbeat_unix=time.time(),heartbeat=dt.datetime.now().isoformat())
            urgent=fields.get('error') or fields.get('ready') is False or 'stopped_at' in fields
            if urgent or time.monotonic()-self.last_publish>=1:
                atomic_json(STATE/(self.platform+'.json'),dict(self.state));self.last_publish=time.monotonic()
    def cleanup_abandoned(self):
        # Called only after the daemon ownership lock and before any new readers exist.
        for folder in self.adapter.snapshots.root.glob('generation-*'):
            if folder.is_dir() and not folder.is_symlink():shutil.rmtree(folder)

    def discover(self):
        a=self.adapter
        if a.mode=='inspector':self.paths=a.remote_discover()
        else:
            try:self.paths=a.discover()
            except PermissionError:
                if self.platform!='wecom':raise
                a.enable_inspector();self.paths=a.remote_discover()
        self.watch.reset(self.paths if a.mode=='direct' else [])
        self.last_discovery=time.monotonic()
    def signatures(self):
        if self.adapter.mode=='inspector':return self.adapter.remote_signatures(self.paths)
        try:return self.adapter.signatures(self.paths)
        except PermissionError:
            if self.platform!='wecom':raise
            self.adapter.enable_inspector();return self.adapter.remote_signatures(self.paths)
    def connect(self):
        if self.c is not None:self.c.ping(reconnect=False);return
        self.c=my.connect(self.database)
        with store.writer_lock(self.c):store.prepare(self.c)
        self.engine=Engine(self.c,self.adapter)
    def requests(self):
        path=STATE/(self.platform+'.request.json')
        if path.exists():
            request=json.loads(path.read_text())
            if request['token']!=self.request_token:
                self.request_token=request['token'];self.force_full=True
        if time.monotonic()>=self.next_full:self.force_full=True
    def iteration(self,woken=False):
        if self.adapter is None:
            self.adapter=adapter_for(self.platform);self.cleanup_abandoned()
        now=time.monotonic();wall=time.time()
        if wall-self.last_wall-(now-self.last_mono)>5 or wall-self.last_wall>300:
            self.force_full=True;self.first_capture=True;self.last_discovery=0
            self.publish(resumed_at=dt.datetime.now().isoformat())
        self.last_wall=wall;self.last_mono=now
        if not self.paths or now-self.last_discovery>=30 or (woken and now-self.last_discovery>=2):self.discover()
        if self.adapter.mode=='inspector' and now-self.last_probe>=60:
            self.last_probe=now
            try:
                paths=self.adapter.discover();self.adapter.signatures(paths)
                self.adapter.mode='direct';self.adapter.snapshots.remote_copy=None;self.paths=paths
            except (OSError,RuntimeError):pass
        if now-self.last_poll>=2 or woken:
            latest=self.signatures();changed={p for p,v in latest.items() if self.seen.get(p)!=v}
            removed=set(self.seen)-set(latest)
            if changed or removed:
                if not self.dirty:self.event_at=time.monotonic()
                self.dirty.update(changed);self.debounce.mark(now)
                self.publish(last_event=dt.datetime.now().isoformat(),changed_files=len(changed),removed_files=len(removed))
            self.seen=latest;self.last_poll=now
        self.requests()
        if self.force_full and not self.dirty:
            self.dirty.update(map(str,self.paths));self.debounce.mark(now)
        if self.dirty and (self.debounce.due(now) or self.once):
            if not self.observe:
                self.connect()
                with store.writer_lock(self.c):pass
                rev=store.revision(self.c,self.platform)
            else:rev=0
            changed=list(self.paths) if self.force_full else [p for p in self.paths if str(p) in self.dirty]
            captured=[]
            for source in changed:
                if self.seen.get(str(source),[None])[0] is None:continue
                entry=self.adapter.snapshots.capture(source,rev)
                self.seen[str(source)]=entry['signature'];captured.append(source)
            if not self.observe:
                # Metadata changes can resolve previously unknown conversation tables.
                metadata_changed=any(p.name in ('session.db','contact.db','user.db') for p in captured)
                schedule=self.paths if self.force_full or (metadata_changed and self.engine.unresolved) else captured
                self.engine.schedule(schedule,event_at=None if self.first_capture else self.event_at,force_full=self.force_full)
                if self.force_full:self.next_full=time.monotonic()+3600
            self.first_capture=False;self.force_full=False;self.dirty.clear();self.event_at=None;self.debounce.clear()
            self.publish(mode=self.adapter.mode,ready=not self.observe,observed=True,validated_files=len(captured),snapshot_at=dt.datetime.now().isoformat(),error=None)
            if self.observe:
                self.adapter.snapshots.collect()
                if self.once:return False
        if self.engine:
            self.engine.step();self.engine.collect()
            self.publish(mode=self.adapter.mode,ready=True,error=None,writer_busy=False,**self.engine.status())
            if self.once and not self.engine.fast and not self.engine.full:return False
        return True
    def run(self):
        wake=False
        try:
            while not self.stop.is_set():
                try:
                    if not self.iteration(wake):break
                    self.failures=0
                    busy=bool(self.engine and (self.engine.fast or self.engine.full))
                    wake=self.watch.wait(0 if busy else 0.5)
                except BlockingIOError:
                    self.publish(writer_busy=True,error='writer_busy');self.stop.wait(1)
                except SnapshotChanged:
                    self.publish(error='snapshot_changed_retry');self.stop.wait(0.3)
                except (Exception,SystemExit) as exc:
                    self.failures+=1
                    self.publish(mode='degraded_hourly',ready=False,error=type(exc).__name__,mysql_errno=exc.args[0] if exc.args and isinstance(exc.args[0],int) else None,
                        error_locations=[Path(f.filename).name+':'+str(f.lineno)+':'+f.name for f in traceback.extract_tb(exc.__traceback__) if '/lib/mysql_store/' in f.filename or f.filename==__file__],failures=self.failures)
                    if self.engine:self.engine.close();self.engine=None
                    if self.c:self.c.close();self.c=None
                    self.adapter=None;self.paths=[];self.dirty.clear();self.seen={};self.force_full=True;self.first_capture=True;self.debounce.clear()
                    if self.once:break
                    self.stop.wait(60)
        finally:
            if self.engine:self.engine.close()
            if self.c:self.c.close()
            self.watch.close();self.publish(ready=False,enabled=False,stopped_at=dt.datetime.now().isoformat())


def manage(action,observe=False):
    domain='gui/'+str(os.getuid());target=domain+'/'+LABEL
    if action in ('disable','uninstall'):
        STATE.mkdir(parents=True,exist_ok=True);(STATE/'disabled').touch(mode=0o600)
    if action=='disable':subprocess.run(['launchctl','disable',target],check=True,capture_output=True)
    subprocess.run(['launchctl','bootout',target],capture_output=True)
    if action=='uninstall':PLIST.unlink(missing_ok=True)
    if action!='install':return
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    (STATE/'disabled').unlink(missing_ok=True)
    args=[str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/chat_watch_sync.py')]
    if observe:args.append('--observe')
    data=dict(Label=LABEL,ProgramArguments=args,EnvironmentVariables=__import__('local_config').launch_environment(),WorkingDirectory=str(ROOT),RunAtLoad=True,KeepAlive=True,
        ThrottleInterval=30,ExitTimeOut=30,ProcessType='Background',Umask=0o077,
        StandardOutPath=str(STATE/'launchd.log'),StandardErrorPath=str(STATE/'error.log'))
    PLIST.write_bytes(plistlib.dumps(data));PLIST.chmod(0o600)
    subprocess.run(['launchctl','enable',target],check=True,capture_output=True)
    for attempt in range(6):
        result=subprocess.run(['launchctl','bootstrap',domain,str(PLIST)],capture_output=True)
        if result.returncode==0:return
        if attempt==5:raise RuntimeError('launchd_bootstrap_failed')
        time.sleep(0.5)


def main():
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__);g=p.add_mutually_exclusive_group()
    for action in ('install','disable','uninstall','status','once','reconcile'):g.add_argument('--'+action,action='store_true')
    p.add_argument('--observe',action='store_true');p.add_argument('--platform',choices=['wechat','wecom']);a=p.parse_args()
    platforms=[a.platform] if a.platform else ['wechat','wecom']
    if a.status:print(json.dumps({v:bridge.read(v) for v in platforms},ensure_ascii=False,indent=2));return 0
    for action in ('install','disable','uninstall'):
        if getattr(a,action):manage(action,a.observe);return 0
    if a.reconcile:
        for platform in platforms:bridge.request(platform,force=True)
        print('已提交逐源表补漏与对账请求');return 0
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700);stop=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set());signal.signal(signal.SIGINT,lambda *_:stop.set())
    try:
        with task_lock(STATE/'daemon.lock'):
            runners=[Runner(platform,stop,a.observe,a.once) for platform in platforms]
            threads=[threading.Thread(target=r.run,name=r.platform) for r in runners]
            for thread in threads:thread.start()
            while any(t.is_alive() for t in threads):
                for runner,thread in zip(runners,threads):
                    if thread.is_alive():runner.publish()
                if stop.is_set():time.sleep(0.1)
                else:stop.wait(2)
            for thread in threads:thread.join()
            return int(any(r.state.get('error') for r in runners))
    except RuntimeError:
        if a.once:
            for platform in platforms:bridge.request(platform)
        print('实时服务已在运行');return 0

if __name__=='__main__':sys.exit(main())
