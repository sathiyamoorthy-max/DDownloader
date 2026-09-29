import unittest
from unittest.mock import Mock
from pocketfm_api import normalize, get_catalog, refresh_episode, API_HOST, API_PATH
from test_pocketfm_batch import functions, payload, SHOW

class PocketAPITests(unittest.TestCase):
    def test_nested_results_pagination_and_account_access(self):
        a=payload([1,2], total=3, cursor=2)
        b=payload([3], total=3, cursor=-1)
        b['stories'][0].update(is_locked=False, coins_required=11, media_url='https://media.example/3.mp3')
        fetch=Mock(side_effect=[{'result':[[a]]},{'result':[b]}])
        result=get_catalog('https://pocketfm.com/show/'+SHOW,fetch,session=True)
        self.assertEqual(len(result['entries']),3)
        self.assertTrue(result['session_request'])
        self.assertEqual(result['warning'],'')
        entry=result['entries'][2]
        fresh,_=refresh_episode(entry, lambda url:{'result':b})
        self.assertEqual(fresh['access'],'available')
        b['stories'][0]['is_locked']=True
        with self.assertRaisesRegex(ValueError,'locked'):
            refresh_episode(entry,lambda url:{'result':b})

    def test_repeated_cursor_reports_partial(self):
        result=get_catalog('https://pocketfm.com/show/'+SHOW,
                           lambda u:{'result':payload([1],total=100,cursor=0)})
        self.assertEqual(len(result['entries']),1)
        self.assertTrue(result['warning'])

    def test_foreign_show_and_errors_rejected(self):
        for d in ({'message':'Unauthorized'}, {'result':[]}, {'result':dict(payload([1]),show_id='other')}):
            with self.assertRaises(ValueError): normalize(d,SHOW,0)

    def test_token_is_exact_host_path_and_https_only(self):
        ns=functions('auth_headers_for_url',KUKU_COOKIE='',AUTH_DOMAINS=set(),
                     POCKET_API_HOST=API_HOST,POCKET_API_PATH=API_PATH,POCKETFM_ACCESS_TOKEN='test-only')
        headers=ns['auth_headers_for_url']
        self.assertEqual(headers('https://'+API_HOST+API_PATH),{'access-token':'test-only'})
        for url in ['http://'+API_HOST+API_PATH,'https://kukufm.com'+API_PATH,
                    'https://cdn.example'+API_PATH,'https://'+API_HOST+'/other']:
            self.assertEqual(headers(url),{})

    def test_guest_fallback_but_account_error_is_not_hidden(self):
        fallback=Mock(return_value={'entries':[],'warning':''})
        ns=functions('pocket_catalog_with_api',POCKETFM_ACCESS_TOKEN='',
                     pocket_api_catalog=Mock(side_effect=RuntimeError('refused')),
                     pocket_api_fetch_json=Mock(),pocketfm_public_show_catalog=fallback)
        result=ns['pocket_catalog_with_api']('https://pocketfm.com/show/test')
        self.assertEqual(result['provider'],'pocketfm')
        self.assertIn('Guest API unavailable',result['warning'])
        ns['POCKETFM_ACCESS_TOKEN']='test-only'
        with self.assertRaises(RuntimeError):ns['pocket_catalog_with_api']('https://pocketfm.com/show/test')
        self.assertEqual(fallback.call_count,1)

if __name__=='__main__':unittest.main()
