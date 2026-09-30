"""Kuku FM catalogue adapter. HTTP and credentials are owned by the caller."""
import re
from urllib.parse import urlparse, quote


def show_slug(url):
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'kukufm.com', 'www.kukufm.com'}:
        return None
    parts = parsed.path.strip('/').split('/')
    if len(parts) != 2 or parts[0] not in {'show', 'audiobook', 'story'}:
        return None
    return parts[1] if re.fullmatch(r'[A-Za-z0-9_-]+', parts[1]) else None


def api_url(slug, page):
    return f'https://kukufm.com/api/v2.3/channels/{quote(slug, safe="")}/episodes/?page={page}'


def normalize_page(data, slug, page):
    if not isinstance(data, dict) or not isinstance(data.get('episodes'), list):
        raise ValueError('Kuku FM returned no episode list. Check login or API compatibility.')
    show = data.get('show') or {}
    if not isinstance(show, dict):
        raise ValueError('Kuku FM returned invalid show metadata.')
    entries = []
    for item in data['episodes']:
        if not isinstance(item, dict):
            raise ValueError('Kuku FM returned invalid episode metadata.')
        number = item.get('index')
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError('Kuku FM episode numbering is missing or unsupported.')
        identity = str(item.get('id') or item.get('slug') or f'{slug}:{number}')
        locked = item.get('is_locked')
        unlocked = item.get('is_unlocked')
        access = 'unknown'
        if locked is True or item.get('is_play_locked') is True:
            access = 'locked'
        elif locked is False or unlocked is True or item.get('is_free') is True or item.get('is_free_unlocked') is True:
            access = 'available'
        content = item.get('content') or {}
        media = content.get('hls_url') if isinstance(content, dict) else None
        entries.append({
            'id': identity, 'number': number, 'title': str(item.get('title') or f'Episode {number}'),
            'url': 'https://kukufm.com/show/' + slug, 'access': access,
            'provider': 'kuku', 'show_slug': slug, 'page': page,
            'media_url': media if isinstance(media, str) else None,
        })
    total = show.get('n_episodes')
    if total is not None and (not isinstance(total, int) or total < 0):
        raise ValueError('Kuku FM returned an invalid episode count.')
    more = data.get('has_more')
    if not isinstance(more, bool):
        raise ValueError('Kuku FM did not return a pagination status.')
    return {'entries': entries, 'total': total or 0, 'has_more': more,
            'title': str(show.get('title') or slug), 'thumbnail': show.get('original_image')}


def get_catalog(url, fetch_json, progress=None, session=False):
    slug = show_slug(url)
    if not slug:
        raise ValueError('Send a full Kuku FM show URL, not an app/share short link.')
    entries = {}
    catalog = {'title': slug, 'total': 0, 'warning': '', 'session_request': session,
               'provider': 'kuku', 'thumbnail': None}
    for page in range(1, 1001):
        try:
            parsed = normalize_page(fetch_json(api_url(slug, page)), slug, page)
            before = len(entries)
            for entry in parsed['entries']:
                entries[entry['id']] = entry
            catalog['title'] = parsed['title']
            catalog['thumbnail'] = parsed['thumbnail'] or catalog['thumbnail']
            catalog['total'] = max(catalog['total'], parsed['total'])
            if progress:
                progress(len(entries), catalog['total'])
            if not parsed['has_more']:
                if catalog['total'] and len(entries) < catalog['total']:
                    raise ValueError('Incomplete catalogue.')
                break
            if len(entries) == before:
                raise ValueError('Repeated or empty episode page.')
        except Exception:
            if not entries:
                raise
            catalog['warning'] = 'Kuku FM list is incomplete. ALL selects only the listed episodes. Send the show link again to retry.'
            break
    else:
        catalog['warning'] = 'Catalogue page limit reached. ALL selects only listed episodes.'
    catalog['entries'] = sorted(entries.values(), key=lambda e: (e['number'], e['id']))
    return catalog


def refresh_episode(entry, fetch_json):
    # Refresh signed media URLs and current access before each download.
    page = normalize_page(fetch_json(api_url(entry['show_slug'], entry['page'])),
                          entry['show_slug'], entry['page'])
    current = next((e for e in page['entries'] if e['id'] == entry['id']), None)
    if not current:
        raise ValueError('Episode moved or disappeared. Reload the show list.')
    if current['access'] == 'locked':
        raise ValueError('This Kuku FM episode is locked for the current session.')
    media = current['media_url']
    if not media or urlparse(media).scheme not in {'http', 'https'}:
        raise ValueError('Kuku FM did not expose a playable media URL for this episode.')
    return current, page
