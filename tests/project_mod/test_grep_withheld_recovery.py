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
import sys

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
    return bool(re.match(rb'^(?:[^\x00]*\x00)?\d+:', line))


def _emit_argv(tmp_path, payload: bytes, *, then: str = ''):
    """Fake backend argv writing *payload* verbatim (NUL bytes included)."""
    script = tmp_path / 'emit.py'
    script.write_text(
        'import sys\n'
        f'sys.stdout.buffer.write({payload!r})\n'
        'sys.stdout.flush()\n' + then)
    return (sys.executable, str(script))


def test_engine_counts_remainder_per_file(tmp_path, monkeypatch):
    monkeypatch.setattr(grep_engine, '_REMAINDER_DRAIN_S', 0.4)
    argv = _emit_argv(tmp_path, (
        b'a.py\x001:x\na.py\x002:x\nb.py\x001:x\nb.py\x002:x\nb.py\x003:x\n'))
    result = run_search(SearchRequest(
        cwd=str(tmp_path), gnu_argv=argv, preferred_backend='gnu',
        max_results=2, timeout=5, match_line=_match_line_classifier))
    assert result.limit_reached and not result.timed_out
    assert result.stdout == b'a.py\x001:x\na.py\x002:x\n'
    assert result.remainder_total == 3
    assert result.remainder_files == (('b.py', 3, 1, 3),)
    # The backend exited on its own: the remainder is provably complete.
    assert result.remainder_complete and not result.remainder_capped


def test_engine_exact_cap_proves_completeness(tmp_path, monkeypatch):
    monkeypatch.setattr(grep_engine, '_REMAINDER_DRAIN_S', 0.4)
    argv = _emit_argv(tmp_path, b'a.py\x001:x\na.py\x002:x\n')
    result = run_search(SearchRequest(
        cwd=str(tmp_path), gnu_argv=argv, preferred_backend='gnu',
        max_results=2, timeout=5, match_line=_match_line_classifier))
    assert result.limit_reached
    assert result.remainder_total == 0 and result.remainder_complete


def test_engine_remainder_ceiling_marks_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(grep_engine, '_REMAINDER_MATCH_CEILING', 5)
    monkeypatch.setattr(grep_engine, '_REMAINDER_DRAIN_S', 5.0)
    lines = b''.join(b'a.py\x00%d:x\n' % i for i in range(1, 60))
    marker = tmp_path / 'finished'
    argv = _emit_argv(tmp_path, lines, then=(
        'import pathlib, time\ntime.sleep(5)\n'
        'pathlib.Path(sys.argv[1]).touch()\n'))
    result = run_search(SearchRequest(
        cwd=str(tmp_path), gnu_argv=(*argv, str(marker)),
        preferred_backend='gnu', max_results=2, timeout=10,
        match_line=_match_line_classifier))
    assert result.remainder_capped
    assert result.remainder_total >= 5
    assert not result.remainder_complete
    assert not marker.exists()  # ceiling killed the backend early


def test_engine_from_line_filters_matches_context_and_separators(tmp_path):
    argv = _emit_argv(tmp_path, (
        b'a.py\x001:m\na.py\x002-c\n--\na.py\x003:m\n'
        b'a.py\x004-c\na.py\x005:m\n'))
    result = run_search(SearchRequest(
        cwd=str(tmp_path), gnu_argv=argv, preferred_backend='gnu',
        max_results=10, from_line=3, timeout=5,
        match_line=_match_line_classifier))
    assert result.stdout == b'a.py\x003:m\na.py\x004-c\na.py\x005:m\n'
    assert not result.limit_reached


def test_engine_floor_reads_bare_record_line_numbers(tmp_path):
    # Single-file records carry no path; their timestamps hold ``:NN:`` and
    # ``-NN-`` runs that must never be mistaken for the line number.
    payload = (b'29-2026-10-24 15:29:55 ctx\n'
               b'30:2026-10-24 15:09:55 match\n'
               b'31-2026-10-24 15:10:00 ctx\n')
    result = run_search(SearchRequest(
        cwd=str(tmp_path), gnu_argv=_emit_argv(tmp_path, payload),
        preferred_backend='gnu', from_line=30, timeout=5,
        match_line=_match_line_classifier))
    assert result.stdout == payload.split(b'\n', 1)[1]


def test_engine_no_floor_no_counting_without_cap(tmp_path):
    argv = _emit_argv(tmp_path, b'a.py\x001:x\na.py\x002:x\n')
    result = run_search(SearchRequest(
        cwd=str(tmp_path), gnu_argv=argv, preferred_backend='gnu',
        timeout=5, match_line=_match_line_classifier))
    assert not result.limit_reached and result.remainder_total == 0
    assert result.stdout == b'a.py\x001:x\na.py\x002:x\n'


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


# ── Record framing: timestamps and dated names never shift the split ───


def _log_line(i, word='needle'):
    # Minutes differ from line numbers, so splitting at the content's
    # ``HH:MM:`` would report a wrong line.
    return f'2026-10-24 15:{(i * 7 + 3) % 60:02d}:55 [INFO] {word} {i}\n'


@needs_backend
def test_single_file_paging_round_trip_on_timestamped_lines(tmp_path, live_only):
    base = str(tmp_path)
    (tmp_path / 'app.log').write_text(
        ''.join(_log_line(i) for i in range(1, 26)))
    page1 = tool_grep(base, 'needle', 'app.log', max_results=10)
    assert '[page with from_line=11]' in page1
    page2 = tool_grep(base, 'needle', 'app.log', max_results=10, from_line=11)
    assert '[page with from_line=21]' in page2
    page3 = tool_grep(base, 'needle', 'app.log', max_results=10, from_line=21)
    shown = []
    for page in (page1, page2, page3):
        shown += [int(n) for n in re.findall(r'^(\d+):2026-10-24 ', page, re.M)]
    assert shown == list(range(1, 26))
    assert not grep_result_was_truncated(page3)


@needs_backend
def test_single_file_context_on_timestamped_lines_counts_matches_only(
        tmp_path, live_only):
    base = str(tmp_path)
    (tmp_path / 'app.log').write_text(''.join(
        _log_line(i, 'needle' if i == 30 else 'filler') for i in range(1, 34)))
    out = tool_grep(base, 'needle', 'app.log', context_lines=1)
    assert '\u2014 1 matches:' in out
    assert out.split('\n\n', 1)[1].splitlines() == [
        f'29-{_log_line(29, "filler").rstrip()}',
        f'30:{_log_line(30).rstrip()}',
        f'31-{_log_line(31, "filler").rstrip()}',
    ]


def _grouped_records(out):
    """``{header: [record, ...]}`` from a grouped multi-file result body."""
    groups: dict[str, list[str]] = {}
    current = None
    for line in out.split('\n\n', 1)[1].splitlines():
        if line == '--':
            continue
        if re.match(r'\d+[:-]', line):
            groups[current].append(line)
        else:
            current = line
            groups.setdefault(current, [])
    return groups


@needs_backend
@pytest.mark.parametrize('search_path', ['live', 'index'])
def test_multifile_dated_names_and_timestamps_keep_exact_records(
        tmp_path, monkeypatch, search_path):
    project = tmp_path / 'proj'
    project.mkdir()
    base = str(project)
    dated = 'app.log.2026-10-24.00001'
    (project / dated).write_text(''.join(
        _log_line(i, 'needle' if i in (12, 30) else 'filler')
        for i in range(1, 34)))
    (project / 'b-1-2.py').write_text('x = 1\nneedle = "12:34:56"\n')
    if search_path == 'live':
        monkeypatch.setattr(
            read_tools.tree_index, 'acquire', lambda *_a, **_kw: None)
    else:
        idx_store = tmp_path / 'idx'
        idx_store.mkdir()
        monkeypatch.setattr(tree_index, '_index_dir', lambda: str(idx_store))
        monkeypatch.setattr(tree_index, '_mem', {})
        monkeypatch.setattr(tree_index, '_building', set())
        tree_index._build_sync(base)

    out = tool_grep(base, 'needle', '.', context_lines=1)
    assert '\u2014 3 matches:' in out
    assert '\0' not in out
    assert _grouped_records(out) == {
        dated: [f'{n}{":" if n in (12, 30) else "-"}'
                f'{_log_line(n, "needle" if n in (12, 30) else "filler").rstrip()}'
                for n in (11, 12, 13, 29, 30, 31)],
        'b-1-2.py': ['1-x = 1', '2:needle = "12:34:56"'],
    }

    capped = tool_grep(base, 'needle', '.', max_results=1)
    summary = capped.splitlines()[-2]
    entries = re.findall(r'(\S+) \((\d+), lines? ([\d\u2013]+)\)', summary)
    assert sum(int(count) for _path, count, _lines in entries) == 2
    assert {path for path, _count, _lines in entries} <= {dated, 'b-1-2.py'}
    for _path, _count, lines in entries:
        assert {int(n) for n in lines.split('\u2013')} <= {2, 12, 30}


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


@pytest.mark.parametrize('jobs', [2, 8, 16])
def test_index_chunks_bound_prefetch_and_preserve_every_result(jobs):
    import threading
    import time
    from types import SimpleNamespace

    active = peak = 0
    lock = threading.Lock()
    results = [None] * 128

    def search(index, chunk):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.002)
            return index, chunk, False, False, 0, SimpleNamespace(limit_reached=False)
        finally:
            with lock:
                active -= 1

    actual = read_tools._run_pool(search, [str(i).encode() for i in range(128)],
        jobs, results, 0, True, 1000, re.compile(b'x'))
    assert actual == (results, False, False, False, None, 127)
    assert results == [str(i).encode() for i in range(128)]
    assert 1 < peak <= jobs
    assert active == 0



def test_unstarted_index_chunk_failure_preserves_incomplete_evidence(monkeypatch):
    import threading
    import time
    from types import SimpleNamespace

    original = threading.Thread.start
    submit_chunk = read_tools._submit_index_chunk
    unstartable = threading.Event()
    called = []
    lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=10)

    # The executor retries one failed carrier start, so chunk 1 stays
    # unstarted only when every start attempted for its submission fails.
    def submit(pool, function, index, chunk):
        if index == 1:
            unstartable.set()
        try:
            return submit_chunk(pool, function, index, chunk)
        finally:
            unstartable.clear()

    def start(thread):
        if thread.name.startswith('project-read') and unstartable.is_set():
            raise RuntimeError('unstarted search chunk')
        return original(thread)

    def search(index, chunk):
        with lock:
            called.append(index)
        barrier.wait()
        return index, chunk, False, False, 0, SimpleNamespace(limit_reached=False)

    monkeypatch.setattr(read_tools, '_submit_index_chunk', submit)
    monkeypatch.setattr(threading.Thread, 'start', start)
    results = [None] * 4
    actual = read_tools._run_pool(search, [b'a', b'b', b'c', b'd'],
        4, results, 0, True, 1000, re.compile(b'x'))
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


def test_count_chunks_bound_prefetch_and_preserve_complete_counts(monkeypatch):
    import threading
    import time
    from types import SimpleNamespace

    active = peak = 0
    lock = threading.Lock()

    def search(request):
        nonlocal active, peak
        path = request.rg_argv[-1]
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.002)
            return SimpleNamespace(stdout=(path + ':1\n').encode(),
                                   unavailable=False, timed_out=False)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(read_tools, 'run_search', search)
    result = read_tools._count_skipped_chunks('.', 'x',
        [[f'f{i}.py'] for i in range(128)], time.monotonic() + 15)
    assert result['total'] == 128 and not result['capped']
    assert len(result['files']) == 128
    assert 1 < peak <= 2
    assert active == 0
