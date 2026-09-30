import ast
import json
import logging
import re
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlparse, urljoin, unquote, parse_qs

from pocketfm_catalog import (
    page_values, catalog_from_values, action_catalog, select_entries,
    episode_action_id, episode_metadata, public_episode_candidates,
    episode_access, access_summary, episode_list_page,
)

SHOW = 'test-show'

def story(n):
    return {'show_id': SHOW, 'story_id': f'story-{n}',
            'seq_number': n, 'story_title': f'Episode {n}'}

def payload(numbers, total=640, cursor=20):
    return {'show_id': SHOW, 'show_title': 'Test (series)',
            'episodes_count': total, 'next_ptr': cursor,
            'stories': [story(n) for n in numbers]}

def html_page(data, split=False):
    text = '1:' + json.dumps(data) + '\n'
    chunks = [text[:35], text[35:]] if split else [text]
    return ''.join('<script>self.__next_f.push(' + json.dumps([1, c]) + ')</script>'
                   for c in chunks)

def functions(*names, **values):
    # bot_app initializes Telegram at import time. Isolate its actual function
    # definitions so regression tests require no token, network or Telegram send.
    tree = ast.parse((Path(__file__).parents[1] / 'bot_app.py').read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for n in nodes:
        n.decorator_list = []
    ns = dict(re=re, json=json, time=time, threading=threading, Path=Path,
              urlparse=urlparse, urljoin=urljoin, unquote=unquote, parse_qs=parse_qs)
    ns.update(values)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot_app_functions', 'exec'), ns)
    return ns

class CatalogueTests(unittest.TestCase):
    def test_access_is_explicit_and_session_sensitive(self):
        for item, expected in [
            ({"coins_required": 0}, "available"),
            ({"coins_required": 11}, "locked"),
            ({"is_locked": True, "coins_required": 0}, "locked"),
            ({"is_locked": False, "coins_required": 11}, "available"),
            ({"is_unlocked": True, "coins_required": 11}, "available"),
            ({"is_locked": True, "is_unlocked": True}, "unknown"),
            ({"media_url": "https://example.com/audio.mp3"}, "unknown"),
            ({}, "unknown"),
        ]:
            self.assertEqual(episode_access(item), expected)
        data = payload([1, 2, 3])
        data['stories'][0]['coins_required'] = 0
        data['stories'][1]['is_locked'] = True
        cat = catalog_from_values([data], SHOW)
        self.assertEqual([e['access'] for e in cat['entries']], ['available', 'locked', 'unknown'])
        self.assertIn('Public: 1', access_summary(cat['entries']))
        self.assertNotIn('Public', access_summary(cat['entries'], session=True))

    def test_episode_pages_fit_telegram_and_do_not_omit_entries(self):
        entries = catalog_from_values([payload(range(1, 641))], SHOW)['entries']
        for e in entries: e['title'] = 'Long title ' * 80
        first = episode_list_page(entries)
        last = episode_list_page(entries, 32)
        self.assertLess(len(first.encode('utf-16-le')) // 2, 4096)
        self.assertIn('Next: /episodes 2', first)
        self.assertIn('640.', last)
        self.assertNotIn('Next:', last)
        with self.assertRaises(ValueError): episode_list_page(entries, 33)

    def test_split_react_chunks_parentheses_dedup_order_and_show_scope(self):
        data = [payload([3, 1, 2]), payload([1]), dict(payload([9]), show_id='other')]
        cat = catalog_from_values(page_values(html_page(data, True)), SHOW)
        self.assertEqual([e['number'] for e in cat['entries']], [1, 2, 3])
        self.assertEqual(cat['title'], 'Test (series)')
        self.assertEqual(cat['total'], 640)

    def test_action_ignores_embedded_initial_page(self):
        response = '0:' + json.dumps({'page': payload(range(1, 21))}) + '\n'
        response += '1:' + json.dumps({'result': payload(range(21, 41), cursor=40)})
        cat = action_catalog(response, SHOW)
        self.assertEqual(cat['next_ptr'], 40)
        self.assertEqual(cat['entries'][0]['number'], 21)

    def test_unknown_action_format_fails(self):
        with self.assertRaises(ValueError):
            action_catalog('1:{"message":"Login required"}', SHOW)

    def test_action_is_named_and_discovered_not_executed(self):
        action = '4' * 42
        script = f'(0,u.createServerReference)("{action}",u.callServer,void 0,u.findSourceMapURL,"fetchEpisodeList")'
        self.assertEqual(episode_action_id(script), action)
        self.assertIsNone(episode_action_id(script.replace('fetchEpisodeList', 'deleteAccount')))

    def test_all_does_not_truncate_640_entries(self):
        entries = catalog_from_values([payload(range(1, 641))], SHOW)['entries']
        self.assertEqual(len(select_entries('ALL', entries)), 640)
        for text in ('1-15', '1 15', '1–15'):
            self.assertEqual(len(select_entries(text, entries)), 15)
        self.assertEqual(select_entries('640', entries)[0]['number'], 640)
        for text in ('0', '15-1', '1-641', 'hello'):
            with self.assertRaises(ValueError): select_entries(text, entries)

    def test_actual_episode_numbers_not_list_positions(self):
        entries = catalog_from_values([payload([1, 3])], SHOW)['entries']
        self.assertEqual(select_entries('3', entries)[0]['id'], 'story-3')
        with self.assertRaises(ValueError): select_entries('1-3', entries)

    def test_star_selection_includes_endpoints_and_preserves_access(self):
        entries = catalog_from_values([payload(range(1, 641))], SHOW)['entries']
        entries[9]['access'] = 'locked'
        for pattern, first, last in [('*', 1, 640), ('*10', 1, 10),
                                      ('25*', 25, 640), ('10*20', 10, 20),
                                      (' 10 * 20 ', 10, 20), ('640*', 640, 640)]:
            with self.subTest(pattern=pattern):
                result = select_entries(pattern, entries)
                self.assertEqual([e['number'] for e in result], list(range(first, last + 1)))
        self.assertEqual(select_entries('10*10', entries)[0]['access'], 'locked')

    def test_star_selection_rejects_gaps_and_bad_input(self):
        entries = catalog_from_values([payload([1, 3, 4])], SHOW)['entries']
        for pattern in ('**', '1**3', '4*3', '*0', '0*', '5*', '*5', '*3', '1*'):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                select_entries(pattern, entries)
        self.assertEqual([e['number'] for e in select_entries('3*', entries)], [3, 4])
        self.assertEqual(select_entries('*', []), [])
        with self.assertRaises(ValueError): select_entries('3*', [])

    def test_requested_episode_not_recommendation(self):
        a, b = story(1), story(2)
        a['media_url'] = 'https://cdn.example/one.mp3'
        b['media_url'] = 'https://cdn.example/two.mp3'
        b['media_url_enc'] = 'https://cdn.example/encrypted.mpd'
        selected = episode_metadata(html_page([a, b]), 'story-2')
        self.assertEqual(public_episode_candidates(selected), ['https://cdn.example/two.mp3'])
        self.assertIsNone(episode_metadata(html_page([a, b]), 'story-3'))

    def test_pagination_covers_full_catalogue(self):
        from pocketfm_catalog import PageParser
        first = html_page(payload(range(1, 21))) + '<script src="/_next/static/list.js"></script>'
        response = Mock(text=first, url='https://pocketfm.com/show/' + SHOW)
        action = Mock(text='(0,u.createServerReference)("' + '4'*42 + '",u.callServer,void 0,u.findSourceMapURL,"fetchEpisodeList")')
        get = Mock(side_effect=[response, action])
        def post(url, **kwargs):
            args = json.loads(kwargs['data'])[0]
            start = args['currPtr']
            return Mock(text='1:' + json.dumps({'result': payload(range(start+1, min(start+20,640)+1), cursor=start+20 if start+20<640 else -1)}))
        ns = functions('pocketfm_public_show_catalog',
            validate_public_http_url=Mock(), scoped_get=get, is_pocketfm_url=lambda u: True,
            PageParser=PageParser, page_values=page_values, catalog_from_values=catalog_from_values,
            episode_action_id=episode_action_id, action_catalog=action_catalog,
            _pocket_action_cache={}, request_headers_for_url=lambda *a: {},
            auth_headers_for_url=lambda *a: {},
            requests=SimpleNamespace(post=Mock(side_effect=post)), logger=logging.getLogger('test'))
        cat = ns['pocketfm_public_show_catalog'](response.url)
        self.assertEqual(len(cat['entries']), 640)
        self.assertEqual(cat['warning'], '')
        self.assertEqual(ns['requests'].post.call_count, 31)
        # A repeated page must explicitly report partial results.
        get.side_effect = [response, action]
        ns['_pocket_action_cache'].clear()
        ns['requests'].post.side_effect = lambda *a, **kw: Mock(text='1:' + json.dumps({'result':payload(range(1,21))}))
        cat = ns['pocketfm_public_show_catalog'](response.url)
        self.assertEqual(len(cat['entries']), 20)
        self.assertTrue(cat['warning'])

class BotTests(unittest.TestCase):
    def test_star_messages_reach_series_selector(self):
        select = Mock()
        ns = functions('handle_url', allowed_user=lambda m: True,
                       rate_limited=lambda u: False, process_pocket_range=select,
                       URL_RE=re.compile(r"https?://[^\s<>]+", re.I))
        for pattern in ('*', '*10', '25*', '10*20', '**'):
            msg = SimpleNamespace(text=pattern, from_user=SimpleNamespace(id=1))
            ns['handle_url'](msg)
            select.assert_called_with(msg)
        self.assertEqual(select.call_count, 5)

    def test_show_link_routing_accepts_www_locale_and_surrounding_text(self):
        ns = functions('extract_pocketfm_show_url', 'is_pocketfm_url',
                       URL_RE=re.compile(r"https?://[^\s<>]+", re.I))
        for link in ['https://pocketfm.com/show/abc',
                     'https://www.pocketfm.com/show/abc?x=1',
                     'https://pocketfm.com/en-us/show/abc']:
            self.assertEqual(ns['extract_pocketfm_show_url']('https://example.com first '+link), link)
        self.assertIsNone(ns['extract_pocketfm_show_url']('https://pocketfm.com.evil.test/show/abc'))

    def test_non_http_redirect_does_not_abort_other_profiles(self):
        calls = []
        def get(url, **kwargs):
            calls.append(url)
            return Mock(status_code=302, headers={'Location':'market://details?id=example'}, close=Mock())
        def validate(url):
            if not url.startswith('https://'): raise AssertionError('Non-HTTP fetch')
        ns = functions('_pocketfm_episode_url_from_text', '_extract_pocketfm_episode_from_payload',
                       'resolve_pocketfm_onelink', validate_public_http_url=validate,
                       requests=SimpleNamespace(get=get))
        resolved, _ = ns['resolve_pocketfm_onelink']('https://pocketfm.onelink.me/test')
        self.assertIsNone(resolved)
        self.assertEqual(len(calls), 3)

    def test_all_batch_continues_after_failure_and_can_cancel(self):
        bot = Mock()
        events = {}
        lock = threading.Lock()
        process = Mock(side_effect=[False] + [True]*639)
        ns = functions('process_url_batch', bot=bot, user_lock=lambda u:lock,
                       _pocket_states_guard=threading.Lock(), _batch_cancel_events=events,
                       process_one_url=process, get_job=lambda u:{},
                       _jobs_guard=threading.Lock(), _jobs={})
        msg=SimpleNamespace(from_user=SimpleNamespace(id=1))
        with patch.object(threading, 'Thread'):
            ns['process_url_batch'](msg, [str(n) for n in range(640)], episode_numbers=list(range(1,641)))
        self.assertEqual(process.call_count, 640)
        self.assertIn('Successful: 639/640', bot.reply_to.call_args.args[1])
        self.assertFalse(lock.locked())
        def stop(*a, **kw): events[1].set();return True
        process.side_effect=stop;process.reset_mock()
        with patch.object(threading, 'Thread'):
            ns['process_url_batch'](msg, ['one', 'two', 'three'])
        self.assertEqual(process.call_count, 1)
        self.assertIn('Not attempted: 2', bot.reply_to.call_args.args[1])
        self.assertEqual(events, {})

if __name__ == '__main__': unittest.main()
