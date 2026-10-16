#!/usr/bin/env python3
"""Run the VIBEOS Creator Studio locally with persistent SQLite + media storage.

This local dashboard manages media, queue plans, notifications and preferences.
It deliberately does NOT claim to publish to Instagram: live publishing requires
Meta credentials and the hosted VIBEOS publishing worker integration.
"""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, unquote
from email.parser import BytesParser
from email.policy import default
import json, mimetypes, os, sqlite3, uuid, datetime, threading, webbrowser
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent
WEB = ROOT / 'web'
DATA = ROOT / 'vibeos_local_data'
MEDIA = DATA / 'media'
DB = DATA / 'studio.sqlite3'
MEDIA.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD = 10 * 1024 * 1024
MIME_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
LOCK = threading.RLock()

def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def db():
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn

@contextmanager
def connection():
    c = db()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def init_db():
    with connection() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS media(id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL, mime TEXT NOT NULL, created_at TEXT NOT NULL, path TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS queue(id TEXT PRIMARY KEY, media_id TEXT NOT NULL, caption TEXT NOT NULL DEFAULT '', format TEXT NOT NULL, scheduled_at TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS activity(id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL, icon TEXT NOT NULL, at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        defaults = {'autopilot': False, 'story': False, 'caption': True}
        for k,v in defaults.items(): c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',(k,json.dumps(v)))

def notify(title, description, icon='✦'):
    with connection() as c:
        c.execute('INSERT INTO activity VALUES(?,?,?,?,?)',(uuid.uuid4().hex,title,description,icon,now()))
        c.execute('DELETE FROM activity WHERE id IN (SELECT id FROM activity ORDER BY at DESC LIMIT -1 OFFSET 100)')

def public_media(r):
    return {'id':r['id'],'name':r['name'],'size':r['size'],'type':r['mime'],'createdAt':r['created_at'],'url':'/media/'+r['id']}
def state():
    with connection() as c:
        media=[public_media(r) for r in c.execute('SELECT * FROM media ORDER BY created_at DESC')]
        queue=[{'id':r['id'],'mediaId':r['media_id'],'caption':r['caption'],'format':r['format'],'when':r['scheduled_at'] or '', 'status':r['status'],'createdAt':r['created_at']} for r in c.execute('SELECT * FROM queue ORDER BY COALESCE(scheduled_at,created_at),created_at')]
        activity=[dict(r) for r in c.execute('SELECT * FROM activity ORDER BY at DESC LIMIT 100')]
        settings={r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
    return {'media':media,'queue':queue,'activity':activity,'settings':settings,'publishingAvailable':False,'instagramConnectUrl':os.environ.get('VIBEOS_INSTAGRAM_CONNECT_URL','https://vibeos-backend.onrender.com/login')}

def json_bytes(obj): return json.dumps(obj).encode()
class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*a,**kw): super().__init__(*a,directory=str(WEB),**kw)
    def log_message(self, fmt, *args): print('[VIBEOS]',fmt%args)
    def send_data(self, status, data, ctype='application/json; charset=utf-8'):
        self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('X-Frame-Options','DENY'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def read_json(self):
        n=int(self.headers.get('Content-Length','0'))
        if n>1024*1024: raise ValueError('Request too large')
        return json.loads(self.rfile.read(n) or b'{}')
    def do_GET(self):
        p=urlparse(self.path).path
        if p=='/api/state': return self.send_data(200,json_bytes(state()))
        if p.startswith('/media/'):
            mid=unquote(p.split('/')[-1])
            with connection() as c: r=c.execute('SELECT * FROM media WHERE id=?',(mid,)).fetchone()
            if not r: return self.send_data(404,b'Not found','text/plain')
            path=MEDIA/r['path']
            if not path.is_file(): return self.send_data(404,b'Not found','text/plain')
            data=path.read_bytes(); return self.send_data(200,data,r['mime'])
        if p=='/healthz': return self.send_data(200,b'{"ok":true}')
        if p=='/': self.path='/index.html'
        return super().do_GET()
    def do_POST(self):
        p=urlparse(self.path).path
        try:
            if p=='/api/media':
                n=int(self.headers.get('Content-Length','0'))
                if n<=0 or n>MAX_UPLOAD+1024*1024: return self.send_data(413,json_bytes({'error':'Photo too large. Maximum 10 MB.'}))
                raw=self.rfile.read(n)
                content_type=self.headers.get('Content-Type','')
                msg=BytesParser(policy=default).parsebytes(('Content-Type: '+content_type+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
                if not msg.is_multipart(): return self.send_data(400,json_bytes({'error':'Expected multipart photo upload'}))
                added=[]
                for part in msg.iter_parts():
                    if part.get_content_disposition()!='form-data' or part.get_param('name',header='content-disposition')!='files': continue
                    name=Path(part.get_filename() or 'photo').name
                    mime=part.get_content_type(); data=part.get_payload(decode=True) or b''
                    ext=Path(name).suffix.lower()
                    if not mime.startswith('image/') or ext not in MIME_EXT: continue
                    if not data or len(data)>MAX_UPLOAD: continue
                    signatures = {'.jpg': data.startswith(b'\xff\xd8\xff'), '.jpeg': data.startswith(b'\xff\xd8\xff'), '.png': data.startswith(b'\x89PNG\r\n\x1a\n'), '.gif': data.startswith((b'GIF87a', b'GIF89a')), '.webp': len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP'}
                    if not signatures.get(ext, False): continue
                    mid=uuid.uuid4().hex; safe_name=mid+ext; (MEDIA/safe_name).write_bytes(data)
                    with connection() as c: c.execute('INSERT INTO media VALUES(?,?,?,?,?,?)',(mid,name,len(data),mime,now(),safe_name))
                    added.append(mid)
                if not added: return self.send_data(400,json_bytes({'error':'Choose valid JPG, PNG, WEBP or GIF photos under 10 MB.'}))
                notify('Photo uploaded to VIBEOS buffer',f'{len(added)} photo(s) stored on this device.','▧')
                return self.send_data(201,json_bytes({'ok':True,'added':len(added),'state':state()}))
            if p=='/api/queue':
                body=self.read_json(); mid=str(body.get('mediaId','')); fmt=body.get('format','Story'); caption=str(body.get('caption',''))[:2200]; when=str(body.get('when','')).strip()
                if fmt not in ('Story','Post','Reel'): return self.send_data(400,json_bytes({'error':'Invalid content type'}))
                with connection() as c:
                    if not c.execute('SELECT id FROM media WHERE id=?',(mid,)).fetchone(): return self.send_data(404,json_bytes({'error':'Photo not found'}))
                    qid=uuid.uuid4().hex; t=now(); status='scheduled' if when else 'draft'
                    c.execute('INSERT INTO queue VALUES(?,?,?,?,?,?,?,?)',(qid,mid,caption,fmt,when or None,status,t,t))
                notify('Added to publishing queue',f'{fmt} saved as a {status} plan. Live Instagram publishing is not configured in local mode.','◷')
                return self.send_data(201,json_bytes({'ok':True,'state':state()}))
            if p=='/api/settings':
                body=self.read_json()
                with connection() as c:
                    for key in ('autopilot','story','caption'):
                        if key in body: c.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(bool(body[key]))))
                notify('Studio settings saved','Your preferences were saved on this device. Automation controls do not activate live publishing in local mode.','⚙')
                return self.send_data(200,json_bytes({'ok':True,'state':state()}))
            if p=='/api/notifications/clear':
                with connection() as c: c.execute('DELETE FROM activity')
                return self.send_data(200,json_bytes({'ok':True}))
            if p=='/api/reset':
                with connection() as c:
                    paths=[r['path'] for r in c.execute('SELECT path FROM media')]
                    c.execute('DELETE FROM media'); c.execute('DELETE FROM queue'); c.execute('DELETE FROM activity')
                for name in paths:
                    try: (MEDIA/name).unlink()
                    except OSError: pass
                notify('Workspace reset','Local media and queue plans were removed.','⌫')
                return self.send_data(200,json_bytes({'ok':True,'state':state()}))
            return self.send_data(404,json_bytes({'error':'Not found'}))
        except (ValueError, json.JSONDecodeError) as e: return self.send_data(400,json_bytes({'error':str(e)}))
        except Exception as e:
            print('[VIBEOS] API error:',repr(e)); return self.send_data(500,json_bytes({'error':'Unexpected local server error'}))
    def do_PUT(self):
        p=urlparse(self.path).path
        if not p.startswith('/api/queue/'): return self.send_data(404,json_bytes({'error':'Not found'}))
        qid=unquote(p.rsplit('/',1)[-1])
        try:
            b=self.read_json(); mid=str(b.get('mediaId','')); fmt=b.get('format','Story'); caption=str(b.get('caption',''))[:2200]; when=str(b.get('when','')).strip()
            if fmt not in ('Story','Post','Reel'): return self.send_data(400,json_bytes({'error':'Invalid content type'}))
            with connection() as c:
                if not c.execute('SELECT id FROM media WHERE id=?',(mid,)).fetchone(): return self.send_data(404,json_bytes({'error':'Photo not found'}))
                cur=c.execute('UPDATE queue SET media_id=?,caption=?,format=?,scheduled_at=?,status=?,updated_at=? WHERE id=?',(mid,caption,fmt,when or None,'scheduled' if when else 'draft',now(),qid))
                if not cur.rowcount: return self.send_data(404,json_bytes({'error':'Queue item not found'}))
            notify('Queue item updated',f'{fmt} plan updated.','◷'); return self.send_data(200,json_bytes({'ok':True,'state':state()}))
        except Exception: return self.send_data(400,json_bytes({'error':'Invalid request'}))
    def do_DELETE(self):
        p=urlparse(self.path).path
        if p.startswith('/api/queue/'):
            qid=unquote(p.rsplit('/',1)[-1])
            with connection() as c: cur=c.execute('DELETE FROM queue WHERE id=?',(qid,))
            if cur.rowcount: notify('Queue item removed','The planned content was removed from your queue.','⌫')
            return self.send_data(200,json_bytes({'ok':True,'state':state()}))
        if p.startswith('/api/media/'):
            mid=unquote(p.rsplit('/',1)[-1])
            with connection() as c:
                r=c.execute('SELECT * FROM media WHERE id=?',(mid,)).fetchone()
                if not r: return self.send_data(404,json_bytes({'error':'Photo not found'}))
                c.execute('DELETE FROM queue WHERE media_id=?',(mid,)); c.execute('DELETE FROM media WHERE id=?',(mid,))
            try: (MEDIA/r['path']).unlink()
            except OSError: pass
            notify('Photo deleted',f'{r["name"]} was removed from local media storage and its queue plans.','⌫')
            return self.send_data(200,json_bytes({'ok':True,'state':state()}))
        return self.send_data(404,json_bytes({'error':'Not found'}))

def main():
    init_db(); host=os.environ.get('VIBEOS_HOST','127.0.0.1'); port=int(os.environ.get('VIBEOS_PORT','8765'))
    server=ThreadingHTTPServer((host,port),Handler)
    print(f'\nVIBEOS Creator Studio is running: http://{host}:{port}')
    print(f'Data is stored locally in: {DATA}')
    print('Note: queue management is live; Instagram publishing requires hosted Meta/publishing integration.\n')
    try:
        if os.environ.get('VIBEOS_NO_BROWSER')!='1': threading.Timer(1.0,lambda:webbrowser.open(f'http://{host}:{port}')).start()
        server.serve_forever()
    except KeyboardInterrupt: print('\nStopping VIBEOS...')
    finally: server.server_close()
if __name__=='__main__': main()
