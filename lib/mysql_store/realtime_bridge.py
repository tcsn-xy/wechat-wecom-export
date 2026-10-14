"""Hourly/manual entrypoints hand reconciliation to a healthy realtime owner."""
import datetime as dt
import json
import os
from pathlib import Path
import time
import uuid
from localio import atomic_json
ROOT=Path(__file__).resolve().parents[2]
STATE=ROOT/'.runtime/chat-watch-sync'


def read(platform):
    try:return json.loads((STATE/(platform+'.json')).read_text())
    except (OSError,ValueError):return {}


def active(platform):
    if (STATE/'disabled').exists():return False
    s=read(platform)
    return bool(s.get('enabled') and s.get('ready') and s.get('mode') in ('direct','inspector') and time.time()-s.get('heartbeat_unix',0)<20)


def request(platform,force=False):
    if not force and not active(platform):
        prior=read(platform)
        # Give a live daemon one heartbeat to recover after machine sleep.
        if prior.get('enabled') and prior.get('mode') in ('direct','inspector') and not (STATE/'disabled').exists():
            try:os.kill(prior.get('pid',0),0)
            except (OSError,TypeError):return False
            deadline=time.monotonic()+3
            while not active(platform) and time.monotonic()<deadline:time.sleep(.2)
        if not active(platform):return False
    atomic_json(STATE/(platform+'.request.json'),dict(token=uuid.uuid4().hex,at=time.time()))
    return True
