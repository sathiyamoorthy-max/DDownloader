import unittest
from unittest.mock import Mock
from pocketfm_catalog import catalog_from_values, story_card_text, PageParser
from pocketfm_api import get_catalog
from test_pocketfm_batch import functions
from test_series_workflow import message


class StoryCardTests(unittest.TestCase):
    def record(self):
        return {'show_id': 'demo', 'show_title': 'ஜித்தன் <test>', 'episodes_count': 2,
                'language': 'Tamil', 'play_count': '2.2M', 'rating': 4.6,
                'rating_count': 555, 'user_info': {'fullname': 'Selva & Mary'},
                'genre': 'Fantasy', 'image_url': 'https://cdn.example/cover.jpg',
                'next_ptr': -1, 'stories': [{'story_id': 'ep1', 'seq_number': 1, 'is_free': True}]}

    def test_show_fields_and_html_escape(self):
        record = self.record()
        other = dict(record, show_id='other', language='Wrong')
        catalog = catalog_from_values([record, other], 'demo')
        caption = story_card_text(catalog, 'https://pocketfm.com/show/demo')
        for expected in ['Tamil', '2.2M', '555', 'Selva &amp; Mary', '&lt;test&gt;', 'Total episodes: 2', 'Loaded: 1']:
            self.assertIn(expected, caption)
        self.assertNotIn('Wrong', caption)
        self.assertIn('Not provided', story_card_text({'title': 'Empty'}, 'https://pocketfm.com/show/demo'))

    def test_api_keeps_metadata_and_og_cover(self):
        catalog = get_catalog('https://pocketfm.com/show/demo', lambda url: {'result': [self.record()]})
        self.assertEqual(catalog['metadata']['language'], 'Tamil')
        parser = PageParser()
        parser.feed('<meta property="og:image" content="https://cdn.example/cover.jpg">')
        self.assertEqual(parser.image, self.record()['image_url'])

    def test_cover_failure_falls_back_without_losing_catalogue(self):
        catalog = catalog_from_values([self.record()], 'demo')
        bot = Mock()
        ns = functions('send_story_card', bot=bot, story_card_text=story_card_text,
                       validate_public_http_url=Mock(), logger=Mock())
        ns['send_story_card'](message(), catalog, 'https://pocketfm.com/show/demo')
        bot.send_photo.assert_called_once()
        bot.send_photo.side_effect = RuntimeError('photo failed')
        ns['send_story_card'](message(), catalog, 'https://pocketfm.com/show/demo')
        bot.reply_to.assert_called_once()
        bot.reset_mock()
        catalog['thumbnail'] += '?token=secret'
        ns['send_story_card'](message(), catalog, 'https://pocketfm.com/show/demo')
        bot.send_photo.assert_not_called()
        self.assertNotIn('secret', bot.reply_to.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
