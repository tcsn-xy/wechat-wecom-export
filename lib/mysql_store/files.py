"""Verified content-addressed media files; additive legacy-safe MySQL upgrade."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import re
import tempfile
from . import conn as my

ROOT = Path(__file__).resolve().parents[2]
KINDS = ('image', 'voice')


def root():
    from local_config import media_root
    return media_root()


def resolve(relative, base=None):
    base = Path(base or root()).resolve()
    target = (base/relative).resolve()
    if not relative or Path(relative).is_absolute() or not target.is_relative_to(base):
        raise ValueError('无效媒体相对路径')
    return target


def hashes(path):
    hs = {n: hashlib.new(n) for n in ('sha256', 'sha1', 'md5')}; size = 0
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1024*1024), b''):
            size += len(block)
            for h in hs.values(): h.update(block)
    return dict(bytes=size, **{k: h.hexdigest() for k,h in hs.items()})


def publish(chunks, platform, account, ext='bin', base=None):
    base = Path(base or root()).resolve()
    if platform not in ('wechat','wecom'): raise ValueError('无效平台')
    # Hash the namespace rather than trusting an account string as a directory.
    folder = base/platform/hashlib.sha256(account.encode()).hexdigest()[:24]
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.incoming-', dir=folder)
    try:
        with os.fdopen(fd,'wb') as fh:
            for block in chunks: fh.write(block)
            fh.flush(); os.fsync(fh.fileno())
        info = hashes(name)
        if not info['bytes']: raise ValueError('空媒体文件')
        ext = ext.lower().lstrip('.')
        if not re.fullmatch('[a-z0-9]{1,12}',ext): ext='bin'
        target = folder/info['sha256'][:2]/(info['sha256']+'.'+ext)
        target.parent.mkdir(exist_ok=True, mode=0o700)
        if target.exists():
            if hashes(target) != info: raise ValueError('已存媒体校验失败')
        else:
            os.replace(name,target)
            dfd=os.open(target.parent,os.O_RDONLY)
            try:os.fsync(dfd)
            finally:os.close(dfd)
        if hashes(target) != info: raise ValueError('发布后媒体校验失败')
        info['path']=str(target.relative_to(base));return info
    finally:
        if os.path.exists(name): os.unlink(name)


def prepare(c):
    """Never call schema.migrate: that can delete legacy media rows."""
    cols={r['Field'] for r in my.query(c,'SHOW COLUMNS FROM media_items')}
    additions={'account_key':"VARCHAR(191) NOT NULL DEFAULT ''",'file_path':'VARCHAR(1024)',
        'file_sha256':'CHAR(64)', 'decoded_path':'VARCHAR(1024)', 'decoded_sha256':'CHAR(64)',
        'content_md5':'CHAR(32)', 'decoded_sha1':'CHAR(40)', 'file_migrated_at':'DATETIME'}
    for name,typ in additions.items():
        if name not in cols: my.execute(c,'ALTER TABLE media_items ADD COLUMN `%s` %s'%(name,typ))
    if my.scalar(c,"SELECT COUNT(*) FROM media_items WHERE account_key='' AND platform='wecom'"):
        from wecom.accounts import profile_accounts
        from wecom import config as wc
        for profile,account in profile_accounts(wc.RAW).items():
            prefix=str(Path(wc.RAW)/'Profiles'/profile/'Caches')+'/'
            my.execute(c,"UPDATE media_items SET account_key=%s WHERE platform='wecom' AND account_key='' AND LEFT(source_path,%s)=%s",(account,len(prefix),prefix))
    # Do not rewrite legacy identities or add their unique key.
    my.execute(c,"UPDATE media_items i JOIN messages m ON m.id=i.message_pk SET i.account_key=m.account_key WHERE i.account_key='' AND i.platform=m.platform")
    my.execute(c,"""CREATE TABLE IF NOT EXISTS image_captions (
      id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY, platform VARCHAR(16) NOT NULL,
      account_key VARCHAR(191) NOT NULL, image_sha256 CHAR(64) NOT NULL,
      prompt_version VARCHAR(32) NOT NULL, provider VARCHAR(32) NOT NULL DEFAULT 'minimax',
      endpoint VARCHAR(255) NOT NULL, caption TEXT, image_text MEDIUMTEXT,
      status VARCHAR(24) NOT NULL DEFAULT 'pending', attempts INT NOT NULL DEFAULT 0,
      error_code VARCHAR(64), next_attempt DATETIME, completed_at DATETIME,
      UNIQUE KEY uk_caption(platform,account_key,image_sha256,prompt_version),
      KEY idx_queue(status,next_attempt)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    c.commit()


def chunks(c, row, decoded=False):
    col='decoded_data' if decoded else 'data';table='media_items.decoded_data' if decoded else 'media_items'
    if row.get(col) is not None:
        yield bytes(row[col]);return
    parts=my.query(c,'SELECT part_no FROM blob_parts WHERE table_name=%s AND row_id=%s ORDER BY part_no',(table,row['id']))
    for i,part in enumerate(parts):
        if part['part_no'] != i: raise ValueError('分片编号不连续')
        yield bytes(my.scalar(c,'SELECT data FROM blob_parts WHERE table_name=%s AND row_id=%s AND part_no=%s',(table,row['id'],i)))


def read(c, pk, decoded=False, base=None):
    row=my.query(c,'SELECT * FROM media_items WHERE id=%s',(pk,))[0]
    path=row.get('decoded_path' if decoded else 'file_path')
    digest=row.get('decoded_sha256' if decoded else 'file_sha256')
    if path:
        target=resolve(path,base)
        if hashes(target)['sha256'] != digest: raise ValueError('媒体文件校验失败')
        return target.read_bytes()
    return b''.join(chunks(c,row,decoded))


def migrate_one(c, pk, base=None):
    # Row and parts are read while the same advisory lock as importers is held by caller.
    row=my.query(c,'SELECT * FROM media_items WHERE id=%s FOR UPDATE',(pk,))[0]
    if row['msg_kind'] not in KINDS: c.rollback();return None
    account=row['account_key']
    if not account: raise ValueError('媒体缺少账号身份，保留 BLOB')
    originals=list(chunks(c,row)); decoded=list(chunks(c,row,True))
    if not originals:
        if decoded and not row.get('file_path'):raise ValueError('缺少原始媒体，保留转换 BLOB')
        if row.get('file_path'): read(c,pk,base=base)
        c.commit();return None
    original=publish(originals,row['platform'],account,row.get('ext') or 'bin',base)
    for name,expected in [('bytes',row['bytes']),('sha1',row['sha1']),('md5',row['content_md5'])]:
        if expected is not None and str(original[name]) != str(expected): raise ValueError('原始媒体与库内校验值不符')
    preview=None
    if decoded:
        ext={'image/jpeg':'jpg','image/png':'png','audio/wav':'wav'}.get(row['decoded_mime'],'bin')
        preview=publish(decoded,row['platform'],account,ext,base)
        if row['decoded_sha1'] and preview['sha1'] != row['decoded_sha1']: raise ValueError('转换产物校验失败')
    elif row.get('decoded_path'):
        read(c,pk,True,base)
    my.execute(c,'UPDATE media_items SET file_path=%s,file_sha256=%s,content_md5=%s,sha1=%s,bytes=%s,data=NULL,decoded_data=NULL,file_migrated_at=NOW() WHERE id=%s',
        (original['path'],original['sha256'],original['md5'],original['sha1'],original['bytes'],pk))
    if preview:
        my.execute(c,'UPDATE media_items SET decoded_path=%s,decoded_sha256=%s,decoded_sha1=%s WHERE id=%s',
            (preview['path'],preview['sha256'],preview['sha1'],pk))
    my.execute(c,'DELETE FROM blob_parts WHERE table_name IN (%s,%s) AND row_id=%s',('media_items','media_items.decoded_data',pk))
    c.commit()
    return dict(id=pk,bytes=original['bytes']+(preview['bytes'] if preview else 0),sha256=original['sha256'])


def save(c,row,data=None,decoded=None,base=None):
    """Save a collected image/voice, retaining legacy IDs and any old BLOBs."""
    row=dict(row)
    hits=my.query(c,'SELECT id,unavailable,file_path,media_role FROM media_items WHERE platform=%s AND message_pk=%s AND msg_kind=%s AND COALESCE(source_key,\'\')=%s ORDER BY id',
        (row['platform'],row['message_pk'],row['msg_kind'],row.get('source_key') or ''))
    if not hits and row.get('source_key')!='missing':
        hits=my.query(c,"SELECT id,unavailable,file_path,media_role FROM media_items WHERE platform=%s AND message_pk=%s AND msg_kind=%s AND source_key='missing' ORDER BY id",(row['platform'],row['message_pk'],row['msg_kind']))
    old=hits[0] if hits else None
    if old and not data:return old['id']
    if old and old['file_path'] and (old['media_role']=='original' or row.get('media_role')=='thumb'):
        try:read(c,old['id'],base=base);return old['id']
        except FileNotFoundError:pass
    if data:
        row.setdefault('needs_decode',0)
        if decoded is None:
            # A new original must not keep a preview belonging to its old thumbnail.
            row.update(decoded_path=None,decoded_sha256=None,decoded_sha1=None,decoded_mime=None)
        item=publish([data],row['platform'],row['account_key'],row.get('ext') or 'bin',base)
        row.update(file_path=item['path'],file_sha256=item['sha256'],content_md5=item['md5'],sha1=item['sha1'],bytes=item['bytes'],unavailable=0)
    if decoded:
        ext='wav' if row.get('decoded_mime')=='audio/wav' else 'jpg'
        item=publish([decoded],row['platform'],row['account_key'],ext,base)
        row.update(decoded_path=item['path'],decoded_sha256=item['sha256'],decoded_sha1=item['sha1'])
    if old:
        sql='UPDATE media_items SET '+','.join('`%s`=%%s'%k for k in row)+' WHERE id=%s'
        my.execute(c,sql,tuple(row.values())+(old['id'],));pk=old['id']
    else:
        my.execute(c,'INSERT INTO media_items ('+','.join('`%s`'%k for k in row)+') VALUES ('+','.join(['%s']*len(row))+')',tuple(row.values()))
        # Retrieve the generated ID on the same connection.
        pk=my.scalar(c,'SELECT LAST_INSERT_ID()')
    return pk
