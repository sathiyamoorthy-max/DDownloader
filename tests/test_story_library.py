import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from story_library import StoryLibrary, show_link
from test_pocketfm_batch import functions
from test_series_workflow import message


SHOW = '504c800760d50ff2dc77e4e1bfd9ff34b9aa31a8'
URL = 'https://pocketfm.com/show/' + SHOW


class LibraryTests(unittest.TestCase):
    def test_canonical_links_strip_auth_and_reject_foreign_hosts(self):
        self.assertEqual(show_link(SHOW), URL)
        self.assertEqual(show_link('https://www.pocketfm.com/de-de/show/' + SHOW + '?token=secret#tgWebAppData=secret'), URL)
        self.assertEqual(show_link('https://kukufm.com/audiobook/test/?token=x'), 'https://kukufm.com/show/test')
        for url in ['https://pocketfm.com.evil.test/show/' + SHOW,
                    'https://user:secret@pocketfm.com/show/' + SHOW,
                    'https://pocketfm.com:8888/show/' + SHOW,
                    'https://pocketfm.com/episode/' + SHOW, '1234567890']:
            with self.assertRaises(ValueError):
                show_link(url)

    def test_persistence_dedup_privacy_and_search(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.db'
            store = StoryLibrary(path)
            identity = store.save(1, 2, URL + '?access_token=secret', 'தமிழ் கதை')
            self.assertEqual(identity, store.save(1, 2, URL, 'தமிழ் கதை புதியது'))
            store = StoryLibrary(path)
            self.assertEqual(store.list(1, 2, 'தமிழ்')[1], 1)
            self.assertEqual(store.list(1, 2, '%')[1], 0)
            self.assertIsNone(store.get(9, 2, identity))
            self.assertIsNone(store.get(1, 9, identity))
            self.assertFalse(store.delete(9, 2, identity))
            self.assertEqual(store.get(1, 2, identity)['url'], URL)
            self.assertNotIn(b'secret', path.read_bytes())
            self.assertTrue(store.delete(1, 2, identity))

    def test_pagination_and_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StoryLibrary(Path(directory)/'state.db')
            for n in range(200):
                store.save(1, 2, f'https://kukufm.com/show/book-{n}', f'Book {n}')
            rows, total = store.list(1, 2, page=20)
            self.assertEqual((len(rows), total), (10, 200))
            self.assertEqual(store.list(1, 2, page=21)[0], [])
            with self.assertRaises(ValueError):
                store.save(1, 2, 'https://kukufm.com/show/overflow', 'Overflow')
            store.save(1, 2, 'https://kukufm.com/show/book-1', 'Updated')

    def test_commands_isolate_chat_and_reload_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StoryLibrary(Path(directory)/'state.db')
            bot, load = Mock(), Mock()
            states = {1: {'chat_id': 99, 'show_url': URL, 'title': 'தமிழ்'}}
            ns = functions('cmd_story_library', allowed_user=lambda m: True, bot=bot,
                           STORY_LIBRARY=store, show_link=show_link,
                           _pocket_states_guard=threading.Lock(), _pocket_states=states,
                           handle_pocket_show=load, logger=Mock())
            run = ns['cmd_story_library']
            run(message('/save'))
            self.assertEqual(store.list(1, 2)[1], 0)
            states[1]['chat_id'] = 2
            run(message('/save'))
            identity = store.list(1, 2)[0][0]['id']
            run(message(f'/open {identity}', user=9))
            load.assert_not_called()
            run(message(f'/open {identity}'))
            self.assertEqual(load.call_args.kwargs['series_url'], URL)
            run(message('/search தமிழ்'))
            self.assertIn('தமிழ்', bot.reply_to.call_args.args[1])
            run(message('/gen@mybot ' + SHOW))
            self.assertEqual(bot.reply_to.call_args.args[1], URL)
            ns['allowed_user'] = lambda m: False
            run(message(f'/forget {identity}'))
            self.assertIsNotNone(store.get(1, 2, identity))


if __name__ == '__main__':
    unittest.main()
