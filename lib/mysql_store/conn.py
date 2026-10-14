"""MySQL 连接与批量写入工具。

口令优先级：``$WX_MYSQL_PASSWORD`` → ``$MYSQL_PWD`` → 本机私密配置。无默认口令。
``$WX_MYSQL_USER`` / ``$WX_MYSQL_HOST`` / ``$WX_MYSQL_PORT`` 同理。
"""
from __future__ import annotations

import os
import time
import re
import uuid
import contextlib
import json
import hashlib
from typing import Iterable, List, Sequence, Tuple

import pymysql
from pymysql.cursors import DictCursor
from localio import identity


def database_name(value):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value or ""):
        raise ValueError("无效的 MySQL 库名")
    return value

DEFAULT_USER = "wxexport"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3306

from local_config import mysql_options


def password() -> str:
    return mysql_options()['password']


def connect(db: str = "", *, autocommit: bool = False, charset: str = "utf8mb4"):
    """打开一个连接。``db`` 为空表示不选库（用于 CREATE DATABASE）。"""
    kw = dict(
        **mysql_options(),
        charset=charset,
        autocommit=autocommit,
        cursorclass=DictCursor,
        local_infile=False,
        max_allowed_packet=256 * 1024 * 1024,
    )
    if db:
        kw["database"] = database_name(db)
    return pymysql.connect(**kw)


def server_info(db: str = "") -> dict:
    with connect(db) as c, c.cursor() as cur:
        cur.execute("SELECT VERSION() AS v, DATABASE() AS db")
        return cur.fetchone()


# --------------------------------------------------------------------------- #
class BatchLoader:
    """按表攒行 → 达到阈值就批量插入。

    * ``insert_ignore=True`` 时用 ``INSERT IGNORE``（重复键直接跳过），用于幂等重跑；
    * ``on_dup`` 非空时改用 ``ON DUPLICATE KEY UPDATE`` 更新指定列；
    * 每张表独立缓冲，互不影响。
    """

    def __init__(self, connection, batch_size: int = 2000, verbose: bool = True):
        self.c = connection
        self.batch = batch_size
        self.verbose = verbose
        self._buf: dict = {}
        self.inserted = 0
        self.updated = 0
        self.skipped = 0

    # ---------------------------------------------------------------- #
    def add(self, table: str, row: dict, *, insert_ignore: bool = True,
            on_dup: Sequence[str] = ()):
        key = (table, insert_ignore, tuple(on_dup))
        buf = self._buf.setdefault(key, [])
        buf.append(row)
        if len(buf) >= self.batch:
            self.flush_table(key)

    def flush_table(self, key):
        table, insert_ignore, on_dup = key
        rows = self._buf.get(key)
        if not rows:
            return
        cols = list(rows[0].keys())
        for r in rows:
            if list(r.keys()) != cols:
                raise ValueError("同批插入的列不一致：%s" % table)
        quoted = ", ".join("`%s`" % c for c in cols)
        holder = ", ".join(["%s"] * len(cols))
        sql = "INSERT %sINTO `%s` (%s) VALUES (%s)" % (
            "IGNORE " if insert_ignore else "", table, quoted, holder)
        if on_dup and not insert_ignore:
            sql += " ON DUPLICATE KEY UPDATE " + ", ".join(
                "`%s`=VALUES(`%s`)" % (c, c) for c in on_dup)
        values = [tuple(r[c] for c in cols) for r in rows]
        with self.c.cursor() as cur:
            cur.executemany(sql, values)
            n = cur.rowcount
        if n is None:
            n = 0
        if n < 0:
            self.skipped += -n
        else:
            self.inserted += n
        self.c.commit()
        self._buf[key] = []

    def flush(self):
        for key in list(self._buf):
            self.flush_table(key)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            self.flush()
        else:
            self._buf.clear()
            self.c.rollback()
        return False


# --------------------------------------------------------------------------- #
# 单条 SQL 的上限（服务端 max_allowed_packet=64MB；hex 转义会放大，取 8MB 留足余量）
MAX_INLINE_BLOB = 8 * 1024 * 1024


def blob_is_chunked(size: int) -> bool:
    return size > MAX_INLINE_BLOB


def insert_blob_parts(connection, table: str, row: dict, blob_col: str,
                      data_or_path, chunk: int = MAX_INLINE_BLOB,
                      commit: bool = True) -> int:
    """元数据插 ``table``，二进制切块插 ``blob_parts``；返回 ``table`` 的自增 id。

    ``data_or_path`` 可以是 ``bytes`` 或文件路径（大文件按块从磁盘读，不整包进内存）。
    """
    return write_media(connection, row, data_or_path, table=table,
                       chunk=chunk, commit=commit)


def _write_parts(connection, table: str, pk: int, data_or_path,
                 chunk: int = MAX_INLINE_BLOB, commit: bool = True):
    def run(size: int):
        with connection.cursor() as cur:
            if isinstance(data_or_path, (bytes, bytearray)):
                buf = bytes(data_or_path)
                for i in range(0, len(buf), size):
                    cur.execute(
                        "INSERT INTO `blob_parts` (`table_name`,`row_id`,`part_no`,"
                        "`data`) VALUES (%s,%s,%s,%s)", (table, pk, i // size,
                                                        buf[i:i + size]))
            else:
                with open(data_or_path, "rb") as fh:
                    part = 0
                    while True:
                        buf = fh.read(size)
                        if not buf:
                            break
                        cur.execute(
                            "INSERT INTO `blob_parts` (`table_name`,`row_id`,"
                            "`part_no`,`data`) VALUES (%s,%s,%s,%s)",
                            (table, pk, part, buf))
                        part += 1

    run(chunk)
    if commit:
        connection.commit()


def media_identity(row):
    # Availability and preview conversion are not part of an item's identity.
    return identity(row.get("platform"), row.get("account_key", ""),
                    row.get("message_pk"), row.get("msg_kind"),
                    row.get("source_key") or row.get("md5") or "none",
                    row.get("source_path") if row.get("message_pk") is None else "")


@contextlib.contextmanager
def import_session(connection, platform, scope, media_enabled):
    db = scalar(connection, "SELECT DATABASE()")
    lock = "chat-import:" + db
    if scalar(connection, "SELECT GET_LOCK(%s,0)", (lock,)) != 1:
        raise RuntimeError("目标库正在入库：" + db)
    media_lock='chat-media:'+db if media_enabled else None
    if media_lock and scalar(connection,"SELECT GET_LOCK(%s,0)",(media_lock,))!=1:
        execute(connection,"SELECT RELEASE_LOCK(%s)",(lock,))
        raise RuntimeError("媒体正在同步")
    try:
        with connection.cursor() as cur:
            cur.execute("INSERT INTO import_runs(platform,scope,media_enabled,status) VALUES (%s,%s,%s,%s)",
                        (platform,json.dumps(scope),bool(media_enabled),"running"))
            run_id=cur.lastrowid
        connection.commit()
        try:
            yield run_id
        except BaseException as exc:
            connection.rollback()
            with connection.cursor() as cur:
                cur.execute("UPDATE import_runs SET status=%s,detail=%s WHERE id=%s",
                            ("failed",str(exc)[:4000],run_id))
            connection.commit()
            raise
        else:
            with connection.cursor() as cur:
                cur.execute("UPDATE import_runs SET status=%s WHERE id=%s",("complete",run_id))
            connection.commit()
    finally:
        if media_lock:execute(connection,"SELECT RELEASE_LOCK(%s)",(media_lock,))
        execute(connection,"SELECT RELEASE_LOCK(%s)",(lock,))


def write_media(connection, row, parts=None, *, table="media_items",
                chunk=MAX_INLINE_BLOB, commit=False):
    """Idempotent media replacement under a savepoint; no whole-batch rollback."""
    if table != "media_items":
        raise ValueError("只支持 media_items")
    row = dict(row)
    row.setdefault("identity_key", media_identity(row))
    preview=row.get("decoded_data")
    preview_parts=preview if preview is not None and len(preview)>MAX_INLINE_BLOB else None
    if preview is not None:
        row["decoded_sha1"]=hashlib.sha1(preview).hexdigest()
    if preview_parts is not None:
        row["decoded_data"]=None
    source = parts if parts is not None else row.get("data")
    if source is not None:
        sha = hashlib.sha1()
        md5 = hashlib.md5()
        length = 0
        if isinstance(source, (bytes, bytearray)):
            sha.update(source); md5.update(source); length = len(source)
        else:
            with open(source,"rb") as fh:
                for block in iter(lambda:fh.read(1024*1024),b""):
                    sha.update(block); md5.update(block); length += len(block)
        row["bytes"],row["sha1"] = length,sha.hexdigest()
        row["content_md5"] = md5.hexdigest()
    name = "media_" + uuid.uuid4().hex
    for attempt in range(2):
        with connection.cursor() as cur:
            cur.execute("SAVEPOINT " + name)
        try:
            cols = list(row)
            with connection.cursor() as cur:
                cur.execute("SELECT id FROM media_items WHERE identity_key=%s FOR UPDATE",
                            (row["identity_key"],))
                old = cur.fetchone()
                if old:
                    pk = old["id"]
                    cur.execute("DELETE FROM blob_parts WHERE table_name IN (%s,%s) AND row_id=%s",
                                (table, table+".decoded_data", pk))
                    cur.execute("UPDATE media_items SET " + ",".join(
                        "`%s`=%%s" % c for c in cols) + " WHERE id=%s",
                        tuple(row[c] for c in cols) + (pk,))
                else:
                    cur.execute("INSERT INTO media_items (%s) VALUES (%s)" % (
                        ",".join("`%s`" % c for c in cols),
                        ",".join(["%s"] * len(cols))), tuple(row[c] for c in cols))
                    pk = cur.lastrowid
            if parts is not None:
                _write_parts(connection, table, pk, parts, chunk, commit=False)
            if preview_parts is not None:
                _write_parts(connection,table+".decoded_data",pk,preview_parts,chunk,commit=False)
            with connection.cursor() as cur:
                cur.execute("RELEASE SAVEPOINT " + name)
            if commit:
                connection.commit()
            return pk
        except Exception as exc:
            # Packet/disconnect/deadlock errors can invalidate the whole transaction.
            # Never pretend a lost connection or batch can be repaired locally.
            if isinstance(exc, pymysql.OperationalError) and exc.args[0] in (1213, 2006, 2013):
                raise
            with connection.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT " + name)
                cur.execute("RELEASE SAVEPOINT " + name)
            if attempt or not (isinstance(exc, pymysql.OperationalError)
                               and exc.args[0] == 1205):
                raise


def blob_for(connection, table: str, pk: int) -> bytes:
    """把 ``blob_parts`` 里的分片拼回来（校验用）。"""
    if table in ('media_items','media_items.decoded_data'):
        columns={r['Field'] for r in query(connection,'SHOW COLUMNS FROM media_items')}
        if 'file_path' in columns:
            col='decoded_path' if table.endswith('.decoded_data') else 'file_path'
            path=scalar(connection,'SELECT `%s` FROM media_items WHERE id=%%s'%col,(pk,))
            if path:
                from .files import read
                return read(connection,pk,table.endswith('.decoded_data'))
    with connection.cursor() as cur:
        cur.execute("SELECT `data` FROM `blob_parts` WHERE `table_name`=%s AND "
                    "`row_id`=%s ORDER BY `part_no`", (table, pk))
        return b"".join(bytes(r["data"]) for r in cur.fetchall())


def delete_media(connection, ids):
    for start in range(0,len(ids),1000):
        batch=ids[start:start+1000]
        placeholders=",".join(["%s"]*len(batch))
        with connection.cursor() as cur:
            cur.execute("DELETE FROM blob_parts WHERE table_name IN (%s,%s) AND row_id IN ("+placeholders+")",("media_items","media_items.decoded_data")+tuple(batch))
            cur.execute("DELETE FROM media_items WHERE id IN ("+placeholders+")",tuple(batch))


def insert_blob_chunked(connection, table: str, row: dict, blob_col: str,
                        chunk: int = MAX_INLINE_BLOB) -> bool:
    """兼容入口：小 blob 直插，大 blob 走 ``blob_parts``。"""
    data = row.get(blob_col)
    if data is not None and len(data) > chunk:
        meta = {k: v for k, v in row.items() if k != blob_col}
        meta["reason"] = (meta.get("reason") or "")[:400] + " [data 见 blob_parts]"
        insert_blob_parts(connection, table, meta, blob_col, data, chunk)
        return False
    with connection.cursor() as cur:
        cols = list(row.keys())
        cur.execute(
            "INSERT INTO `%s` (%s) VALUES (%s)" % (
                table, ", ".join("`%s`" % c for c in cols),
                ", ".join(["%s"] * len(cols))),
            tuple(row[c] for c in cols))
    connection.commit()
    return True


def execute(connection, sql: str, args: Tuple = ()):
    with connection.cursor() as cur:
        cur.execute(sql, args)
        return cur


def query(connection, sql: str, args: Tuple = ()) -> List[dict]:
    with connection.cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())


def scalar(connection, sql: str, args: Tuple = ()):
    with connection.cursor() as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
        if not row:
            return None
        return list(row.values())[0] if isinstance(row, dict) else row[0]


def retry(fn, attempts: int = 3, delay: float = 2.0):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:          # noqa: BLE001 —— 网络/超时都要重试
            last = exc
            if i + 1 < attempts:
                time.sleep(delay * (i + 1))
    raise last
