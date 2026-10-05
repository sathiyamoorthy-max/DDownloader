"""User/chat-scoped bookmarks. Store canonical show links, never session URLs."""
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit


def show_link(value):
    value = value.strip()
    if re.fullmatch(r'[a-fA-F0-9]{24,64}', value):
        return 'https://pocketfm.com/show/' + value.lower()
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password or parsed.port:
        raise ValueError('Use a PocketFM show ID or a PocketFM/Kuku show URL.')
    host = (parsed.hostname or '').lower().removeprefix('www.')
    if host == 'pocketfm.com':
        match = re.fullmatch(r'/(?:[a-z]{2}-[a-z]{2}/)?show/([a-fA-F0-9]{24,64})/?', parsed.path)
        if match:
            return 'https://pocketfm.com/show/' + match[1].lower()
    if host == 'kukufm.com':
        match = re.fullmatch(r'/(?:show|story|audiobook)/([A-Za-z0-9_-]{1,200})/?', parsed.path)
        if match:
            return 'https://kukufm.com/show/' + match[1]
    raise ValueError('Use a PocketFM show ID or a PocketFM/Kuku show URL.')


class StoryLibrary:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS saved_stories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL,
                chat INTEGER NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
                updated REAL NOT NULL, UNIQUE(owner, chat, url))''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def save(self, owner, chat, url, title):
        url = show_link(url)
        title = ' '.join(str(title).split())[:120] or url.rsplit('/', 1)[-1]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT id FROM saved_stories WHERE owner=? AND chat=? AND url=?',
                                  (owner, chat, url)).fetchone()
            if not existing and db.execute('SELECT count(*) FROM saved_stories WHERE owner=? AND chat=?',
                                          (owner, chat)).fetchone()[0] >= 200:
                raise ValueError('Saved-story limit is 200 per chat. Use /forget <ID> first.')
            db.execute('''INSERT INTO saved_stories(owner,chat,url,title,updated) VALUES(?,?,?,?,?)
                ON CONFLICT(owner,chat,url) DO UPDATE SET title=excluded.title, updated=excluded.updated''',
                       (owner, chat, url, title, time.time()))
            return db.execute('SELECT id FROM saved_stories WHERE owner=? AND chat=? AND url=?',
                              (owner, chat, url)).fetchone()[0]

    def list(self, owner, chat, query='', page=1):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM saved_stories WHERE owner=? AND chat=? ORDER BY updated DESC, id DESC',
                              (owner, chat)).fetchall()
        # Python casefold supports Tamil and Unicode; SQL wildcards stay literal.
        matches = [dict(row) for row in rows if query.casefold() in row['title'].casefold()]
        return matches[(page-1)*10:page*10], len(matches)

    def get(self, owner, chat, identity):
        with self.connect() as db:
            row = db.execute('SELECT * FROM saved_stories WHERE owner=? AND chat=? AND id=?',
                             (owner, chat, identity)).fetchone()
        return dict(row) if row else None

    def delete(self, owner, chat, identity):
        with self.connect() as db:
            return bool(db.execute('DELETE FROM saved_stories WHERE owner=? AND chat=? AND id=?',
                                   (owner, chat, identity)).rowcount)
