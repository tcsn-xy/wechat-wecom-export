"""Synthetic accounts only. Never load project data, keys or exports."""
from pathlib import Path
import hashlib
import json
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "lib"), str(ROOT / "scripts")]

import sqlcipher3
import zstandard


def wx_fixture(root):
    from wxexport import config
    root = Path(root)
    source = root / "wx"
    capture = root / "capture.jsonl"
    source.mkdir()
    capture.write_text(json.dumps(dict(t="pbkdf", rounds=256000,
                                      pw=b"fixture-only".hex())) + "\n")
    (source / "_snapshot.json").write_text(json.dumps(dict(wxid="wxid_self", uin="1",
                                                         cloned_at="2026-10-06 00:00:00")))
    def database(relative, sql):
        path = source / "db_storage" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlcipher3.connect(str(path))
        c.execute("PRAGMA key='fixture-only'")
        c.executescript(sql)
        c.commit()
        return c
    session = database("session/session.db", "CREATE TABLE SessionTable(username TEXT,last_timestamp INTEGER); CREATE TABLE Name2Id(user_name TEXT);")
    contact = database("contact/contact.db", "CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT,alias TEXT); CREATE TABLE stranger(username TEXT,nick_name TEXT,remark TEXT); CREATE TABLE chat_room(username TEXT,owner TEXT,ext_buffer BLOB);")
    rooms = ["100@chatroom", "200@chatroom"]
    for room in rooms:
        session.execute("INSERT INTO SessionTable VALUES (?,?)", (room, 1790000002))
        contact.execute("INSERT INTO contact VALUES (?,?,?,?)", (room, "同名群", "", ""))
        contact.execute("INSERT INTO chat_room VALUES (?,?,?)", (room, "wxid_friend", b"\x13wxid_friend\x00"))
    contact.execute("INSERT INTO contact VALUES ('wxid_friend','昵称','备注','friend')")
    session.commit(); session.close(); contact.commit(); contact.close()
    tables = {}
    for shard,room in [(0,rooms[0]), (1,rooms[0]), (3,rooms[1])]:
        table = "Msg_" + hashlib.md5(room.encode()).hexdigest()
        tables[shard] = table
        c = database("message/message_%s.db" % shard,
                     "CREATE TABLE Name2Id(user_name TEXT); INSERT INTO Name2Id VALUES ('wxid_friend'); "
                     "CREATE TABLE %s(local_id INTEGER PRIMARY KEY,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content BLOB,server_id INTEGER,sort_seq INTEGER,WCDB_CT_message_content INTEGER);" % table)
        text = b"wxid_friend:\ncompressed needle" if shard==1 else b"wxid_friend:\nplain needle"
        raw = zstandard.ZstdCompressor().compress(text) if shard==1 else text
        c.execute("INSERT INTO %s VALUES (1,1,1,1790000000,?,1,1,?)" % table, (raw,4 if shard==1 else 0))
        if shard==0:
            c.execute("INSERT INTO %s VALUES (2,1,0,1790000001,'',2,2,0)" % table)
            c.execute("CREATE TABLE Msg_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa(local_id INTEGER PRIMARY KEY,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content TEXT,server_id INTEGER,sort_seq INTEGER)")
            c.execute("INSERT INTO Msg_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa VALUES (1,1,0,1790000000,'orphan needle',1,1)")
        c.commit(); c.close()
    return config.load(require_snapshot=True, snapshot_root=str(source), capture=str(capture),
                       export_root=str(root / "wx-out")), tables


def wecom_fixture(root):
    from wecom_selftest import make_plain_dataset
    root=Path(root)
    raw=root/"wecom-raw"; plain=root/"wecom-plain"
    ds=plain/"1001_Data"
    make_plain_dataset(str(ds))
    raw.mkdir()
    return raw,plain,ds


def media_fixture(paths,tables,raw=None,ds=None):
    """Tiny real JPEG/GIF/WAV/MP4 assets, wholly synthetic."""
    import io
    import wave
    from PIL import Image
    import av
    blobs={}
    for kind,fmt in [('image','JPEG'),('sticker','GIF')]:
        buf=io.BytesIO();Image.new('RGB',(16,16),'blue').save(buf,format=fmt)
        blobs[kind]=buf.getvalue()
    buf=io.BytesIO()
    with wave.open(buf,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(b'\0\0'*800)
    blobs['voice']=buf.getvalue();blobs['file']=b'%PDF-1.4\n% synthetic fixture\n%%EOF\n'
    video=Path(paths.msg)/'video'/'fixture.mp4';video.parent.mkdir(parents=True,exist_ok=True)
    container=av.open(str(video),'w');stream=container.add_stream('libx264',rate=1)
    stream.width=16;stream.height=16;stream.pix_fmt='yuv420p'
    frame=av.VideoFrame(16,16,'rgb24')
    for plane in frame.planes:plane.update(bytes(plane.buffer_size))
    for packet in stream.encode(frame):container.mux(packet)
    for packet in stream.encode():container.mux(packet)
    container.close();blobs['video']=video.read_bytes();video.unlink()
    md5={kind:hashlib.md5(blob).hexdigest() for kind,blob in blobs.items()}
    for kind,ext in [('file','pdf'),('video','mp4')]:
        dest=Path(paths.msg)/kind/(md5[kind]+('_raw' if kind=='video' else '')+'.'+ext)
        dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(blobs[kind])
    image=Path(paths.chat_attach_dir('100@chatroom'))/'image.dat'
    image.parent.mkdir(parents=True,exist_ok=True);image.write_bytes(bytes(b^0x70 for b in blobs['image']))
    sticker=Path(paths.export)/'共享资源'/'表情包'/(md5['sticker']+'.gif')
    sticker.parent.mkdir(parents=True,exist_ok=True);sticker.write_bytes(blobs['sticker'])
    c=sqlcipher3.connect(str(Path(paths.db_storage)/'message/message_0.db'));c.execute("PRAGMA key='fixture-only'")
    payloads=[(3,3,'<msg><img md5="%s"/></msg>'%md5['image']),
              (4,34,'<msg><voicemsg/></msg>'),
              (5,43,'<msg><videomsg newmd5="%s"/></msg>'%md5['video']),
              (6,49,'<msg><appmsg><title>synthetic.pdf</title><type>6</type><appattach md5="%s"/></appmsg></msg>'%md5['file']),
              (7,47,'<msg><emoji md5="%s"/></msg>'%md5['sticker'])]
    for lid,kind,payload in payloads:
        c.execute('INSERT INTO %s VALUES (?,?,1,1790000002,?,?,?,0)'%tables[0],(lid,kind,'wxid_friend:\n'+payload,lid,lid))
    c.commit();c.close()
    voice=sqlcipher3.connect(str(Path(paths.db_storage)/'message/media_0.db'));voice.execute("PRAGMA key='fixture-only'")
    voice.execute('CREATE TABLE VoiceInfo(local_id INTEGER,create_time INTEGER,voice_data BLOB)')
    voice.execute('INSERT INTO VoiceInfo VALUES (4,1790000002,?)',(blobs['voice'],));voice.commit();voice.close()
    if raw and ds:
        folders={'image':('Images','jpg'),'sticker':('Emotions','gif'),'voice':('Voices','wav'),'video':('Videos','mp4'),'file':('Files','pdf')}
        db=sqlite3.connect(Path(ds)/'message.db')
        for index,(kind,(folder,ext)) in enumerate(folders.items(),10):
            path=Path(raw)/'Profiles'/'1001'/'Caches'/folder/md5[kind]/('fixture.'+ext)
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(blobs[kind])
            db.execute('INSERT INTO message_table VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (index,index,index,'u_a','R:1001',503 if kind=='image' else 16,1790000002,0,
                        ('<msg><resource md5="%s"/></msg>'%md5[kind]).encode(),b'\x00extra\xff'))
        db.commit();db.close()
    return blobs
