"""把快照里的全部会话导出成可读文件。

对每个会话做的事情：
    读消息 → 解压/解 XML → 解析发送者 → 配对本地图片 → 生成 HTML/TXT/CSV
并把所有会话汇总成一个索引页 ``export/index.html``。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
import html
from typing import Dict, List, Optional, Tuple

from . import config, keys, media, names, parse, render
from .refs import enrich_refs
from localio import atomic_json, task_lock, staged_directory, iter_rows

# 生成文件名时保留哪些字符
_NAME_OK = re.compile(r"[^\w\u4e00-\u9fff-]", re.U)


def _fmt(ts) -> str:
    try:
        return _dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "1970-01-01 00:00:00"


class Exporter:
    def __init__(self, paths: config.Paths, *, force: bool = False,
                 with_media: bool = True, min_messages: int = 1,
                 verbose: bool = True):
        self.p = paths
        self.force = force
        self.with_media = with_media
        self.min_messages = min_messages
        self.verbose = verbose

        self.kr = keys.Keyring(self.p.capture)
        self.dir = names.Directory(self.p.contact_db(), self.p.capture)
        self.self_wxid = self.p.wxid or ""
        self.self_name = "我"
        self.v2key = config.media_v2_key(self.p)

        self.manifest_path = os.path.join(self.p.export, "_manifest.json")
        self.manifest = self._load_manifest()
        if self.manifest.get("account") not in (None, self.self_wxid):
            raise ValueError("输出目录属于另一个账号，请指定独立 --out")
        self.manifest["account"] = self.self_wxid
        self.manifest["version"] = 2
        self.layout = render.Layout(self.p.ensure_export(), self.manifest["chats"])
        self.sticker_store = self.layout.sticker_dir
        self.p._resource_resolver = None
        self._fingerprint_globals = None

        self.stats = {"chats": 0, "messages": 0, "images": 0, "image_missing": 0,
                      "stickers": 0, "sticker_missing": 0, "thumbs": 0,
                      "attachments": 0}

    # ------------------------------------------------------------------ #
    def _load_manifest(self) -> Dict:
        if os.path.exists(self.manifest_path):
            try:
                with open(self.manifest_path, encoding="utf-8") as f:
                    result=json.load(f)
                if not isinstance(result,dict) or not isinstance(result.get("chats"),dict):
                    raise ValueError("完成清单结构无效")
                if result["chats"] and not result.get("account"):
                    index=Path(self.p.export)/"index.html"
                    match=re.search(r"账号\s+([^<\s]+)</div>",index.read_text(encoding="utf-8")) if index.exists() else None
                    if not match or html.unescape(match.group(1))!=self.self_wxid:
                        raise ValueError("旧清单无法验证所属账号，请使用独立输出目录")
                    result["account"]=self.self_wxid
                return result
            except (OSError, ValueError) as exc:
                raise ValueError("完成清单无法安全读取；旧导出保留：%s" % exc) from exc
        return {"chats": {}}

    def _save_manifest(self):
        atomic_json(self.manifest_path, self.manifest)

    def log(self, msg: str):
        if self.verbose:
            print(msg, flush=True)

    # ------------------------------------------------------------------ #
    def _sender_map(self, conn) -> Dict[int, str]:
        try:
            return {r[0]: r[1] for r in conn.execute("SELECT rowid, user_name FROM Name2Id")}
        except Exception:
            return {}

    def enumerate_chats(self) -> List[Dict]:
        with task_lock(os.path.join(self.p.data,".snapshot.lock")):
            return self._enumerate_chats()

    def _enumerate_chats(self) -> List[Dict]:
        self._fingerprint_globals = None
        """找出所有有消息的会话。"""
        # 1) 候选 chat_id（用来把 Msg_<md5> 反查回会话）
        candidates = set()
        conn = self.kr.connect(self.p.session_db())
        for (u,) in conn.execute("SELECT username FROM SessionTable"):
            if u:
                candidates.add(str(u))
        try:
            for (u,) in conn.execute("SELECT user_name FROM Name2Id"):
                if u:
                    candidates.add(str(u))
        except Exception:
            pass

        cconn = self.kr.connect(self.p.contact_db())
        for tbl in ("contact", "chat_room", "stranger"):
            try:
                for (u,) in cconn.execute("SELECT username FROM %s" % tbl):
                    if u:
                        candidates.add(str(u))
            except Exception:
                pass

        by_md5 = {}
        for u in candidates:
            by_md5["Msg_" + hashlib.md5(u.encode()).hexdigest()] = u

        # 2) session 表里的元信息
        sess = {}
        for u, last in conn.execute(
                "SELECT username, last_timestamp FROM SessionTable"):
            if u:
                sess[str(u)] = int(last or 0)

        # 3) 扫所有消息库，统计每个表的条数与时间范围
        found: Dict[str, Dict] = {}
        dbs = [(p, False) for p in self.p.message_dbs()] + \
              [(p, True) for p in self.p.biz_message_dbs()]
        for db_path, is_biz in dbs:
            try:
                c = self.kr.connect(db_path)
            except Exception as e:
                raise RuntimeError("打不开消息库 %s" % db_path) from e
            tbls = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
            for t in tbls:
                try:
                    n, lo, hi = c.execute(
                        "SELECT COUNT(*), MIN(create_time), MAX(create_time) FROM %s" % t
                    ).fetchone()
                except Exception as exc:
                    raise RuntimeError("读表失败 %s/%s" % (db_path,t)) from exc
                if not n:
                    continue
                chat_id = by_md5.get(t)
                if chat_id is None:
                    chat_id = "ORPHAN_%s_%s" % (os.path.basename(db_path).replace(".db", ""), t[4:28])
                entry = found.setdefault(chat_id, {
                    "chat_id": chat_id, "tables": [], "count": 0,
                    "lo": None, "hi": None, "biz": is_biz})
                entry["tables"].append((db_path, t))
                entry["count"] += n
                if lo and (entry["lo"] is None or lo < entry["lo"]):
                    entry["lo"] = lo
                if hi and (entry["hi"] is None or hi > entry["hi"]):
                    entry["hi"] = hi
            c.close()

        conn.close(); cconn.close()
        out = []
        for chat_id, e in found.items():
            if e["count"] < self.min_messages:
                continue
            info = self.dir.describe(chat_id, self.self_wxid)
            if chat_id.startswith("ORPHAN_"):
                info.update(title="孤立消息表 " + chat_id, is_orphan=True)
            info.update(e)
            info["last"] = sess.get(chat_id, e["hi"] or 0)
            out.append(info)
        out.sort(key=lambda x: -(x["hi"] or 0))
        return out

    # ------------------------------------------------------------------ #
    def _sticker_path(self, md5: str) -> Optional[str]:
        for ext in ("gif", "jpg", "png", "webp", "bmp"):
            p = os.path.join(self.sticker_store, "%s.%s" % (md5, ext))
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return p
        return None

    def _media_for_chat(self, chat_id: str):
        d = self.p.chat_attach_dir(chat_id)
        if not os.path.isdir(d):
            return None
        if not self.with_media or not self.v2key:
            return None
        idx = media.ImageIndex(d, self.v2key).build()
        return idx

    # ------------------------------------------------------------------ #
    def _collect_records(self, chat: Dict, idx, imgdir: str,
                         since_ts: Optional[int] = None,
                         until_ts: Optional[int] = None):
        """读出一个会话的消息并配对媒体。

        返回 ``(records, local_missing)``；``since_ts`` / ``until_ts``
        用于只取某个时间窗（见 scripts/recent.py）。
        """
        records: List[Dict] = []
        local_missing = {"image": 0, "sticker": 0}

        for db_path, tbl in chat["tables"]:
            c = self.kr.connect(db_path)
            senders = self._sender_map(c)
            cols = "local_id, local_type, real_sender_id, create_time, message_content"
            has_ct = True
            try:
                c.execute("SELECT WCDB_CT_message_content FROM %s LIMIT 1" % tbl)
            except Exception:
                has_ct = False
            if has_ct:
                cols += ", WCDB_CT_message_content"
            try:
                where, params = [], []
                if since_ts is not None:
                    where.append("create_time>=?"); params.append(since_ts)
                if until_ts is not None:
                    where.append("create_time<=?"); params.append(until_ts)
                rows = c.execute("SELECT %s FROM %s %s ORDER BY create_time, local_id" %
                                 (cols, tbl, ("WHERE " + " AND ".join(where)) if where else ""), params)
            except Exception as e:
                raise RuntimeError("读表失败 %s/%s" % (db_path, tbl)) from e

            for row in iter_rows(rows):
                if has_ct:
                    lid, lt, rsid, ts, raw, ct = row
                else:
                    lid, lt, rsid, ts, raw = row
                    ct = None
                ts = int(ts or 0)
                if since_ts is not None and ts < since_ts:
                    continue
                if until_ts is not None and ts > until_ts:
                    continue
                real = int(lt) & parse.TYPE_MASK
                payload = parse.decode_content(raw, ct)
                sender, payload = parse.split_sender(payload)
                if sender is None and rsid and senders.get(rsid):
                    sender = senders[rsid]
                # 群自身的系统消息会拿"会话 ID"当前缀（例如「XXX邀请你加入了群聊」），
                # 那不是一个人，归到系统消息里
                if sender and (sender == chat["chat_id"]
                               or sender.endswith("@chatroom")):
                    sender = None
                sender = sender or "系统"
                kind, text, media_refs = parse.describe(real, payload)
                media_refs=enrich_refs(kind,payload,media_refs)

                disp = self.self_name if sender == self.self_wxid else \
                    self.dir.display(sender, chat["chat_id"])
                rec = {
                    "local_id": lid, "kind": kind, "ts": ts,
                    "time": _fmt(ts), "sender": sender, "name": disp,
                    "text": re.sub(r"\s*\n\s*", " ⏎ ", text or "").strip(),
                    "img": None, "thumb": False,
                    "source_db": os.path.basename(db_path), "source_table": tbl,
                    "media": [],
                }

                # ---- 图片 ----
                if kind == "image" and media_refs:
                    md5 = media_refs[0][1]
                    hit = idx.get(md5) if idx else None
                    if hit:
                        src, data, ext = hit
                        thumb = ext.endswith("#thumb")
                        ext = ext.split("#")[0]
                        safe = _NAME_OK.sub("", disp)[:20] or "未知"
                        fname = "%s_%s_%s.%s" % (
                            rec["time"].replace(" ", "_").replace(":", ""),
                            safe, md5[:8], ext)
                        dst = os.path.join(imgdir, fname)
                        if not os.path.exists(dst):
                            os.makedirs(imgdir, exist_ok=True)
                            with open(dst, "wb") as fh:
                                fh.write(data)
                        rec["img"] = dst
                        if ext == "wxgf":
                            rec["img"]=None
                            rec["text"]="[图片]（本机有原始数据，格式暂不可预览）"
                            rec["media"].append(dict(kind="file",path=dst,name=fname,
                                                     reason="wxgf 无法生成预览；保留原始文件"))
                        rec["thumb"] = thumb
                        self.stats["images"] += 1
                        if thumb:
                            self.stats["thumbs"] += 1
                    else:
                        self.stats["image_missing"] += 1
                        local_missing["image"] += 1
                        rec["text"] = "[图片]（本机无缓存 md5=%s）" % md5[:8]

                # ---- 表情包 ----
                elif kind == "sticker" and media_refs:
                    md5 = media_refs[0][1]
                    sp = self._sticker_path(md5)
                    if sp:
                        rec["img"] = sp
                        self.stats["stickers"] += 1
                    else:
                        self.stats["sticker_missing"] += 1
                        local_missing["sticker"] += 1

                if self.with_media and kind in ("voice", "video", "file"):
                    from .resources import resolve_media
                    rec["media"] = resolve_media(self.p, self.kr, chat["chat_id"], lid, ts,
                                                 kind, payload, os.path.dirname(imgdir))

                records.append(rec)
            c.close()

        records.sort(key=lambda r: (r["ts"], r["local_id"] or 0))
        return records, local_missing

    # ------------------------------------------------------------------ #
    def export_chat(self, chat: Dict) -> Optional[Dict]:
        chat_id = chat["chat_id"]
        target = self.layout.chat_dir(chat["is_room"], chat["title"], chat_id)
        with staged_directory(target) as tmp:
            entry = self._export_empty_chat(chat,tmp,target) if chat.get("source_missing") else self._export_chat(chat, tmp)
            if entry is None:
                raise ValueError("会话没有可导出的记录：%s" % chat_id)
        entry["dir"] = target
        entry["relative_dir"] = os.path.relpath(target,self.p.export)
        entry["href"] = self.layout.rel(os.path.join(self.p.export, "index.html"),
                                        os.path.join(target, "聊天记录.html"))
        entry["fingerprint"] = self.fingerprint(chat)
        entry["last_ts"] = int(chat.get("hi") or 0)
        self.manifest["chats"][chat_id] = entry
        return entry

    def _export_empty_chat(self,chat,cdir,target):
        """Publish a source tombstone without discarding the previous export."""
        meta=dict(chat,summary="源快照已清空或移除此会话；上一份导出保留在 .previous",owner_name="")
        render.write_txt(os.path.join(cdir,"聊天记录.txt"),meta,[])
        render.write_csv(os.path.join(cdir,"聊天记录.csv"),[])
        page=os.path.join(cdir,"聊天记录.html")
        render.write_html(page,meta,[],self.layout,self.self_name)
        if os.path.isdir(target):
            with open(page,"a",encoding="utf-8") as fh:
                fh.write('<p><a href="%s">上一份历史记录</a></p>' % self.layout.rel(page,os.path.join(target+".previous","聊天记录.html")))
        if chat['is_room']:
            roster=[]
            for uid in chat.get('member_ids',[]):
                row=self.dir.profile(uid,chat['chat_id']);row.update(count=0,role='我' if uid==self.self_wxid else '群主' if uid==chat.get('owner') else '')
                roster.append(row)
            render.write_members(os.path.join(cdir,"成员名单.csv"),os.path.join(cdir,"成员名单.html"),roster,
                dict(title=chat['title'],chat_id=chat['chat_id'],total=0,unattributed=0),self.layout,page)
        Path(cdir,"说明.txt").write_text(meta['summary'],encoding="utf-8")
        self.stats['chats']+=1
        return dict(chat_id=chat['chat_id'],title=chat['title'],kind='群聊' if chat['is_room'] else '私聊',
                    count=0,images=0,stickers=0,first='',last='',owner='',members=chat.get('members',0),
                    source_missing=True)

    def _export_chat(self, chat: Dict, cdir) -> Optional[Dict]:
        chat_id = chat["chat_id"]
        title = chat["title"]
        os.makedirs(cdir, exist_ok=True)
        imgdir = os.path.join(cdir, "图片")
        attdir = os.path.join(cdir, "附件")

        idx = self._media_for_chat(chat_id)
        if idx is not None:
            os.makedirs(imgdir, exist_ok=True)

        # ---------------- 读消息 ---------------- #
        records, local_missing = self._collect_records(chat, idx, imgdir)
        if not records:
            shutil.rmtree(cdir, ignore_errors=True)
            return None

        # ---------------- 附件 ---------------- #
        if self.with_media:
            att_src = self.p.chat_attach_dir(chat_id)
            extras = media.extra_attachments(att_src)
            if extras:
                os.makedirs(attdir, exist_ok=True)
                for src, ext in extras:
                    base = os.path.basename(src)
                    dst = os.path.join(attdir, base if "." in base else base + "." + ext)
                    if not os.path.exists(dst):
                        try:
                            shutil.copy2(src, dst)
                            self.stats["attachments"] += 1
                        except OSError:
                            pass

        # ---------------- 渲染 ---------------- #
        n_img = sum(1 for r in records if r.get("img")
                    and r["kind"] == "image")
        n_stk = sum(1 for r in records if r.get("img")
                    and r["kind"] == "sticker")
        counts: Dict[str, int] = {}
        for r in records:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1

        members = chat.get("members", 0)
        summary = "｜".join(x for x in [
            "%d 条消息" % len(records),
            "%s ~ %s" % (records[0]["time"], records[-1]["time"]),
            "图片 %d 张" % n_img,
            "表情 %d 个" % n_stk,
            ("群成员 %d 人" % members) if chat["is_room"] and members else "",
        ] if x)

        meta = dict(chat)
        meta["summary"] = summary
        meta["owner_name"] = self.dir.display(chat.get("owner", ""),
                                             chat["chat_id"]) \
            if chat.get("owner") else ""

        html_path = os.path.join(cdir, "聊天记录.html")
        render.write_txt(os.path.join(cdir, "聊天记录.txt"), meta, records)
        render.write_csv(os.path.join(cdir, "聊天记录.csv"), records)
        months = render.write_html(html_path, meta, records, self.layout,
                                   self.self_name)

        # ---------------- 成员名单（群聊才有意义） ---------------- #
        if chat["is_room"]:
            spoke = {}
            for r in records:
                s = r["sender"] or ""
                # 群自己的系统消息会带会话 ID 前缀，别把它当成一个"人"
                if not s or s == "系统" or s == chat_id or s.endswith("@chatroom"):
                    continue
                spoke[s] = spoke.get(s, 0) + 1
            unattributed = len(records) - sum(spoke.values())
            everyone = set(spoke)
            everyone.update(chat.get("member_ids") or [])
            everyone.discard(chat_id)
            owner = chat.get("owner", "")
            mrows = []
            for w in everyone:
                m = self.dir.profile(w, chat_id)
                m["count"] = spoke.get(w, 0)
                m["role"] = ("我" if w == self.self_wxid
                             else "群主" if w == owner else "")
                mrows.append(m)
            # 说话多的排前面，没说话的按名字排后面
            mrows.sort(key=lambda m: (-m["count"], m["shown"]))
            render.write_members(
                os.path.join(cdir, "成员名单.csv"),
                os.path.join(cdir, "成员名单.html"),
                mrows,
                {"title": title, "chat_id": chat_id, "total": len(records),
                 "unattributed": unattributed,
                 "exported_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "self_wxid": self.self_wxid},
                self.layout, page=html_path)

        # 会话内说明
        with open(os.path.join(cdir, "说明.txt"), "w", encoding="utf-8") as f:
            f.write("%s\n%s\n\n" % (title, "=" * 40))
            f.write("会话 ID ：%s\n" % chat_id)
            if chat["is_room"]:
                f.write("群主    ：%s\n" % (meta["owner_name"] or "—"))
                f.write("群成员  ：%d 人\n" % members)
            f.write("消息总数：%d\n" % len(records))
            f.write("时间范围：%s ~ %s\n\n" % (records[0]["time"],
                                               records[-1]["time"]))
            f.write("消息类型统计：\n")
            for k, v in sorted(counts.items(), key=lambda x: -x[1]):
                f.write("   %-16s %d\n" % (k, v))
            f.write("\n图片：%d 张已导出到 图片/（文件名＝时间_发送者_编号）\n" % n_img)
            if idx is not None:
                f.write("      本会话媒体目录解出 %d 个可识别图片，"
                        "另有 %d 条图片消息本机没有缓存\n"
                        % (len(idx.by_md5) + len(idx.thumbs),
                           local_missing["image"]))
            f.write("表情包：%d 条消息有图（另有 %d 条 CDN 链接已失效或未取回），"
                    "文件在 ../../共享资源/表情包/\n"
                    % (n_stk, local_missing["sticker"]))
            f.write("附件  ：%s\n" % ("附件/ 目录" if os.path.isdir(attdir) else "无"))
            if chat["is_room"]:
                f.write("名单  ：成员名单.html / 成员名单.csv"
                        "（每人一行：群内显示名、群昵称、备注、微信昵称、微信号、wxid、发言数）\n")
            if months:
                f.write("分月  ：消息较多，HTML 已按月份拆分到 分月/ 目录\n")

        entry = {
            "chat_id": chat_id, "title": title, "kind": "群聊" if chat["is_room"] else "私聊",
            "count": len(records), "images": n_img, "stickers": n_stk,
            "first": records[0]["time"], "last": records[-1]["time"],
            "dir": cdir, "href": self.layout.rel(
                os.path.join(self.p.export, "index.html"), html_path),
            "owner": meta["owner_name"], "members": members,
            "owner_id": chat.get("owner", ""),
            "kinds": counts,
        }
        self.stats["chats"] += 1
        self.stats["messages"] += len(records)
        return entry

    # ------------------------------------------------------------------ #
    def run(self) -> Dict:
        with task_lock(os.path.join(self.p.data, ".snapshot.lock")), \
             task_lock(os.path.join(self.p.export, ".export.lock")):
            return self._run()

    def fingerprint(self, chat):
        h = hashlib.sha256()
        for db, table in chat["tables"]:
            c = self.kr.connect(db)
            try:
                for row in iter_rows(c.execute("SELECT * FROM %s ORDER BY local_id" % table)):
                    for value in row:
                        data = value if isinstance(value, bytes) else str(value).encode()
                        h.update(len(data).to_bytes(8, "big")); h.update(data)
            finally:
                c.close()
        h.update(json.dumps([chat.get("title"), chat.get("member_ids"),
                             chat.get("owner"), self.dir.alias,
                             self.dir.room_names.get(chat["chat_id"]), self.dir.remarks,
                             self.dir.nicks, names.NAME_PRIORITY, self.with_media,
                             self.v2key.hex() if self.v2key else None],
                            ensure_ascii=False, sort_keys=True).encode())
        if self.with_media:
            for db in (os.path.join(self.p.db_storage,"message","media_0.db"),
                       os.path.join(self.p.db_storage,"hardlink","hardlink.db")):
                if os.path.exists(db):
                    st=os.stat(db);h.update(str((db,st.st_size,st.st_mtime_ns)).encode())
            if self._fingerprint_globals is None:
                shared=hashlib.sha256()
                for root in (self.sticker_store,os.path.join(self.p.msg,"file"),os.path.join(self.p.msg,"video")):
                    for r,ds,files in os.walk(root):
                        ds.sort()
                        for name in sorted(files):
                            st=os.stat(os.path.join(r,name))
                            shared.update(str((r,name,st.st_size,st.st_mtime_ns)).encode())
                self._fingerprint_globals=shared.digest()
            h.update(self._fingerprint_globals)
            for root in (self.p.chat_attach_dir(chat["chat_id"]),):
                for r, ds, files in os.walk(root):
                    ds.sort()
                    for name in sorted(files):
                        st = os.stat(os.path.join(r, name))
                        h.update(str((r, name, st.st_size, st.st_mtime_ns)).encode())
        return h.hexdigest()

    def _run(self) -> Dict:
        self._fingerprint_globals=None
        self.manifest=self._load_manifest()
        if self.manifest.get("account") not in (None,self.self_wxid):
            raise ValueError("输出目录属于另一个账号")
        self.manifest.update(account=self.self_wxid,version=2)
        self.layout=render.Layout(self.p.export,self.manifest["chats"])
        t0 = time.time()
        self.log("扫描会话…")
        chats = self.enumerate_chats()
        self.log("发现 %d 个有消息的会话" % len(chats))

        for i, chat in enumerate(chats, 1):
            pre = self.manifest["chats"].get(chat["chat_id"])
            if pre and not self.force and pre.get("fingerprint") == self.fingerprint(chat):
                self.log("[%d/%d] 跳过（已完成）%s" % (i, len(chats), chat["title"]))
                continue
            self.log("[%d/%d] %s（%s 条）" % (i, len(chats), chat["title"], chat["count"]))
            try:
                self.export_chat(chat)
            except Exception as ex:
                self.log("   ! 失败：%s" % ex)
                import traceback
                traceback.print_exc()
                raise
            self._save_manifest()

        # 索引
        results = self.rebuild_index(chats)
        self.manifest["stats"] = self.stats
        self.manifest["finished_at"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.manifest["elapsed_sec"] = round(time.time() - t0, 1)
        self._save_manifest()
        return {"chats": results, "stats": self.stats,
                "elapsed": self.manifest["elapsed_sec"],
                "manifest": self.manifest_path}

    # ------------------------------------------------------------------ #
    def rebuild_index(self, chats: Optional[List[Dict]] = None) -> List[Dict]:
        """按 manifest 里已完成的会话重写总索引页与总览 CSV。"""
        if chats is None:
            chats = self.enumerate_chats()
        by_id = {c["chat_id"]: c for c in chats}
        results = []
        for chat_id, e in self.manifest["chats"].items():
            c = by_id.get(chat_id, {})
            e.setdefault("owner", c.get("owner", ""))
            e["owner"] = e.get("owner") or c.get("owner", "")
            results.append(e)

        global_meta = {
            "exported_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "wxid": self.self_wxid,
            "note": ("导出的是本机微信 4.x 本地数据库里的全部会话，"
                     "只读复制，未修改微信任何文件。"
                     "图片来自微信本机缓存（只有点开过的才会缓存），"
                     "表情包来自消息里的 CDN 链接。"),
        }
        render.write_index(self.layout, results, global_meta)
        render.write_overview_csv(self.layout, results)
        return results
