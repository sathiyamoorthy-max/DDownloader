"""Parse PocketFM's public web catalogue without executing page JavaScript."""

import json
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.sources = []
        self.title = None
        self._parts = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self._parts = []
            if attrs.get("src"):
                self.sources.append(attrs["src"])
        if tag == "meta" and attrs.get("property") == "og:title":
            self.title = attrs.get("content")

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


def catalog_from_values(values, show_id):
    entries = {}
    total = 0
    title = None
    next_ptr = None
    longest_page = -1
    for value in values:
        for item in objects(value):
            if item.get("show_id") != show_id or not isinstance(item.get("stories"), list):
                continue
            title = item.get("show_title") or title
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
                }
    return {
        "title": title, "total": total, "next_ptr": next_ptr,
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
    text = text.strip().lower()
    if text in {"all", "அனைத்தும்"}:
        return list(entries)
    match = re.fullmatch(r"(\d+)(?:\s*[-–]\s*|\s+)(\d+)", text)
    if text.isdigit():
        start = end = int(text)
    elif match:
        start, end = map(int, match.groups())
    else:
        raise ValueError("Send ALL, an episode number, or a range such as 1-15.")
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
