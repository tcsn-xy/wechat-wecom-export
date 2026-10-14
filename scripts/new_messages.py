#!/usr/bin/env python3
"""Dispatch platform-specific incremental arguments and aggregate failures."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]


def commands(args,python=sys.executable):
    common=[]
    if args.chat: common+=['--chat',args.chat]
    if args.no_snapshot: common+=['--no-snapshot']
    wx=[python,str(ROOT/'scripts/wx_recent.py')]+common
    wc=[python,str(ROOT/'scripts/wecom_recent.py')]+common+['--since',args.since]
    if args.merge:wc+=['--merge']
    if args.text_only:wx+=['--text-only']
    if args.with_media:wx+=['--with-media']
    return [('企业微信',wc),('微信',wx)]


def run_all(tasks,run=subprocess.run):
    results=[]
    for label,cmd in tasks:
        print('── '+label,flush=True)
        try: code=run(cmd).returncode
        except OSError as exc: print(str(exc));code=1
        results.append((label,code))
    print('；'.join('%s：%s'%(label,'成功' if not code else '失败/不可刷新 (%s)'%code) for label,code in results))
    return int(any(code for label,code in results))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--since',default='last')
    ap.add_argument('--chat',default='')
    ap.add_argument('--merge',action='store_true')
    ap.add_argument('--no-snapshot',action='store_true')
    ap.add_argument('--text-only',action='store_true')
    ap.add_argument('--with-media',action='store_true')
    args=ap.parse_args()
    return run_all(commands(args,os.environ.get('WXEXPORT_PYTHON') or sys.executable))

if __name__=='__main__':sys.exit(main())
