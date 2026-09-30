import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from kuku_catalog import show_slug, normalize_page, get_catalog, refresh_episode
from runtime_checks import check_download_environment
from test_pocketfm_batch import functions


def data(numbers, more=False, **extra):
    return {'show': {'title': 'Example', 'n_episodes': 3, 'original_image': 'https://images.example/cover.jpg'},
            'has_more': more,
            'episodes': [dict(id=n, index=n, title=f'Episode {n}',
                             content={'hls_url':f'https://media.example/{n}.m3u8'}, **extra) for n in numbers]}


class KukuTests(unittest.TestCase):
    def test_strict_provider_routing(self):
        self.assertEqual(show_slug('https://www.kukufm.com/show/example/?ref=web'), 'example')
        for url in ['https://kukufm.com.evil.test/show/x', 'https://kukufm.com/api/private',
                    'https://kukufm.com/show/%2e%2e', 'file://kukufm.com/show/x']:
            self.assertIsNone(show_slug(url))

    def test_pagination_and_refresh(self):
        fetch = Mock(side_effect=[data([2, 1], True), data([3])])
        cat = get_catalog('https://kukufm.com/show/example', fetch)
        self.assertEqual([e['number'] for e in cat['entries']], [1, 2, 3])
        self.assertEqual(cat['warning'], '')
        entry = cat['entries'][2]
        fresh = data([3]); fresh['episodes'][0]['content']['hls_url'] = 'https://media.example/new.m3u8'
        episode, _ = refresh_episode(entry, lambda url: fresh)
        self.assertEqual(episode['media_url'], 'https://media.example/new.m3u8')
        fresh['episodes'][0]['is_locked'] = True
        with self.assertRaisesRegex(ValueError, 'locked'):
            refresh_episode(entry, lambda url: fresh)

    def test_partial_pagination_is_not_silently_complete(self):
        cat = get_catalog('https://kukufm.com/show/example', Mock(side_effect=[data([1],True), data([1],True)]))
        self.assertEqual(len(cat['entries']), 1)
        self.assertTrue(cat['warning'])
        with self.assertRaises(ValueError):
            get_catalog('https://kukufm.com/show/example', lambda url: {'message':'login required'})

    def test_access_never_inferred_from_media(self):
        self.assertEqual(normalize_page(data([1]), 'example', 1)['entries'][0]['access'], 'unknown')
        self.assertEqual(normalize_page(data([1], is_free=True), 'example', 1)['entries'][0]['access'], 'available')
        self.assertEqual(normalize_page(data([1], is_locked=True, is_free=True), 'example', 1)['entries'][0]['access'], 'locked')

    def test_cookie_does_not_cross_provider_or_redirect_host(self):
        ns = functions('auth_headers_for_url', KUKU_COOKIE='session=kuku-test-only',
                       AUTH_DOMAINS={'pocketfm.com'}, AUTH_COOKIE='pocket-test-only',
                       AUTHORIZATION_HEADER='', AUTH_REFERER='', POCKET_API_HOST='api.pocketfm.com',
                       POCKET_API_PATH='/v2/content_api/show.get_details', POCKETFM_ACCESS_TOKEN='')
        headers = ns['auth_headers_for_url']
        self.assertEqual(headers('https://kukufm.com/api/episodes'), {'Cookie':'session=kuku-test-only'})
        self.assertEqual(headers('https://pocketfm.com/show/test'), {'Cookie':'pocket-test-only'})
        self.assertEqual(headers('https://cdn.example/audio.m3u8'), {})
        self.assertEqual(headers('http://kukufm.com/api/episodes'), {})
        self.assertEqual(headers('https://kukufm.com.evil.test'), {})

    def test_account_cookie_requires_private_bot(self):
        ns = functions('kuku_fetch_json', KUKU_COOKIE='test', ALLOWED_USER_IDS=set())
        with self.assertRaisesRegex(RuntimeError, 'ALLOWED_USER_IDS'):
            ns['kuku_fetch_json']('https://kukufm.com/api/test')

    def test_refresh_download_routes_correct_metadata(self):
        current = {'title':'Episode 2', 'media_url':'https://media.example/2.m3u8'}
        ns = functions('kuku_download_entry',
            kuku_refresh_episode=Mock(return_value=(current, {'title':'Series', 'thumbnail':'cover'})),
            kuku_fetch_json=Mock(), validate_public_http_url=Mock(),
            download_public_candidate=Mock(return_value={'path':Path('audio.m4a')}))
        result = ns['kuku_download_entry']({'id':'2'}, Path('.'))
        self.assertEqual(result['title'], 'Episode 2')
        self.assertEqual(result['performer'], 'Series')
        ns['validate_public_http_url'].assert_called_once_with(current['media_url'])

    def test_series_selection_preserves_kuku_episode_identity(self):
        import threading, time
        entries = normalize_page(data([1, 2, 3]), 'example', 1)['entries']
        from pocketfm_catalog import select_entries
        batch = Mock()
        ns = functions('process_pocket_range', allowed_user=lambda m:True,
            _pocket_states_guard=threading.Lock(),
            _pocket_states={1:{'entries':entries, 'provider':'kuku', 'chat_id':2,
                              'created':time.time(), 'title':'Series'}},
            select_entries=select_entries, process_url_batch=batch, bot=Mock())
        ns['process_pocket_range'](SimpleNamespace(from_user=SimpleNamespace(id=1),
                                                   chat=SimpleNamespace(id=2), text='2-3'))
        self.assertEqual([e['number'] for e in batch.call_args.kwargs['media_entries']], [2, 3])
        self.assertEqual(batch.call_args.kwargs['batch_label'], 'Kuku FM batch')

    def test_preflight_raises_without_exiting(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('runtime_checks.shutil.which', return_value=None):
                with self.assertRaisesRegex(RuntimeError, 'Missing binaries'):
                    check_download_environment(folder)
            with patch('runtime_checks.shutil.which', return_value='/usr/bin/tool'), patch(
                'runtime_checks.shutil.disk_usage', return_value=SimpleNamespace(free=1024*1024)):
                with self.assertRaisesRegex(RuntimeError, 'Insufficient disk'):
                    check_download_environment(folder, 256)
                self.assertEqual(check_download_environment(folder, 0)['free_mb'], 1)

if __name__ == '__main__': unittest.main()
