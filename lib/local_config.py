"""Local configuration shared by CLI and launchd; never contains shipped secrets."""
from __future__ import annotations
import json
import os
from pathlib import Path
import stat

ROOT = Path(__file__).resolve().parents[1]


def config_path():
    return Path(os.environ.get('WXEXPORT_CONFIG', ROOT / 'keys/local.json')).expanduser().resolve()


def read_private_json(path):
    path = Path(path)
    try:
        with path.open('rb') as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
                raise ValueError('Private configuration must be a regular file with mode 0600')
            value = json.load(stream)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError('Invalid private JSON configuration') from None
    if not isinstance(value, dict):
        raise ValueError('Private configuration must be a JSON object')
    return value


def load():
    path = config_path()
    if not path.exists():
        if 'WXEXPORT_CONFIG' in os.environ:
            raise ValueError('Configured private configuration file is missing')
        return {}
    return read_private_json(path)


def mysql_options():
    settings = load().get('mysql', {})
    if not isinstance(settings, dict):
        raise ValueError('Invalid MySQL configuration')
    secret = os.environ.get('WX_MYSQL_PASSWORD') or os.environ.get('MYSQL_PWD') or settings.get('password')
    if not isinstance(secret, str) or not secret:
        raise ValueError('MySQL password missing; run scripts/deploy.py configure-mysql')
    try:
        port = int(os.environ.get('WX_MYSQL_PORT', settings.get('port', 3306)))
    except (ValueError, TypeError):
        raise ValueError('Invalid MySQL port') from None
    if not 1 <= port <= 65535:
        raise ValueError('Invalid MySQL port')
    user = os.environ.get('WX_MYSQL_USER', settings.get('user', 'wxexport'))
    host = os.environ.get('WX_MYSQL_HOST', settings.get('host', '127.0.0.1'))
    if not isinstance(user, str) or not user or not isinstance(host, str) or not host:
        raise ValueError('Invalid MySQL host or user')
    return dict(user=user, password=secret, host=host, port=port)


def media_root():
    value = os.environ.get('CHAT_MEDIA_ROOT') or load().get('media_root') or ROOT / 'media_store'
    return Path(value).expanduser().resolve()


def launch_environment():
    # Credentials stay in a private file, not plist or process arguments.
    result = {'PYTHONUNBUFFERED': '1'}
    if 'WXEXPORT_CONFIG' in os.environ:
        result['WXEXPORT_CONFIG'] = str(config_path())
    if 'CHAT_MEDIA_ROOT' in os.environ:
        result['CHAT_MEDIA_ROOT'] = str(media_root())
    return result
