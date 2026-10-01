import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from encrypted_media import demo, process, read_keys, encryption_markers, run_checked


class EncryptionTests(unittest.TestCase):
    def test_rotating_hls_keys_and_none_do_not_hide_encryption(self):
        text = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="first.key"\n#EXT-X-KEY:METHOD=NONE\n#EXT-X-KEY:METHOD=AES-128,URI="second.key"'
        self.assertEqual(encryption_markers(text), ['HLS AES-128'])
        self.assertEqual(encryption_markers('#EXT-X-KEY:METHOD=NONE'), [])
        self.assertEqual(encryption_markers('#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI="skd://id"'),
                         ['FairPlay', 'HLS SAMPLE-AES'])
        self.assertEqual(encryption_markers('<ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011"/><cenc:pssh>widevine</cenc:pssh>'), ['Widevine', 'CENC'])

    def test_keys_and_errors_never_echo_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'private.json'
            p.write_text(json.dumps({'not-a-kid':'secret-value'}))
            with self.assertRaises(ValueError) as error:
                read_keys(p)
            self.assertNotIn('secret-value', str(error.exception))
        with patch('encrypted_media.subprocess.run', return_value=subprocess.CompletedProcess([],0,stderr=b'decode error with secret')):
            with self.assertRaises(RuntimeError) as error:
                run_checked(['ffmpeg'], 'Validation', reject_errors=True)
            self.assertNotIn('secret', str(error.exception))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires FFmpeg')
    def test_real_generated_hls_and_cenc_outputs_decode(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = demo(tmp)
            for path in paths:
                probe = subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(path)], capture_output=True,check=True)
                self.assertGreater(float(json.loads(probe.stdout)['format']['duration']), 2.9)
            before = paths[0].read_bytes()
            with self.assertRaises(ValueError):
                process(paths[0], paths[0], 'cenc')
            self.assertEqual(paths[0].read_bytes(), before)
            with self.assertRaises(ValueError):
                process('https://example.com/manifest.m3u8', Path(tmp)/'network.m4a', 'hls')
            bad = Path(tmp)/'bad.mp4'
            bad.write_bytes(b'broken media')
            keyfile = Path(tmp)/'keys.json'
            keyfile.write_text(json.dumps({'0'*32:'1'*32}))
            output = Path(tmp)/'bad-output.m4a'
            with self.assertRaises(RuntimeError):
                process(bad, output, 'cenc', keyfile)
            self.assertFalse(output.exists())


if __name__ == '__main__': unittest.main()
