#!/usr/bin/env python3
"""Reproducible end-to-end validation using synthetic data and disposable MySQL DBs."""
from pathlib import Path
import argparse
import json
import subprocess
import sys
import tempfile
import uuid
import os
import datetime

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tests'),str(ROOT/'lib'),str(ROOT/'scripts')]
from fixtures import wx_fixture,wecom_fixture,media_fixture
from mysql_store import conn,schema
from mysql_report import build_report


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out',default=str(ROOT/'.runtime/validation'))
    ap.add_argument('--report',default=str(ROOT/'docs/隔离验收报告.md'))
    args=ap.parse_args()
    base=Path(args.out);base.mkdir(parents=True,exist_ok=True)
    root=Path(tempfile.mkdtemp(prefix='synthetic-',dir=base))
    paths,tables=wx_fixture(root);raw,plain,ds=wecom_fixture(root)
    media_fixture(paths,tables,raw,ds)
    token=uuid.uuid4().hex[:12]
    wx='repairtest_wx_'+token;wc='repairtest_wc_'+token
    databases=[wx,wc]
    log=[]
    def run(script,*flags):
        command=[sys.executable,'-B',str(ROOT/'scripts'/script),*map(str,flags)]
        result=subprocess.run(command,capture_output=True,text=True)
        log.append({'script':script,'code':result.returncode,'output':result.stdout+result.stderr})
        if result.returncode:raise RuntimeError(script+': '+(result.stderr or result.stdout)[-1600:])
        return result.stdout
    try:
        run('export_all.py','--snapshot-root',paths.data,'--capture',paths.capture,'--out',paths.export,'--force','--quiet')
        run('doctor.py','--snapshot-root',paths.data,'--capture',paths.capture,'--out',paths.export,'--links')
        results=json.loads(run('wccli_search.py','compressed','--source','auto','--snapshot-root',paths.data,'--capture',paths.capture))
        assert results['count']==1 and results['source']=='full'
        run('wecom_export.py','--raw',raw,'--plain',plain,'--out',root/'wecom-out')
        # Version-1 checkpoint migration replays every available ID once.
        progress=root/'checkpoint.json';progress.write_text(json.dumps({'last_send_time':9999999999}))
        run('wecom_recent.py','--raw',raw,'--plain',plain,'--out',root/'wecom-recent',
            '--watermark',progress,'--no-snapshot','--merge')
        before=progress.read_bytes()
        run('wecom_recent.py','--raw',raw,'--plain',plain,'--out',root/'wecom-recent',
            '--watermark',progress,'--no-snapshot','--merge')
        assert progress.read_bytes()==before
        for script,database,flags in [
            ('mysql_import_wx.py',wx,['--snapshot-root',paths.data,'--capture',paths.capture]),
            ('mysql_import_wecom.py',wc,['--raw',raw,'--plain',plain])]:
            run(script,'--database',database,'--rebuild','--quiet',*flags)
            run(script,'--database',database,'--quiet',*flags)
        text,result=build_report(wx_database=wx,wecom_database=wc,snapshot_root=paths.data,capture=paths.capture,
                                 wecom_plain=str(plain),wecom_raw=str(raw))
        if not result['ok']:raise RuntimeError('隔离报告不通过：'+repr(result['checks']))
        root.joinpath('mysql-report.md').write_text(text,encoding='utf-8')
        summary='''# 隔离修复验收报告

本报告仅使用程序生成的虚构账号、消息和媒体；没有重建正式库、刷新正式快照或重导正式记录。

验证包含：微信全量导出与不变量、资源引用、压缩消息全量搜索回退、企微 HTML 与媒体浏览、旧进度迁移及重复合并，以及两个临时 MySQL 库重复入库。

临时库在检查后删除；合成输入、导出和命令日志保留在 `%s`，便于复查。正式数据修复和真实全量验收仍待执行。

'''%root
        Path(args.report).write_text(summary+text,encoding='utf-8')
        print(json.dumps(dict(output=str(root),report=args.report,passed=result['passed'],failed=result['failed'],skipped=result['skipped']),ensure_ascii=False))
    finally:
        root.joinpath('commands.json').write_text(json.dumps(log,ensure_ascii=False,indent=2),encoding='utf-8')
        c=conn.connect()
        try:
            for name in databases:
                if not name.startswith('repairtest_'):raise ValueError('Unsafe cleanup target')
                schema.drop_db(c,name)
        finally:c.close()


if __name__=='__main__':main()
