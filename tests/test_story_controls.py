import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from story_library import StoryLibrary, audio_caption
from test_pocketfm_batch import functions
from test_series_workflow import message


class StoryControlsTests(unittest.TestCase):
    def test_caption_persistence_isolation_reset_and_literal_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'db.sqlite3'
            store = StoryLibrary(path)
            store.set_caption(1, 2, '🎧 {title} — {artist} {title.__class__}')
            store = StoryLibrary(path)
            self.assertEqual(store.caption(2, 2), '')
            self.assertEqual(store.caption(1, 3), '')
            rendered = audio_caption(store.caption(1, 2), 'தமிழ்', 'Author')
            self.assertEqual(rendered, '🎧 தமிழ் — Author {title.__class__}')
            self.assertLessEqual(len(audio_caption('{title}', '🎧'*1000, '').encode('utf-16-le')), 2000)
            with self.assertRaises(ValueError):
                store.set_caption(1, 2, '🎧'*251)
            store.set_caption(1, 2, '')
            self.assertEqual(store.caption(1, 2), '')

    def test_button_owner_chat_and_stale_card_checks(self):
        callbacks = {name: Mock() for name in ['cmd_output_format', 'cmd_story_library',
                     'cmd_caption', 'cmd_episodes', 'cmd_available', 'cmd_start']}
        bot = Mock()
        states = {1: {'chat_id': 2, 'card_token': 'abcdef123456'}}
        ns = functions('handle_story_button', copy=copy, bot=bot, allowed_user=lambda m: True,
                       _pocket_states_guard=threading.Lock(), _pocket_states=states, **callbacks)
        msg = message(user=999)
        query = SimpleNamespace(id='q', message=msg, from_user=SimpleNamespace(id=1),
                                data='story:1:abcdef123456:save')
        ns['handle_story_button'](query)
        sent = callbacks['cmd_story_library'].call_args.args[0]
        self.assertEqual((sent.from_user.id, sent.text), (1, '/save'))
        self.assertEqual(msg.from_user.id, 999)
        for data, user, chat in [('story:1:abcdef123456:save', 3, 2),
                                 ('story:1:000000000000:save', 1, 2),
                                 ('story:1:abcdef123456:save', 1, 3),
                                 ('story:1:abcdef123456:invalid', 1, 2)]:
            query.data, query.from_user.id, query.message.chat.id = data, user, chat
            ns['handle_story_button'](query)
        self.assertEqual(callbacks['cmd_story_library'].call_count, 1)
        query.data, query.from_user.id, query.message.chat.id = 'story:1:abcdef123456:available', 1, 2
        ns['allowed_user'] = lambda m: False
        ns['handle_story_button'](query)
        callbacks['cmd_available'].assert_not_called()

    def test_caption_command_preview_and_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StoryLibrary(Path(directory)/'db')
            bot = Mock()
            ns = functions('cmd_caption', STORY_LIBRARY=store, audio_caption=audio_caption,
                           allowed_user=lambda m: True, bot=bot)
            ns['cmd_caption'](message('/caption {title} - {artist}'))
            self.assertIn('Episode 1 - Story name', bot.reply_to.call_args.args[1])
            ns['cmd_caption'](message('/caption reset'))
            self.assertEqual(store.caption(1, 2), '')


if __name__ == '__main__':
    unittest.main()
