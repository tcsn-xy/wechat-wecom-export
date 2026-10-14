"""Consistent private DB/WAL copies and macOS change notifications."""
from __future__ import annotations
import ctypes
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import tempfile
import time
import uuid


class SnapshotChanged(RuntimeError):pass


def stamp(path):
    p=Path(path)
    try:
        s=p.stat()
        with p.open('rb') as f:header=f.read(32)
        return [str(s.st_dev),str(s.st_ino),str(s.st_size),str(s.st_mtime_ns),hashlib.sha256(header).hexdigest()]
    except FileNotFoundError:return None


def signature(path):
    return [stamp(path),stamp(str(path)+'-wal')]


def clone(src,dst):
    Path(dst).parent.mkdir(parents=True,exist_ok=True)
    if os.uname().sysname=='Darwin':
        libc=ctypes.CDLL(None,use_errno=True)
        if libc.clonefile(os.fsencode(src),os.fsencode(dst),0)==0:return
    shutil.copyfile(src,dst)


class SnapshotStore:
    def __init__(self,root,validate,*,copier=clone,remote_copy=None):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.validate=validate;self.copier=copier;self.remote_copy=remote_copy
        self.current={};self.retired=[]

    def capture(self,source,revision,expected=None):
        folder=Path(tempfile.mkdtemp(prefix='generation-',dir=self.root))
        target=folder/source.name
        try:
            if self.remote_copy:
                before,after=self.remote_copy(source,folder)
            else:
                before=signature(source)
                if before[0] is None:raise FileNotFoundError('source_missing')
                self.copier(source,target)
                if before[1] is not None:self.copier(Path(str(source)+'-wal'),Path(str(target)+'-wal'))
                after=signature(source)
            if before!=after:raise SnapshotChanged('source_changed_during_copy')
            checked=self.validate(target)
            entry=dict(path=Path(checked or target),raw=target,signature=after,revision=revision,folder=folder)
            old=self.current.get(str(source))
            self.current[str(source)]=entry
            if old:self.retired.append(old['folder'])
            return entry
        except BaseException:shutil.rmtree(folder,ignore_errors=True);raise

    def collect(self,retained=()):
        # Only our own obsolete generations, never original data or recovery backups.
        protected={Path(p) for p in retained}
        rest=[]
        for folder in self.retired:
            if folder in protected:rest.append(folder)
            else:shutil.rmtree(folder,ignore_errors=True)
        self.retired=rest


class Changes:
    """kqueue wakes the poller; periodic signatures remain the source of truth."""
    def __init__(self):
        self.queue=select.kqueue() if hasattr(select,'kqueue') else None
        self.fds={}
    def reset(self,paths):
        wanted=set()
        for p in paths:
            p=Path(p);wanted.add(str(p.parent))
            for name in [p,Path(str(p)+'-wal')]:
                if name.exists():wanted.add(str(name))
        for name in list(self.fds):
            try:replaced=os.fstat(self.fds[name]).st_ino!=os.stat(name).st_ino
            except OSError:replaced=True
            if name not in wanted or replaced:os.close(self.fds.pop(name))
        if not self.queue:return
        for name in wanted-self.fds.keys():
            fd=None
            try:
                fd=os.open(name,getattr(os,'O_EVTONLY',os.O_RDONLY))
                event=select.kevent(fd,filter=select.KQ_FILTER_VNODE,flags=select.KQ_EV_ADD|select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_WRITE|select.KQ_NOTE_EXTEND|select.KQ_NOTE_RENAME|select.KQ_NOTE_DELETE)
                self.queue.control([event],0,0);self.fds[name]=fd
            except OSError:
                if fd is not None:
                    try:os.close(fd)
                    except OSError:pass
    def wait(self,timeout=2):
        if self.queue:
            try:return bool(self.queue.control(None,128,timeout))
            except OSError:return False
        time.sleep(timeout);return False
    def close(self):
        for fd in self.fds.values():os.close(fd)
        if self.queue:self.queue.close()


class Debounce:
    def __init__(self,quiet=3,maximum=10):self.quiet=quiet;self.maximum=maximum;self.first=None;self.last=None
    def mark(self,now):
        if self.first is None:self.first=now
        self.last=now
    def due(self,now):return self.first is not None and (now-self.last>=self.quiet or now-self.first>=self.maximum)
    def clear(self):self.first=self.last=None
