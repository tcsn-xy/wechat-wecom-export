#!/usr/bin/env python3
"""Run WeChat and WeCom hourly ingestion sequentially with independent failures."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'lib'),str(ROOT/'scripts')]
from localio import atomic_json,task_lock
import wx_mysql_sync as wx

STATE=ROOT/'.runtime/chat-mysql-sync/status.json'


def configuration():
    p=wx.launch_config()
    p['ProgramArguments']=[str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/chat_mysql_sync.py')]
    return p


def run_all(run=subprocess.run,bridge_request=None):
    result={};started=wx.now()
    atomic_json(STATE,dict(last_attempt=started,status='running',platforms=result))
    from mysql_store import realtime_bridge
    bridge_request=bridge_request or (realtime_bridge.request if run is subprocess.run else lambda platform:False)
    for platform,script in [('wecom','wecom_mysql_sync.py'),('wechat','wx_mysql_sync.py'),('media','chat_media_sync.py')]:
        if platform!='media' and bridge_request(platform):
            result[platform]=dict(exit_code=0,status='reconcile_queued');continue
        try:code=run([sys.executable,str(ROOT/'scripts'/script)]).returncode
        except OSError:code=1
        result[platform]=dict(exit_code=code)
        atomic_json(STATE,dict(last_attempt=started,status='running',platforms=result))
    ok=all(r['exit_code']==0 for r in result.values())
    atomic_json(STATE,dict(last_attempt=started,finished_at=wx.now(),status=('reconcile_queued' if any(r.get('status')=='reconcile_queued' for r in result.values()) else 'success') if ok else 'partial_failure',platforms=result))
    return 0 if ok else 1


def main():
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group()
    for a in ('status','install','disable','uninstall'):group.add_argument('--'+a,action='store_true')
    args=p.parse_args()
    if args.status:
        from mysql_store import realtime_bridge
        for platform,path in [('wecom',ROOT/'.runtime/wecom-mysql-sync/status.json'),('wechat',wx.STATE)]:
            print(json.dumps(dict(platform=platform,**wx.read_state(path),realtime=realtime_bridge.read(platform)),ensure_ascii=False,indent=2))
        return 0
    for action in ('install','disable','uninstall'):
        if getattr(args,action):wx.manage(action,configuration() if action=='install' else None);return 0
    try:
        with task_lock(STATE.with_suffix('.lock')):return run_all()
    except RuntimeError:print('双平台入库正在运行，本次跳过');return 0


if __name__=='__main__':sys.exit(main())
