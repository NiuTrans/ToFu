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
        test_update_json_atomic_jsonc_default,
        test_per_path_lock_is_per_path,
        test_write_then_read_json_array,
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
