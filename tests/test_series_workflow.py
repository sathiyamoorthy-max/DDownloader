import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from batch_state import BatchStore, pending_indices, failure_report, failure_category
from pocketfm_catalog import select_entries, access_summary
from test_pocketfm_batch import functions


def message(text='', user=1, chat=2):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=user),
                           chat=SimpleNamespace(id=chat), message_id=3)


def entries():
    return [dict(id=str(n), number=n, url=f'https://pocketfm.com/episode/{n}',
                 provider='pocketfm_api', show_id='series', cursor=0,
                 media_candidates=['https://cdn.example/?secret=must-not-persist'],
                 access=a) for n, a in enumerate(['available', 'locked', 'unknown'], 1)]


class WorkflowTests(unittest.TestCase):
    def test_available_excludes_locked_unknown_without_mutation(self):
        rows = entries()
        self.assertEqual([e['number'] for e in select_entries('AVAILABLE', rows)], [1])
        self.assertEqual(len(rows), 3)

    def test_state_survives_reopen_is_scoped_and_omits_media_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'state.sqlite3'
            store = BatchStore(path)
            batch = store.create(1, 2, entries(), 'Show')
            batch['items'][0]['status'] = 'done'
            batch['items'][1]['status'] = 'failed'
            batch['items'][1]['reason'] = 'locked'
            batch['items'][2]['status'] = 'running'
            store.save(1, 2, batch)
            other = BatchStore(path)
            self.assertEqual(pending_indices(other.load(1, 2)), [2])
            self.assertEqual(pending_indices(other.load(1, 2), retry=True), [1])
            self.assertIsNone(other.load(1, 3))
            self.assertIsNone(other.load(3, 2))
            self.assertNotIn('secret', repr(other.load(1, 2)))
            self.assertIn('2: locked', failure_report(batch))

    def test_retry_and_resume_update_original_batch_without_repeating_success(self):
        with tempfile.TemporaryDirectory() as d:
            store = BatchStore(Path(d)/'state.sqlite3')
            batch = store.create(1, 2, entries(), 'Show')
            batch['items'][0]['status'] = 'done'
            batch['items'][1]['status'] = 'failed'
            batch['items'][2]['status'] = 'running'
            store.save(1, 2, batch)
            process = Mock(return_value=True)
            lock = threading.Lock()
            ns = functions('process_url_batch', bot=Mock(), user_lock=lambda u: lock,
                           _pocket_states_guard=threading.Lock(), _batch_cancel_events={},
                           process_one_url=process, get_job=lambda u:{}, _jobs_guard=threading.Lock(),
                           _jobs={}, BATCH_STORE=store, pending_indices=pending_indices,
                           failure_report=failure_report, failure_category=failure_category,
                           series_controls=Mock())
            with patch.object(threading, 'Thread'):
                ns['process_url_batch'](message(), [], saved_mode='retry')
                self.assertEqual(process.call_args.kwargs['media_entry']['number'], 2)
                ns['process_url_batch'](message(), [], saved_mode='resume')
                self.assertEqual(process.call_args.kwargs['media_entry']['number'], 3)
            self.assertEqual(process.call_count, 2)
            self.assertTrue(all(i['status']=='done' for i in store.load(1, 2)['items']))
            self.assertFalse(lock.locked())

    def test_failed_batch_records_category_and_cancel_leaves_pending(self):
        with tempfile.TemporaryDirectory() as d:
            store = BatchStore(Path(d)/'state.sqlite3')
            events = {}
            def process(*args, **kwargs):
                events[1].set()
                return False
            ns = functions('process_url_batch', bot=Mock(), user_lock=lambda u: threading.Lock(),
                           _pocket_states_guard=threading.Lock(), _batch_cancel_events=events,
                           process_one_url=process, get_job=lambda u:{'detail':'[audio_decode_failed] error'},
                           _jobs_guard=threading.Lock(), _jobs={}, BATCH_STORE=store,
                           pending_indices=pending_indices, failure_report=failure_report,
                           failure_category=failure_category, series_controls=Mock())
            rows = entries()
            with patch.object(threading, 'Thread'):
                ns['process_url_batch'](message(), [e['url'] for e in rows],
                                        journal_entries=rows, media_entries=rows)
            batch = store.load(1, 2)
            self.assertEqual(batch['items'][0]['reason'], 'audio_decode_failed')
            self.assertEqual(pending_indices(batch), [1, 2])

    def test_accountcheck_cookie_does_not_claim_verified_login(self):
        bot = Mock()
        response = Mock()
        ns = functions('cmd_accountcheck', bot=bot, allowed_user=lambda m: True,
                       _pocket_states_guard=threading.Lock(), _pocket_states={},
                       extract_series_url=lambda t:'https://pocketfm.com/show/abc',
                       kuku_show_slug=lambda u:None, POCKETFM_ACCESS_TOKEN='',
                       auth_headers_for_url=lambda u:{'Cookie':'test-only'},
                       scoped_get=Mock(return_value=response), failure_category=failure_category)
        ns['cmd_accountcheck'](message('/accountcheck'))
        self.assertIn('NOT verified', bot.reply_to.call_args.args[1])
        response.close.assert_called_once()

    def test_api_check_success_denial_and_chat_isolation(self):
        bot = Mock()
        fetch = Mock(return_value={})
        ns = functions('cmd_accountcheck', bot=bot, allowed_user=lambda m: True,
                       _pocket_states_guard=threading.Lock(),
                       _pocket_states={1:{'chat_id':99,'show_url':'https://kukufm.com/show/private'}},
                       extract_series_url=lambda t:None, failure_category=failure_category)
        ns['cmd_accountcheck'](message('/accountcheck'))
        self.assertIn('Use /accountcheck', bot.reply_to.call_args.args[1])
        ns.update(extract_series_url=lambda t:'https://kukufm.com/show/test',
                  kuku_show_slug=lambda u:'test', auth_headers_for_url=lambda u:{'Cookie':'test-only'},
                  kuku_fetch_json=fetch, kuku_api_url=lambda s,p:'https://kukufm.com/api',
                  kuku_normalize=lambda d,s,p:{'entries':entries()}, access_summary=access_summary)
        ns['cmd_accountcheck'](message('/accountcheck'))
        self.assertIn('not proof', bot.reply_to.call_args.args[1])
        fetch.side_effect = RuntimeError('401 refused access')
        ns['cmd_accountcheck'](message('/accountcheck'))
        self.assertIn('session_or_access_denied', bot.reply_to.call_args.args[1])

    def test_buttons_dispatch_without_mutating_user_message(self):
        callbacks = {name:Mock() for name in ('cmd_episodes','cmd_available','cmd_continue_batch',
                     'cmd_cancel','cmd_accountcheck','cmd_failures')}
        ns = functions('button_series_action', allowed_user=lambda m:True, copy=copy, **callbacks)
        for label, name, command in [('⬇️ Available','cmd_available','/available'),
                                     ('🔄 Retry','cmd_continue_batch','/retry'),
                                     ('▶️ Resume','cmd_continue_batch','/resume'),
                                     ('⛔ Cancel','cmd_cancel','/cancel')]:
            msg = message(label)
            ns['button_series_action'](msg)
            self.assertEqual(callbacks[name].call_args.args[0].text, command)
            self.assertEqual(msg.text, label)


if __name__ == '__main__':
    unittest.main()
