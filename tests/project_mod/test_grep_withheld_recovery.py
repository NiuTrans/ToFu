"""Withheld-evidence maps and ``from_line`` paging for ``grep_search``.

A truncated grep must say what it withheld and where, so the model can
narrow by path/include or page one file with a line-number cursor:

- engine: after the match cap stops forwarding, the backend is drained
  briefly and matches are counted per file (bounded by ceilings/deadline);
  a drain that reaches natural EOF proves the page complete;
- formatter: the truncation trailer names the fired limit, the per-file
  remainder distribution (with line ranges), and the recovery hint;
- index path: skipped candidate chunks get an exact bounded count pass.

These tests never assert a specific match ORDER (live walk and index paths
enumerate differently); they assert totals, attribution, and hints.
"""

from __future__ import annotations

import re
import shutil

import pytest

from lib.project_mod import grep_engine, tree_index
from lib.project_mod.grep_engine import SearchRequest, run_search
from lib.project_mod import read_tools
from lib.project_mod.read_tools import (
    grep_result_was_truncated,
    tool_grep,
    tool_grep_batch,
)

pytestmark = pytest.mark.unit

_HAS_BACKEND = bool(shutil.which('rg') or shutil.which('grep'))
needs_backend = pytest.mark.skipif(
    not _HAS_BACKEND, reason='no rg/grep backend on this host')


def _write_lines(path, count, *, prefix='needle line'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(f'{prefix} {i}\n' for i in range(1, count + 1)))


# ── Engine-level: drain-and-count + from_line floor ────────────────────


def _match_line_classifier(line: bytes) -> bool:
    return bool(re.match(rb'^(?:.+?:)?\d+:', line))


def _backend(tmp_path, name, body):
    script = tmp_path / name
    script.write_text(body)
    script.chmod(0o755)
    return str(script)


def test_engine_counts_remainder_per_file(tmp_path, monkeypatch):
    monkeypatch.setattr(grep_engine, '_REMAINDER_DRAIN_S', 0.4)
    backend = _backend(
        tmp_path, 'emit.sh',
        '#!/bin/sh\nprintf "a.py:1:x\\na.py:2:x\\nb.py:1:x\\nb.py:2:x\\nb.py:3:x\\n"\n')
    result = run_search(SearchRequest(
        cwd=str(tmp_path), gnu_argv=(backend,), preferred_backend='gnu',
        max_results=2, timeout=5, match_line=_match_line_classifier))
    assert result.limit_reached and not result.timed_out
    assert result.stdout == b'a.py:1:x\na.py:2:x\n'
    assert result.remainder_total == 3
    assert result.remainder_files == (('b.py', 3, 1, 3),)
    # The backend exited on its own: the remainder is provably complete.
    assert result.remainder_complete and not result.remainder_capped


def test_engine_exact_cap_proves_completeness(tmp_path, monkeypatch):
    monkeypatch.setattr(grep_engine, '_REMAINDER_DRAIN_S', 0.4)
    backend = _backend(
        tmp_path, 'emit.sh',
        '#!/bin/sh\nprintf "a.py:1:x\\na.py:2:x\\n"\n')
    result = run_search(SearchRequest(
        cwd=str(tmp_path), gnu_argv=(backend,), preferred_backend='gnu',
        max_results=2, timeout=5, match_line=_match_line_classifier))
    assert result.limit_reached
    assert result.remainder_total == 0 and result.remainder_complete


def test_engine_remainder_ceiling_marks_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(grep_engine, '_REMAINDER_MATCH_CEILING', 5)
    monkeypatch.setattr(grep_engine, '_REMAINDER_DRAIN_S', 5.0)
    lines = ''.join(f'a.py:{i}:x\\n' for i in range(1, 60))
    marker = tmp_path / 'finished'
    backend = _backend(
        tmp_path, 'emit.sh',
        f'#!/bin/sh\nprintf "{lines}"\nsleep 5\ntouch "$1"\n')
    result = run_search(SearchRequest(
        cwd=str(tmp_path), gnu_argv=(backend, str(marker)),
        preferred_backend='gnu', max_results=2, timeout=10,
        match_line=_match_line_classifier))
    assert result.remainder_capped
    assert result.remainder_total >= 5
    assert not result.remainder_complete
    assert not marker.exists()  # ceiling killed the backend early


def test_engine_from_line_filters_matches_context_and_separators(tmp_path):
    body = (
        '#!/bin/sh\nprintf "a.py:1:m\\na.py-2-c\\n--\\na.py:3:m\\n'
        'a.py-4-c\\na.py:5:m\\n"\n')
    backend = _backend(tmp_path, 'emit.sh', body)
    result = run_search(SearchRequest(
        cwd=str(tmp_path), gnu_argv=(backend,), preferred_backend='gnu',
        max_results=10, from_line=3, timeout=5,
        match_line=_match_line_classifier))
    assert result.stdout == b'a.py:3:m\na.py-4-c\na.py:5:m\n'
    assert not result.limit_reached


def test_engine_no_floor_no_counting_without_cap(tmp_path):
    backend = _backend(
        tmp_path, 'emit.sh', '#!/bin/sh\nprintf "a.py:1:x\\na.py:2:x\\n"\n')
    result = run_search(SearchRequest(
        cwd=str(tmp_path), gnu_argv=(backend,), preferred_backend='gnu',
        timeout=5, match_line=_match_line_classifier))
    assert not result.limit_reached and result.remainder_total == 0
    assert result.stdout == b'a.py:1:x\na.py:2:x\n'


# ── Public tool: truncation evidence + from_line paging (live path) ────


@pytest.fixture
def live_only(monkeypatch):
    """Force the live rg/grep walk (index coverage is tested separately)."""
    monkeypatch.setattr(
        read_tools.tree_index, 'acquire', lambda *_a, **_kw: None)


@needs_backend
def test_multifile_cap_reports_per_file_remainder(tmp_path, live_only):
    base = str(tmp_path)
    _write_lines(tmp_path / 'a.py', 40)
    _write_lines(tmp_path / 'b.py', 25)
    _write_lines(tmp_path / 'sub' / 'c.py', 10)
    out = tool_grep(base, 'needle', '.', include='*.py', max_results=30)
    assert '\u2014 30 matches:' in out
    summary = out.splitlines()[-2]
    assert summary.startswith('\u2026 (30 shown')
    assert '45 more matches' in summary
    # Per-file entries carry exact counts; their sum is the withheld total.
    # (Which file was partially shown is enumeration-order dependent, and a
    # fully shown file correctly does not appear in the remainder map.)
    entries = re.findall(r'([\w./]+) \((\d+)(?:, lines? [\d\u2013]+)?\)', summary)
    assert entries
    assert sum(int(count) for _name, count in entries) == 45
    assert {name for name, _count in entries} <= {'a.py', 'b.py', 'sub/c.py'}
    hint = out.splitlines()[-1]
    assert hint.startswith('[') and 'path=' in hint and 'from_line' in hint
    assert grep_result_was_truncated(out)


@needs_backend
def test_single_file_paging_round_trip(tmp_path, live_only):
    base = str(tmp_path)
    _write_lines(tmp_path / 'a.py', 25)
    page1 = tool_grep(base, 'needle', 'a.py', max_results=10)
    assert 'this file (15, lines 11\u201325)' in page1
    assert '[page with from_line=11]' in page1

    page2 = tool_grep(base, 'needle', 'a.py', max_results=10, from_line=11)
    assert '(from line 11)' in page2
    assert 'this file (5, lines 21\u201325)' in page2
    assert '[page with from_line=21]' in page2

    page3 = tool_grep(base, 'needle', 'a.py', max_results=10, from_line=21)
    shown = []
    for page in (page1, page2, page3):
        shown += re.findall(r'^(\d+):needle line', page, re.M)
    assert sorted(int(n) for n in shown) == list(range(1, 26))
    # Page 3 covered the tail exactly: no truncation claim may remain.
    assert not grep_result_was_truncated(page3)


@needs_backend
def test_exact_cap_is_not_marked_truncated(tmp_path, live_only):
    base = str(tmp_path)
    _write_lines(tmp_path / 'a.py', 10)
    out = tool_grep(base, 'needle', 'a.py', max_results=10)
    assert '\u2014 10 matches:' in out
    assert not grep_result_was_truncated(out)


@needs_backend
def test_char_limit_names_the_char_budget(tmp_path, live_only):
    base = str(tmp_path)
    for n in range(60):
        (tmp_path / f'f{n:02}.py').write_text('needle ' + 'x' * 280 + '\n')
    out = tool_grep(base, 'needle', '.', include='*.py', max_results=500)
    summary = out.splitlines()[-2]
    assert 'shown at 12000-char limit' in summary
    assert 'more matches' in summary
    assert grep_result_was_truncated(out)


@needs_backend
def test_from_line_drops_earlier_context_and_separators(tmp_path, live_only):
    base = str(tmp_path)
    (tmp_path / 'a.py').write_text(
        ''.join(f'needle {i}\n' for i in range(1, 8)))
    out = tool_grep(base, 'needle', 'a.py', context_lines=1, from_line=5)
    assert '\u2026' not in out  # nothing truncated, no trailer
    body = out.split('\n\n', 1)[1]
    assert '3-' not in body and '--' not in body.splitlines()
    assert re.search(r'^5:needle 5', body, re.M)
    assert not re.search(r'^[12][: -]', body, re.M)


@needs_backend
def test_count_only_unchanged(tmp_path, live_only):
    base = str(tmp_path)
    _write_lines(tmp_path / 'a.py', 7)
    out = tool_grep(base, 'needle', '.', include='*.py', count_only=True)
    assert out.endswith('7 matches (count only)')
    assert not grep_result_was_truncated(out)


@needs_backend
def test_batch_passes_from_line(tmp_path, live_only):
    base = str(tmp_path)
    _write_lines(tmp_path / 'a.py', 15)
    out = tool_grep_batch(base, [
        {'pattern': 'needle', 'path': 'a.py', 'from_line': 12,
         'max_results': 10},
    ])
    assert '(from line 12)' in out
    assert re.search(r'^12:needle line 12', out, re.M)
    assert not re.search(r'^11:needle', out, re.M)
    assert not grep_result_was_truncated(out)


@needs_backend
def test_no_match_beyond_floor_hint(tmp_path, live_only):
    base = str(tmp_path)
    _write_lines(tmp_path / 'a.py', 5)
    out = tool_grep(base, 'needle', 'a.py', from_line=99)
    assert 'No matches found' in out
    assert 'line 99 or later' in out


# ── Index path: skipped chunks get an exact bounded count pass ─────────


@needs_backend
def test_index_path_counts_skipped_chunks(tmp_path, monkeypatch):
    idx_store = tmp_path / '.idx-store'
    idx_store.mkdir()
    base = str(tmp_path)
    for n in range(4):
        _write_lines(tmp_path / f'f{n}.py', 5)
    monkeypatch.setattr(tree_index, '_index_dir', lambda: str(idx_store))
    monkeypatch.setattr(tree_index, '_mem', {})
    monkeypatch.setattr(tree_index, '_building', set())
    tree_index._build_sync(base)
    # One file per chunk, serial pool: the cap stops after the first chunk
    # and every later chunk is only count-passed.
    monkeypatch.setenv('TOFU_GREP_INDEX_JOBS', '1')
    monkeypatch.setenv('TOFU_GREP_INDEX_CHUNK', '1')
    out = tool_grep(base, 'needle', '.', include='*.py', max_results=2)
    assert '\u2014 2 matches:' in out
    summary = out.splitlines()[-2]
    assert '18 more matches' in summary  # 3 withheld + 3 × 5 count-passed
    for n in range(4):
        assert f'f{n}.py' in summary
    assert grep_result_was_truncated(out)


@needs_backend
def test_index_path_exact_coverage_stays_quiet(tmp_path, monkeypatch):
    idx_store = tmp_path / '.idx-store'
    idx_store.mkdir()
    base = str(tmp_path)
    _write_lines(tmp_path / 'f0.py', 2)
    monkeypatch.setattr(tree_index, '_index_dir', lambda: str(idx_store))
    monkeypatch.setattr(tree_index, '_mem', {})
    monkeypatch.setattr(tree_index, '_building', set())
    tree_index._build_sync(base)
    monkeypatch.setenv('TOFU_GREP_INDEX_JOBS', '1')
    monkeypatch.setenv('TOFU_GREP_INDEX_CHUNK', '1')
    out = tool_grep(base, 'needle', '.', include='*.py', max_results=2)
    assert '\u2014 2 matches:' in out
    assert not grep_result_was_truncated(out)


# ── Count-pass unit contract ───────────────────────────────────────────


@needs_backend
def test_count_skipped_chunks_exact_and_bounded(tmp_path):
    _write_lines(tmp_path / 'x.py', 4)
    _write_lines(tmp_path / 'y.py', 6)
    import time
    withheld = read_tools._count_skipped_chunks(
        str(tmp_path), 'needle',
        [[str(tmp_path / 'x.py'), str(tmp_path / 'y.py')]],
        time.monotonic() + 30)
    assert withheld is not None and not withheld['capped']
    assert withheld['total'] == 10
    files = dict((p, c) for p, c, _lo, _hi in withheld['files'])
    assert files == {str(tmp_path / 'x.py'): 4, str(tmp_path / 'y.py'): 6}


def test_index_chunks_start_without_count_window():
    import threading
    import time
    from types import SimpleNamespace

    barrier = threading.Barrier(32, timeout=10)
    results = [None] * 32

    def search(index, chunk):
        barrier.wait()
        return index, chunk, False, False, 0, SimpleNamespace(limit_reached=False)

    started = time.monotonic()
    actual = read_tools._run_pool(search, [str(i).encode() for i in range(32)],
        1, results, 0, True, 1000, re.compile(b'x'))
    assert actual == (results, False, False, False, None, 31)
    assert results == [str(i).encode() for i in range(32)]
    deadline = time.monotonic() + 2
    while any(t.name.startswith('project-read') for t in threading.enumerate()):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    print(f'32 search chunks: {(time.monotonic() - started) * 1000:.2f} ms; 0 workers')


def test_unstarted_index_chunk_failure_preserves_incomplete_evidence(monkeypatch):
    import threading
    import time
    from types import SimpleNamespace

    original = threading.Thread.start
    starts = 0
    called = []
    lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=10)

    def start(thread):
        nonlocal starts
        if thread.name.startswith('project-read'):
            starts += 1
            if starts == 2:
                raise RuntimeError('unstarted search chunk')
        return original(thread)

    def search(index, chunk):
        with lock:
            called.append(index)
        barrier.wait()
        return index, chunk, False, False, 0, SimpleNamespace(limit_reached=False)

    monkeypatch.setattr(threading.Thread, 'start', start)
    results = [None] * 4
    actual = read_tools._run_pool(search, [b'a', b'b', b'c', b'd'],
        1, results, 0, True, 1000, re.compile(b'x'))
    assert results == [b'a', b'', b'c', b'd']
    assert actual[2] is True  # Never certify missing chunk as an empty match set.
    assert sorted(called) == [0, 2, 3]
    deadline = time.monotonic() + 2
    while any(t.name.startswith('project-read') for t in threading.enumerate()):
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_incomplete_index_results_use_live_fallback(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(tree_index, 'acquire', lambda root: SimpleNamespace())
    monkeypatch.setattr(tree_index, 'grep_candidates', lambda *args: (['a.py'], 10))
    monkeypatch.setattr(read_tools, '_run_index_chunks',
        lambda *args, **kwargs: (b'a.py:1:partial-match\n', False, True, None))
    assert read_tools._grep_via_index(str(tmp_path), str(tmp_path), 'match',
        None, 0, 100, False, 5) is None


def test_all_count_chunks_start_without_count_window(monkeypatch):
    import threading
    import time
    from types import SimpleNamespace

    barrier = threading.Barrier(32, timeout=10)

    def search(request):
        path = request.rg_argv[-1]
        barrier.wait()
        return SimpleNamespace(stdout=(path + ':1\n').encode(),
                               unavailable=False, timed_out=False)

    monkeypatch.setattr(read_tools, 'run_search', search)
    started = time.monotonic()
    result = read_tools._count_skipped_chunks('.', 'x',
        [[f'f{i}.py'] for i in range(32)], time.monotonic() + 15)
    assert result['total'] == 32 and not result['capped']
    assert len(result['files']) == 32
    deadline = time.monotonic() + 2
    while any(t.name.startswith('project-count') for t in threading.enumerate()):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    print(f'32 count chunks: {(time.monotonic() - started) * 1000:.2f} ms; 0 workers')
