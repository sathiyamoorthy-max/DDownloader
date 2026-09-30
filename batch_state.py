"""One resumable series batch per user/chat; no cookies or signed media URLs."""
import json
import sqlite3
import time
from pathlib import Path


def failure_category(error, stage="download"):
    text = str(error).lower()
    for category in ('configuration_error', 'audio_decode_failed', 'session_or_access_denied', 'locked',
                     'timeout', 'upload_failed', 'download_or_processing_failed'):
        if '[' + category + ']' in text:
            return category
    if any(word in text for word in ('allowed_user_ids', 'cookie format', 'cookie header',
                                     'netscape cookie', 'no unexpired matching cookies')):
        return 'configuration_error'
    if any(word in text for word in ('could not be decoded', 'audio playback validation')):
        return 'audio_decode_failed'
    if any(word in text for word in ('401', '403', 'refused access', 'unauthorized', 'expired token')):
        return 'session_or_access_denied'
    if 'locked' in text:
        return 'locked'
    if 'timed out' in text or 'timeout' in text:
        return 'timeout'
    if stage == 'uploading':
        return 'upload_failed'
    return 'download_or_processing_failed'


class BatchStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS batches (owner INTEGER, chat INTEGER, body TEXT, PRIMARY KEY(owner, chat))')

    def connect(self):
        return sqlite3.connect(self.path, timeout=20)

    def save(self, owner, chat, batch):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO batches VALUES (?, ?, ?)',
                       (owner, chat, json.dumps(batch, ensure_ascii=False)))

    def load(self, owner, chat):
        with self.connect() as db:
            row = db.execute('SELECT body FROM batches WHERE owner=? AND chat=?', (owner, chat)).fetchone()
        return json.loads(row[0]) if row else None

    def create(self, owner, chat, entries, title):
        keys = ('id', 'number', 'provider', 'show_id', 'cursor', 'show_slug', 'page')
        items = []
        for entry in entries:
            clean = {k: entry[k] for k in keys if k in entry}
            # Reconstruct known provider URLs rather than retaining query tokens.
            if clean.get('provider') == 'kuku':
                clean['url'] = 'https://kukufm.com/show/' + clean['show_slug']
            else:
                clean['url'] = 'https://pocketfm.com/episode/' + clean['id']
            items.append({'entry': clean, 'status': 'pending', 'reason': ''})
        batch = {'title': title, 'created': time.time(), 'items': items}
        self.save(owner, chat, batch)
        return batch


def pending_indices(batch, retry=False):
    wanted = {'failed'} if retry else {'pending', 'running'}
    return [i for i, item in enumerate(batch['items']) if item['status'] in wanted]


def failure_report(batch):
    failed = [item for item in batch['items'] if item['status'] == 'failed']
    lines = [f'Failed episodes: {len(failed)}']
    for item in failed[:60]:
        lines.append(f"{item['entry']['number']}: {item['reason']}")
    if len(failed) > 60:
        lines.append('Only the first 60 failures are shown. /retry retries all failed episodes.')
    return '\n'.join(lines)
