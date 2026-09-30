import unittest
from unittest.mock import patch
from provider_cookies import cookie_header
from test_pocketfm_batch import functions


class CookieTests(unittest.TestCase):
    def test_netscape_filters_scope_expiry_and_preserves_session_values(self):
        raw = '# Netscape HTTP Cookie File\n' + '\n'.join([
            'pocketfm.com\tFALSE\t/\tTRUE\t0\tsession\tx=y',
            '#HttpOnly_.pocketfm.com\tTRUE\t/\tTRUE\t200\tsecure\tz',
            'pocketfm.com\tFALSE\t/account\tTRUE\t0\tpathonly\tp',
            'pocketfm.com\tFALSE\t/\tTRUE\t50\told\texpired',
            '.google.com\tTRUE\t/\tTRUE\t0\tforeign\tnever-send',
            'pocketfm.com\tFALSE\t/\tTRUE\t0\tempty\t',
        ])
        with patch('provider_cookies.time.time', return_value=100):
            self.assertEqual(cookie_header(raw, 'https://pocketfm.com/show/test', 'pocketfm.com'),
                             'session=x=y; secure=z; empty=')
            self.assertEqual(cookie_header(raw, 'https://www.pocketfm.com/', 'pocketfm.com'), 'secure=z')
            self.assertIn('pathonly=p', cookie_header(raw, 'https://pocketfm.com/account/a', 'pocketfm.com'))
            self.assertNotIn('pathonly', cookie_header(raw, 'https://pocketfm.com/accounting', 'pocketfm.com'))
        for url in ('http://pocketfm.com/', 'https://kukufm.com/', 'https://pocketfm.com.evil.test/'):
            self.assertEqual(cookie_header(raw, url, 'pocketfm.com'), '')

    def test_raw_and_invalid_exports(self):
        self.assertEqual(cookie_header('Cookie: a=1; b=x=y', 'https://kukufm.com', 'kukufm.com'), 'a=1; b=x=y')
        for raw in ('a=1\nInjected=x', '# Netscape\npocketfm.com invalid',
                    'pocketfm.com\tFALSE\t/\tTRUE\t1\ta\texpired'):
            with self.assertRaises(ValueError):
                cookie_header(raw, 'https://pocketfm.com', 'pocketfm.com')

    def test_guest_requests_do_not_require_user_allowlist_or_cookies(self):
        ns = functions('auth_headers_for_url', KUKU_COOKIE='', POCKETFM_COOKIE='',
                       POCKETFM_ACCESS_TOKEN='', ALLOWED_USER_IDS=set(), AUTH_DOMAINS=set(),
                       POCKET_API_HOST='api.pocketfm.com', POCKET_API_PATH='/v2/content_api/show.get_details')
        for url in ('https://pocketfm.com/episode/test', 'https://kukufm.com/show/test'):
            self.assertEqual(ns['auth_headers_for_url'](url), {})
        ns['POCKETFM_COOKIE'] = 'pocketfm.com\tFALSE\t/\tTRUE\t0\tsession\tfake'
        ns['ALLOWED_USER_IDS'] = {123}
        self.assertEqual(ns['auth_headers_for_url']('https://pocketfm.com/episode/test'), {'Cookie':'session=fake'})


if __name__ == '__main__': unittest.main()
