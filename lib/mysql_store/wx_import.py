"""个人微信 → MySQL（``wx_chat``）的全量入库。

复用 ``lib/wxexport`` 里已经验证过的读库/解析逻辑（``keys`` / ``parse`` /
``names`` / ``export.Exporter``），只在外面套一层 MySQL 写入，**不改那些文件**。

零遗漏的落点
------------
1. 消息：每个 ``Msg_<md5>`` 表的每一行都要进 ``messages``；
   ``message_0.db`` / ``message_1.db`` 是时间分片，靠唯一键
   ``(chat_pk, source_db, source_table, local_id)`` 各自成行，不被去重掉。
2. 反查不到会话的孤立表：建「孤儿会话」（``is_orphan=1``）把消息装进去，不丢。
3. 媒体：图片走 ``msg/attach`` 解密配对；文件/视频走 ``hardlink.db`` + 文件系统；
   语音走 ``media_0.db.VoiceInfo``（并尝试 SILK 解码成 WAV）。
   配不到的一律写 ``unavailable=1`` + ``reason``，绝不静默丢弃。
4. 链接：正文里所有 ``http(s)://`` 与 appmsg 的 ``url`` / ``href`` / ``cdnurl``。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple
from localio import iter_rows

from wxexport import config, keys, media as wxmedia, names, parse

from . import conn as my
from . import silk, wx_media, wx_refs

URL_RE = re.compile(r"https?://[^\s\"'<>\\\x00-\x1f]+")
# 需要落媒体的消息类型
MEDIA_KINDS = ("image", "sticker", "file", "video", "voice")


def _u8(*vals) -> str:
    """把任意值压成数据库能接受的非空字符串（微信里偶发 NUL 与控制字符）。"""
    out = []
    for v in vals:
        if v is None:
            continue
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        s = str(v).replace("\x00", "")
        if s.strip():
            out.append(s)
    return " ".join(out).strip()


def _safe_text(s, limit: int = 0) -> str:
    if s is None:
        return ""
    if isinstance(s, bytes):
        s = s.decode("utf-8", "replace")
    s = str(s).replace("\x00", "")
    return s[:limit] if limit else s



def decode_message(self, row, has_ct, cid, pk, db_name, tbl, senders):
    if has_ct: lid,lt,rsid,ts,raw,svr,seq,ct=row
    else: lid,lt,rsid,ts,raw,svr,seq=row;ct=None
    ts=int(ts or 0);lid=int(lid)
    original=parse.decode_content(raw,ct)
    sender,payload=parse.split_sender(original)
    sender=sender or senders.get(int(rsid or 0)) or "系统"
    if sender==cid or sender.endswith("@chatroom"):sender="系统"
    kind,text,refs=parse.describe(int(lt)&parse.TYPE_MASK,payload)
    refs=wx_refs.enrich_refs(kind,payload,refs)
    links=self._extract_links(payload,text)
    disp="我" if sender==self.self_wxid else self.dir.display(sender,cid)
    main=("wechat",self.self_wxid or "unknown",pk,cid,lid,_safe_text(svr,64) or None,
          int(seq) if seq is not None else None,int(lt or 0),kind,_safe_text(sender,320),
          _safe_text(disp,512),int(rsid) if rsid is not None else None,ts,self._fmt(ts),
          _safe_text(text) or None,str(int(lt)&parse.TYPE_MASK),"wechat-snapshot",db_name,tbl,
          int(bool(refs) or kind in MEDIA_KINDS),int(bool(links)))
    blob=bytes(raw) if isinstance(raw,(bytes,bytearray)) else (str(raw).encode() if raw is not None else None)
    return main,(original,ct,len(blob or b""),"zstd" if ct==4 else "plain",blob),links,refs,sender,kind,ts,lid

class WxImporter:
    def __init__(self, paths: config.Paths, *, limit: int = 0, chat: str = "",
                 skip_media: bool = False, decode_voice: bool = True,
                 verbose: bool = True, database: str = "wx_chat"):
        self.p = paths
        self.limit = limit
        self.only_chat = chat
        self.skip_media = skip_media
        self.decode_voice = decode_voice
        self.verbose = verbose

        self.kr = keys.Keyring(self.p.capture)
        self.dir = names.Directory(self.p.contact_db(), self.p.capture)
        self.v2key = config.media_v2_key(self.p)
        self.self_wxid = self.p.wxid or ""
        self.db = my.connect(database)
        self.media_root = wx_media.MediaRoot(
            self.p.msg,
            os.path.join(self.p.db_storage, "hardlink", "hardlink.db"),
            self.p.capture)
        voice_path = os.path.join(self.p.db_storage, "message", "media_0.db")
        self.voice_conn = self.kr.connect(voice_path) if os.path.exists(voice_path) else None

        self.chat_pk: Dict[str, int] = {}
        # 来源归属由本轮完整枚举确定；过滤不能把正常表变成孤儿。
        # 早先是回头查 MySQL 的 import_sources，但那张表会被上一轮跑剩的行污染
        # （甚至被中断的一轮），于是真会話全被判成孤儿、整批重复入库（实测多灌 75 万条）。
        self.covered_sources: set = set()
        self.media_pk: Dict[Tuple[int, str, str], int] = {}
        self.global_img = None       # wx_media.GlobalImageIndex，run() 里建
        self.stats = dict(chats=0, messages=0, links=0, media_ok=0, media_missing=0,
                          media_bytes=0, voice_decoded=0, voice_failed=0,
                          orphan_chats=0, orphan_messages=0)
        self._hardlink_loaded = False

    def log(self, msg: str):
        if self.verbose:
            print(msg, flush=True)

    # ================================================================== #
    # 账号 / 联系人 / 会话
    # ================================================================== #
    def upsert_account(self):
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO `accounts` (`platform`,`account_key`,`display_name`,"
                "`alias`,`uin`,`app_version`,`snapshot_time`,`snapshot_note`) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                "`display_name`=VALUES(`display_name`),`uin`=VALUES(`uin`),"
                "`snapshot_time`=VALUES(`snapshot_time`)",
                ("wechat", self.self_wxid or "unknown",
                 _safe_text(self.p.nickname, 255) or None, None, self.p.uin, None,
                 self.p.cloned_at or None,
                 "本地 cp -Rc 快照；图片 AES key = md5(uin+wxid)[:16]"))
        self.db.commit()

    def _all_contacts(self):
        """``contact`` + ``stranger`` 的并集（备注可能只在其中一张里）。"""
        seen: Dict[str, dict] = {}
        c = self.kr.connect(self.p.contact_db())
        for tbl in ("contact", "stranger"):
            try:
                for u, nick, remark, alias in c.execute(
                        "SELECT username, nick_name, remark, alias FROM %s" % tbl):
                    u = _u8(u)
                    if not u:
                        continue
                    row = seen.setdefault(u, {"alias": "", "nick": "", "remark": ""})
                    row["alias"] = row["alias"] or _u8(alias)
                    row["nick"] = row["nick"] or _u8(nick)
                    row["remark"] = row["remark"] or _u8(remark)
            except Exception:
                continue
        return seen

    def import_contacts(self):
        acct = self.self_wxid or "unknown"
        seen = self._all_contacts()
        with self.db.cursor() as cur:
            cur.executemany(
                "INSERT INTO `contacts` (`platform`,`account_key`,`contact_id`,"
                "`alias`,`nick_name`,`remark`) VALUES (%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE `alias`=VALUES(`alias`),"
                "`nick_name`=VALUES(`nick_name`),`remark`=VALUES(`remark`)",
                [("wechat", acct, u, _safe_text(v["alias"], 191) or None,
                  _safe_text(v["nick"], 255) or None,
                  _safe_text(v["remark"], 255) or None)
                 for u, v in seen.items()])
        self.db.commit()
        self.log("   联系人 %d 条" % len(seen))

    def ensure_chat(self, chat: Dict) -> int:
        cid = chat["chat_id"]
        if cid in self.chat_pk:
            return self.chat_pk[cid]
        acct = self.self_wxid or "unknown"
        is_room = bool(chat.get("is_room"))
        kind = "群聊" if is_room else ("孤儿" if chat.get("is_orphan") else "单聊")
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO `chats` (`platform`,`account_key`,`chat_id`,`is_room`,"
                "`kind`,`title`,`owner_id`,`member_count`,`is_orphan`) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                "`title`=VALUES(`title`),`member_count`=VALUES(`member_count`),"
                "`is_room`=VALUES(`is_room`),`owner_id`=VALUES(`owner_id`),"
                "`is_orphan`=VALUES(`is_orphan`)",
                ("wechat", acct, cid, 1 if is_room else 0, kind,
                 _safe_text(chat.get("title"), 512) or None,
                 _safe_text(chat.get("owner"), 320) or None,
                 chat.get("members") or None, 1 if chat.get("is_orphan") else 0))
            cur.execute("SELECT `chat_pk` FROM `chats` WHERE `platform`='wechat' "
                        "AND `account_key`=%s AND `chat_id`=%s", (acct, cid))
            got = cur.fetchone()
        self.db.commit()
        pk = int(list(got.values())[0])
        self.chat_pk[cid] = pk
        return pk

    def import_members(self, chat: Dict, sender_counts: Dict[str, int],
                       first_last: Dict[str, Tuple[int, int]]):
        cid = chat["chat_id"]
        pk = self.chat_pk[cid]
        member_ids = set(chat.get("member_ids") or [])
        everyone = member_ids | set(sender_counts)
        everyone.discard(cid)
        if not everyone:
            return
        owner = chat.get("owner") or ""
        rows = []
        for wxid in sorted(everyone):
            prof = self.dir.profile(wxid, cid)
            fl = first_last.get(wxid, (None, None))
            rows.append((
                "wechat", pk, cid, wxid,
                _safe_text(prof.get("room_nick"), 255) or None,
                _safe_text(prof.get("nick"), 255) or None,
                _safe_text(prof.get("remark"), 255) or None,
                _safe_text(prof.get("alias"), 191) or None,
                _safe_text(prof.get("shown"), 512) or None,
                "我" if wxid == self.self_wxid else
                ("群主" if owner and wxid == owner else "成员"),
                sender_counts.get(wxid, 0), fl[0], fl[1],
                1 if wxid in member_ids else 0,
            ))
        with self.db.cursor() as cur:
            cur.executemany(
                "INSERT INTO `chat_members` (`platform`,`chat_pk`,`chat_id`,"
                "`contact_id`,`room_nick`,`nick_name`,`remark`,`alias`,`shown`,"
                "`role`,`message_count`,`first_ts`,`last_ts`,`in_member_list`) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE `room_nick`=VALUES(`room_nick`),"
                "`nick_name`=VALUES(`nick_name`),`remark`=VALUES(`remark`),"
                "`alias`=VALUES(`alias`),`shown`=VALUES(`shown`),`role`=VALUES(`role`),"
                "`message_count`=VALUES(`message_count`),`first_ts`=VALUES(`first_ts`),"
                "`last_ts`=VALUES(`last_ts`),`in_member_list`=VALUES(`in_member_list`)",
                rows)
        self.db.commit()

    # ================================================================== #
    # 消息
    # ================================================================== #
    def _sender_map(self, c) -> Dict[int, str]:
        """``Name2Id`` 的 rowid → user_name。

        注意：``keys.Keyring.connect`` 打开的是**原生 tuple 行**（没有 row_factory），
        所以这里只能按下标取，不能写 ``r["user_name"]``。
        """
        try:
            return {int(r[0]): _u8(r[1]) for r in c.execute(
                "SELECT rowid, user_name FROM Name2Id")}
        except Exception:
            return {}

    def _voice_for(self, local_id: int, ts: int) -> Optional[bytes]:
        """``media_0.db.VoiceInfo`` 按 (local_id, create_time) 取语音字节。

        ⚠️ 这是 **SQLite**（sqlcipher3），占位符必须是 ``?`` 而不是 MySQL 的 ``%s``。
        写 ``%s`` 会抛 ``near "%": syntax error``，而这里又被 try/except 包着，
        于是 3,722 条语音全部**静默**变成 None（实测踩过，报告里表现为
        "语音落库 0 条"）。出问题时必须能看见，所以第一次报错要打日志。
        """
        if self.voice_conn is None:
            return None
        try:
            row = self.voice_conn.execute(
                "SELECT voice_data FROM VoiceInfo WHERE local_id=? AND "
                "create_time=? LIMIT 1", (local_id, ts)).fetchone()
            if row and row[0]:
                return row[0]
            return None
        except Exception as exc:
            if not getattr(self, "_voice_err_logged", False):
                self._voice_err_logged = True
                self.log("   ! 语音查询失败（后续不再重复打印）：%s" % exc)
            return None

    def import_chat(self, chat: Dict) -> dict:
        cid = chat["chat_id"]
        pk = self.ensure_chat(chat)
        idx = None
        if not self.skip_media and chat.get("tables"):
            d = self.p.chat_attach_dir(cid)
            if os.path.isdir(d) and self.v2key:
                t0 = time.time()
                idx = wx_media.ChatMediaIndex(d, self.v2key).build()
                self.log("   图片素材解出 %d 个（失败 %d，%.0fs）"
                         % (len(idx.by_md5), idx.failed, time.time() - t0))

        self._written_media=set()
        sender_counts: Dict[str, int] = {}
        first_last: Dict[str, Tuple[int, int]] = {}
        media_refs: List[Tuple] = []
        chat_min, chat_max = None, None
        src_dbs = set()

        for db_path, tbl in chat["tables"]:
            db_name = os.path.basename(db_path)
            src_dbs.add(db_name)
            self.covered_sources.add((db_name, tbl))
            src = self._import_table(chat, db_path, tbl, pk, sender_counts,
                                     first_last, media_refs)
            if src["min_ts"] is not None:
                chat_min = src["min_ts"] if chat_min is None else min(chat_min, src["min_ts"])
            if src["max_ts"] is not None:
                chat_max = src["max_ts"] if chat_max is None else max(chat_max, src["max_ts"])
            self._save_source(chat, db_name, tbl, src)

        self.import_members(chat, sender_counts, first_last)
        if media_refs:
            self._persist_media(media_refs, idx)
        if not self.skip_media:
            rows=my.query(self.db,"SELECT i.id FROM media_items i JOIN messages m ON m.id=i.message_pk WHERE m.chat_pk=%s",(pk,))
            my.delete_media(self.db,[r['id'] for r in rows if r['id'] not in self._written_media])
            self.db.commit()
        self._finalize_chat(chat, chat_min, chat_max, src_dbs)
        self.stats["chats"] += 1
        return {"chat_id": cid, "messages": sum(sender_counts.values())}

    def _import_table(self, chat, db_path, tbl, pk, sender_counts,
                      first_last, media_refs) -> dict:
        cid = chat["chat_id"]
        out = dict(source_rows=0, inserted=0, min_ts=None, max_ts=None, links=0)
        c = self.kr.connect(db_path)
        senders = self._sender_map(c)
        cols = ("local_id, local_type, real_sender_id, create_time, "
                "message_content, server_id, sort_seq")
        has_ct = True
        try:
            c.execute("SELECT WCDB_CT_message_content FROM %s LIMIT 1" % tbl)
        except Exception:
            has_ct = False
        if has_ct:
            cols += ", WCDB_CT_message_content"
        try:
            rows = c.execute("SELECT %s FROM %s ORDER BY create_time, local_id"
                             % (cols, tbl))
        except Exception as exc:
            self.log("   ! 读表失败 %s：%s" % (tbl, exc))
            raise RuntimeError("读表失败 %s/%s" % (db_path, tbl)) from exc

        db_name=os.path.basename(db_path)
        msg_sql = (
                        "INSERT INTO `messages` (`platform`,`account_key`,`chat_pk`,"
                        "`chat_id`,`local_id`,`server_id`,`sort_seq`,`local_type`,`kind`,"
                        "`sender_id`,`sender_name`,`real_sender_id`,`ts`,`msg_time`,"
                        "`text`,`content_type`,`source_dataset`,`source_db`,"
                        "`source_table`,`has_media`,`has_link`) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE `text`=VALUES(`text`),`kind`=VALUES(`kind`),"
                        "`sender_name`=VALUES(`sender_name`),`has_media`=VALUES(`has_media`),"
                        "`has_link`=VALUES(`has_link`),`ts`=VALUES(`ts`),`msg_time`=VALUES(`msg_time`),"
                        "`local_type`=VALUES(`local_type`),`content_type`=VALUES(`content_type`),"
                        "`sender_id`=VALUES(`sender_id`),`real_sender_id`=VALUES(`real_sender_id`),"
                        "`server_id`=VALUES(`server_id`),`sort_seq`=VALUES(`sort_seq`)"
        )
        raw_sql = (
                        "INSERT INTO `message_raw` (`message_pk`,`platform`,"
                        "`raw_content`,`compress_type`,`raw_bytes`,`decoded_from`,`raw_blob`) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                        "`raw_content`=VALUES(`raw_content`),`raw_blob`=VALUES(`raw_blob`),"
                        "`compress_type`=VALUES(`compress_type`),`raw_bytes`=VALUES(`raw_bytes`),`decoded_from`=VALUES(`decoded_from`)"
        )
        link_sql = (
                    "INSERT INTO `message_links` (`platform`,`message_pk`,`chat_id`,`ts`,"
                    "`url`,`url_hash`,`title`,`des`,`app_type`,`origin`) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE `title`=VALUES(`title`),"
                    "`des`=VALUES(`des`)"
        )
        source_sql="SELECT %s FROM %s ORDER BY create_time,local_id" % (cols,tbl)

        def decode(row):
            return decode_message(self,row,has_ct,cid,pk,db_name,tbl,senders)

        def flush(sql,buffer):
            if buffer:
                with self.db.cursor() as cur:cur.executemany(sql,buffer)
                self.db.commit();buffer.clear()

        try:
            batch=[]
            for row in iter_rows(rows):
                main,original,links,refs,sender,kind,ts,lid=decode(row)
                batch.append(main);out["source_rows"]+=1
                out["min_ts"]=ts if out["min_ts"] is None else min(out["min_ts"],ts)
                out["max_ts"]=ts if out["max_ts"] is None else max(out["max_ts"],ts)
                sender_counts[sender]=sender_counts.get(sender,0)+1
                previous=first_last.get(sender)
                first_last[sender]=(ts,ts) if previous is None else (min(previous[0],ts),max(previous[1],ts))
                if len(batch)>=2000:flush(msg_sql,batch)
            flush(msg_sql,batch)
            self.stats["messages"]+=out["source_rows"]
            # One source-table ID lookup, preserving the M1 invariant.
            with self.db.cursor() as cur:
                cur.execute("SELECT id,local_id FROM messages WHERE chat_pk=%s AND source_db=%s AND source_table=%s",(pk,db_name,tbl))
                id_map={int(r["local_id"]):int(r["id"]) for r in cur.fetchall()}
                cur.execute("DELETE l FROM message_links l JOIN messages m ON m.id=l.message_pk WHERE m.chat_pk=%s AND m.source_db=%s AND m.source_table=%s",(pk,db_name,tbl))
            out["inserted"]=out["source_rows"]
            raw_rows=[];link_rows=[]
            for row in iter_rows(c.execute(source_sql)):
                main,original,links,refs,sender,kind,ts,lid=decode(row)
                mpk=id_map[lid]
                raw_rows.append((mpk,"wechat",*original))
                for url,title,des,app_type,origin in links:
                    link_rows.append(("wechat",mpk,cid,ts,url,hashlib.sha1(url.encode("utf-8","replace")).hexdigest(),
                                      _safe_text(title,512) or None,_safe_text(des,1024) or None,_safe_text(app_type,32) or None,origin))
                    out["links"]+=1;self.stats["links"]+=1
                if not self.skip_media and kind in MEDIA_KINDS:
                    if kind=="voice":media_refs.append((mpk,cid,ts,kind,"voiceinfo","",lid))
                    elif not refs:media_refs.append((mpk,cid,ts,kind,"none","",lid))
                    else:
                        for ref_kind,md5 in refs:media_refs.append((mpk,cid,ts,kind,ref_kind,md5,lid))
                if len(raw_rows)>=2000:flush(raw_sql,raw_rows)
                if len(link_rows)>=1000:flush(link_sql,link_rows)
            flush(raw_sql,raw_rows);flush(link_sql,link_rows)
            self.db.commit()
            return out
        finally:
            c.close()

    # ================================================================== #
    # 媒体落库
    # ================================================================== #
    def _add_media(self, *, message_pk, chat_id, ts, msg_kind, media_role,
                   data, ext, md5, source_kind, source_path, source_key,
                   is_thumb=False, reason="", file_name="", decode_voice=False,
                   data_path: str = ""):
        """写一行 ``media_items``。

        ``data`` 走内存；``data_path`` 走磁盘（大视频/大文件不整包进内存）。
        超过 ``MAX_INLINE_BLOB`` 的二进制切块进 ``blob_parts`` —— 服务端
        ``max_allowed_packet`` 只有 64MB，实测一次 600MB 的 INSERT 直接
        ``MySQL server has gone away``（断连）。切块的是 8MB，留足 hex 转义余量。
        """
        size = None
        if data is not None:
            size = len(data)
        elif data_path:
            try:
                size = os.path.getsize(data_path)
            except OSError:
                size = None
        if data is None and data_path and size and not my.blob_is_chunked(size):
            with open(data_path,"rb") as fh:
                data=fh.read()
            size=len(data)
        if not size and not reason:
            reason = "本机无缓存"
        w = h = dur = None
        decoded_mime = decoded_data = None
        needs_decode = 0
        if size:
            if msg_kind in ("image", "sticker") and data is not None:
                w, h = wx_media.probe_image(data)
                if data.startswith(b"wxgf"):
                    bitstream, fmt = wxmedia.unwrap_wxgf(data)
                    preview = wxmedia.hevc_to_jpeg(bitstream) if fmt == "wxgf" else bitstream
                    ext = "wxgf"
                    if preview:
                        decoded_mime, decoded_data = "image/jpeg", preview
                        w, h = wx_media.probe_image(preview)
                    else:
                        needs_decode=1
                        reason="wxgf 尚未解码，保留原始字节"
            elif msg_kind == "video":
                dur = wx_media.probe_video_ms(data_path or source_path or "")
            elif msg_kind == "voice" and decode_voice and data is not None:
                got = silk.decode_to_wav(data)
                if got:
                    wav, sr, d = got
                    decoded_mime, decoded_data, dur = "audio/wav", wav, d
                    self.stats["voice_decoded"] += 1
                else:
                    needs_decode = 1
                    self.stats["voice_failed"] += 1

        chunked = bool(size) and my.blob_is_chunked(size)
        row = {
            "platform": "wechat", "account_key": self.self_wxid or "unknown",
            "message_pk": message_pk, "chat_id": chat_id,
            "ts": ts, "msg_kind": msg_kind, "media_role": media_role,
            "file_name": _safe_text(file_name, 512) or None,
            "ext": _safe_text(ext, 32) or None,
            "mime": silk.mime_for(ext) if ext else None,
            "bytes": size,
            "md5": _safe_text(md5, 32) or None,
            "sha1": wx_media.sha1_of(data) if data is not None else
                    (self._sha1_file(data_path) if chunked else None),
            "width": w, "height": h, "duration_ms": dur,
            "source_kind": source_kind or None,
            "source_path": _safe_text(source_path, 1024) or None,
            "source_key": _safe_text(source_key, 255) or None,
            "unavailable": 0 if size else 1,
            "reason": (None if size and not needs_decode
                       else (_safe_text(reason, 512) or
                             ("SILK 解码失败，保留原始字节" if needs_decode
                              else "本机无缓存"))),
            "is_thumb": 1 if is_thumb else 0,
            "data": data if (data is not None and not chunked) else None,
            "needs_decode": needs_decode,
            "decoded_mime": decoded_mime, "decoded_data": decoded_data,
        }
        if chunked:
            row["reason"] = _safe_text(
                (row["reason"] + " " if row["reason"] else "")
                + "[大文件分片存 blob_parts]", 512)
            pk = my.insert_blob_parts(self.db, "media_items", row, "data",
                                      data_path or data, commit=False)
        else:
            pk = my.write_media(self.db, row)
        self.media_pk[(message_pk, msg_kind, md5 or source_key or "")] = pk
        self._written_media.add(pk)
        if size:
            self.stats["media_ok"] += 1
            self.stats["media_bytes"] += size
        else:
            self.stats["media_missing"] += 1
        return pk

    @staticmethod
    def _sha1_file(path: str) -> Optional[str]:
        """大文件按块算 sha1，不整包读进内存。"""
        if not path or not os.path.exists(path):
            return None
        h = hashlib.sha1()
        try:
            with open(path, "rb") as fh:
                for buf in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(buf)
        except OSError:
            return None
        return h.hexdigest()

    def _persist_media(self, media_refs, idx):
        """逐行写 media_items；**收尾统一提交**。

        每条都 commit 会让 1GB+ 图片落库变成上千次 fsync（实测拖慢一个数量级）。
        """
        if not self._hardlink_loaded:
            self.media_root.load_hardlink()
            self._hardlink_loaded = True
        hardlink = self.media_root.load_hardlink()
        for (mpk, cid, ts, kind, ref_kind, ref_md5, local_id) in media_refs:
            if not ref_md5 and kind not in ("voice", "__none__"):
                if kind in MEDIA_KINDS and ref_kind == "none":
                    self._add_media(
                        message_pk=mpk, chat_id=cid, ts=ts, msg_kind=kind,
                        media_role="original", data=None, ext="", md5="",
                        source_kind="none", source_path="", source_key="",
                        reason="消息声明了%s，但正文里没有带任何文件引用（空壳消息）"
                               % kind)
                    continue
                continue
            if kind == "voice":
                blob = self._voice_for(int(local_id or 0), ts)
                if blob:
                    self._add_media(
                        message_pk=mpk, chat_id=cid, ts=ts, msg_kind="voice",
                        media_role="original", data=blob, ext="silk", md5="",
                        source_kind="voiceinfo",
                        source_path="media_0.db/VoiceInfo",
                        source_key="%s@%s" % (local_id, ts),
                        decode_voice=self.decode_voice)
                else:
                    self._add_media(
                        message_pk=mpk, chat_id=cid, ts=ts, msg_kind="voice",
                        media_role="original", data=None, ext="silk", md5="",
                        source_kind="voiceinfo", source_path="media_0.db/VoiceInfo",
                        source_key="%s@%s" % (local_id, ts),
                        reason="VoiceInfo 里没有这条语音")
            elif kind in ("image", "sticker"):
                got = idx.get(ref_md5) if idx else None
                if got is None and kind == "sticker":
                    for ext in ("gif","jpg","png","webp","bmp"):
                        cached=os.path.join(self.p.export,"共享资源","表情包",ref_md5+"."+ext)
                        if os.path.isfile(cached):
                            with open(cached,"rb") as fh:
                                blob=fh.read()
                            if hashlib.md5(blob).hexdigest()==ref_md5:
                                got=(blob,ext,False,cached)
                                break
                if got is None and self.global_img is not None:
                    got = self.global_img.get(ref_md5)
                if got:
                    data, ext, thumb, path = got
                    self._add_media(
                        message_pk=mpk, chat_id=cid, ts=ts, msg_kind=kind,
                        media_role="thumb" if thumb else "original", data=data,
                        ext=ext, md5=ref_md5, source_kind="attach",
                        source_path=path, source_key=ref_md5, is_thumb=thumb,
                        file_name=os.path.basename(path),
                        reason="" if not thumb else "原图未缓存，仅有缩略图")
                elif kind == "sticker":
                    self._add_media(
                        message_pk=mpk, chat_id=cid, ts=ts, msg_kind="sticker",
                        media_role="original", data=None, ext="", md5=ref_md5,
                        source_kind="cdn", source_path="", source_key=ref_md5,
                        reason="本机无该表情缓存（表情来自 CDN，未下载）")
                else:
                    self._add_media(
                        message_pk=mpk, chat_id=cid, ts=ts, msg_kind="image",
                        media_role="original", data=None, ext="", md5=ref_md5,
                        source_kind="attach", source_path="", source_key=ref_md5,
                        reason="本机无缓存（微信只缓存点开过的图片）")
            elif kind in ("file", "video"):
                fname = hardlink.get(ref_md5, "")
                path = self.media_root.find(kind, ref_md5, fname) \
                    or self.media_root.find_any(ref_md5, fname)
                if path:
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        size = 0
                    ext = os.path.splitext(path)[1].lower().lstrip(".")
                    if not ext:
                        try:
                            with open(path, "rb") as fh:
                                head = fh.read(64)
                            ext = wxmedia.plain_ext(head) or "bin"
                        except OSError:
                            ext = "bin"
                    if my.blob_is_chunked(size):
                        # 大文件：按块从磁盘直灌 blob_parts，绝不整包进内存
                        self._add_media(
                            message_pk=mpk, chat_id=cid, ts=ts, msg_kind=kind,
                            media_role="original", data=None, ext=ext, md5=ref_md5,
                            source_kind="hardlink", source_path=path,
                            source_key=ref_md5, file_name=os.path.basename(path),
                            data_path=path)
                    else:
                        try:
                            with open(path, "rb") as fh:
                                data = fh.read()
                        except OSError as exc:
                            self._add_media(
                                message_pk=mpk, chat_id=cid, ts=ts, msg_kind=kind,
                                media_role="original", data=None, ext=ext,
                                md5=ref_md5, source_kind="hardlink",
                                source_path=path, source_key=ref_md5,
                                file_name=os.path.basename(path),
                                reason="文件存在但读取失败：%s" % exc)
                            continue
                        self._add_media(
                            message_pk=mpk, chat_id=cid, ts=ts, msg_kind=kind,
                            media_role="original", data=data, ext=ext, md5=ref_md5,
                            source_kind="hardlink", source_path=path,
                            source_key=ref_md5, file_name=os.path.basename(path))
                else:
                    self._add_media(
                        message_pk=mpk, chat_id=cid, ts=ts, msg_kind=kind,
                        media_role="original", data=None, ext="", md5=ref_md5,
                        source_kind="hardlink", source_path="", source_key=ref_md5,
                        file_name=fname,
                        reason="本机无该%s缓存" % ("文件" if kind == "file" else "视频"))
        self.db.commit()

    # ================================================================== #
    # 链接 / 元信息 / 对账
    # ================================================================== #
    def _extract_links(self, payload: str, text: str):
        out, seen = [], set()

        def push(url, title, des, app_type, origin):
            url = (url or "").strip()
            if not url or not url.lower().startswith(("http://", "https://")):
                return
            key = (url, origin)
            if key in seen:
                return
            seen.add(key)
            out.append((url, title, des, app_type, origin))

        root = parse.xml_root(payload or "")
        if root is not None:
            app = root.find(".//appmsg")
            if app is not None:
                title = (app.findtext("title") or "").strip()
                des = (app.findtext("des") or "").strip()
                sub = (app.findtext("type") or "").strip()
                push((app.findtext("url") or "").strip(), title, des, sub, "appmsg")
                for el in app.iter():
                    if el.tag.lower() in ("href", "cdnurl", "thumburl", "cover",
                                          "src", "lowurl"):
                        push((el.text or "").strip(), title, des, sub, el.tag.lower())
            for el in root.iter():
                if el.tag.lower() in ("url", "href", "cdnurl", "thumburl", "src",
                                      "lowurl"):
                    push((el.text or "").strip(), "", "", "", el.tag.lower())
        for m in URL_RE.finditer(text or ""):
            push(m.group(0).rstrip("）。，、；」』】"), "", "", "", "text")
        for m in URL_RE.finditer(payload or ""):
            push(m.group(0), "", "", "", "raw")
        return out

    @staticmethod
    def _fmt(ts) -> Optional[str]:
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
        except (OSError, ValueError, OverflowError):
            return None

    def _save_source(self, chat, db_name, tbl, src):
        """登记一张源表的行数。

        **先 DELETE 再 INSERT**（而不是 `ON DUPLICATE KEY UPDATE`）：这张表上的
        "同一张源表"可能来自 message_0.db 与 message_1.db 两个库、却共用同一个
        chat_id，靠唯一键去重会把其中一行挤掉（实测少算 message_0.db 的 1 张表、
        数值 126,994 条）。所以幂等只能靠在 Python 侧按
        (platform, account_key, source_db, source_table, chat_id) 显式删。
        """
        key = ("wechat", self.self_wxid or "unknown", db_name, tbl, chat["chat_id"])
        with self.db.cursor() as cur:
            cur.execute(
                "DELETE FROM `import_sources` WHERE `platform`=%s AND "
                "`account_key`=%s AND `source_db`=%s AND `source_table`=%s AND "
                "`chat_id`=%s", key)
            cur.execute(
                "INSERT INTO `import_sources` (`platform`,`account_key`,`dataset`,"
                "`source_db`,`source_table`,`chat_id`,`source_rows`,`inserted_rows`,"
                "`missing_rows`,`min_ts`,`max_ts`,`media_ok`,`media_missing`,"
                "`link_rows`) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                key[:2] + ("wechat-snapshot",) + key[2:] + (src["source_rows"], src["inserted"],
                       max(0, src["source_rows"] - src["inserted"]), src["min_ts"],
                       src["max_ts"], 0, 0, src["links"]))
        self.db.commit()

    def _finalize_chat(self, chat, lo, hi, src_dbs):
        cid = chat["chat_id"]
        pk = self.chat_pk[cid]
        with self.db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT `sender_id`) AS s "
                        "FROM `messages` WHERE `chat_pk`=%s", (pk,))
            r = cur.fetchone()
            cur.execute(
                "UPDATE `chats` SET `message_count`=%s, `sender_count`=%s, "
                "`first_ts`=%s, `last_ts`=%s, `first_time`=%s, `last_time`=%s, "
                "`source_datasets`=%s WHERE `chat_pk`=%s",
                (r["n"], r["s"], lo, hi, self._fmt(lo) if lo else None,
                 self._fmt(hi) if hi else None,
                 json.dumps(sorted(src_dbs), ensure_ascii=False), pk))
        self.db.commit()

    def reconcile(self):
        acct = self.self_wxid or "unknown"
        rows = []

        def add(scope, name, metric, s, d, detail=""):
            try:
                s2, d2 = int(s or 0), int(d or 0)
            except (TypeError, ValueError):
                s2, d2 = 0, 0
            rows.append((scope, name, metric, s2, d2, d2 - s2,
                         "ok" if s2 == d2 else ("error" if scope in
                                                ("total", "db", "chat") else "warn"),
                         detail))

        src_total = my.scalar(self.db, "SELECT COALESCE(SUM(`source_rows`),0) "
                                       "FROM `import_sources`")
        add("total", "wechat", "messages", src_total,
            my.scalar(self.db, "SELECT COUNT(*) FROM `messages`"),
            "import_sources.source_rows 合计 vs messages 行数")
        for db_name in ("message_0.db", "message_1.db", "biz_message_0.db"):
            s = my.scalar(self.db, "SELECT COALESCE(SUM(`source_rows`),0) "
                                   "FROM `import_sources` WHERE `source_db`=%s",
                          (db_name,))
            d = my.scalar(self.db, "SELECT COUNT(*) FROM `messages` "
                                   "WHERE `source_db`=%s", (db_name,))
            if s or d:
                add("db", db_name, "messages", s, d, "逐库对账")
        n_link_msg = my.scalar(self.db, "SELECT COUNT(*) FROM `messages` "
                                        "WHERE `has_link`=1")
        n_link_pk = my.scalar(self.db, "SELECT COUNT(DISTINCT `message_pk`) "
                                       "FROM `message_links`")
        add("link", "wechat", "messages_with_link", n_link_msg, n_link_pk,
            "带链接的消息数 vs 链接表覆盖的消息数")
        with self.db.cursor() as cur:
            cur.execute("DELETE FROM import_reconciliation WHERE platform=%s", ("wechat",))
            cur.executemany(
                "INSERT INTO `import_reconciliation` (`platform`,`scope`,`name`,"
                "`metric`,`source_value`,`dest_value`,`diff`,`status`,`detail`) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [("wechat",) + r for r in rows])
        self.db.commit()
        return rows

    # ================================================================== #
    def run(self):
        with my.import_session(self.db,"wechat",dict(chat=self.only_chat,limit=self.limit),not self.skip_media) as run_id:
            self.run_id=run_id
            return self._run()

    def _run(self):
        from wxexport import export as wxexport_export
        E = wxexport_export.Exporter(self.p, verbose=False)
        chats = E.enumerate_chats()
        self.known_sources = {(os.path.basename(p), t) for c in chats for p, t in c["tables"]}
        if self.only_chat:
            chats = [c for c in chats if c["chat_id"] == self.only_chat]
        if self.limit:
            chats = sorted(chats, key=lambda c: -c["count"])[:self.limit]
        if self.only_chat and not chats:
            raise ValueError("没有找到指定会话："+self.only_chat)
        scope=dict(chat=self.only_chat,limit=self.limit,account=self.self_wxid,
                   sources=[(os.path.basename(p),t) for chat in chats for p,t in chat["tables"]])
        my.execute(self.db,"UPDATE import_runs SET scope=%s WHERE id=%s",(json.dumps(scope),self.run_id))
        self.db.commit()
        self.log("发现 %d 个会话，共 %d 条消息"
                 % (len(chats), sum(c["count"] for c in chats)))

        self.upsert_account()
        self.import_contacts()

        # 关于"跨会话图片索引"的实测结论（别再重复试一遍）：
        # 我一度以为图片会被转发到别的会话目录里，于是建了一张全局 md5→路径 表
        # （15,981 个 md5，58 秒）。抽查最近 70 个会话的 2,996 条图片消息：
        #   本会话目录命中 168，**跨会话命中 0**，剩余 2,828 条本机确实没有。
        # 也就是说这台机器的图片缓存严格只落在"当时打开它的那个会话目录"里，
        # 全局索引零增益却要额外 1 分钟，所以不建它。真要重开就把
        # wx_media.GlobalImageIndex 接回下面的 _persist_media 里。
        for i, chat in enumerate(chats, 1):
            self.log("[%d/%d] %s（%d 条）"
                     % (i, len(chats), _safe_text(chat.get("title") or chat["chat_id"], 60),
                        chat["count"]))
            try:
                self.import_chat(chat)
                if chat.get("is_orphan"):
                    self.stats["orphan_chats"] += 1
                    self.stats["orphan_messages"] += chat["count"]
            except Exception as exc:
                import traceback
                traceback.print_exc()
                self.log("   ! 会话失败 %s：%s" % (chat["chat_id"], exc))
                self.db.rollback()
                raise
        # enumerate_chats includes genuine orphans; filters never expand scope.
        if not self.only_chat and not self.limit and not self.skip_media:
            self._import_unmatched_files()
        self.reconcile()
        return self.stats

    def _import_unmatched_files(self):
        """Preserve plaintext cached files not referenced by any message, by content."""
        covered={r['sha1'] for r in my.query(self.db,"SELECT sha1 FROM media_items WHERE account_key=%s AND unavailable=0",(self.self_wxid,))}
        self._written_media=set()
        for folder in ('file','video'):
            for root,dirs,files in os.walk(os.path.join(self.p.msg,folder)):
                dirs.sort()
                for name in sorted(files):
                    path=os.path.join(root,name)
                    if name.startswith('.') or os.path.islink(path):continue
                    digest=self._sha1_file(path)
                    if digest in covered:continue
                    if digest is None:raise RuntimeError("无法读取快照媒体："+path)
                    ext=wx_media.guess_ext(path)
                    kind='image' if ext in ('jpg','jpeg','png','gif','webp') else folder
                    self._add_media(message_pk=None,chat_id=None,ts=None,msg_kind=kind,
                        media_role='attachment',data=None,ext=ext,md5='',source_kind='snapshot-file',
                        source_path=path,source_key=digest,file_name=name,data_path=path)
                    covered.add(digest)
        self.db.commit()

    def _import_orphans(self, known_chats):
        """反查不到会话的 ``Msg_*`` 表：建孤儿会话把消息装进去，一条都不丢。

        判定"已覆盖"用的是**本轮内存里记下的** ``covered_sources``，
        绝不查 ``import_sources`` 表（那张表会被上一轮的行污染，见
        ``self.covered_sources`` 的注释）。
        """
        covered = set(getattr(self, "known_sources", self.covered_sources))
        dbs = [(p, False) for p in self.p.message_dbs()] + \
              [(p, True) for p in self.p.biz_message_dbs()]
        n = 0
        for db_path, _is_biz in dbs:
            db_name = os.path.basename(db_path)
            c = self.kr.connect(db_path)
            tbls = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'Msg_%'")]
            for t in tbls:
                if (db_name, t) in covered:
                    continue
                try:
                    cnt = c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                except Exception:
                    continue
                if not cnt:
                    continue
                # 用表名里 24 位 hex 做后缀：之前只取 8 位（t[4:12]）时
                # 不同表有概率撞成同一个虚拟会话 ID，会把两份记录混到一起。
                virtual = "ORPHAN_%s_%s" % (db_name.replace(".db", ""), t[4:28])
                chat = {"chat_id": virtual, "is_room": False, "is_orphan": True,
                        "title": "孤立消息表 %s/%s" % (db_name, t),
                        "tables": [(db_path, t)], "count": cnt, "owner": "",
                        "members": 0, "member_ids": []}
                self.log("   孤儿表 %s/%s：%d 条" % (db_name, t, cnt))
                self.import_chat(chat)
                self.stats["orphan_chats"] += 1
                self.stats["orphan_messages"] += cnt
                n += cnt
        if n:
            self.log("   孤立消息合计 %d 条，已全部入库" % n)


def run(paths=None, **kw):
    p = paths or config.load(require_snapshot=True)
    from localio import task_lock
    with task_lock(os.path.join(p.data,".snapshot.lock")):
        imp = WxImporter(p, **kw)
        try:
            return imp.run()
        finally:
            if imp.voice_conn is not None:
                imp.voice_conn.close()
            imp.db.close()
