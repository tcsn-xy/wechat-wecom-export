"""Read-only source adapters; use existing platform decoders and identities."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from . import wx_import,wecom_import,realtime_store as store,conn as my
from .realtime_snapshot import signature,SnapshotStore
from wxexport import keys,names
from wecom import messages as wm,crypto


def quoted(value):return '"'+value.replace('"','""')+'"'
def blob(value):return bytes(value) if isinstance(value,(bytes,bytearray)) else (str(value).encode() if value is not None else None)
def row_hash(row):
    h=hashlib.sha256()
    for value in row:
        b=blob(value) or b'';h.update(str(len(b)).encode()+b':'+b)
    return h.hexdigest()


class CachedKeyring(keys.Keyring):
    def key_for(self,path):
        stat=Path(self.capture_path).stat();version=(stat.st_mtime_ns,stat.st_size)
        if getattr(self,'_capture_version',None)!=version:
            self._cache.clear();self._passphrase=None;self._capture_version=version
        salt=keys.db_salt(path)
        if salt not in self._cache:self._cache[salt]=keys.derive_key(self.passphrase,salt).hex()
        return self._cache[salt]


class Wechat:
    platform='wechat'
    def __init__(self,root,account,capture,private):
        self.root=Path(root);self.account=account;self.kr=CachedKeyring(str(capture));self.capture=str(capture)
        self.snapshots=SnapshotStore(private,self.validate);self.mode='direct';self.helper=wx_import.WxImporter.__new__(wx_import.WxImporter)
        self.helper.self_wxid=account;self.known={};self.directory=None;self.metadata_token=None;self.candidates={};self.unresolved_counts={}
    def discover(self):
        # Explicit directory traversal: PermissionError must never masquerade as an empty source.
        found=[]
        for sub in ('message','biz_message','session','contact'):
            directory=self.root/sub
            try:items=list(directory.iterdir())
            except FileNotFoundError:continue
            for p in items:
                if (sub in ('session','contact') and p.name==sub+'.db') or re.fullmatch(r'(?:biz_)?message_\d+\.db',p.name):found.append(p)
        if not found:raise FileNotFoundError('wechat_sources_missing')
        return found
    def signatures(self,paths):return {str(p):signature(p) for p in paths}
    def validate(self,path):
        c=self.kr.connect(str(path))
        try:c.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
        finally:c.close()
        return path
    def metadata(self,c):
        snapshots=self.snapshots.current
        contact=snapshots.get(str(self.root/'contact/contact.db'));session=snapshots.get(str(self.root/'session/session.db'))
        if not contact or not session:raise RuntimeError('wechat_metadata_missing')
        token=(str(contact['path']),str(session['path']))
        if token==self.metadata_token:return
        contact_changed=self.metadata_token is None or self.metadata_token[0]!=token[0]
        if contact_changed:
            self.directory=names.Directory(str(contact['path']),self.capture);self.helper.dir=self.directory
        candidates=set()
        for entry,queries in [(contact,[('contact','username'),('chat_room','username'),('stranger','username')]),(session,[('SessionTable','username'),('Name2Id','user_name')])]:
            sc=self.kr.connect(str(entry['path']))
            try:
                tables={r[0] for r in sc.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table,col in queries:
                    if table in tables:
                        columns={r[1] for r in sc.execute('PRAGMA table_info('+quoted(table)+')')}
                        if col in columns:candidates.update(str(r[0]) for r in sc.execute('SELECT '+quoted(col)+' FROM '+quoted(table)) if r[0])
            finally:sc.close()
        self.candidates={'Msg_'+hashlib.md5(v.encode()).hexdigest():v for v in candidates}
        self.known={(r['source_db'],r['source_table']):r['chat_id'] for r in my.query(c,'SELECT source_db,source_table,chat_id FROM import_sources WHERE platform=%s AND account_key=%s AND dataset=%s',('wechat',self.account,'wechat-snapshot')) if r['chat_id']}
        # Existing name/remark priority is preserved by the same Directory/parser.
        self.helper.kr=self.kr;self.helper.p=type('Paths',(),{'contact_db':lambda _:str(contact['path'])})()
        self.helper.db=c;self.helper.verbose=False
        if contact_changed:self.helper.import_contacts()
        self.metadata_token=token
    def descriptors(self,paths,c):
        self.metadata(c);out=[];unresolved=0
        for original in paths:
            if not re.fullmatch(r'(?:biz_)?message_\d+\.db',original.name):continue
            entry=self.snapshots.current.get(str(original))
            if not entry:continue
            sc=self.kr.connect(str(entry['path']))
            try:
                senders=self.helper._sender_map(sc)
                mapping=dict(self.candidates)
                mapping.update({'Msg_'+hashlib.md5(v.encode()).hexdigest():v for v in senders.values()})
                self.unresolved_counts[str(original)]=0
                for table,sql in sc.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"):
                    cid=self.known.get((original.name,table)) or mapping.get(table)
                    if not cid:unresolved+=1;self.unresolved_counts[str(original)]+=1;continue
                    hi=sc.execute('SELECT MAX(local_id) FROM '+quoted(table)).fetchone()[0] or 0
                    cols={r[1] for r in sc.execute('PRAGMA table_info('+quoted(table)+')')}
                    latest=sc.execute('SELECT create_time FROM '+quoted(table)+' WHERE local_id=?',(hi,)).fetchone()
                    desc=dict(priority_ts=int(latest[0] or 0) if latest else 0,platform='wechat',account_key=self.account,dataset='wechat-snapshot',source_db=original.name,source_table=table,
                        schema_hash=hashlib.sha256((sql or '').encode()).hexdigest(),file_identity=str(entry['signature'][0][1]),original=original,
                        entry=entry,counter='local_id',max_id=int(hi),chat_id=cid,senders=senders,has_ct='WCDB_CT_message_content' in cols)
                    out.append(desc)
            finally:sc.close()
        return out,unresolved
    def rows(self,desc,after=None,tail=False,ids=None,batch_size=500):
        cols='local_id,local_type,real_sender_id,create_time,message_content,server_id,sort_seq'
        if desc['has_ct']:cols+=',WCDB_CT_message_content'
        boundary=after;remaining=2000 if tail else None
        while remaining is None or remaining>0:
            query='SELECT '+cols+' FROM '+quoted(desc['source_table']);where=[];args=[]
            if ids is not None:where.append('local_id IN ('+','.join(['?']*len(ids))+')');args.extend(ids)
            if boundary is not None:where.append('local_id'+('<' if tail else '>')+'?');args.append(boundary)
            if where:query+=' WHERE '+' AND '.join(where)
            query+=' ORDER BY local_id '+('DESC' if tail else 'ASC')+' LIMIT ?';args.append(min(batch_size,remaining) if remaining is not None else batch_size)
            sc=self.kr.connect(str(desc['entry']['path']))
            try:rows=[tuple(r) for r in sc.execute(query,args).fetchall()]
            finally:sc.close()
            if not rows:return
            boundary=rows[-1][0]
            if remaining is not None:remaining-=len(rows)
            yield rows
    def decode(self,desc,row):
        cid=desc['chat_id']
        main,raw,links,refs,sender,kind,ts,lid=wx_import.decode_message(self.helper,row,desc['has_ct'],cid,0,desc['source_db'],desc['source_table'],desc['senders'])
        chat=self.directory.describe(cid,self.account)
        if cid.startswith('ORPHAN_'):chat.update(is_orphan=True)
        return dict(message=dict(zip(store.WX_COLUMNS,main)),raw=dict(zip(['raw_content','compress_type','raw_bytes','decoded_from','raw_blob'],raw)),links=links,chat=chat,member_profile=self.directory.profile(sender,cid))


class Wecom:
    platform='wecom'
    def __init__(self,root,accounts,key,private,media_root=None):
        self.root=Path(root);self.accounts=set(accounts);self.key=key;self.mode='direct';self.port=None
        self.snapshots=SnapshotStore(private,self.validate);self.helper=wecom_import.WecomImporter.__new__(wecom_import.WecomImporter)
        self.users={};self.convs={};self.tokens={};self.media_root=media_root;self.media_keys={}
    def discover(self):
        found=[]
        def walk(path,depth=0):
            if depth>6:return
            for p in path.iterdir():
                if 'Backup' in p.name:continue
                if p.is_dir():walk(p,depth+1)
                elif p.name in ('message.db','session.db','user.db'):found.append(p)
        walk(self.root)
        return self.approved(found)
    def approved(self,paths):
        out=[]
        for p in paths:
            ds=str(p.parent.relative_to(self.root)).replace('/','_');match=re.match(r'\d+',ds)
            if match and match.group(0) in self.accounts:out.append(p)
        if not any(p.name=='message.db' for p in out):raise RuntimeError('bound_wecom_account_missing')
        return out
    def signatures(self,paths):return {str(p):signature(p) for p in paths}
    def validate(self,path):
        data=path.read_bytes();wal=Path(str(path)+'-wal');plain,_=crypto.decrypt_database_bytes(data,self.key,wal=wal.read_bytes() if wal.exists() else None)
        target=path.parent/('plain-'+path.name);target.write_bytes(plain)
        sc=wm.open_ro(target)
        try:
            if sc.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('wecom_integrity')
        finally:sc.close()
        return target
    def metadata(self,c):
        datasets={p.parent for p in map(Path,self.snapshots.current)}
        for ds in datasets:
            name=str(ds.relative_to(self.root)).replace('/','_');account=re.match(r'\d+',name).group(0)
            entries={p.name:self.snapshots.current[str(p)] for p in map(Path,self.snapshots.current) if p.parent==ds}
            if not all(k in entries for k in ('message.db','session.db','user.db')):continue
            token=tuple(str(entries[k]['path']) for k in ('session.db','user.db'))
            if self.tokens.get(name)==token:continue
            users=wm.load_users({k:e['path'] for k,e in entries.items()});self.users[name]=users
            if self.media_root:
                from wecom.resources import CacheIndex
                self.media_keys[name]=set(CacheIndex(self.media_root,account).items)
            sc=wm.open_ro(entries['session.db']['path']);convs={}
            try:
                table=wm.find_table(sc,('conversation_table','conversation'))
                if table:
                    mapped=wm.mapping(sc,table,wm.CONV_COLS);wanted=[(k,v) for k,v in mapped.items() if v]
                    for row in sc.execute('SELECT '+','.join(quoted(v) for k,v in wanted)+' FROM '+quoted(table)):
                        values=dict(zip((k for k,v in wanted),row));cid=str(values.get('id') or '')
                        if cid:convs[cid]=wm.resolve_conv_name(cid,str(values.get('name') or ''),users,{},account)
            finally:sc.close()
            self.convs[name]=convs
            with c.cursor() as cur:
                cur.executemany('INSERT INTO contacts(platform,account_key,contact_id,real_name) VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE real_name=VALUES(real_name)',[('wecom',account,uid,title[:255]) for uid,title in users.items()])
            c.commit();self.tokens[name]=token
    def descriptors(self,paths,c):
        self.metadata(c);out=[]
        for original in paths:
            if original.name!='message.db':continue
            entry=self.snapshots.current.get(str(original))
            if not entry:continue
            name=str(original.parent.relative_to(self.root)).replace('/','_');account=re.match(r'\d+',name).group(0)
            sc=wm.open_ro(entry['path'])
            try:
                for table in wm.tables(sc):
                    if 'message' not in table.lower() or table.startswith('sqlite_'):continue
                    mapped=wm.mapping(sc,table,wm.COLS)
                    if not mapped.get('content') or not mapped.get('conversation'):continue
                    sql=sc.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()[0]
                    hi=sc.execute('SELECT MAX(rowid) FROM '+quoted(table)).fetchone()[0] or 0
                    out.append(dict(platform='wecom',account_key=account,dataset=name,source_db='message.db',source_table=table,
                        schema_hash=hashlib.sha256(sql.encode()).hexdigest(),file_identity=str(entry['signature'][0][1]),original=original,
                        entry=entry,counter='rowid',max_id=int(hi),mapped=mapped))
            finally:sc.close()
        return out,0
    def rows(self,desc,after=None,tail=False,ids=None,batch_size=500):
        wanted=[(k,v) for k,v in desc['mapped'].items() if v];boundary=after;remaining=2000 if tail else None
        while remaining is None or remaining>0:
            query='SELECT rowid,'+','.join(quoted(v) for k,v in wanted)+' FROM '+quoted(desc['source_table']);where=[];args=[]
            if ids is not None:where.append('rowid IN ('+','.join(['?']*len(ids))+')');args.extend(ids)
            if boundary is not None:where.append('rowid'+('<' if tail else '>')+'?');args.append(boundary)
            if where:query+=' WHERE '+' AND '.join(where)
            query+=' ORDER BY rowid '+('DESC' if tail else 'ASC')+' LIMIT ?';args.append(min(batch_size,remaining) if remaining is not None else batch_size)
            sc=wm.open_ro(desc['entry']['path'])
            try:rows=[tuple(r) for r in sc.execute(query,args).fetchall()]
            finally:sc.close()
            if not rows:return
            boundary=rows[-1][0]
            if remaining is not None:remaining-=len(rows)
            yield rows
    def decode(self,desc,row):
        rec=dict(zip(['_source_rowid']+[k for k,v in desc['mapped'].items() if v],row));cid=str(rec.get('conversation') or '')
        if not cid:raise ValueError('unresolved_wecom_conversation')
        raw=rec.get('content');extra=rec.get('extra');text,links,md5s=wecom_import.decode_message(self.helper,raw,extra,rec.get('ctype'),cid)
        ts=wecom_import._to_ts(rec.get('time'));uid=str(rec.get('sender') or '');kind=self.helper._kind_of(rec.get('ctype'))
        safe=wecom_import._safe_text;users=self.users.get(desc['dataset'],{})
        # Media classification still uses only real cached references or known media kinds.
        m=dict(platform='wecom',account_key=desc['account_key'],chat_id=cid,chat_pk=0,local_id=int(rec.get('msg_id') or rec['_source_rowid']),server_id=str(rec.get('server_id') or '') or None,
            sort_seq=int(rec['sequence']) if str(rec.get('sequence') or '').lstrip('-').isdigit() else None,
            local_type=int(rec['ctype']) if str(rec.get('ctype') or '').lstrip('-').isdigit() else 0,
            kind=kind,sender_id=safe(uid,320) or None,sender_name=safe(users.get(uid,uid),512) or None,ts=ts,msg_time=self.helper._fmt(ts),text=safe(text,0) or None,
            content_type=safe(rec.get('ctype'),64) or None,source_dataset=desc['dataset'],source_db='message.db',source_table=desc['source_table'],has_media=int(kind in wx_import.MEDIA_KINDS or bool(set(md5s)&self.media_keys.get(desc['dataset'],set()))),has_link=int(bool(links)),extra=self.helper._extra_json(rec))
        original=blob(raw);extra_blob=blob(extra)
        return dict(message=m,raw=dict(raw_content=original.decode('utf-8','replace') if original else '',raw_blob=original,extra_blob=extra_blob,raw_bytes=len(original or b''),decoded_from='protobuf'),links=links,
            chat=dict(title=self.convs.get(desc['dataset'],{}).get(cid,cid),kind=wm._conv_kind(cid),is_room=wm._conv_kind(cid)=='群聊'))

# Narrow read-only sandbox bridge. Source DBs are never opened for writing.
_NODE_PRELUDE=r'''
const req=process.mainModule.require.bind(process.mainModule),fs=req('fs'),crypto=req('crypto');
function stamp(p){
 try {const s=fs.statSync(p,{bigint:true}),fd=fs.openSync(p,'r'),b=Buffer.alloc(32);let n;
 try {n=fs.readSync(fd,b,0,32,0);} finally {fs.closeSync(fd);}
 return [String(s.dev),String(s.ino),String(s.size),String(s.mtimeNs),crypto.createHash('sha256').update(b.slice(0,n)).digest('hex')];
 } catch(e){if(e.code==='ENOENT')return null;throw e;}
}
function signature(p){return [stamp(p),stamp(p+'-wal')];}
'''


def _node(self,body,args):
    from wecom import access
    if self.port is None:_,self.port=access.wecom_access()
    expression='(function(a){'+_NODE_PRELUDE+body+'})('+json.dumps(args)+')'
    return json.loads(access.run_js(expression,self.port,timeout=30))


def _remote_discover(self):
    paths=self._node("""let out=[];function walk(p,depth){if(depth>6)return;
 for(const e of fs.readdirSync(p,{withFileTypes:true})){if(e.name.includes('Backup'))continue;
 const f=p+'/'+e.name;if(e.isDirectory())walk(f,depth+1);else if(['message.db','session.db','user.db'].includes(e.name))out.push(f);}}
 walk(a.root,0);return JSON.stringify(out);""",dict(root=str(self.root)))
    return self.approved([Path(p) for p in paths])


def _remote_signatures(self,paths):
    return self._node('let out={};for(const p of a.paths)out[p]=signature(p);return JSON.stringify(out);',dict(paths=list(map(str,paths))))


def _remote_copy(self,source,folder):
    import shutil
    temp=Path(tempfile.mkdtemp(prefix='wecom_realtime-',dir=Path.home()/'Downloads'))
    try:
        result=self._node("""const before=signature(a.source);
 for(const suffix of ['', '-wal']){const src=a.source+suffix,dst=a.target+suffix;
 if(!fs.existsSync(src))continue;fs.copyFileSync(src,dst,fs.constants.COPYFILE_FICLONE);fs.chmodSync(dst,0o600);}
 return JSON.stringify([before,signature(a.source)]);""",dict(source=str(source),target=str(temp/source.name)))
        for p in temp.iterdir():shutil.move(str(p),str(folder/p.name))
        return result
    finally:shutil.rmtree(temp,ignore_errors=True)


def _enable_inspector(self):
    from wecom import access
    _,self.port=access.wecom_access();self.mode='inspector';self.snapshots.remote_copy=self.remote_copy

Wecom._node=_node
Wecom.remote_discover=_remote_discover
Wecom.remote_signatures=_remote_signatures
Wecom.remote_copy=_remote_copy
Wecom.enable_inspector=_enable_inspector
