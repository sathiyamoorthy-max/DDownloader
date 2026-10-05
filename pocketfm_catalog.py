"""Parse PocketFM's public web catalogue without executing page JavaScript."""

import json
import re
from html import unescape, escape
from html.parser import HTMLParser
from urllib.parse import urlparse


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.sources = []
        self.title = None
        self.image = None
        self._parts = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self._parts = []
            if attrs.get("src"):
                self.sources.append(attrs["src"])
        if tag == "meta" and attrs.get("property") == "og:title":
            self.title = attrs.get("content")
        if tag == "meta" and attrs.get("property") == "og:image":
            self.image = attrs.get("content")

    def handle_data(self, data):
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._parts is not None:
            self.scripts.append("".join(self._parts))
            self._parts = None


def flight_values(text):
    """Read JSON records only; ignore React imports and non-JSON records."""
    for line in text.splitlines():
        _, sep, payload = line.partition(":")
        if sep:
            try:
                yield json.loads(payload)
            except (ValueError, RecursionError):
                continue


def page_values(html):
    parser = PageParser()
    parser.feed(html)
    pieces = []
    for script in parser.scripts:
        try:
            yield json.loads(script)
            continue
        except (ValueError, RecursionError):
            pass
        for match in re.finditer(r"self\.__next_f\.push\(", script):
            try:
                chunk, _ = json.JSONDecoder().raw_decode(script[match.end():].lstrip())
            except (ValueError, RecursionError):
                continue
            if isinstance(chunk, list) and len(chunk) > 1 and chunk[0] == 1:
                if isinstance(chunk[1], str):
                    pieces.append(chunk[1])
    yield from flight_values("".join(pieces))


def objects(value):
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))


def episode_access(story):
    """Classify only explicit access metadata, never infer access from a URL."""
    locked, unlocked = story.get("is_locked"), story.get("is_unlocked")
    if locked is True:
        return "unknown" if unlocked is True else "locked"
    if locked is False or unlocked is True:
        return "available"
    coins = story.get("coins_required")
    if isinstance(coins, (int, float)) and not isinstance(coins, bool):
        if coins > 0:
            return "locked"
        if coins == 0:
            return "available"
    return "unknown"


def access_label(access, session=False):
    if access == "available":
        return "🟢 Available (session)" if session else "🟢 Public"
    return "🔒 Locked" if access == "locked" else "❔ Unknown"


def access_summary(entries, session=False):
    counts = {key: 0 for key in ("available", "locked", "unknown")}
    for entry in entries:
        counts[entry.get("access", "unknown")] += 1
    return " | ".join(f"{access_label(key, session)}: {count}" for key, count in counts.items())


def episode_list_page(entries, page=1, session=False):
    page_size = 20
    pages = max(1, (len(entries) + page_size - 1) // page_size)
    if page < 1 or page > pages:
        raise ValueError(f"Choose a page from 1 to {pages}: /episodes 1")
    lines = [f"Episodes — page {page}/{pages}"]
    for entry in entries[(page - 1) * page_size:page * page_size]:
        title = " ".join(str(entry.get("title", "")).split())[:70]
        lines.append(f"{entry['number']}. {access_label(entry.get('access'), session)} — {title}")
    if page < pages:
        lines.append(f"Next: /episodes {page + 1}")
    return "\n".join(lines)


def show_metadata(record):
    """Keep only display fields supplied by a matching show record."""
    aliases = {
        'language': ('language', 'show_language'),
        'plays': ('play_count', 'plays', 'total_plays'),
        'rating': ('rating', 'average_rating'),
        'reviews': ('rating_count', 'ratings_count', 'review_count'),
        'author': ('author_name', 'author'),
        'genre': ('genre', 'genres', 'category_name'),
        'thumbnail': ('image_url', 'thumbnail_url'),
    }
    result = {}
    for key, names in aliases.items():
        for name in names:
            value = record.get(name)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip():
                result[key] = str(value)
                break
            if key == 'genre' and isinstance(value, list):
                labels = [v for v in value if isinstance(v, str)]
                if labels:
                    result[key] = ', '.join(labels)
                    break
    user = record.get('user_info')
    if not result.get('author') and isinstance(user, dict) and isinstance(user.get('fullname'), str):
        result['author'] = user['fullname']
    return result


def story_card_text(catalog, url):
    """Telegram HTML caption: source values only, missing values explicit."""
    metadata = catalog.get('metadata') or {}
    def clean(value, limit=65):
        return escape(' '.join(str(value).split())[:limit])
    def field(name):
        return clean(metadata.get(name) or 'Not provided')
    title = clean(catalog.get('title') or 'Story', 90)
    identity = clean(urlparse(url).path.rstrip('/').rsplit('/', 1)[-1], 200)
    total = catalog.get('total') or 'Unknown'
    lines = [f'📖 <b>{title}</b>', '', f'🗣 Language: {field("language")}',
             f'📊 Total episodes: {clean(total)}', f'🏆 Plays: {field("plays")}',
             f'⭐ Rating: {field("rating")}', f'💬 Reviews: {field("reviews")}',
             f'✍️ Author: {field("author")}', f'🎭 Genre: {field("genre")}',
             f'🆔 Show ID: <code>{identity}</code>']
    entries = catalog.get('entries') or []
    counts = {key: sum(e.get('access', 'unknown') == key for e in entries)
              for key in ('available', 'locked', 'unknown')}
    lines.extend(['', f'Loaded: {len(entries)} | Available: {counts["available"]} | Locked: {counts["locked"]} | Unknown: {counts["unknown"]}',
                  'Access is catalogue metadata; playback is not verified.', '/episodes 1 • /available • /save'])
    return '\n'.join(lines)


def catalog_from_values(values, show_id):
    entries = {}
    total = 0
    title = None
    next_ptr = None
    longest_page = -1
    metadata = {}
    for value in values:
        for item in objects(value):
            if item.get("show_id") != show_id or not isinstance(item.get("stories"), list):
                continue
            title = item.get("show_title") or title
            metadata.update(show_metadata(item))
            count = item.get("episodes_count")
            if isinstance(count, int):
                total = max(total, count)
            stories = item["stories"]
            if len(stories) > longest_page:
                longest_page = len(stories)
                next_ptr = item.get("next_ptr")
            for story in stories:
                if not isinstance(story, dict):
                    continue
                story_id = story.get("story_id")
                if not isinstance(story_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", story_id):
                    continue
                if story.get("show_id", show_id) != show_id:
                    continue
                number = story.get("natural_sequence_number") or story.get("seq_number")
                if not isinstance(number, int) or number < 1:
                    continue
                entries[story_id] = {
                    "id": story_id, "number": number,
                    "title": story.get("story_title") or f"Episode {number}",
                    "url": "https://pocketfm.com/episode/" + story_id,
                    "access": episode_access(story),
                }
    return {
        "title": title, "total": total, "next_ptr": next_ptr,
        "metadata": metadata, "thumbnail": metadata.get('thumbnail'),
        "entries": sorted(entries.values(), key=lambda e: (e["number"], e["id"])),
    }


def episode_action_id(script):
    match = re.search(
        r'createServerReference\)\(["\']([a-f0-9]{40,64})["\']'
        r'[^;]{0,300}?["\']fetchEpisodeList["\']\)', script,
    )
    return match.group(1) if match else None


def action_catalog(text, show_id):
    # The action response contains the page too. Only use the returned action
    # result, otherwise the initial page can reset the pagination cursor.
    for value in flight_values(text):
        if isinstance(value, dict) and isinstance(value.get("result"), dict):
            result = value["result"]
            if result.get("show_id") == show_id and "stories" in result:
                return catalog_from_values([result], show_id)
    raise ValueError("PocketFM did not return an episode-list page.")


def select_entries(text, entries):
    """Select inclusive episode numbers, including open-ended star patterns.

    Open ends refer to the loaded catalogue, which may be partial. Never
    interpret malformed input as ALL or substitute list offsets for numbers.
    """
    text = text.strip().lower()
    if text == 'available':
        return [entry for entry in entries if entry.get('access') == 'available']
    if text in {"all", "அனைத்தும்", "*"}:
        return list(entries)
    match = re.fullmatch(r"(\d+)(?:\s*[-–]\s*|\s+)(\d+)", text)
    star = re.fullmatch(r"(\d*)\s*\*\s*(\d*)", text)
    if text.isdigit():
        start = end = int(text)
    elif match:
        start, end = map(int, match.groups())
    elif star:
        if not entries:
            raise ValueError("The catalogue is empty. Send the show link again.")
        first, last = star.groups()
        start = int(first) if first else 1
        end = int(last) if last else max(e["number"] for e in entries)
    else:
        raise ValueError("Send ALL, *, an episode number, 1-15, *10, 25*, or 10*20.")
    if start < 1 or end < start:
        raise ValueError("Episode range must start at 1 or above and be ascending.")
    selected = [e for e in entries if start <= e["number"] <= end]
    if len({e["number"] for e in selected}) != end - start + 1:
        raise ValueError("Some episode numbers in that range are missing from the catalogue.")
    return selected


def episode_metadata(html, episode_id):
    """Select only the requested episode, never another episode's media."""
    for value in page_values(html):
        for item in objects(value):
            if item.get("story_id") == episode_id and item.get("story_title"):
                return item
    return None


def public_episode_candidates(story):
    candidates = []
    for key in ("media_url", "video_url"):
        url = story.get(key)
        if isinstance(url, str) and urlparse(url).scheme in {"http", "https"}:
            if "mock" not in url.lower() and url not in candidates:
                candidates.append(unescape(url))
    return candidates
