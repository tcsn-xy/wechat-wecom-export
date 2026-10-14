#!/usr/bin/env python3
"""Hourly local WeChat → MySQL text/raw/link sync, including restored history."""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'lib'), str(ROOT / 'scripts')]
from localio import atomic_json, task_lock
from mysql_store import conn as my, schema, wx_import
from wxexport import config, export, keys
from wx_recent import running_wechats

LABEL = 'local.wxexport.mysql-sync'
STATE = ROOT / '.runtime/wx-mysql-sync/status.json'
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')


def now():
    return dt.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def read_state(path):
    return json.loads(Path(path).read_text()) if Path(path).exists() else {}


class SyncError(RuntimeError):
    """Only fixed, non-sensitive messages may escape into status/logs."""


def child(command, timeout=1200):
    # Neither debugger output nor import exceptions belong in scheduler logs.
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SyncError('快照子任务超时；本轮停止，下轮重试') from None
    if result.returncode:
        raise SyncError('快照刷新或验证失败；旧快照保留，下轮重试')


def refresh():
    procs = running_wechats()
    if procs:
        expected = str(ROOT / '.runtime/WeChatRun.app/Contents/MacOS/WeChat')
        if len(procs) != 1 or procs[0][1] != expected:
            raise SyncError('当前运行原版或多个微信进程，无法自动刷新；请使用项目副本')
        child(['sudo', '-n', sys.executable, str(ROOT / 'scripts/clone_data.py'),
               '--db-only'])
    else:
        child([sys.executable, str(ROOT / 'scripts/clone_data_local.py'), '--no-media'])


def prepare_text_schema(c, database):
    """Add only text-sync requirements; never run legacy media deduplication."""
    with c.cursor() as cur:
        cur.execute("CREATE DATABASE IF NOT EXISTS `%s` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci" % database)
        cur.execute("USE `%s`" % database)
        for ddl in schema.TABLES:
            cur.execute(ddl)
        columns = {r['Field'] for r in my.query(c, 'SHOW COLUMNS FROM message_raw')}
        for name in ('raw_blob','extra_blob'):
            if name not in columns:
                cur.execute('ALTER TABLE message_raw ADD COLUMN `%s` LONGBLOB' % name)
        if not my.query(c, "SHOW COLUMNS FROM messages LIKE 'source_identity'"):
            cur.execute("ALTER TABLE messages ADD COLUMN source_identity CHAR(64) CHARACTER SET ascii "
                "GENERATED ALWAYS AS (SHA2(CAST(JSON_ARRAY(platform,account_key,chat_id,source_dataset,source_db,source_table,local_id) AS CHAR),256)) STORED, "
                "DROP INDEX uk_msg, ADD UNIQUE KEY uk_msg (source_identity)")
        index = my.query(c, "SHOW INDEX FROM contacts WHERE Key_name='uk_contact'")
        if any(r['Sub_part'] is not None for r in index):
            cur.execute('ALTER TABLE contacts DROP INDEX uk_contact, ADD UNIQUE KEY uk_contact (platform,contact_id,account_key)')
        # Older _save_source incorrectly shifted source fields one position.
        legacy = my.query(c, 'SELECT id,dataset,source_db,source_table FROM import_sources WHERE platform=%s AND chat_id=%s',
                          ('wechat','wechat-snapshot'))
        for row in legacy:
            if re.fullmatch(r'(?:biz_)?message_\d+\.db',row['dataset']) and re.fullmatch(r'Msg_[a-fA-F0-9]{32}',row['source_db']):
                cur.execute('UPDATE import_sources SET dataset=%s,source_db=%s,source_table=%s,chat_id=%s WHERE id=%s',
                            ('wechat-snapshot',row['dataset'],row['source_db'],row['source_table'],row['id']))
        cur.execute("CREATE TABLE IF NOT EXISTS import_runs (id BIGINT AUTO_INCREMENT PRIMARY KEY, "
            "platform VARCHAR(16) NOT NULL, scope JSON NOT NULL, media_enabled BOOL NOT NULL, "
            "status VARCHAR(16) NOT NULL, detail TEXT, started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.commit()


def totals(c, account):
    return my.query(c, 'SELECT COUNT(*) AS messages,MIN(ts) AS earliest_ts,MAX(ts) AS latest_ts '
                    'FROM messages WHERE platform=%s AND account_key=%s',
                    ('wechat', account))[0]


def audit(c, paths, chats):
    """Compare actual source identities, raw bytes and link coverage; retain DB extras."""
    kr = keys.Keyring(paths.capture)
    source_rows = missing = raw_mismatch = missing_links = 0
    sources = []
    chat_pks = {r['chat_id']:r['chat_pk'] for r in my.query(c,
        'SELECT chat_id,chat_pk FROM chats WHERE platform=%s AND account_key=%s',
        ('wechat', paths.wxid))}
    for chat in chats:
        for db, table in chat['tables']:
            sqlite = kr.connect(db)
            try:
                dest = my.query(c, 'SELECT m.local_id,r.message_pk,r.raw_blob FROM messages m '
                    'LEFT JOIN message_raw r ON r.message_pk=m.id '
                    'WHERE m.chat_pk=%s AND m.platform=%s AND m.account_key=%s AND m.chat_id=%s '
                    'AND m.source_dataset=%s AND m.source_db=%s AND m.source_table=%s',
                    (chat_pks.get(chat['chat_id'],0), 'wechat', paths.wxid, chat['chat_id'], 'wechat-snapshot', Path(db).name, table))
                by_id = {int(r['local_id']): (r['message_pk'], r['raw_blob']) for r in dest}
                n = absent = bad_raw = 0
                cursor = sqlite.execute('SELECT local_id,message_content FROM "%s"' % table)
                for lid, raw in cursor:
                    n += 1
                    if int(lid) not in by_id:
                        absent += 1
                    else:
                        original = bytes(raw) if isinstance(raw, (bytes, bytearray)) else (str(raw).encode() if raw is not None else None)
                        if by_id[int(lid)][0] is None or by_id[int(lid)][1] != original:
                            bad_raw += 1
                source_rows += n; missing += absent; raw_mismatch += bad_raw
                sources.append(dict(chat_id=chat['chat_id'], source_db=Path(db).name,
                                    source_table=table, source_rows=n,
                                    retained_rows=len(dest)-n+absent, missing=absent, raw_mismatch=bad_raw))
            finally:
                sqlite.close()
    missing_links = my.scalar(c, 'SELECT COUNT(*) FROM messages m WHERE platform=%s '
        'AND account_key=%s AND has_link=1 AND NOT EXISTS '
        '(SELECT 1 FROM message_links l WHERE l.message_pk=m.id)', ('wechat', paths.wxid))
    duplicates = my.scalar(c, 'SELECT COUNT(*) FROM (SELECT chat_id,server_id '
        'FROM messages WHERE platform=%s AND account_key=%s AND server_id>0 '
        'GROUP BY chat_id,server_id HAVING COUNT(DISTINCT source_identity)>1) d',
        ('wechat', paths.wxid))
    return dict(source_rows=source_rows, missing_messages=missing, raw_mismatch=raw_mismatch,
                missing_links=missing_links, suspected_duplicate_groups=duplicates, sources=sources,
                ok=not (missing or raw_mismatch or missing_links))


def sync(args):
    from mysql_store import realtime_bridge
    if args.database=='wx_chat' and Path(args.state).resolve()==STATE.resolve() and realtime_bridge.request('wechat'):
        print('实时服务已接收补漏请求；通过 chat_watch_sync.py --status 查看完成状态')
        return 0

    state_path = Path(args.state)
    started = time.monotonic()
    try:
        with task_lock(state_path.with_suffix('.lock')):
            for log in (state_path.parent / 'launchd.log', state_path.parent / 'launchd-error.log'):
                if log.exists() and log.stat().st_size > 4 * 1024 * 1024:
                    with log.open('w'):
                        pass
            state = read_state(state_path)
            previous_success = state.get('last_success')
            for field in ('before','after','added_rows','source_rows','suspected_duplicate_groups','elapsed_seconds','mysql_errno'):
                state.pop(field, None)
            if not state.get('account_key') and not args.snapshot_root:
                state['account_key'] = config.load().wxid
            state.update(last_attempt=now(), last_success=previous_success,
                         status='running', stage='snapshot', error=None)
            atomic_json(state_path, state)
            try:
                if not args.no_snapshot:
                    refresh()
                paths = config.load(require_snapshot=True, snapshot_root=args.snapshot_root,
                                    capture=args.capture)
                if not paths.wxid or paths.wxid == 'unknown':
                    raise SyncError('快照账号身份缺失，拒绝同步')
                expected = state.get('account_key')
                if expected and expected != paths.wxid:
                    raise SyncError('快照账号与后台绑定账号不同，拒绝同步')
                state.update(account_key=paths.wxid, snapshot_time=paths.cloned_at,
                             stage='database')
                atomic_json(state_path, state)
                # Readers and manual importers share this lock throughout enumeration/import/audit.
                with task_lock(Path(paths.data) / '.snapshot.lock'):
                    c = my.connect()
                    try:
                        schema_lock = 'chat-import:' + args.database
                        if my.scalar(c, 'SELECT GET_LOCK(%s,0)', (schema_lock,)) != 1:
                            raise SyncError('目标库正在入库，下轮重试')
                        try:
                            prepare_text_schema(c, args.database)
                        finally:
                            my.execute(c, 'SELECT RELEASE_LOCK(%s)', (schema_lock,))
                    finally:
                        c.close()
                    c = my.connect(args.database)
                    try:
                        # Hold the same database advisory lock as manual importers.
                        lock = 'chat-import:' + args.database
                        if my.scalar(c, 'SELECT GET_LOCK(%s,0)', (lock,)) != 1:
                            raise SyncError('目标库正在入库，下轮重试')
                        try:
                            before = totals(c, paths.wxid)
                            media_before = my.scalar(c, 'SELECT COUNT(*) FROM media_items')
                            c.commit()
                            chats = export.Exporter(paths, with_media=False, verbose=False).enumerate_chats()
                            state.update(stage='import', before=before)
                            atomic_json(state_path, state)
                            # Reuse importer on the connection owning the advisory lock.
                            imp = wx_import.WxImporter(paths, database=args.database,
                                                       skip_media=True, verbose=False)
                            imp.db.close(); imp.db = c
                            try:
                                with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                                    imp.run()
                                # MySQL GET_LOCK is recursive: import_session releases only
                                # its own acquisition; our outer lock remains through audit.
                            finally:
                                if imp.voice_conn is not None:
                                    imp.voice_conn.close()
                            state['stage'] = 'audit'
                            atomic_json(state_path, state)
                            result = audit(c, paths, chats)
                            after = totals(c, paths.wxid)
                            result['media_rows_before'] = media_before
                            result['media_rows_after'] = my.scalar(c, 'SELECT COUNT(*) FROM media_items')
                            result['ok'] = result['ok'] and result['media_rows_after'] == media_before
                            atomic_json(state_path.with_name('audit.json'), dict(at=now(), account_key=paths.wxid, before=before, after=after, **result))
                            state.update(after=after, added_rows=after['messages']-before['messages'],
                                         source_rows=result['source_rows'],
                                         suspected_duplicate_groups=result['suspected_duplicate_groups'])
                            if not result['ok']:
                                raise SyncError('来源、原文或链接对账未通过，查看本机 audit.json')
                        finally:
                            my.execute(c, 'SELECT RELEASE_LOCK(%s)', (lock,))
                    finally:
                        c.close()
                state.update(status='success', last_success=now(), stage='complete')
                code = 0
            except (Exception, SystemExit) as exc:
                errno = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
                state.update(status='failed', mysql_errno=errno,
                             error=str(exc) if isinstance(exc, SyncError)
                             else '阶段 %s 失败（%s%s）；下轮重试' %
                             (state['stage'], type(exc).__name__, '，错误码 %s' % errno if errno else ''))
                code = 1
            state['elapsed_seconds'] = round(time.monotonic()-started, 1)
            atomic_json(state_path, state)
            print(json.dumps(state, ensure_ascii=False))
            return code
    except RuntimeError:
        print('同步任务已在运行，本次跳过')
        return 0


def launch_config():
    logs = ROOT / '.runtime/wx-mysql-sync'
    return dict(Label=LABEL, ProgramArguments=[str(ROOT / '.venv/bin/python'),
        str(ROOT / 'scripts/wx_mysql_sync.py')], WorkingDirectory=str(ROOT),
        RunAtLoad=True, StartInterval=3600, ProcessType='Background',
        StandardOutPath=str(logs / 'launchd.log'), StandardErrorPath=str(logs / 'launchd-error.log'),
        Umask=0o077, EnvironmentVariables=__import__('local_config').launch_environment())


def manage(action, configuration=None):
    domain = 'gui/%s' % os.getuid()
    target = domain + '/' + LABEL
    if action in ('disable', 'uninstall', 'install'):
        # Missing services are normal on first install; other errors are checked below.
        present = subprocess.run(['launchctl', 'print', target], capture_output=True).returncode == 0
        if present:
            subprocess.run(['launchctl', 'bootout', target], check=True)
    if action == 'install':
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        STATE.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_bytes(plistlib.dumps(configuration or launch_config()))
        PLIST.chmod(0o600)
        subprocess.run(['launchctl', 'enable', target], check=True)
        subprocess.run(['launchctl', 'bootstrap', domain, str(PLIST)], check=True)
    elif action == 'disable':
        subprocess.run(['launchctl', 'disable', target], check=True)
    elif action == 'uninstall':
        PLIST.unlink(missing_ok=True)
    print('后台任务：' + action)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--disable', action='store_true')
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--no-snapshot', action='store_true', help='只同步已有快照，供离线补导')
    parser.add_argument('--snapshot-root')
    parser.add_argument('--capture')
    parser.add_argument('--database', default='wx_chat')
    parser.add_argument('--state', default=str(STATE))
    args = parser.parse_args()
    args.database = my.database_name(args.database)
    actions = [name for name in ('status','install','disable','uninstall') if getattr(args,name)]
    if len(actions)>1:
        parser.error('管理选项只能指定一个')
    if args.snapshot_root and not args.no_snapshot:
        parser.error('--snapshot-root 必须同时指定 --no-snapshot')
    if actions:
        if actions[0]=='status':
            print(json.dumps(read_state(args.state), ensure_ascii=False, indent=2))
            subprocess.run(['launchctl','print','gui/%s/%s' % (os.getuid(),LABEL)])
        else:
            if args.database!='wx_chat' or args.snapshot_root or args.capture or args.state!=str(STATE):
                parser.error('后台安装仅支持默认正式账号配置；自定义参数用于手动验收')
            manage(actions[0])
        return 0
    return sync(args)


if __name__ == '__main__':
    sys.exit(main())
