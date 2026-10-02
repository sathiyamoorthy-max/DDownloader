"""Validate untrusted Telegram WebApp data before invoking bot actions."""
import json
import re
from urllib.parse import urlsplit

ACTIONS = {'show', 'download', 'select', 'episodes', 'status', 'resume', 'retry',
           'cancel', 'failures', 'authstatus', 'accountcheck', 'system'}


def parse_action(raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 4096:
        raise ValueError('Mini App request is too large.')
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('Invalid Mini App request.') from None
    if not isinstance(data, dict) or data.get('action') not in ACTIONS:
        raise ValueError('Unknown Mini App action.')
    if set(data) - {'action', 'value', 'format'}:
        raise ValueError('Unexpected Mini App fields.')
    action = data['action']
    value = data.get('value', '')
    fmt = data.get('format', 'mp3')
    if fmt not in ('mp3', 'mp4') or not isinstance(value, str) or len(value) > 1800:
        raise ValueError('Invalid output format or input.')
    value = value.strip()
    if action in {'show', 'download'} or (action == 'accountcheck' and value):
        try:
            url = urlsplit(value)
            if url.scheme != 'https' or not url.hostname or url.username or url.password:
                raise ValueError()
            if re.search(r'[\s\x00-\x1f]', value):
                raise ValueError()
            if action in {'show', 'accountcheck'} and not (
                url.hostname in {'pocketfm.com', 'www.pocketfm.com', 'kukufm.com', 'www.kukufm.com'}
            ):
                raise ValueError()
        except ValueError:
            raise ValueError('Use one valid HTTPS provider/show or media URL.') from None
    elif action == 'select':
        if not re.fullmatch(r'(?i:ALL|AVAILABLE)|\d{1,5}(?:\s*[- ]\s*\d{1,5})?', value):
            raise ValueError('Choose AVAILABLE, ALL, an episode number or a range such as 1-5.')
    elif value:
        raise ValueError('This action does not accept input.')
    return action, value, fmt
