"""Local output transactions; contains no platform-specific database logic."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading

_locks = threading.local()


def _owner(fd):
    if os.geteuid()==0 and os.environ.get("SUDO_UID","").isdigit():
        os.fchown(fd,int(os.environ["SUDO_UID"]),int(os.environ.get("SUDO_GID",-1)))


def _parent(path):
    missing=[];current=Path(path)
    while not current.exists():
        missing.append(current);current=current.parent
    Path(path).mkdir(parents=True,exist_ok=True)
    if os.geteuid()==0 and os.environ.get("SUDO_UID","").isdigit():
        for directory in missing:
            os.chown(directory,int(os.environ["SUDO_UID"]),int(os.environ.get("SUDO_GID",-1)))


def identity(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def atomic_json(path, data):
    path = Path(path)
    _parent(path.parent)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        _owner(fd)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def task_lock(path):
    path = Path(path).absolute()
    active = getattr(_locks,"active",{})
    if str(path) in active:
        yield
        return
    _parent(path.parent)
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
    _owner(fd)
    with os.fdopen(fd,"a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("另一个任务正在使用：%s" % path) from exc
        try:
            active[str(path)] = fh
            _locks.active = active
            yield
        finally:
            active.pop(str(path),None)
            fcntl.flock(fh, fcntl.LOCK_UN)


def _remove(path):
    path = Path(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def recover(journal):
    """Rollback an interrupted switch; a completed switch retains its backup."""
    journal = Path(journal)
    if not journal.exists():
        return
    state = json.loads(journal.read_text(encoding="utf-8"))
    if not state.get("committed"):
        for op in reversed(state["operations"]):
            target, backup = Path(op["target"]), Path(op["backup"])
            if backup.exists():
                _remove(target)
                os.replace(backup, target)
            elif not op["existed"]:
                _remove(target)
    journal.unlink()


def publish_many(mapping, journal):
    """Switch validated staging outputs while preserving the previous generation.

    Call under task_lock. Readers use the same lock. Never remove active data
    until all staging outputs exist; recovery restores the complete old set.
    """
    recover(journal)
    ops = []
    for target, stage in mapping.items():
        target, stage = Path(target).absolute(), Path(stage).absolute()
        if not stage.exists():
            raise FileNotFoundError(stage)
        backup = target.with_name(target.name + ".previous")
        _remove(backup)
        ops.append(dict(target=str(target), stage=str(stage), backup=str(backup),
                        existed=target.exists()))
    state = dict(committed=False, operations=ops)
    atomic_json(journal, state)
    try:
        for op in ops:
            target = Path(op["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            if op["existed"]:
                os.replace(target, op["backup"])
            os.replace(op["stage"], target)
        state["committed"] = True
        atomic_json(journal, state)
        recover(journal)
    except BaseException:
        recover(journal)
        raise


@contextlib.contextmanager
def staged_directory(target):
    target = Path(target).absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    journal = target.with_name("." + target.name + ".switch.json")
    recover(journal)
    tmp = Path(tempfile.mkdtemp(prefix="." + target.name + "-", dir=target.parent))
    try:
        yield str(tmp)
        publish_many({target: tmp}, journal)
    finally:
        _remove(tmp)


def iter_rows(cursor, size=2000):
    while True:
        rows = cursor.fetchmany(size)
        if not rows:
            return
        yield from rows
