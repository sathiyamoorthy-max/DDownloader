"""Account-aware PocketFM show API adapter; credentials stay in the HTTP layer."""
from urllib.parse import urlencode, urlparse
import re
from pocketfm_catalog import objects, catalog_from_values, episode_access, public_episode_candidates

API_HOST = 'api.pocketfm.com'
API_PATH = '/v2/content_api/show.get_details'


def api_url(show_id, cursor):
    return 'https://' + API_HOST + API_PATH + '?' + urlencode(
        {'show_id': show_id, 'curr_ptr': cursor, 'info_level': 'max'})


def normalize(data, show_id, cursor):
    if not isinstance(data, dict) or 'result' not in data:
        raise ValueError('PocketFM API returned no catalogue. Check account access/API compatibility.')
    records = [r for r in objects(data['result'])
               if isinstance(r.get('stories'), list) and r.get('show_id', show_id) == show_id]
    if not records:
        raise ValueError('PocketFM API returned no matching show records.')
    record = dict(max(records, key=lambda r: len(r['stories'])))
    record['show_id'] = show_id
    result = catalog_from_values([record], show_id)
    result['thumbnail'] = record.get('image_url')
    stories = {str(s.get('story_id')): s for s in record['stories'] if isinstance(s, dict)}
    for entry in result['entries']:
        entry.update(provider='pocketfm_api', show_id=show_id, cursor=cursor,
                     media_candidates=public_episode_candidates(stories[entry['id']]))
    return result


def get_catalog(url, fetch_json, progress=None, session=False):
    parsed = urlparse(url)
    match = re.search(r'/show/([A-Za-z0-9_-]+)', parsed.path)
    if parsed.hostname not in {'pocketfm.com', 'www.pocketfm.com'} or not match:
        raise ValueError('Send a PocketFM show URL.')
    show_id = match.group(1)
    entries, seen = {}, set()
    cursor = 0
    catalog = {'title': show_id, 'total': 0, 'warning': '', 'session_request': session,
               'provider': 'pocketfm_api', 'thumbnail': None}
    for _ in range(1000):
        try:
            if cursor in seen:
                raise ValueError('PocketFM repeated its pagination cursor.')
            seen.add(cursor)
            page = normalize(fetch_json(api_url(show_id, cursor)), show_id, cursor)
            before = len(entries)
            entries.update({e['id']: e for e in page['entries']})
            catalog['title'] = page['title'] or catalog['title']
            catalog['total'] = max(catalog['total'], page['total'])
            catalog['thumbnail'] = page['thumbnail'] or catalog['thumbnail']
            if progress:
                progress(len(entries), catalog['total'])
            cursor = page['next_ptr']
            if catalog['total'] and len(entries) >= catalog['total']:
                break
            if cursor is None or cursor == -1:
                if catalog['total'] and len(entries) < catalog['total']:
                    raise ValueError('Incomplete API catalogue.')
                break
            if not isinstance(cursor, int) or len(entries) == before:
                raise ValueError('PocketFM pagination stopped making progress.')
        except Exception:
            if not entries:
                raise
            catalog['warning'] = 'PocketFM API returned a partial list. ALL selects only listed episodes. Reload the show to retry.'
            break
    else:
        catalog['warning'] = 'Catalogue page limit reached; only listed episodes are available.'
    catalog['entries'] = sorted(entries.values(), key=lambda e: (e['number'], e['id']))
    return catalog


def refresh_episode(entry, fetch_json):
    data = fetch_json(api_url(entry['show_id'], entry['cursor']))
    page = normalize(data, entry['show_id'], entry['cursor'])
    current = next((e for e in page['entries'] if e['id'] == entry['id']), None)
    if not current:
        raise ValueError('Episode is no longer on this API page. Reload the show.')
    # Recheck raw lock flags even when contradictory metadata displays Unknown.
    raw = next((s for s in objects(data['result']) if s.get('story_id') == entry['id']), {})
    if raw.get('is_locked') is True or episode_access(raw) == 'locked':
        raise ValueError('This PocketFM episode is locked for the current account.')
    if not current['media_candidates']:
        raise ValueError('No supported media URL is available for this PocketFM episode.')
    return current, page
