#!/usr/bin/env python3
"""Servidor del Devocional Diario con usuarios y sesiones."""

from http.server import HTTPServer, BaseHTTPRequestHandler
from contextlib import contextmanager
import urllib.request, urllib.parse
import json, re, os, threading, webbrowser, sqlite3, hashlib, secrets
from datetime import date, timedelta, datetime

PORT    = int(os.environ.get('PORT', 7291))
DIR     = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get('DB_PATH', os.path.join(DIR, 'devotional.db'))

# ── Database ──────────────────────────────────────────────────────────────

@contextmanager
def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS passages (
                date     TEXT PRIMARY KEY,
                ref      TEXT NOT NULL,
                title    TEXT,
                saved_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE NOT NULL COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at    TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT DEFAULT (datetime('now'))
            );
        ''')
        # reads table may already exist without user_id — migrate if needed
        cols = [r['name'] for r in conn.execute("PRAGMA table_info(reads)").fetchall()]
        if not cols:
            conn.execute('''
                CREATE TABLE reads (
                    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    date       TEXT NOT NULL,
                    is_read    INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (user_id, date)
                )
            ''')
        elif 'user_id' not in cols:
            # Old single-user schema — rename and recreate
            conn.execute('ALTER TABLE reads RENAME TO reads_v1')
            conn.execute('''
                CREATE TABLE reads (
                    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    date       TEXT NOT NULL,
                    is_read    INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (user_id, date)
                )
            ''')

# ── Auth helpers ──────────────────────────────────────────────────────────

def hash_pw(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}:{h}"

def verify_pw(password, stored):
    try:
        salt, _ = stored.split(':', 1)
        return stored == hash_pw(password, salt)
    except Exception:
        return False

def create_session(user_id):
    token = secrets.token_hex(32)
    with db() as conn:
        conn.execute('INSERT INTO sessions (token, user_id) VALUES (?,?)', (token, user_id))
    return token

def user_from_token(token):
    if not token:
        return None
    with db() as conn:
        row = conn.execute(
            'SELECT u.id, u.username FROM sessions s JOIN users u ON s.user_id=u.id WHERE s.token=?',
            (token,)
        ).fetchone()
    return dict(row) if row else None

# ── Business logic ────────────────────────────────────────────────────────

def db_save_passage(date_str, ref, title):
    with db() as conn:
        conn.execute(
            'INSERT OR IGNORE INTO passages (date, ref, title) VALUES (?,?,?)',
            (date_str, ref, title or '')
        )

def db_set_read(user_id, date_str, is_read):
    with db() as conn:
        conn.execute('''
            INSERT INTO reads (user_id, date, is_read, updated_at)
            VALUES (?,?,?,datetime('now'))
            ON CONFLICT(user_id, date) DO UPDATE SET
                is_read=excluded.is_read, updated_at=excluded.updated_at
        ''', (user_id, date_str, 1 if is_read else 0))

def db_get_history(user_id):
    with db() as conn:
        rows = conn.execute('''
            SELECT p.date, p.ref, p.title,
                   COALESCE(r.is_read, 0) AS is_read
            FROM passages p
            LEFT JOIN reads r ON p.date=r.date AND r.user_id=?
            ORDER BY p.date DESC
        ''', (user_id,)).fetchall()
    return [dict(r) for r in rows]

def db_get_reads(user_id, month=None):
    with db() as conn:
        if month:
            rows = conn.execute(
                "SELECT date, is_read FROM reads WHERE user_id=? AND date LIKE ?",
                (user_id, f'{month}%')
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT date, is_read FROM reads WHERE user_id=?', (user_id,)
            ).fetchall()
    return {r['date']: r['is_read'] for r in rows}

def calc_streak(user_id):
    with db() as conn:
        rows = conn.execute(
            "SELECT date FROM reads WHERE user_id=? AND is_read=1", (user_id,)
        ).fetchall()
    read_set = {r['date'] for r in rows}
    streak = 0
    d = date.today()
    while d.isoformat() in read_set:
        streak += 1
        d -= timedelta(days=1)
    return streak

def get_leaderboard():
    with db() as conn:
        users = conn.execute('SELECT id, username FROM users ORDER BY username').fetchall()
    month_prefix = date.today().strftime('%Y-%m')
    result = []
    for u in users:
        streak = calc_streak(u['id'])
        with db() as conn:
            mr = conn.execute(
                "SELECT COUNT(*) c FROM reads WHERE user_id=? AND date LIKE ? AND is_read=1",
                (u['id'], f'{month_prefix}%')
            ).fetchone()['c']
        result.append({'username': u['username'], 'streak': streak, 'month_reads': mr})
    result.sort(key=lambda x: (-x['streak'], -x['month_reads'], x['username']))
    return result

# ── Scraping ──────────────────────────────────────────────────────────────

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}
BOOK_RE = re.compile(r'\b((?:[1-3]\s)?[A-Z][a-z]+(?:\s[A-Z][a-z]+)*)\s+(\d+:\d+(?:-\d+)?)\b')
KNOWN_BOOKS = {
    'genesis','exodus','leviticus','numbers','deuteronomy','joshua','judges','ruth',
    'samuel','kings','chronicles','ezra','nehemiah','esther','job','psalms','psalm',
    'proverbs','ecclesiastes','isaiah','jeremiah','lamentations','ezekiel','daniel',
    'hosea','joel','amos','obadiah','jonah','micah','nahum','habakkuk','zephaniah',
    'haggai','zechariah','malachi','matthew','mark','luke','john','acts','romans',
    'corinthians','galatians','ephesians','philippians','colossians','thessalonians',
    'timothy','titus','philemon','hebrews','james','peter','jude','revelation',
}
BG_VERSIONS = {'rv60':'RVR1960','nbla':'NBLA','nvi':'NVI','rva':'RVA-2015','dhh':'DHH','nblh':'NBLH'}

def fetch_url(url, method='GET', body=None, extra_headers=None):
    h = dict(HEADERS)
    if extra_headers:
        h.update(extra_headers)
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=12) as r:
        return r.read().decode('utf-8', errors='replace')

def parse_duranno(html, date_str):
    tm = re.search(rf'href=["\'][^"\']*\?OD={re.escape(date_str)}["\'][^>]*>\s*([^<]+)\s*<', html, re.I)
    title, ref = (tm.group(1).strip() if tm else None), None
    for m in BOOK_RE.finditer(html):
        book = re.match(r'^(?:[1-3]\s)?([A-Z][a-z]+)', m.group(0))
        if book and book.group(1).lower() in KNOWN_BOOKS:
            ref = m.group(0).strip(); break
    return title, ref

def parse_biblegateway(html):
    verses, seen = [], set()
    for span in re.finditer(r'<span[^>]+class="[^"]*\btext\b[^"]*"[^>]*>(.*?)</span>', html, re.DOTALL):
        content = span.group(1)
        nm = re.search(r'<sup[^>]*class="versenum"[^>]*>(\d+)', content)
        if not nm: continue
        num = int(nm.group(1))
        if num in seen: continue
        seen.add(num)
        text = re.sub(r'<sup[^>]*>.*?</sup>', '', content, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\s+', ' ', text).strip().replace('\xa0', ' ')
        if text: verses.append({'verse': num, 'text': text})
    verses.sort(key=lambda v: v['verse'])
    return verses

# ── HTTP Handler ──────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(p.query)

        if p.path in ('/', '/devotional.html'):
            self._file('devotional.html', 'text/html; charset=utf-8')
        elif p.path == '/manifest.json':
            self._file('manifest.json', 'application/manifest+json')
        elif p.path == '/sw.js':
            self._file('sw.js', 'application/javascript')
        elif p.path == '/icon-192.png':
            self._file('icon-192.png', 'image/png')
        elif p.path == '/icon-512.png':
            self._file('icon-512.png', 'image/png')
        elif p.path == '/api/me':
            user = self._auth()
            if user: self._json({'id': user['id'], 'username': user['username']})
            else: self._json({'error': 'No autenticado'}, 401)
        elif p.path == '/api/passage':
            self._passage(q.get('date', [date.today().isoformat()])[0])
        elif p.path == '/api/bible':
            self._bible(q.get('ref',[''])[0], q.get('version',['rv60'])[0])
        elif p.path == '/api/history':
            user = self._require_auth(); user and self._json(db_get_history(user['id']))
        elif p.path == '/api/reads':
            user = self._require_auth()
            user and self._json(db_get_reads(user['id'], q.get('month',[None])[0]))
        elif p.path == '/api/leaderboard':
            if not self._require_auth(): return
            self._json(get_leaderboard())
        else:
            self._respond(404, 'text/plain', b'Not found')

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError:
            self._json({'error': 'JSON inválido'}, 400); return

        p = urllib.parse.urlparse(self.path)

        if p.path == '/api/register':
            self._register(data)
        elif p.path == '/api/login':
            self._login(data)
        elif p.path == '/api/logout':
            self._logout()
        elif p.path == '/api/read':
            user = self._require_auth()
            if not user: return
            d = data.get('date') or date.today().isoformat()
            is_read = bool(data.get('is_read', True))
            db_set_read(user['id'], d, is_read)
            self._json({'ok': True, 'date': d, 'is_read': is_read})
        elif p.path == '/api/save-passage':
            if not self._require_auth(): return
            ref = data.get('ref','')
            if ref: db_save_passage(data.get('date', date.today().isoformat()), ref, data.get('title',''))
            self._json({'ok': True})
        else:
            self._respond(404, 'text/plain', b'Not found')

    # ── Auth endpoints ────────────────────────────────────────────────────

    def _register(self, data):
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        if not username or not password:
            self._json({'error': 'Usuario y contraseña requeridos'}, 400); return
        if len(username) < 2:
            self._json({'error': 'El usuario debe tener al menos 2 caracteres'}, 400); return
        if len(password) < 4:
            self._json({'error': 'La contraseña debe tener al menos 4 caracteres'}, 400); return
        try:
            with db() as conn:
                conn.execute('INSERT INTO users (username, password_hash) VALUES (?,?)',
                             (username, hash_pw(password)))
                user_id = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()['id']
        except sqlite3.IntegrityError:
            self._json({'error': 'Ese nombre de usuario ya existe'}, 409); return
        token = create_session(user_id)
        self._json({'token': token, 'username': username})

    def _login(self, data):
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        with db() as conn:
            row = conn.execute('SELECT id, password_hash FROM users WHERE username=?', (username,)).fetchone()
        if not row or not verify_pw(password, row['password_hash']):
            self._json({'error': 'Usuario o contraseña incorrectos'}, 401); return
        token = create_session(row['id'])
        self._json({'token': token, 'username': username})

    def _logout(self):
        token = self._token()
        if token:
            with db() as conn:
                conn.execute('DELETE FROM sessions WHERE token=?', (token,))
        self._json({'ok': True})

    # ── Passage / Bible endpoints ─────────────────────────────────────────

    def _passage(self, date_str):
        with db() as conn:
            row = conn.execute('SELECT ref, title FROM passages WHERE date=?', (date_str,)).fetchone()
        if row:
            self._json({'reference': row['ref'], 'title': row['title'], 'cached': True}); return
        url  = 'https://www.duranno.com/livinglife/qt/reload_default1.asp'
        body = f'OD={date_str}'.encode()
        extra = {'Origin':'https://www.duranno.com',
                 'Referer':f'https://www.duranno.com/livinglife/qt/?OD={date_str}',
                 'Content-Type':'application/x-www-form-urlencoded'}
        try:
            html = fetch_url(url, method='POST', body=body, extra_headers=extra)
        except Exception as e:
            self._json({'error': str(e)}, 502); return
        title, ref = parse_duranno(html, date_str)
        if ref: db_save_passage(date_str, ref, title or '')
        self._json({'reference': ref, 'title': title})

    def _bible(self, ref, version):
        if not ref:
            self._json({'error': 'ref requerido'}, 400); return
        bg_ver = BG_VERSIONS.get(version, 'RVR1960')
        url = f'https://www.biblegateway.com/passage/?search={urllib.parse.quote_plus(ref)}&version={bg_ver}&interface=print'
        try:
            verses = parse_biblegateway(fetch_url(url))
        except Exception as e:
            self._json({'error': str(e)}, 502); return
        if not verses:
            self._json({'error': f'No se encontraron versículos para {ref} ({bg_ver})'}, 404); return
        self._json({'verses': verses, 'version': bg_ver})

    # ── Helpers ───────────────────────────────────────────────────────────

    def _token(self):
        auth = self.headers.get('Authorization', '')
        return auth[7:] if auth.startswith('Bearer ') else None

    def _auth(self):
        return user_from_token(self._token())

    def _require_auth(self):
        user = self._auth()
        if not user: self._json({'error': 'No autenticado'}, 401)
        return user

    def _file(self, filename, ctype):
        path = os.path.join(DIR, filename)
        try:
            with open(path, 'rb') as f:
                self._respond(200, ctype, f.read())
        except FileNotFoundError:
            self._respond(404, 'text/plain', b'Not found')

    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self._respond(status, 'application/json; charset=utf-8', data, cors=True)

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')

    def _respond(self, status, ctype, data, cors=False):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        if cors: self._cors()
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_): pass


def open_browser():
    import time; time.sleep(0.6)
    webbrowser.open(f'http://localhost:{PORT}')

if __name__ == '__main__':
    init_db()
    host   = '0.0.0.0' if os.environ.get('PORT') else 'localhost'
    server = HTTPServer((host, PORT), Handler)
    print(f'✓ Devocional → http://localhost:{PORT}')
    print(f'  DB: {DB_FILE}')
    print('  Ctrl+C para detener.')
    threading.Thread(target=open_browser, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nServidor detenido.')
