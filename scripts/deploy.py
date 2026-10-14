#!/usr/bin/env python3
"""Install dependencies and configure a local deployment without starting services."""
from __future__ import annotations
import argparse
import getpass
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'lib'), str(ROOT / 'scripts')]
from local_config import config_path, load, mysql_options, read_private_json


def private_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print('Private configuration created; secrets omitted')


def configure_mysql(args):
    path = config_path()
    if path.exists():
        raise ValueError('Configuration already exists; edit the private file explicitly')
    host = input('MySQL host [127.0.0.1]: ').strip() or '127.0.0.1'
    user = input('MySQL user [wxexport]: ').strip() or 'wxexport'
    port = int(input('MySQL port [3306]: ').strip() or '3306')
    if not 1 <= port <= 65535:
        raise ValueError('Invalid MySQL port')
    password = getpass.getpass('MySQL password (hidden): ')
    if not password:
        raise ValueError('Password must not be empty')
    config = dict(mysql=dict(host=host, user=user, port=port, password=password))
    if args.media_root:
        config['media_root'] = str(Path(args.media_root).expanduser().resolve())
    private_write(path, config)


def configure_caption(args):
    path = ROOT / 'keys/minimax.json'
    if path.exists():
        raise ValueError('Caption configuration already exists; edit the private file explicitly')
    api_key = getpass.getpass('MiniMax Coding Plan API key (hidden): ')
    if not api_key:
        raise ValueError('API key must not be empty')
    private_write(path, dict(host=args.host, api_key=api_key, enabled=args.allow_image_upload,
                            image_external_consent=args.allow_image_upload))


def build_environment():
    env = os.environ.copy()
    if sys.platform == 'darwin' and shutil.which('brew'):
        for formula in ('sqlcipher', 'openssl@3'):
            result = subprocess.run(['brew', '--prefix', formula], capture_output=True, text=True)
            if result.returncode:
                continue
            prefix = result.stdout.strip()
            env['CFLAGS'] = env.get('CFLAGS', '') + ' -I' + prefix + '/include'
            env['LDFLAGS'] = env.get('LDFLAGS', '') + ' -L' + prefix + '/lib'
            env['PKG_CONFIG_PATH'] = prefix + '/lib/pkgconfig:' + env.get('PKG_CONFIG_PATH', '')
    return env


def dependencies(_args):
    interpreter = ROOT / '.venv/bin/python'
    if not interpreter.exists():
        raise ValueError('Run bootstrap with Python 3.11 or newer first')
    subprocess.run([str(interpreter), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')],
                   env=build_environment(), check=True)


def bootstrap(args):
    if sys.version_info < (3, 11):
        raise ValueError('Python 3.11 or newer is required')
    destination = ROOT / '.venv'
    if destination.exists() and not (destination / 'bin/python').exists():
        raise ValueError('Existing .venv is incomplete; inspect before replacing it')
    if not destination.exists():
        venv.EnvBuilder(with_pip=True).create(destination)
    if not args.skip_dependencies:
        dependencies(args)
    print('Local environment ready; services remain uninstalled')


def check(args):
    checks = {'python': sys.version_info >= (3, 11), 'macos': sys.platform == 'darwin'}
    for module in ('sqlcipher3', 'zstandard', 'Crypto', 'PIL', 'av', 'pymysql', 'pysilk'):
        checks[module] = importlib.util.find_spec(module) is not None
    for command in ('clang', 'lldb', 'codesign', 'launchctl'):
        checks[command] = shutil.which(command) is not None
    if args.mysql:
        try:
            from mysql_store import conn
            with conn.connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT 1')
                    checks['mysql'] = cursor.fetchone() is not None
        except Exception as exc:
            checks['mysql'] = False
            checks['mysql_error_category'] = type(exc).__name__
    if args.caption:
        try:
            from mysql_store.captions import load_config
            load_config()
            checks['caption'] = True
        except Exception as exc:
            checks['caption'] = False
            checks['caption_error_category'] = type(exc).__name__
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return int(any(value is False for value in checks.values()))


def initialize_databases(_args):
    from mysql_store import conn, schema
    from wx_mysql_sync import prepare_text_schema
    with conn.connect() as connection:
        for database in ('wx_chat', 'wecom_chat'):
            present = conn.scalar(connection, 'SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s', (database,))
            if present:
                print(database + ': existing database preserved; importers handle additive upgrades')
            else:
                # New empty database only: no generic legacy media merge migration.
                prepare_text_schema(connection, database)
                print(database + ': initialized')


def service(args):
    scripts = {'hourly': 'chat_mysql_sync.py', 'realtime': 'chat_watch_sync.py', 'caption': 'image_caption_worker.py'}
    labels = {'hourly': 'local.wxexport.mysql-sync', 'realtime': 'local.wxexport.realtime-sync', 'caption': 'local.wxexport.image-caption'}
    if sys.platform != 'darwin':
        raise ValueError('launchd services require macOS')
    plist = Path.home() / 'Library/LaunchAgents' / (labels[args.name] + '.plist')
    if plist.exists():
        existing = plistlib.loads(plist.read_bytes())
        if Path(existing.get('WorkingDirectory', '')).resolve() != ROOT:
            raise ValueError('Service belongs to another deployment; manage it from that deployment first')
    if args.action == 'install':
        settings = load()
        if not settings.get('mysql', {}).get('password'):
            raise ValueError('Background installation requires a persistent private MySQL configuration')
        mysql_options()
        if args.name == 'caption':
            if not args.allow_image_upload:
                raise ValueError('Caption installation requires --allow-image-upload')
            from mysql_store.captions import load_config
            load_config()
    command = [str(ROOT / '.venv/bin/python'), str(ROOT / 'scripts' / scripts[args.name]), '--' + args.action]
    if args.name == 'realtime' and args.action == 'install' and args.observe:
        command.append('--observe')
    if args.name == 'caption' and args.action == 'install':
        command += ['--concurrency', str(args.concurrency)]
    return subprocess.run(command).returncode


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('bootstrap');p.add_argument('--skip-dependencies', action='store_true');p.set_defaults(func=bootstrap)
    p = commands.add_parser('dependencies');p.set_defaults(func=dependencies)
    p = commands.add_parser('configure-mysql');p.add_argument('--media-root');p.set_defaults(func=configure_mysql)
    p = commands.add_parser('configure-caption');p.add_argument('--host', choices=['https://api.minimaxi.com','https://api.minimax.cn','https://api.minimax.io'], default='https://api.minimaxi.com');p.add_argument('--allow-image-upload', action='store_true');p.set_defaults(func=configure_caption)
    p = commands.add_parser('check');p.add_argument('--mysql', action='store_true');p.add_argument('--caption', action='store_true');p.set_defaults(func=check)
    p = commands.add_parser('initialize-databases');p.set_defaults(func=initialize_databases)
    p = commands.add_parser('service');p.add_argument('name', choices=['hourly','realtime','caption']);p.add_argument('action', choices=['install','disable','uninstall','status']);p.add_argument('--observe', action='store_true');p.add_argument('--allow-image-upload', action='store_true');p.add_argument('--concurrency', type=int, choices=range(1,65), default=4);p.set_defaults(func=service)
    args = parser.parse_args()
    try:
        return args.func(args) or 0
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        # Do not print exception bodies from drivers, subprocesses or private configs.
        if isinstance(exc, ValueError):print(str(exc), file=sys.stderr)
        else:print('Deployment failed: ' + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
