"""Read-only installed-artifact certification, usable without a checkout."""
import sys

from . import load_discovery, load_discovery_process, load_kernel
from ._discovery import DiscoveryError


def check_stream():
    """Exercise a real untracked Python source without external Git/grammars."""
    import json
    import os
    from pathlib import Path
    import struct
    import tempfile
    from . import open_stream_process
    from ._process_discovery import retire_process
    with tempfile.TemporaryDirectory(prefix='tofu-stream-certify-') as temporary:
        root = Path(temporary)
        (root/'.git/objects').mkdir(parents=True)
        (root/'.git/refs/heads').mkdir(parents=True)
        (root/'.git/HEAD').write_text('ref: refs/heads/main\n')
        (root/'.git/config').write_text('[core]\n repositoryformatversion = 0\n bare = false\n')
        source = 'def installed_probe(): pass\n'
        (root/'sample.py').write_text(source)
        request = json.dumps(dict(format='tofu.repository-stream/v2', path_indexes=[0],
                                  max_source_bytes=512*1024, max_reference_chars=65536,
                                  include_refs=True)).encode()
        with open_stream_process() as executable:
            process = executable.start(root, root/'unused-grammars', 5_000_000_000,
                                       (1 << 20, 1 << 20, 1 << 20, 1 << 20),
                                       environment={'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'})
            try:
                output, errors = process.communicate(struct.pack('<Q', len(request))+request, timeout=10)
                if process.returncode or errors:
                    raise RuntimeError('native repository stream failed its installed-artifact probe')
                frames = []
                while output:
                    if len(output) < 8:
                        raise RuntimeError('native stream probe returned a truncated header')
                    length, = struct.unpack('<Q', output[:8]); output = output[8:]
                    if length > len(output):
                        raise RuntimeError('native stream probe returned a truncated frame')
                    frames.append(output[:length]); output = output[length:]
                if len(frames) != 3 or frames[0] != b'\0sample.py\0' or frames[2] != b'':
                    raise RuntimeError('native stream probe returned unexpected framing')
                row = json.loads(frames[1])
                if row != dict(path_index=0, text=source, admitted=True, tags=[], grammar_error=None):
                    raise RuntimeError('native stream probe returned incorrect source data')
            finally:
                primary = sys.exc_info()[1]
                try:
                    retire_process(process)
                except BaseException as cleanup:
                    if primary is None:
                        raise
                    if hasattr(primary, 'add_note'):
                        primary.add_note('stream probe cleanup: '+type(cleanup).__name__)


def check_grammars():
    """Certify bundled byte pins/notices and every actual grammar null/ABI guard."""
    import json
    from . import _load_lock, _package_manifest, open_stream_process
    from ._grammars import check_bundle
    from ._process_discovery import retire_process
    with _load_lock:
        directory, record = _package_manifest()
        bundle = record['stream'].get('grammar_bundle')
    if bundle is None:
        return None
    expected = check_bundle(directory, bundle)
    with open_stream_process() as executable:
        process = executable.start_grammar_check()
        try:
            output, errors = process.communicate(timeout=30)
            if process.returncode or errors or json.loads(output) != expected:
                raise RuntimeError('native repository grammar bundle failed certification')
        finally:
            retire_process(process)
    return expected


def main():
    kernel = load_kernel()
    if kernel.solve([0, 1], [0], [1.0], [1.0]) != [1.0]:
        raise RuntimeError('native repository kernel failed its installed-artifact probe')
    discovery = load_discovery()
    try:
        discovery.list_files('.', timeout=0)
    except DiscoveryError as error:
        if error.status != 3:
            raise RuntimeError('native discovery failed its installed-artifact probe') from error
    else:
        raise RuntimeError('native discovery did not enforce its deadline')
    if sys.platform == 'linux':
        from ._process_discovery import retire_process
        process = load_discovery_process()._start(b'.', 0)
        try:
            output, errors = process.communicate(timeout=10)
            if process.returncode != 3 or output or errors:
                raise RuntimeError('native discovery worker failed its installed-artifact probe')
        finally:
            retire_process(process)
        check_stream()
        grammars = check_grammars()
        print('native repository ranking, discovery and stream artifacts ready')
        if grammars is not None:
            print(f"sealed grammar bundle ready ({grammars['libraries']} libraries, {grammars['bytes']} bytes)")
    else:
        print('native repository ranking and discovery artifacts ready; '
              f'sealed stream process unsupported on {sys.platform}')


if __name__ == '__main__':
    main()
