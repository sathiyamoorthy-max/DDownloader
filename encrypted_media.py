"""Owner-operated media pipeline: HLS AES-128 or CENC with supplied content keys.

No CDM extraction, license acquisition or provider-specific unlocking. Not a
Telegram endpoint: filesystem paths, keys and network permission belong to the
operator running this CLI.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse


def encryption_markers(text):
    lower = (text or '').lower()
    result = []
    for label, tokens in (
        ('Widevine', ('edef8ba9-79d6-4ace-a3c8-27dcd51d21ed', 'widevine')),
        ('PlayReady', ('9a04f079-9840-4286-ab92-e65be0885f95', 'playready', 'mspr:pro')),
        ('FairPlay', ('com.apple.fps', 'skd://', 'fairplay')),
    ):
        if any(token in lower for token in tokens):
            result.append(label)
    for line in (text or '').splitlines():
        if not line.strip().upper().startswith(('#EXT-X-KEY:', '#EXT-X-SESSION-KEY:')):
            continue
        match = re.search(r'(?:[:,])\s*METHOD\s*=\s*"?([A-Z0-9-]+)', line, re.I)
        method = match.group(1).upper() if match else 'UNKNOWN'
        if method == 'NONE':
            continue
        label = 'HLS AES-128' if method == 'AES-128' else 'HLS ' + method
        if label not in result:
            result.append(label)
    if ('mp4protection:2011' in lower or 'cenc:pssh' in lower) and 'CENC' not in result:
        result.append('CENC')
    return result


def run_checked(args, phase, timeout=900, reject_errors=False):
    """Do not echo signed URLs or content keys in command/error output."""
    try:
        result = subprocess.run(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(f'{phase}: required executable is not installed.') from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'{phase}: timed out.') from None
    if result.returncode or (reject_errors and result.stderr.strip()):
        raise RuntimeError(f'{phase} failed. Check input, access and supplied keys; no validated output published.')


def read_keys(path):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or not data or len(data) > 32:
        raise ValueError('Keys file must be a JSON object mapping 32-digit hex KIDs to 32-digit hex keys.')
    if not all(isinstance(k, str) and isinstance(v, str) and
               re.fullmatch('[0-9a-fA-F]{32}', k) and re.fullmatch('[0-9a-fA-F]{32}', v)
               for k, v in data.items()):
        raise ValueError('Each KID and key must contain exactly 32 hexadecimal digits.')
    return data


def process(source, output, mode, keys_file=None, allow_network=False):
    if mode not in {'hls', 'cenc'}:
        raise ValueError('Choose hls or cenc mode.')
    for binary in ('ffmpeg', 'ffprobe'):
        if not shutil.which(binary):
            raise RuntimeError(binary + ' is required.')
    output = Path(output).resolve()
    if output.suffix.lower() not in {'.m4a', '.mp4'}:
        raise ValueError('Output must end in .m4a or .mp4.')
    if output.exists():
        raise ValueError('Output already exists; choose a new filename.')
    remote = urlparse(str(source)).scheme in {'http', 'https'}
    if remote and (mode != 'hls' or not allow_network):
        raise ValueError('Remote input requires HLS mode and explicit --allow-network.')
    source = str(source) if remote else str(Path(source).resolve(strict=True))
    if mode == 'cenc' and not keys_file:
        raise ValueError('CENC requires --keys-file with your supplied content keys.')
    if mode == 'hls' and keys_file:
        raise ValueError('HLS uses the playlist key URI and IV, not a CENC keys file.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.media-', dir=output.parent) as tmp:
        tmp = Path(tmp)
        args = ['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-y']
        if mode == 'cenc':
            keys = read_keys(keys_file)
            if len(keys) == 1:
                args += ['-decryption_key', next(iter(keys.values()))]
            else:
                if not shutil.which('mp4decrypt'):
                    raise RuntimeError('Multiple CENC keys require Bento4 mp4decrypt.')
                decrypted = tmp / 'decrypted.mp4'
                command = ['mp4decrypt']
                for kid, key in keys.items():
                    command += ['--key', kid + ':' + key]
                run_checked(command + [source, str(decrypted)], 'CENC decryption')
                source = str(decrypted)
        else:
            protocols = ['crypto', 'data']
            if not remote:
                protocols.append('file')
            if allow_network:
                protocols += ['http', 'https', 'tcp', 'tls']
            args += ['-protocol_whitelist', ','.join(protocols),
                     '-allowed_extensions', 'key,bin,ts,m3u8,m4a,m4s,mp4,aac']
            if allow_network:
                args += ['-rw_timeout', '30000000']
        candidate = tmp / ('validated' + output.suffix)
        args += ['-i', source, '-map', '0:a:0']
        if output.suffix.lower() == '.mp4':
            args += ['-map', '0:v:0?']
        args += ['-c', 'copy', '-movflags', '+faststart', str(candidate)]
        run_checked(args, 'Media conversion')
        if not candidate.is_file() or candidate.stat().st_size == 0:
            raise RuntimeError('No output media was produced.')
        run_checked(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type',
                     str(candidate)], 'Container validation', reject_errors=True)
        run_checked(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-err_detect', 'explode',
                     '-i', str(candidate), '-map', '0:a:0', '-map', '0:v:0?',
                     '-f', 'null', '-'], 'Full decode validation', reject_errors=True)
        # Atomic no-clobber publish on the same filesystem.
        os.link(candidate, output)
    return output


def demo(folder):
    """Generate our own encrypted tone, decrypt and validate it; no account needed."""
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder) as tmp:
        tmp = Path(tmp)
        key, kid = os.urandom(16), os.urandom(16)
        key_path = tmp / 'sample.key'
        key_path.write_bytes(key)
        key_info = tmp / 'keyinfo.txt'
        key_info.write_text(f'{key_path}\n{key_path}\n{os.urandom(16).hex()}\n')
        base = ['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                'sine=frequency=440:duration=3', '-c:a', 'aac']
        playlist = tmp / 'sample.m3u8'
        run_checked(base + ['-hls_time', '1', '-hls_key_info_file', str(key_info),
                           '-hls_segment_filename', str(tmp / 'part%03d.ts'), str(playlist)], 'Generate HLS sample')
        hls_output = process(playlist, folder / 'hls-demo.m4a', 'hls')
        cenc = tmp / 'encrypted.mp4'
        run_checked(base + ['-encryption_scheme', 'cenc-aes-ctr', '-encryption_key', key.hex(),
                           '-encryption_kid', kid.hex(), str(cenc)], 'Generate CENC sample')
        keys = tmp / 'keys.json'
        keys.write_text(json.dumps({kid.hex(): key.hex()}))
        cenc_output = process(cenc, folder / 'cenc-demo.m4a', 'cenc', keys)
    return hls_output, cenc_output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    demo_parser = sub.add_parser('demo')
    demo_parser.add_argument('--output-dir', required=True)
    convert = sub.add_parser('convert')
    convert.add_argument('--mode', choices=['hls', 'cenc'], required=True)
    convert.add_argument('--input', required=True)
    convert.add_argument('--output', required=True)
    convert.add_argument('--keys-file')
    convert.add_argument('--allow-network', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'demo':
            outputs = demo(args.output_dir)
            print('Both generated samples decrypted and passed full decode validation.')
            for path in outputs:
                print(path.name)
        else:
            path = process(args.input, args.output, args.mode, args.keys_file, args.allow_network)
            print('Validated output saved: ' + path.name)
    except (ValueError, RuntimeError, OSError):
        # Do not print exceptions from JSON/filesystem libraries: they may echo
        # sensitive input. The library API retains useful non-secret exceptions.
        parser.exit(1, 'Operation failed. Check mode, files, dependencies, access and keys. No unvalidated output was published.\n')


if __name__ == '__main__':
    main()
