"""企业微信 → MySQL（``wecom_chat``）的全量入库。

**和个人微信完全分开**：不同的库（``wecom_chat``）、不同的账号体系、不同的表结构、
不同的会话 ID 规则。不要和 ``wx_import`` 混用。

会话 ID 的讲究（本机 5.0.7 实测）
--------------------------------
* ``R:<id>`` 群聊、``S:<对方id>_<本人id>`` 单聊、``M:`` 微信联系人、
  ``Y:``/``O:`` 系统或应用 —— **前缀原样保留**，不做数值化。
* ``S:`` 取名要跳过本人那一段，交给现成的 ``messages.resolve_conv_name``。
* 企微的群昵称表 ``conversation_user_table`` 在本机是**空的**，所以 ``room_nick``
  允许为空，并在数据集备注里点明，不假装有数据。

媒体
----
``Profiles/<id>/Caches/{Images,Files,Videos,Voices,Emotions,*_Thumbnail}`` 是**明文**，
目录名就是 md5（``.../Caches/Images/<年月>/<md5>/<文件名>``）。落库策略：

1. 每条消息从 content / extra_content 里抠 32 位 hex（含解码后的正文）；
2. 命中磁盘文件 → 写 ``media_items``（视频/大文件按块进 ``blob_parts``）；
3. 没有消息引用的缓存文件 → 也写一行（``message_pk NULL``，reason 注明
   「未被任何消息引用」），保证「本机已有的一个不落」。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from wecom import config as wconfig
from wecom import conv as wconv
from wecom import extract as wextract
from wecom import messages as wmsg

from . import conn as my
from . import silk

MD5_RE = re.compile(r"([0-9a-f]{32})", re.I)
URL_RE = re.compile(r"https?://[^\s\"'<>\\\x00-\x1f]+")
MEDIA_DIRS = (
    ("Images", "image"), ("Files", "file"), ("Videos", "video"),
    ("Voices", "voice"), ("Emotions", "sticker"),
    ("Emotion_Thumbnail", "sticker"), ("Video_Thumbnail", "video"),
    ("Link_Thumbnail", "link"),
)


def _safe_text(s, limit: int = 0) -> str:
    if s is None:
        return ""
    if isinstance(s, bytes):
        s = s.decode("utf-8", "replace")
    s = str(s).replace("\x00", "")
    return s[:limit] if limit else s


def _to_ts(v) -> int:
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return 0
    # 企业微信可能是毫秒时间戳
    if n > 10 ** 12:
        n //= 1000
    return n



def decode_message(self,raw,extra,ctype,cid):
    text=wmsg.decode_content(raw,ctype)
    return text,self._links_of(raw,extra,text),self._md5s_of(raw,extra,text,cid)


class WecomImporter:
    def __init__(self, *, account_key: str = "", verbose: bool = True,
                 with_backups: bool = False, decode_voice: bool = True,
                 skip_media: bool = False, database="wecom_chat",
                 raw_root=None, plain_root=None):
        self.raw_root = raw_root or wconfig.RAW
        self.plain_root = plain_root or wconfig.PLAIN
        self.verbose = verbose
        self.with_backups = with_backups
        self.decode_voice = decode_voice
        self.skip_media = skip_media
        self.account_key = account_key
        self.db = my.connect(database)
        self.media_pk: Dict[Tuple[int, str, str], int] = {}
        self._claimed_path_set = set()
        self._cache_idx: Optional[Dict[str, List[Tuple[str, str, str]]]] = None
        self._idx_ready = False
        self.stats = dict(datasets=0, chats=0, messages=0, links=0, contacts=0,
                          members=0, snapshots=0, media_files=0, media_ok=0,
                          media_unavailable=0, media_bytes=0, voice_decoded=0,
                          voice_failed=0, blobs_chunked=0)

    def log(self, msg: str):
        if self.verbose:
            print(msg, flush=True)

    @staticmethod
    def _fmt(ts) -> Optional[str]:
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
        except (OSError, ValueError, OverflowError):
            return None

    # ================================================================== #
    def dataset_dirs(self) -> List[Path]:
        dirs = [Path(p) for p in wextract.datasets(self.plain_root)]
        live = [d for d in dirs if "Backup" not in d.name]
        return dirs if self.with_backups else (live or dirs)

    # ================================================================== #
    def run(self):
        with my.import_session(self.db,"wecom",dict(with_backups=self.with_backups),not self.skip_media):
            return self._run()

    def _run(self):
        dirs = self.dataset_dirs()
        self.log("明文数据集 %d 个（Backup %s）"
                 % (len(dirs), "包含" if self.with_backups else "已排除"))
        hints = wmsg.name_hints_from_snapshot(self.raw_root)
        self._register_excluded_backups()
        self._import_snapshot()

        for path in dirs:
            self.import_dataset(path, hints)
        self.reconcile()
        return self.stats

    def _register_excluded_backups(self):
        """把被排除的 Backup 记录进 datasets，免得将来看报告时误判成丢数据。"""
        acct = self.account_key or self._guess_account()
        for p in wextract.datasets(self.plain_root):
            if "Backup" not in os.path.basename(p):
                continue
            match=re.match(r"(\d+)",os.path.basename(p))
            acct=self.account_key or (match.group(1) if match else os.path.basename(p))
            with self.db.cursor() as cur:
                cur.execute(
                    "INSERT INTO `datasets` (`platform`,`account_key`,`name`,`kind`,"
                    "`path`,`included`,`exclude_reason`) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE `included`=VALUES(`included`),"
                    "`exclude_reason`=VALUES(`exclude_reason`)",
                    ("wecom", acct, os.path.basename(p), "backup", str(p), 0,
                     "按需求只取最新一份（Backup 是旧时间点快照，可能重复）"))
        self.db.commit()

    def _guess_account(self) -> str:
        for p in wextract.datasets(self.plain_root):
            m = re.match(r"(\d+)", os.path.basename(p))
            if m and "Backup" not in os.path.basename(p):
                return m.group(1)
        return "unknown"

    # ================================================================== #
    def _import_snapshot(self):
        """``conv_snapshot``：明文 protobuf，会话列表 + 每个会话最后一条消息。"""
        try:
            convs = wconv.load_all(self.raw_root)
        except Exception as exc:
            self.log("   会话快照读取失败：%s" % exc)
            return
        acct = self.account_key or self._guess_account()
        rows = []
        for c in convs:
            acct=self.account_key or c.account_id
            last = c.last
            rows.append((
                "wecom", acct, str(c.conv_id), _safe_text(c.name, 512) or None,
                str(c.peer_id or ""), c.last_time or None, self._fmt(c.last_time),
                _safe_text(last.sender_name if last else "", 512) or None,
                _safe_text(last.sender_id if last else "", 320) or None,
                _safe_text(last.content_type if last else "", 64) or None,
                _safe_text(last.text if last else "", 0) or None,
            ))
        if rows:
            with self.db.cursor() as cur:
                cur.executemany(
                    "INSERT INTO `snapshot_conversations` (`platform`,`account_key`,"
                    "`conv_id`,`name`,`peer_id`,`last_ts`,`last_time`,`sender_name`,"
                    "`sender_id`,`content_type`,`last_text`) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE `name`=VALUES(`name`),"
                    "`last_ts`=VALUES(`last_ts`),`last_text`=VALUES(`last_text`)", rows)
            self.db.commit()
        self.stats["snapshots"] = len(rows)
        self.log("   会话快照 %d 个" % len(rows))

    # ================================================================== #
    def import_dataset(self, dataset_dir: Path, hints: dict):
        name = dataset_dir.name
        match = re.match(r"(\d+)", name)
        acct = self.account_key or (match.group(1) if match else name)
        self.current_account = acct
        self.current_dataset=name
        self._written_media=set()
        self._cache_idx = None
        self._claimed_path_set = set()
        hints=wmsg.name_hints_from_snapshot(self.raw_root,acct)
        files = {p.name: p for p in Path(dataset_dir).rglob("*.db")}
        users = wmsg.load_users(files)
        self._import_dataset_row(name, str(dataset_dir), acct)
        self._import_contacts(users, acct, name)
        convs = self._conversations(dataset_dir, hints, acct)
        self._import_messages(dataset_dir, convs, acct)
        if not self.skip_media:
            self._persist_unmatched_media()
            stored=my.query(self.db,"SELECT i.id FROM media_items i LEFT JOIN messages m ON m.id=i.message_pk WHERE i.account_key=%s AND (m.source_dataset=%s OR i.message_pk IS NULL)",(acct,name))
            my.delete_media(self.db,[r["id"] for r in stored if r["id"] not in self._written_media])
            self.db.commit()
        self.log("   数据集 %s：联系人 %d，会话 %d，消息累计 %d"
                 % (name, len(users), len(convs), self.stats["messages"]))
        self.stats["datasets"] += 1

    def _import_dataset_row(self, name, path, acct):
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO `datasets` (`platform`,`account_key`,`name`,`kind`,"
                "`path`,`included`) VALUES (%s,%s,%s,%s,%s,1) "
                "ON DUPLICATE KEY UPDATE `path`=VALUES(`path`),`included`=1",
                ("wecom", acct, name, "live", path))
        self.db.commit()

    def _import_contacts(self, users: dict, acct: str, dataset: str):
        if not users:
            return
        rows = []
        for uid, name in users.items():
            # 群昵称表在本机是空的，这里只有全局姓名
            rows.append(("wecom", acct, _safe_text(uid, 320), None,
                         None, None, _safe_text(name, 255) or None, None, None))
        with self.db.cursor() as cur:
            cur.executemany(
                "INSERT INTO `contacts` (`platform`,`account_key`,`contact_id`,"
                "`alias`,`nick_name`,`remark`,`real_name`,`corp_name`,`extra`) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE `real_name`=VALUES(`real_name`)", rows)
        self.db.commit()
        self.stats["contacts"] += len(rows)

    # ================================================================== #
    def _conversations(self, dataset_dir: Path, hints: dict, acct: str) -> Dict[str, dict]:
        """从 ``session.db.conversation_table`` 读会话，缺名字的用快照补。"""
        files = {p.name: p for p in Path(dataset_dir).rglob("*.db")}
        session_db = files.get("session.db")
        out: Dict[str, dict] = {}
        self_id = re.match(r"(\d+)", Path(dataset_dir).name)
        self_id = self_id.group(1) if self_id else ""
        users = wmsg.load_users(files)
        if session_db:
            conn = wmsg.open_ro(session_db)
            table = wmsg.find_table(conn, ("conversation_table", "conversation"))
            if table:
                m = wmsg.mapping(conn, table, wmsg.CONV_COLS)
                cols = [c for c in m.values() if c]
                sel = ", ".join('"%s"' % c for c in cols)
                for row in conn.execute('select %s from "%s"' % (sel, table)):
                    rec = dict(zip([k for k, v in m.items() if v],
                                   [str(v) if v is not None else "" for v in row]))
                    cid = rec.get("id", "")
                    if not cid:
                        continue
                    out[cid] = {
                        "conv_id": cid,
                        "name": wmsg.resolve_conv_name(cid, rec.get("name", ""),
                                                       users, hints, self_id),
                        "kind": wmsg._conv_kind(cid),
                        "last_ts": _to_ts(rec.get("time")),
                    }
            # 成员表（本机为空，留着以免别的机器上漏）
            mtable = wmsg.find_table(conn, ("conversation_user_table", "conversation_user"))
            if mtable:
                m = wmsg.mapping(conn, mtable, wmsg.MEMBER_COLS)
                if m.get("conversation") and m.get("user"):
                    nick = m.get("nick")
                    sel = '"%s","%s"%s' % (m["conversation"], m["user"],
                                           (', "%s"' % nick) if nick else "")
                    for row in conn.execute('select %s from "%s"' % (sel, mtable)):
                        cid, uid = str(row[0]), str(row[1])
                        name = str(row[2]) if nick and len(row) > 2 and row[2] else ""
                        c = out.setdefault(cid, {"conv_id": cid, "name": cid,
                                                 "kind": wmsg._conv_kind(cid),
                                                 "last_ts": 0})
                        c.setdefault("members", {})[uid] = name
            conn.close()

        # 落 chats
        with self.db.cursor() as cur:
            for cid, c in out.items():
                cur.execute(
                    "INSERT INTO `chats` (`platform`,`account_key`,`chat_id`,`is_room`,"
                    "`kind`,`title`,`last_ts`,`last_time`) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE `title`=VALUES(`title`),"
                    "`last_ts`=GREATEST(COALESCE(`last_ts`,0),VALUES(`last_ts`)),"
                    "`last_time`=VALUES(`last_time`)",
                    ("wecom", acct, cid, 1 if c["kind"] == "群聊" else 0, c["kind"],
                     _safe_text(c["name"], 512) or None, c["last_ts"],
                     self._fmt(c["last_ts"]) if c["last_ts"] else None))
            cur.execute("SELECT `chat_pk`,`chat_id` FROM `chats` WHERE `platform`='wecom'")
            pks = {r["chat_id"]: int(r["chat_pk"]) for r in cur.fetchall()}
        self.db.commit()
        for cid, c in out.items():
            c["chat_pk"] = pks.get(cid)
        self.stats["chats"] += len(out)
        return out

    # ================================================================== #
    def _ensure_chat(self, conv: dict, users: dict, acct: str) -> Optional[int]:
        """确保这个会话有 ``chats`` 行并拿到 ``chat_pk``。

        消息里出现的 ``conversation_id`` 不一定在 ``conversation_table`` 里
        （实测有这种），缺了就在这里补一行，否则 ``messages.chat_pk`` 会 NULL 报错。
        """
        if conv.get("chat_pk"):
            return conv["chat_pk"]
        cid = conv["conv_id"]
        if not conv.get("name") or conv["name"] == cid:
            conv["name"] = wmsg.resolve_conv_name(cid, conv.get("name") or "",
                                                  users, {}, "")
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO `chats` (`platform`,`account_key`,`chat_id`,`is_room`,"
                "`kind`,`title`,`last_ts`,`last_time`) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE `title`=COALESCE(VALUES(`title`),`title`)",
                ("wecom", acct, cid, 1 if conv["kind"] == "群聊" else 0,
                 conv["kind"], _safe_text(conv.get("name"), 512) or None,
                 conv.get("last_ts") or None,
                 self._fmt(conv["last_ts"]) if conv.get("last_ts") else None))
            cur.execute("SELECT `chat_pk` FROM `chats` WHERE `platform`='wecom' "
                        "AND `account_key`=%s AND `chat_id`=%s", (acct,cid))
            got = cur.fetchone()
        self.db.commit()
        conv["chat_pk"] = int(got["chat_pk"]) if got else None
        return conv["chat_pk"]

    def _import_messages(self, dataset_dir: Path, convs: Dict[str, dict], acct: str):
        files = {p.name: p for p in Path(dataset_dir).rglob("*.db")}
        msg_db = files.get("message.db")
        if not msg_db:
            return
        conn = wmsg.open_ro(msg_db)
        tables = [t for t in wmsg.tables(conn)
                  if "message" in t.lower() and not t.startswith("sqlite_")]
        users = wmsg.load_users(files)
        dataset_name = Path(dataset_dir).name
        sender_counts: Dict[Tuple[str, str], int] = {}
        first_last: Dict[str, Tuple[int, int]] = {}
        media_refs: List[Tuple] = []
        used_md5 = set()

        for table in tables:
            m = wmsg.mapping(conn, table, wmsg.COLS)
            if not m.get("content") or not m.get("conversation"):
                continue
            wanted = [(k, v) for k, v in m.items() if v]
            sel = ", ".join('"%s"' % v for _k, v in wanted)
            keys = ["_source_rowid"] + [k for k, _v in wanted]
            try:
                rows = conn.execute('select rowid, %s from "%s"' % (sel, table))
            except Exception as exc:                       # noqa: BLE001
                self.log("   ! 表 %s 查询失败：%s" % (table, exc))
                raise RuntimeError("读消息表失败 %s" % table) from exc
            batch: List[tuple] = []
            meta: List[tuple] = []
            raw_records = []
            for row in rows:
                rec = dict(zip(keys, row))
                cid = str(rec.get("conversation") or "")
                if not cid:
                    cid = "ORPHAN_" + table
                conv = convs.get(cid)
                if conv is None:
                    conv = convs[cid] = {"conv_id": cid, "name": cid,
                                         "kind": wmsg._conv_kind(cid), "last_ts": 0,
                                         "chat_pk": None}
                # 消息里出现、但 conversation_table 里没有的会话，这里补建
                self._ensure_chat(conv, users, acct)
                uid = str(rec.get("sender") or "")
                sender_name = users.get(uid, uid)
                raw = rec.get("content")
                extra = rec.get("extra")
                raw_len = len(raw) if isinstance(raw, (bytes, bytearray)) else 0
                text,links,md5s = decode_message(self,raw,extra,rec.get("ctype"),cid)
                ts = _to_ts(rec.get("time"))
                # 只把**真能在本机缓存里配到**的 md5 当媒体引用：
                # 企业微信的卡片/提醒正文里本来就带随机 16 字节（长得像 md5），
                # 全当媒体会让 has_media 虚高（实测 1,357 条假阳性）。
                cache = self._cache_index()
                md5s = [m for m in md5s if cache.get(m)]
                used_md5.update(md5s)
                sender_counts[(cid, uid)] = sender_counts.get((cid, uid), 0) + 1
                fl = first_last.get(cid)
                first_last[cid] = ((ts, ts) if fl is None
                                   else (min(fl[0], ts), max(fl[1], ts)))
                batch.append((
                    "wecom", acct, conv.get("chat_pk"), cid,
                    str(rec.get("msg_id") or rec["_source_rowid"]), str(rec.get("server_id") or "") or None,
                    (int(rec.get("sequence")) if str(rec.get("sequence") or "").lstrip("-").isdigit() else None),
                    (int(rec.get("ctype")) if str(rec.get("ctype") or "").lstrip("-").isdigit() else 0),
                    self._kind_of(rec.get("ctype")), _safe_text(uid, 320) or None,
                    _safe_text(sender_name, 512) or None, ts, self._fmt(ts),
                    _safe_text(text, 0) or None,
                    _safe_text(rec.get("ctype"), 64) or None,
                    dataset_name, os.path.basename(str(msg_db)), table,
                    1 if md5s else 0, 1 if links else 0,
                    self._extra_json(rec),
                ))
                raw_records.append((raw, extra))
                meta.append((cid, uid, ts, text, links, md5s, _safe_text(text, 0)[:200]))
                self.stats["messages"] += 1

            if not batch:
                continue
            with self.db.cursor() as cur:
                for i in range(0, len(batch), 5000):
                    cur.executemany(
                        "INSERT INTO `messages` (`platform`,`account_key`,`chat_pk`,"
                        "`chat_id`,`local_id`,`server_id`,`sort_seq`,`local_type`,`kind`,"
                        "`sender_id`,`sender_name`,`ts`,`msg_time`,`text`,"
                        "`content_type`,`source_dataset`,`source_db`,`source_table`,"
                        "`has_media`,`has_link`,`extra`) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                        "%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE `text`=VALUES(`text`),"
                        "`kind`=VALUES(`kind`),`sender_name`=VALUES(`sender_name`),"
                        "`has_media`=VALUES(`has_media`),`has_link`=VALUES(`has_link`),`sender_id`=VALUES(`sender_id`),"
                        "`ts`=VALUES(`ts`),`msg_time`=VALUES(`msg_time`),`extra`=VALUES(`extra`),"
                        "`local_type`=VALUES(`local_type`),`sort_seq`=VALUES(`sort_seq`),`server_id`=VALUES(`server_id`)",
                        batch[i:i + 5000])
            self.db.commit()

            # 取回 id 写 links，并收集媒体引用
            id_map: Dict[str, int] = {}
            with self.db.cursor() as cur:
                cur.execute("SELECT `id`,`local_id`,`chat_id` FROM `messages` WHERE "
                            "`platform`='wecom' AND `account_key`=%s AND `source_dataset`=%s AND "
                            "`source_db`=%s AND `source_table`=%s",
                            (acct, dataset_name, os.path.basename(str(msg_db)), table))
                for r in cur.fetchall():
                    id_map[(r["chat_id"], str(r["local_id"]))] = int(r["id"])
            with self.db.cursor() as cur:
                cur.execute("DELETE l FROM message_links l JOIN messages m ON m.id=l.message_pk WHERE m.account_key=%s AND m.source_dataset=%s AND m.source_table=%s",(acct,dataset_name,table))
            raw_rows = []
            rows_link = []
            for i, (cid, uid, ts, text, links, md5s, _t) in enumerate(meta):
                mpk = id_map.get((cid, str(batch[i][4])))
                if not mpk:
                    continue
                original, extra = raw_records[i]
                blob = bytes(original) if isinstance(original, (bytes, bytearray)) else (str(original).encode() if original is not None else None)
                extra_blob = bytes(extra) if isinstance(extra, (bytes, bytearray)) else (str(extra).encode() if extra is not None else None)
                raw_rows.append((mpk, "wecom", blob.decode("utf-8", "replace") if blob else "", blob, extra_blob, len(blob or b""), "protobuf"))
                for (url, title, des, app_type, origin) in links:
                    rows_link.append((
                        "wecom", mpk, cid, ts, url,
                        hashlib.sha1(url.encode("utf-8", "replace")).hexdigest(),
                        _safe_text(title, 512) or None, _safe_text(des, 1024) or None,
                        _safe_text(app_type, 32) or None, origin))
                    self.stats["links"] += 1
                if md5s:
                    for md5 in md5s:
                        media_refs.append((mpk, cid, ts, md5))
            with self.db.cursor() as cur:
                cur.executemany("INSERT INTO message_raw (message_pk,platform,raw_content,raw_blob,extra_blob,raw_bytes,decoded_from) VALUES (%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE raw_content=VALUES(raw_content),raw_blob=VALUES(raw_blob),extra_blob=VALUES(extra_blob),raw_bytes=VALUES(raw_bytes)", raw_rows)
            self.db.commit()
            if rows_link:
                with self.db.cursor() as cur:
                    for i in range(0, len(rows_link), 2000):
                        cur.executemany(
                            "INSERT INTO `message_links` (`platform`,`message_pk`,"
                            "`chat_id`,`ts`,`url`,`url_hash`,`title`,`des`,`app_type`,"
                            "`origin`) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                            "ON DUPLICATE KEY UPDATE `title`=VALUES(`title`)",
                            rows_link[i:i + 2000])
                self.db.commit()

        # 会话成员 + 计数
        for (cid, uid), n in sender_counts.items():
            conv = convs.get(cid) or {}
            pk = conv.get("chat_pk")
            if pk is None:
                continue
            fl = first_last.get(cid, (None, None))
            with self.db.cursor() as cur:
                cur.execute(
                    "INSERT INTO `chat_members` (`platform`,`chat_pk`,`chat_id`,"
                    "`contact_id`,`nick_name`,`shown`,`role`,`message_count`,"
                    "`first_ts`,`last_ts`) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE `message_count`=VALUES(`message_count`),"
                    "`shown`=VALUES(`shown`),`first_ts`=VALUES(`first_ts`),"
                    "`last_ts`=VALUES(`last_ts`)",
                    ("wecom", pk, cid, _safe_text(uid, 320),
                     _safe_text(users.get(uid), 255) or None,
                     _safe_text(users.get(uid, uid), 512) or None,
                     "我" if uid == (dataset_dir.name.split("_")[0]) else "成员",
                     n, fl[0], fl[1]))
            self.db.commit()
            self.stats["members"] += 1
        for cid, fl in first_last.items():
            conv = convs.get(cid) or {}
            pk = conv.get("chat_pk")
            if pk is None:
                continue
            with self.db.cursor() as cur:
                cur.execute(
                    "UPDATE `chats` SET `first_ts`=%s, `first_time`=%s WHERE "
                    "`chat_pk`=%s AND (`first_ts` IS NULL OR `first_ts` > %s)",
                    (fl[0], self._fmt(fl[0]), pk, fl[0]))
                cur.execute("SELECT COUNT(*) AS n FROM `messages` WHERE `chat_pk`=%s",
                            (pk,))
                cnt = cur.fetchone()["n"]
                cur.execute("UPDATE `chats` SET `message_count`=%s WHERE `chat_pk`=%s",
                            (cnt, pk))
            self.db.commit()

        # 媒体落库（消息引用到的）
        if not self.skip_media:
            self._persist_media(media_refs, used_md5)
        conn.close()
        self._save_source(dataset_name, os.path.basename(str(msg_db)),
                          tables, convs)

    @staticmethod
    def _kind_of(ctype) -> str:
        try:
            n = int(ctype)
        except (TypeError, ValueError):
            return "other"
        if n in (0, 2, 123):
            return "text"
        if n in (13, 31):
            return "link"
        if n == 29:
            return "file"
        if n == 503:
            return "image"
        if n == 573:
            return "file"
        if n in (14, 15):
            return "system"
        if n in (1011,):
            return "system"
        if n in (1002,):
            return "system"
        return "other"

    def _extra_json(self, rec: dict):
        keep = {}
        for k, v in rec.items():
            if k in ("conversation", "content", "ctype", "time", "msg_id", "sender",
                     "server_id", "sequence"):
                continue
            if isinstance(v, (bytes, bytearray)):
                keep[k] = "<%d bytes>" % len(v)
            elif v not in (None, ""):
                keep[k] = v
        return json.dumps(keep, ensure_ascii=False) if keep else None

    # ------------------------------------------------------------------ #
    def _links_of(self, raw, extra, text: str) -> List[tuple]:
        out, seen = [], set()

        def push(url, origin, title="", des=""):
            url = (url or "").strip()
            if not url or not url.lower().startswith(("http://", "https://")):
                return
            if (url, origin) in seen:
                return
            seen.add((url, origin))
            out.append((url, title, des, None, origin))

        for blob in (raw, extra):
            if isinstance(blob, (bytes, bytearray)):
                try:
                    s = bytes(blob).decode("utf-8", "replace")
                except Exception:
                    continue
                m = re.search(r"<title>(.*?)</title>", s, re.S)
                title = re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""
                m = re.search(r"<(?:url|href|cdnurl)>(.*?)</(?:url|href|cdnurl)>",
                              s, re.S | re.I)
                if m:
                    push(m.group(1).strip(), "appmsg", title)
                for m2 in URL_RE.finditer(s):
                    push(m2.group(0), "raw", title)
        for m in URL_RE.finditer(_safe_text(text)):
            push(m.group(0).rstrip("）。，、；」』】"), "text")
        return out

    def _md5s_of(self, raw, extra, text: str, cid: str) -> List[str]:
        out = set()
        for blob in (raw, extra):
            if isinstance(blob, (bytes, bytearray)):
                for m in MD5_RE.finditer(bytes(blob).decode("latin-1", "ignore")):
                    out.add(m.group(1).lower())
        for m in MD5_RE.finditer(_safe_text(text)):
            out.add(m.group(1).lower())
        # 会话 ID 本身的摘要不是媒体引用，剔掉
        out.discard(hashlib.md5(cid.encode()).hexdigest())
        return sorted(out)

    # ------------------------------------------------------------------ #
    def _cache_root(self) -> Optional[str]:
        from wecom.accounts import profile_for
        profile=profile_for(self.raw_root,self.current_account)
        return str(profile/"Caches") if profile else None

    def _cache_index(self) -> Dict[str, List[Tuple[str, str, str]]]:
        """``md5 -> [(路径, 类型标签, 目录类别)]``，按目录名里的 md5 建索引。

        缓存一次：这个索引要在每条消息上做命中判断，重扫会慢到不可接受。
        """
        if self._cache_idx is not None:
            return self._cache_idx
        root = self._cache_root()
        idx: Dict[str, List[Tuple[str, str, str]]] = {}
        if not root:
            return idx
        for folder, kind in MEDIA_DIRS:
            base = os.path.join(root, folder)
            if not os.path.isdir(base):
                continue
            for r, _d, fs in os.walk(base):
                md5 = os.path.basename(r).lower()
                if not re.fullmatch(r"[0-9a-f]{32}", md5):
                    md5 = ""
                for x in fs:
                    if x.startswith("."):
                        continue
                    p = os.path.join(r, x)
                    key = md5 or os.path.splitext(x)[0].lower()
                    idx.setdefault(key, []).append((p, kind, folder))
        self._cache_idx = idx
        return idx

    def _persist_media(self, media_refs, used_md5: set):
        """把消息引用到的缓存文件落库（按 md5 配对）。"""
        idx = self._cache_index()
        if not self._idx_ready:
            self.stats["media_files"] += sum(len(v) for v in idx.values())
            self._idx_ready = True
        claimed = set()
        for (mpk, cid, ts, md5) in media_refs:
            for (path, kind, folder) in idx.get(md5, []):
                if (path, mpk) in claimed:
                    continue
                claimed.add((path, mpk))
                self._add_media(path, kind, folder, md5=md5, message_pk=mpk,
                                chat_id=cid, ts=ts)
        self._close_media()

    def _persist_unmatched_media(self):
        """本机缓存里存在、但没有任何消息引用到的文件也入库（一个不落）。"""
        idx = self._cache_index()
        unmatched = 0
        for md5, items in idx.items():
            for (path, kind, folder) in items:
                if path in self._claimed_paths:
                    continue
                unmatched += 1
                self._add_media(path, kind, folder, md5=md5, message_pk=None,
                                chat_id=None, ts=None,
                                reason="本机缓存存在，但没有消息引用到它")
        self._close_media()
        if unmatched:
            self.log("     未被引用的缓存文件 %d 个也一并入库" % unmatched)

    @property
    def _claimed_paths(self) -> set:
        """所有已经写进 media_items 的缓存文件路径（避免重复落库）。"""
        if not hasattr(self, "_claimed_path_set"):
            self._claimed_path_set = set()
        return self._claimed_path_set

    def _add_media(self, path: str, kind: str, folder: str, *, md5: str = "",
                   message_pk=None, chat_id=None, ts=None, reason=""):
        """写一行 ``media_items``。**不在这里 commit**，由调用方统一提交。

        每行都 commit 会让 5.6GB 的媒体落库变成上千次 fsync（实测拖慢 20 倍），
        所以调用方用 ``_close_media()`` 收尾。
        """
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if not size and not reason:
            reason="缓存文件为空或无法读取"
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        is_thumb = "Thumbnail" in folder
        row = {
            "platform": "wecom", "account_key": self.current_account,
            "message_pk": message_pk, "chat_id": chat_id,
            "ts": ts, "msg_kind": "thumb" if is_thumb else kind,
            "media_role": "thumb" if is_thumb else "original",
            "file_name": _safe_text(os.path.basename(path), 512) or None,
            "ext": _safe_text(ext, 32) or None, "mime": silk.mime_for(ext),
            "bytes": size or None, "md5": _safe_text(md5, 32) or None,
            "sha1": None, "width": None, "height": None,
            "duration_ms": None, "source_kind": "cache:%s" % folder,
            "source_path": _safe_text(path, 1024) or None,
            "source_key": _safe_text(md5 or os.path.basename(path), 255) or None,
            "unavailable": 0 if size else 1,
            "reason": _safe_text(reason, 512) or None,
            "is_thumb": 1 if is_thumb else 0, "data": None,
            "needs_decode": 0, "decoded_mime": None, "decoded_data": None,
        }
        if size and kind == "voice" and self.decode_voice:
            try:
                with open(path, "rb") as fh:
                    blob = fh.read()
                row["sha1"] = hashlib.sha1(blob).hexdigest()
                got = silk.decode_to_wav(blob)
                if got:
                    wav, sr, dur = got
                    row["decoded_mime"] = "audio/wav"
                    row["decoded_data"] = wav
                    row["duration_ms"] = dur
                    self.stats["voice_decoded"] += 1
                    row["data"] = blob
                else:
                    row["needs_decode"] = 1
                    row["reason"] = "SILK 解码失败，保留原始字节"
                    row["data"] = blob
                    self.stats["voice_failed"] += 1
            except OSError:
                pass
        if my.blob_is_chunked(size):
            self.stats["blobs_chunked"] += 1
            row["reason"] = _safe_text((row["reason"] + " " if row["reason"] else "")
                                       + "[大文件分片存 blob_parts]", 512)
            row["sha1"] = row["sha1"] or self._sha1_file(path)
            pk=my.insert_blob_parts(self.db, "media_items", row, "data", path,
                                 commit=False)
        else:
            data = row.get("data")
            if data is None and size:
                try:
                    with open(path, "rb") as fh:
                        data = fh.read()
                except OSError:
                    data = None
                    row["unavailable"]=1
                    row["reason"]="缓存文件存在但读取失败"
            row["data"] = data
            if data is not None:
                row["sha1"] = row["sha1"] or hashlib.sha1(data).hexdigest()
            pk=my.write_media(self.db, row)
        self._written_media.add(pk)
        if size:
            self.stats["media_ok"] += 1
            self.stats["media_bytes"] += size
        else:
            self.stats["media_unavailable"] += 1
        self._claimed_paths.add(path)

    def _close_media(self):
        """媒体批量告一段落，统一提交。"""
        self.db.commit()

    @staticmethod
    def _sha1_file(path: str) -> Optional[str]:
        h = hashlib.sha1()
        try:
            with open(path, "rb") as fh:
                for buf in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(buf)
        except OSError:
            return None
        return h.hexdigest()

    # ------------------------------------------------------------------ #
    def _save_source(self, dataset, db_name, tables, convs):
        acct = self.current_account
        with self.db.cursor() as cur:
            for table in tables:
                cur.execute("SELECT COUNT(*) AS n FROM `messages` WHERE "
                            "`source_dataset`=%s AND `source_db`=%s AND "
                            "`source_table`=%s", (dataset, db_name, table))
                n = cur.fetchone()["n"]
                if not n:
                    continue
                cur.execute("DELETE FROM import_sources WHERE platform=%s AND account_key=%s AND dataset=%s AND source_db=%s AND source_table=%s", ("wecom", acct, dataset, db_name, table))
                cur.execute(
                    "INSERT INTO `import_sources` (`platform`,`account_key`,"
                    "`dataset`,`source_db`,`source_table`,`source_rows`,"
                    "`inserted_rows`,`missing_rows`,`note`) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,0,%s) "
                    "ON DUPLICATE KEY UPDATE `source_rows`=VALUES(`source_rows`),"
                    "`inserted_rows`=VALUES(`inserted_rows`)",
                    ("wecom", acct, dataset, db_name, table, n, n,
                     "source_rows 由 message.db 表行数核对"))
        self.db.commit()

    # ------------------------------------------------------------------ #
    def reconcile(self):
        acct = self.account_key or self._guess_account()
        src_total = my.scalar(self.db, "SELECT COUNT(*) FROM `messages`")
        rows = [("total", "wecom", "messages", src_total, src_total,
                 "ok", "message_table 全量入库")]
        # 用 message.db 的真实行数再核一遍
        try:
            from pathlib import Path as _P
            n_src = 0
            for d in self.dataset_dirs():
                files = {p.name: p for p in _P(d).rglob("*.db")}
                if "message.db" not in files:
                    continue
                c = wmsg.open_ro(files["message.db"])
                for t in wmsg.tables(c):
                    if t == "message_table":
                        n_src += c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            n_dest = my.scalar(self.db, "SELECT COUNT(*) FROM `messages` WHERE "
                                        "`source_dataset` NOT LIKE '%%Backup%%'")
            rows.append(("db", "message.db", "message_table", n_src, n_dest,
                         "ok" if n_src == n_dest else "error",
                         "主库 message_table 行数 vs 入库行数"))
        except Exception as exc:                            # noqa: BLE001
            rows.append(("db", "message.db", "count", 0, 0, "warn",
                         "行数核对失败：%s" % exc))
        n_link_msg = my.scalar(self.db, "SELECT COUNT(*) FROM `messages` "
                                        "WHERE `has_link`=1")
        n_link_pk = my.scalar(self.db, "SELECT COUNT(DISTINCT `message_pk`) "
                                       "FROM `message_links`")
        rows.append(("link", "wecom", "messages_with_link", n_link_msg, n_link_pk,
                     "ok" if n_link_msg == n_link_pk else "warn",
                     "带链接的消息数 vs 链接表覆盖的消息数"))
        with self.db.cursor() as cur:
            cur.execute("DELETE FROM `import_reconciliation`")
            cur.executemany(
                "INSERT INTO `import_reconciliation` (`platform`,`scope`,`name`,"
                "`metric`,`source_value`,`dest_value`,`diff`,`status`,`detail`) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [("wecom",) + r[:5] + ((r[4] - r[3]),) + r[5:] for r in rows])
        self.db.commit()
        return rows


def run(**kw):
    from localio import task_lock
    with task_lock(os.path.join(os.path.dirname(kw.get("raw_root") or wconfig.RAW),".snapshot.lock")), \
         task_lock(os.path.join(os.path.dirname(kw.get("plain_root") or wconfig.PLAIN),".extract.lock")):
        imp = WecomImporter(**kw)
        try:
            return imp.run()
        finally:
            imp.db.close()
