"""Convert raw or Netscape cookies to a request-scoped header without logging values."""
import re
import time
from urllib.parse import urlparse


def cookie_header(raw, url, provider):
    raw = raw.strip(' \r\n')
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or host not in {provider, 'www.' + provider}:
        return ''
    if not raw:
        return ''
    if '\t' not in raw and not raw.startswith('#'):
        if raw.lower().startswith('cookie:'):
            raw = raw[7:].strip()
        if '\r' in raw or '\n' in raw:
            raise ValueError('Cookie format invalid. Use a single-line header or a tab-separated Netscape export.')
        pairs = []
        for piece in raw.split(';'):
            name, sep, value = piece.strip().partition('=')
            if not sep or not re.fullmatch(r'[!#$%&\x27*+.^_`|~0-9A-Za-z-]+', name):
                raise ValueError('Cookie header contains an invalid name/value pair.')
            if any(ord(c) < 32 or ord(c) > 126 for c in value):
                raise ValueError('Cookie header contains unsupported characters.')
            pairs.append(name + '=' + value)
        return '; '.join(pairs)
    matches = []
    for line in raw.splitlines():
        if line.startswith('#HttpOnly_'):
            line = line[len('#HttpOnly_'):]
        elif line.startswith('#') or not line.strip():
            continue
        fields = line.split('\t')
        # Dashboard/config trimming can remove the last tab of an empty value.
        if len(fields) == 6:
            fields.append('')
        if len(fields) != 7:
            raise ValueError('Netscape cookie rows must have seven tab-separated fields. Export again without editing the file.')
        domain, subdomains, path, secure, expiry, name, value = fields
        domain = domain.lstrip('.').lower()
        # Never load unrelated website cookies from a browser-wide export.
        if domain not in {provider, 'www.' + provider}:
            continue
        if subdomains not in {'TRUE', 'FALSE'} or secure not in {'TRUE', 'FALSE'}:
            raise ValueError('Invalid Netscape cookie flags.')
        try:
            expired = int(expiry)
        except ValueError:
            raise ValueError('Invalid Netscape cookie expiry.') from None
        if expired and expired <= time.time():
            continue
        if host != domain and not (subdomains == 'TRUE' and host.endswith('.' + domain)):
            continue
        request_path = parsed.path or '/'
        if not path.startswith('/'):
            raise ValueError('Invalid Netscape cookie path.')
        if request_path != path and not (request_path.startswith(path) and
                                        (path.endswith('/') or request_path[len(path):].startswith('/'))):
            continue
        pair = cookie_header(name + '=' + value, url, provider)
        matches.append((len(path), pair))
    if not matches:
        raise ValueError('No unexpired matching cookies for this provider URL. Export a fresh session or clear the cookie setting for public access.')
    matches.sort(key=lambda item: -item[0])
    return '; '.join(pair for _, pair in matches)
