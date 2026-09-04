#!/usr/bin/env python3
"""Unit tests for lib.json_store."""

import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _tmp(name='test.json'):
    """Create a fresh empty temp dir + return path inside it."""
    d = tempfile.mkdtemp(prefix='jsonstore-test-')
    return os.path.join(d, name), d


def test_read_json_missing_returns_default():
    from lib.json_store import read_json
    p, _ = _tmp()
    assert read_json(p) is None
    assert read_json(p, default={'x': 1}) == {'x': 1}
    assert read_json(p, default=[]) == []
    _ok('read_json on missing file → default')


def test_read_json_valid():
    from lib.json_store import read_json, write_json_atomic
    p, _ = _tmp()
    write_json_atomic(p, {'a': 1, 'b': [1, 2, 3]})
    assert read_json(p) == {'a': 1, 'b': [1, 2, 3]}
    _ok('read_json round-trips a simple object')


def test_read_json_empty_file_returns_default():
    """A zero-byte file (touched but never written) should yield default."""
    from lib.json_store import read_json
    p, _ = _tmp()
    open(p, 'w').close()  # empty file
    assert read_json(p, default={'fallback': True}) == {'fallback': True}
    _ok('read_json on empty file → default')


def test_read_json_invalid_returns_default():
    from lib.json_store import read_json
    p, _ = _tmp()
    with open(p, 'w') as f:
        f.write('this is not json {{{')
    assert read_json(p, default=[]) == []
    _ok('read_json on garbage file → default (with warning)')


def test_read_json_byte_limit_is_enforced_during_read(tmp_path):
    from lib.json_store import JsonStoreReadError, read_json

    path = tmp_path / 'bounded.json'
    path.write_text('{"value":"oversized"}', encoding='utf-8')
    assert read_json(
        str(path), default={'safe': True}, max_bytes=8) == {'safe': True}
    try:
        read_json(str(path), strict=True, max_bytes=8)
    except JsonStoreReadError as error:
        assert 'exceeds 8 bytes' in str(error)
    else:
        raise AssertionError('strict bounded read accepted an oversized file')

    assert read_json(str(path), max_bytes=64) == {'value': 'oversized'}


def test_read_json_rejects_invalid_byte_limits(tmp_path):
    from lib.json_store import read_json

    path = tmp_path / 'valid.json'
    path.write_text('{}', encoding='utf-8')
    for invalid_limit in (0, -1, True, 1.5):
        try:
            read_json(str(path), max_bytes=invalid_limit)
        except ValueError:
            pass
        else:
            raise AssertionError(
                f'invalid max_bytes was accepted: {invalid_limit!r}')


def test_read_json_jsonc_with_comments():
    """jsonc=True strips // and /* */ comments and trailing commas."""
    from lib.json_store import read_json
    p, _ = _tmp()
    with open(p, 'w') as f:
        f.write('''
        {
          // line comment
          "a": 1,
          /* block
             comment */
          "b": "hello",
          "c": [1, 2, 3,],
        }
        ''')
    assert read_json(p, jsonc=False, default=None) is None  # plain JSON parse fails
    assert read_json(p, jsonc=True) == {'a': 1, 'b': 'hello', 'c': [1, 2, 3]}
    _ok('read_json(jsonc=True) strips comments + trailing commas')


def test_jsonc_string_aware():
    """// and */ inside JSON strings must NOT be treated as comments."""
    from lib.json_store import read_json
    p, _ = _tmp()
    with open(p, 'w') as f:
        f.write('{"glob": "**/data/**", "url": "http://example.com/a/b"}')
    assert read_json(p, jsonc=True) == {
        'glob': '**/data/**',
        'url': 'http://example.com/a/b',
    }
    _ok('JSONC strip is string-aware (//, */ inside strings preserved)')


def test_write_json_atomic_basic():
    from lib.json_store import write_json_atomic
    p, _ = _tmp()
    write_json_atomic(p, {'k': 'v'})
    with open(p) as f:
        content = f.read()
    assert json.loads(content) == {'k': 'v'}
    assert content.endswith('\n')  # always trailing newline
    _ok('write_json_atomic writes valid JSON with trailing newline')


def test_write_json_atomic_overwrites():
    from lib.json_store import write_json_atomic, read_json
    p, _ = _tmp()
    write_json_atomic(p, {'first': True})
    write_json_atomic(p, {'second': True})
    assert read_json(p) == {'second': True}
    _ok('write_json_atomic overwrites existing file')


def test_write_json_atomic_no_partial_on_crash():
    """If mid-write fails, the original file must remain intact."""
    from lib.json_store import write_json_atomic, read_json
    p, _ = _tmp()
    write_json_atomic(p, {'original': True})

    # Inject a failure into the json.dumps call by passing un-serialisable data
    class NotJSON:
        pass

    crashed = False
    try:
        write_json_atomic(p, NotJSON())
    except TypeError:
        crashed = True

    assert crashed
    # Original must still be readable AND no leftover .tmp
    assert read_json(p) == {'original': True}
    parent = os.path.dirname(p)
    leftovers = [f for f in os.listdir(parent) if f.endswith('.tmp')]
    assert leftovers == []
    _ok('write_json_atomic preserves original on serialise failure')


def test_write_creates_parent_dir():
    from lib.json_store import write_json_atomic, read_json
    d = tempfile.mkdtemp(prefix='jsonstore-')
    p = os.path.join(d, 'sub1', 'sub2', 'config.json')
    write_json_atomic(p, {'nested': True})
    assert read_json(p) == {'nested': True}
    _ok('write_json_atomic auto-creates parent directories')


def test_write_text_atomic():
    from lib.json_store import write_text_atomic, read_text
    p, _ = _tmp('plain.txt')
    write_text_atomic(p, 'hello world\n')
    assert read_text(p) == 'hello world\n'
    _ok('write_text_atomic + read_text round-trip')


def test_write_bytes_atomic_is_private_mode_capable_and_concurrent(tmp_path):
    from lib.json_store import write_bytes_atomic

    path = tmp_path / 'asset.bin'
    payloads = [bytes([index]) * 32_768 for index in range(16)]
    errors = []

    def _write(payload):
        try:
            write_bytes_atomic(str(path), payload, mode=0o600)
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=_write, args=(payload,))
               for payload in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert path.read_bytes() in payloads
    assert (os.stat(path).st_mode & 0o777) == 0o600
    assert not list(tmp_path.glob('.binstore-*.tmp'))


def test_write_bytes_atomic_failure_preserves_previous_file(
        tmp_path, monkeypatch):
    from lib import json_store

    path = tmp_path / 'asset.bin'
    json_store.write_bytes_atomic(str(path), b'old')
    monkeypatch.setattr(
        json_store.os, 'replace',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('disk full')))

    try:
        json_store.write_bytes_atomic(str(path), b'new')
    except OSError as error:
        assert 'disk full' in str(error)
    else:
        raise AssertionError('injected replace failure did not propagate')

    assert path.read_bytes() == b'old'
    assert not list(tmp_path.glob('.binstore-*.tmp'))


def test_update_json_atomic_initial_default():
    """update_json_atomic on missing file uses default and writes result."""
    from lib.json_store import update_json_atomic, read_json
    p, _ = _tmp()
    def add_one(cfg):
        cfg['count'] = cfg.get('count', 0) + 1
        return cfg
    result = update_json_atomic(p, add_one, default={})
    assert result == {'count': 1}
    assert read_json(p) == {'count': 1}
    _ok('update_json_atomic uses default on missing file')


def test_update_json_atomic_increments():
    """Repeated calls correctly read-modify-write."""
    from lib.json_store import update_json_atomic, read_json
    p, _ = _tmp()
    def add_one(cfg):
        cfg['count'] = cfg.get('count', 0) + 1
        return cfg
    for _ in range(5):
        update_json_atomic(p, add_one, default={})
    assert read_json(p) == {'count': 5}
    _ok('update_json_atomic round-trips 5 increments')


def test_update_json_atomic_none_skips_write():
    """When mutator returns None, the file is not written."""
    from lib.json_store import update_json_atomic, read_json
    p, _ = _tmp()
    # Write initial
    update_json_atomic(p, lambda c: {'a': 1}, default={})

    # Get mtime before
    mtime_before = os.path.getmtime(p)
    time.sleep(0.05)

    # Mutator returns None — should skip write
    def conditional(c):
        return None  # skip
    result = update_json_atomic(p, conditional, default={})
    assert result is None
    # mtime must NOT have changed
    assert os.path.getmtime(p) == mtime_before
    assert read_json(p) == {'a': 1}
    _ok('update_json_atomic with mutator→None skips write')


def test_strict_update_never_overwrites_an_existing_corrupt_store(tmp_path):
    from lib.json_store import JsonStoreReadError, update_json_atomic

    path = tmp_path / 'corrupt.json'
    path.write_text('{broken', encoding='utf-8')

    try:
        update_json_atomic(
            str(path), lambda _current: {'replacement': True},
            default={}, strict=True)
    except JsonStoreReadError:
        pass
    else:
        raise AssertionError('strict update accepted a corrupt store')

    assert path.read_text(encoding='utf-8') == '{broken'


def test_update_json_atomic_thread_safe():
    """Concurrent updates must serialize and not lose increments."""
    from lib.json_store import update_json_atomic, read_json
    p, _ = _tmp()

    NUM_THREADS = 8
    INCREMENTS_PER_THREAD = 25

    def worker():
        def inc(cfg):
            cfg['count'] = cfg.get('count', 0) + 1
            return cfg
        for _ in range(INCREMENTS_PER_THREAD):
            update_json_atomic(p, inc, default={})

    threads = [threading.Thread(target=worker) for _ in range(NUM_THREADS)]
    for t in threads: t.start()
    for t in threads: t.join()

    final = read_json(p)
    expected = NUM_THREADS * INCREMENTS_PER_THREAD
    assert final == {'count': expected}, f'expected count={expected}, got {final}'
    _ok(f'update_json_atomic thread-safe under {NUM_THREADS}×{INCREMENTS_PER_THREAD} concurrent increments')


_IPC_WORKER_SRC = '''
import os, sys
sys.path.insert(0, {root!r})
from lib.json_store import update_json_atomic
p = {path!r}
N = {n}
def inc(cfg):
    cfg["count"] = cfg.get("count", 0) + 1
    return cfg
for _ in range(N):
    update_json_atomic(p, inc, default={{}})
'''


def test_update_json_atomic_inter_process_safe():
    """Concurrent updates from separate PROCESSES must not lose increments.

    Without the sidecar flock, two processes' read-modify-write cycles
    interleave and clobber each other → final count < expected. With it,
    every increment lands. Skips cleanly on platforms without fcntl (the
    flock degrades to a no-op there, so this guarantee doesn't hold).
    """
    try:
        import fcntl  # noqa: F401
    except ImportError:
        _ok('inter-process test skipped (no fcntl on this platform)')
        return
    import subprocess

    p, _ = _tmp()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    NUM_PROCS = 4
    INCREMENTS = 30
    src = _IPC_WORKER_SRC.format(root=root, path=p, n=INCREMENTS)

    procs = [subprocess.Popen([sys.executable, '-c', src])
             for _ in range(NUM_PROCS)]
    for pr in procs:
        pr.wait()
    assert all(pr.returncode == 0 for pr in procs), \
        f'worker(s) failed: {[pr.returncode for pr in procs]}'

    from lib.json_store import read_json
    final = read_json(p)
    expected = NUM_PROCS * INCREMENTS
    assert final == {'count': expected}, \
        f'inter-process lost updates: expected {expected}, got {final}'
    _ok(f'update_json_atomic inter-process-safe under {NUM_PROCS}×{INCREMENTS} '
        f'cross-process increments')


def test_update_json_atomic_jsonc_default():
    """update_json_atomic with jsonc=True can read a file with comments."""
    from lib.json_store import update_json_atomic, read_json
    p, _ = _tmp()
    with open(p, 'w') as f:
        f.write('// header\n{"value": 42}')
    def double(cfg):
        cfg['value'] *= 2
        return cfg
    result = update_json_atomic(p, double, jsonc=True)
    assert result == {'value': 84}
    # After write, no more comments (we re-emit clean JSON)
    assert read_json(p) == {'value': 84}
    _ok('update_json_atomic with jsonc=True reads comments, writes clean JSON')


def test_per_path_lock_is_per_path():
    """Locks should be path-keyed, not global."""
    from lib.json_store import _path_lock
    p1, _ = _tmp('a.json')
    p2, _ = _tmp('b.json')
    l1a = _path_lock(p1)
    l1b = _path_lock(p1)
    l2 = _path_lock(p2)
    assert l1a is l1b  # same path → same lock
    assert l1a is not l2  # different path → different lock
    _ok('_path_lock is per-path, deterministic')


def test_write_then_read_json_array():
    """JSON arrays as the top-level (lists are valid)."""
    from lib.json_store import write_json_atomic, read_json
    p, _ = _tmp()
    data = [{'id': 1}, {'id': 2}]
    write_json_atomic(p, data)
    assert read_json(p) == data
    _ok('top-level list is supported')


def test_atomic_output_path_concurrent_publishers_are_isolated():
    """Path-only writers get unique staging files and whole-file publish."""
    from lib.json_store import atomic_output_path
    p, _ = _tmp('artifact.bin')
    payloads = [bytes([index]) * 65536 for index in range(16)]
    barrier = threading.Barrier(len(payloads))
    staging = []
    failures = []

    def publish(payload):
        try:
            with atomic_output_path(p, fsync=False) as tmp:
                staging.append(tmp)
                with open(tmp, 'wb') as handle:
                    handle.write(payload)
                barrier.wait(timeout=5)
        except Exception as error:  # retain worker errors for the main thread
            failures.append(error)

    threads = [threading.Thread(target=publish, args=(payload,))
               for payload in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    assert len(set(staging)) == len(payloads)
    assert open(p, 'rb').read() in payloads
    assert not any(name.startswith('.output-')
                   for name in os.listdir(os.path.dirname(p)))
    _ok('atomic_output_path isolates concurrent path-only writers')


def test_atomic_output_path_failure_preserves_last_good_file():
    from lib.json_store import atomic_output_path
    p, _ = _tmp('artifact.bin')
    with open(p, 'wb') as handle:
        handle.write(b'last-good')
    try:
        with atomic_output_path(p, fsync=False) as tmp:
            with open(tmp, 'wb') as handle:
                handle.write(b'partial')
            raise RuntimeError('injected writer failure')
    except RuntimeError:
        pass
    assert open(p, 'rb').read() == b'last-good'
    assert not any(name.startswith('.output-')
                   for name in os.listdir(os.path.dirname(p)))
    _ok('atomic_output_path failure preserves destination and cleans staging')


def test_temporary_output_path_preserves_suffix_and_never_auto_publishes():
    from lib.json_store import temporary_output_path
    p, _ = _tmp('movie.mp4')
    with open(p, 'wb') as handle:
        handle.write(b'last-good')
    with temporary_output_path(p, suffix='.tmp.mp4') as tmp:
        assert tmp.endswith('.tmp.mp4')
        assert os.path.basename(tmp).startswith('.movie.mp4-')
        with open(tmp, 'wb') as handle:
            handle.write(b'unverified')
    assert open(p, 'rb').read() == b'last-good'
    assert not os.path.exists(tmp)
    _ok('temporary_output_path keeps format suffix and requires promotion')


def test_unicode_preserved():
    """Non-ASCII characters round-trip without escaping."""
    from lib.json_store import write_json_atomic, read_json
    p, _ = _tmp()
    data = {'msg': '你好世界 🎉 émoji'}
    write_json_atomic(p, data)
    with open(p, 'r', encoding='utf-8') as f:
        raw = f.read()
    assert '你好世界' in raw   # not \u escaped
    assert read_json(p) == data
    _ok('Unicode/emoji preserved (ensure_ascii=False)')


def test_strip_jsonc_alone():
    from lib.json_store import _strip_jsonc
    src = '''
    // top
    {
      "a": 1,  // inline
      /* block */
      "b": 2,
    }
    '''
    cleaned = _strip_jsonc(src)
    assert json.loads(cleaned) == {'a': 1, 'b': 2}
    _ok('_strip_jsonc handles all three patterns')


def main():
    print()
    print(_color('═══ json_store.py Unit Tests ═══', '36'))
    print()
    tests = [
        test_read_json_missing_returns_default,
        test_read_json_valid,
        test_read_json_empty_file_returns_default,
        test_read_json_invalid_returns_default,
        test_read_json_jsonc_with_comments,
        test_jsonc_string_aware,
        test_write_json_atomic_basic,
        test_write_json_atomic_overwrites,
        test_write_json_atomic_no_partial_on_crash,
        test_write_creates_parent_dir,
        test_write_text_atomic,
        test_update_json_atomic_initial_default,
        test_update_json_atomic_increments,
        test_update_json_atomic_none_skips_write,
        test_update_json_atomic_thread_safe,
        test_update_json_atomic_inter_process_safe,
        test_update_json_atomic_jsonc_default,
        test_per_path_lock_is_per_path,
        test_write_then_read_json_array,
        test_atomic_output_path_concurrent_publishers_are_isolated,
        test_atomic_output_path_failure_preserves_last_good_file,
        test_temporary_output_path_preserves_suffix_and_never_auto_publishes,
        test_unicode_preserved,
        test_strip_jsonc_alone,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
