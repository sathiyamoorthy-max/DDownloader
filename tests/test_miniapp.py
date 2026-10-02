import threading
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from miniapp_bridge import parse_action
from test_pocketfm_batch import functions

class MiniAppTests(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(parse_action('{"action":"select","value":"1-5","format":"mp4"}'), ('select','1-5','mp4'))
        self.assertEqual(parse_action('{"action":"status"}'), ('status','','mp3'))
        for raw in ('[]','{}','bad','{"action":"unlock"}',
                    '{"action":"status","user_id":1}',
                    '{"action":"download","value":"file:///secret"}',
                    '{"action":"show","value":"https://evil.example/show/abc"}',
                    '{"action":"download","value":"https://u:p@example.com"}',
                    '{"action":"select","value":"/cancel"}',
                    '{"action":"status","format":"exe"}', 'x'*4097):
            with self.subTest(raw=raw[:70]), self.assertRaises(ValueError): parse_action(raw)

    def test_owner_identity_comes_from_telegram_and_controls_dispatch(self):
        status=Mock(); download=Mock(); bot=Mock(); prefs={}
        ns=functions('handle_miniapp_data',allowed_user=lambda m:True,copy=copy,parse_action=parse_action,
          _pocket_states_guard=threading.Lock(),_output_formats=prefs,bot=bot,handle_url=download,
          cmd_status=status,cmd_episodes=Mock(),cmd_continue_batch=Mock(),cmd_cancel=Mock(),cmd_failures=Mock(),
          cmd_authstatus=Mock(),cmd_accountcheck=Mock(),cmd_system=Mock())
        m=SimpleNamespace(from_user=SimpleNamespace(id=71),chat=SimpleNamespace(id=72,type='private'),web_app_data=SimpleNamespace(data='{"action":"status"}'))
        ns['handle_miniapp_data'](m)
        self.assertEqual(status.call_args.args[0].text,'/status')
        m.web_app_data.data='{"action":"download","value":"https://example.com/audio.mp3","format":"mp4"}'
        ns['handle_miniapp_data'](m)
        self.assertEqual(prefs,{(71,72):'mp4'})
        self.assertEqual(download.call_args.args[0].from_user.id,71)
        m.chat.type='group';download.reset_mock();ns['handle_miniapp_data'](m);download.assert_not_called()
