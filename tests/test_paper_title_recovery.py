#!/usr/bin/env python3
"""Headless tests for paper title recovery (the arXiv-ID-as-title fix).

Covers the two recovery layers added 2026-06-25:

  Layer 1 — ``fetch_arxiv_title``: short API retry, then a fallback that
  scrapes the arXiv abs HTML page. Verified by monkeypatching ``http_get`` to
  (a) throttle the Atom API (raise) and (b) serve a canned abs page → the
  function must recover the title from the abs page.

  Layer 2 — report Paper-Card backfill: ``_extract_title_from_report`` parses
  the ``| **Title** | … |`` row (EN + ZH), and ``_backfill_library_title``
  upserts it into ``paper_library`` ONLY when the stored title is a bare
  ``arXiv:<id>`` placeholder — never clobbering a user-renamed title.

Run standalone: ``python3 tests/test_paper_title_recovery.py``
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Isolated SQLite DB so get_thread_db() works without PG and never touches the
# real project DB. MUST be set before importing lib.database.
_TMP = tempfile.mkdtemp(prefix='tofu-title-test-')
os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', os.path.join(_TMP, 'test.db'))
os.environ.setdefault('TRADING_ENABLED', '0')


def _bootstrap_test_db():
    """Repoint the DB layer at the isolated temp file AND create the schema.

    Just setting TOFU_DB_PATH is not enough — the SQLite schema (incl.
    paper_library) is only created by init_db(). reset_sqlite_for_tests does
    both: repoint + drop cached connections + re-run init_db.
    """
    from lib.database._core import reset_sqlite_for_tests
    reset_sqlite_for_tests(os.path.join(_TMP, 'test.db'))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ─── Layer 1: fetch_arxiv_title robustness ───────────────────────


def _fresh_arxiv_module():
    """Return lib.paper.arxiv with module attributes reset to source values.

    These three tests exercise the REAL fetch_arxiv_title with only its
    http_get seam faked. Another suite (test_paper_harvest._patch_harvest)
    replaces ``lib.paper.arxiv.fetch_arxiv_title`` process-wide and restores
    it in a finally — which a pytest-timeout kill (thread-method timeouts
    abandon the test thread) never runs, so the stub ('Title of <id>') leaks
    into whatever runs afterwards in that worker (measured on public CI
    2026-08-05: a 300s timeout in test_paper_harvest left the stub installed
    and all three tests below read it). Reloading the module undoes foreign
    attribute replacement so this file does not depend on other suites'
    cleanup discipline.
    """
    import importlib
    import lib.paper.arxiv as ax
    return importlib.reload(ax)

ABS_PAGE_HTML = (
    '<!DOCTYPE html><html><head>'
    '<title>[1706.03762] Attention Is All You Need</title>'
    '<meta name="citation_title" content="Attention Is All You Need" />'
    '</head><body>…</body></html>'
)


class _FakeResp:
    def __init__(self, *, text='', content=b''):
        self.text = text
        self.content = content
    def raise_for_status(self):
        pass


def test_fetch_title_recovers_via_abs_page():
    """API throttled → title recovered from the abs HTML page."""
    ax = _fresh_arxiv_module()
    orig_get = ax.http_get
    orig_sleep = ax.time.sleep
    calls = {'api': 0, 'abs': 0}

    def _fake_get(url, **kw):
        if 'export.arxiv.org/api' in url:
            calls['api'] += 1
            raise RuntimeError('429 Too Many Requests (simulated throttle)')
        if 'arxiv.org/abs/' in url:
            calls['abs'] += 1
            return _FakeResp(text=ABS_PAGE_HTML)
        raise AssertionError(f'unexpected URL: {url}')

    ax.http_get = _fake_get
    ax.time.sleep = lambda *_a, **_k: None  # don't actually wait through retries
    try:
        title = ax.fetch_arxiv_title('1706.03762')
    finally:
        ax.http_get = orig_get
        ax.time.sleep = orig_sleep

    assert title == 'Attention Is All You Need', f'got {title!r}'
    assert calls['api'] == ax._ARXIV_TITLE_RETRIES, f'API tried {calls["api"]}× (expected retry)'
    assert calls['abs'] == 1, 'abs-page fallback not used'
    _ok('fetch_arxiv_title recovers via abs-page fallback when API is throttled')


def test_fetch_title_api_success_no_fallback():
    """API succeeds on first try → no abs-page fallback needed."""
    ax = _fresh_arxiv_module()
    orig_get = ax.http_get
    calls = {'api': 0, 'abs': 0}
    atom = (
        '<?xml version="1.0"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><title>BERT: Pre-training of Deep Bidirectional Transformers</title></entry>'
        '</feed>'
    ).encode()

    def _fake_get(url, **kw):
        if 'export.arxiv.org/api' in url:
            calls['api'] += 1
            return _FakeResp(content=atom)
        calls['abs'] += 1
        return _FakeResp(text=ABS_PAGE_HTML)

    ax.http_get = _fake_get
    try:
        title = ax.fetch_arxiv_title('1810.04805')
    finally:
        ax.http_get = orig_get
    assert title.startswith('BERT'), f'got {title!r}'
    assert calls['api'] == 1 and calls['abs'] == 0, 'should not retry or fall back on success'
    _ok('fetch_arxiv_title uses API on success, no needless retry/fallback')


def test_fetch_title_all_sources_fail_returns_empty():
    """Every source down → '' (caller falls back to arXiv:<id>)."""
    ax = _fresh_arxiv_module()
    orig_get, orig_sleep = ax.http_get, ax.time.sleep

    def _fake_get(url, **kw):
        raise RuntimeError('network down')

    ax.http_get = _fake_get
    ax.time.sleep = lambda *_a, **_k: None
    try:
        title = ax.fetch_arxiv_title('2301.99999')
    finally:
        ax.http_get, ax.time.sleep = orig_get, orig_sleep
    assert title == '', f'expected empty, got {title!r}'
    _ok('fetch_arxiv_title returns empty when every source fails (no crash)')


# ─── Layer 2a: Paper-Card title extraction ───────────────────────

def test_extract_title_from_report_en():
    from lib.paper import _extract_title_from_report
    report = (
        '# arXiv:1706.03762\n\n## ⚡ TL;DR\nblah\n\n## 📋 Paper Card\n'
        '| Field | Detail |\n|-------|--------|\n'
        '| **Title** | Attention Is All You Need |\n'
        '| **Authors** | Vaswani et al. |\n'
    )
    assert _extract_title_from_report(report) == 'Attention Is All You Need'
    _ok('_extract_title_from_report parses the EN Paper Card title row')


def test_extract_title_from_report_zh():
    from lib.paper import _extract_title_from_report
    report = (
        '## ⚡ 一句话总结\n...\n\n## 📋 论文信息卡\n'
        '| 字段 | 内容 |\n|------|------|\n'
        '| **标题** | Attention Is All You Need |\n'
    )
    assert _extract_title_from_report(report) == 'Attention Is All You Need'
    _ok('_extract_title_from_report parses the ZH Paper Card (标题) row')


def test_extract_title_rejects_placeholder_and_arxiv():
    from lib.paper import _extract_title_from_report
    # Unfilled prompt placeholder → ''
    assert _extract_title_from_report(
        '## 📋 Paper Card\n| **Title** | (full title) |\n') == ''
    # A title that is itself an arXiv id is no improvement → ''
    assert _extract_title_from_report(
        '## 📋 Paper Card\n| **Title** | arXiv:1706.03762 |\n') == ''
    # Missing row → ''
    assert _extract_title_from_report('## TL;DR\nno card here') == ''
    _ok('_extract_title_from_report rejects placeholders / arXiv-id / missing row')


def test_extract_title_strips_markdown_link():
    from lib.paper import _extract_title_from_report
    report = '## 📋 Paper Card\n| **Title** | [Real Title](https://x.com) |\n'
    assert _extract_title_from_report(report) == 'Real Title'
    _ok('_extract_title_from_report strips a markdown link to its text')


# ─── Layer 2d: _ensure_title_heading placeholder-H1 repair ───────
# (cache / re-render path — the heading that shows arXiv:<id> instead of the
#  paper title because the model baked a placeholder H1 into the report body)

def test_ensure_heading_repairs_placeholder_h1_from_card():
    from lib.paper import _ensure_title_heading
    # No library row for this hash → repair must come from the Paper Card.
    report = (
        '# arXiv:2601.04171v1\n\n## ⚡ TL;DR\nx\n\n## 📋 Paper Card\n'
        '| Field | Detail |\n|-------|--------|\n'
        '| **Title** | Agentic Rubrics as Contextual Verifiers for SWE Agents |\n'
    )
    out = _ensure_title_heading(report, 'abcdef0000000000000000000000bb01')
    assert out.splitlines()[0] == \
        '# Agentic Rubrics as Contextual Verifiers for SWE Agents', \
        f'placeholder H1 not repaired: {out.splitlines()[0]!r}'
    # Body below the heading is untouched.
    assert '## ⚡ TL;DR' in out and '## 📋 Paper Card' in out
    _ok('_ensure_title_heading swaps a placeholder # arXiv:<id> H1 for the Card title')


def test_ensure_heading_leaves_real_h1_untouched():
    from lib.paper import _ensure_title_heading
    report = '# Attention Is All You Need\n\n## TL;DR\nx\n'
    assert _ensure_title_heading(report, 'abcdef0000000000000000000000bb02') == report
    _ok('_ensure_title_heading leaves a correct H1 untouched')


def test_ensure_heading_prepends_when_missing():
    from lib.paper import _ensure_title_heading
    report = (
        '## ⚡ TL;DR\nx\n\n## 📋 Paper Card\n'
        '| **Title** | Some Paper Title |\n'
    )
    out = _ensure_title_heading(report, 'abcdef0000000000000000000000bb03')
    assert out.splitlines()[0] == '# Some Paper Title', out.splitlines()[0]
    _ok('_ensure_title_heading prepends a title when no H1 is present')


def test_ensure_heading_keeps_placeholder_when_no_better_title():
    from lib.paper import _ensure_title_heading
    # Placeholder H1, no card title, no library row → leave as-is (nothing better).
    report = '# arXiv:2601.04171v1\n\n## TL;DR\nbody\n'
    out = _ensure_title_heading(report, 'abcdef0000000000000000000000bb04')
    assert out.splitlines()[0] == '# arXiv:2601.04171v1', out.splitlines()[0]
    _ok('_ensure_title_heading keeps placeholder H1 when no better title exists')


# ─── Layer 2b: library backfill (placeholder-only) ───────────────

def _seed_library_row(phash, title, row_id='p1'):
    from lib.database import get_thread_db
    from lib.database._core_schema import PAPER_LIBRARY, upsert
    import time as _t
    db = get_thread_db()
    upsert(db, PAPER_LIBRARY, {
        'id': row_id, 'user_id': 1, 'title': title,
        'pdf_url': '', 'pdf_filename': '', 'arxiv_id': '1706.03762',
        'paper_hash': phash, 'parsed_text': '', 'parser_version': '', 'qa_history': '[]',
        'images': '[]', 'babel_cache': '{}', 'page_count': 0, 'folder_id': '',
        'created_at': int(_t.time()), 'updated_at': int(_t.time()),
    })
    db.commit()


def _read_title(phash, row_id='p1'):
    from lib.database import get_thread_db
    row = get_thread_db().execute(
        'SELECT title FROM paper_library WHERE paper_hash=? AND id=?',
        (phash, row_id)).fetchone()
    return (row['title'] if row else None)


def test_backfill_heals_arxiv_placeholder():
    from lib.paper import _backfill_library_title
    phash = 'abcdef0000000000000000000000000a'
    _seed_library_row(phash, 'arXiv:1706.03762')
    out = _backfill_library_title(phash, 'Attention Is All You Need')
    assert out == 'Attention Is All You Need', f'returned {out!r}'
    assert _read_title(phash) == 'Attention Is All You Need'
    _ok('backfill heals a row whose title is a bare arXiv:<id>')


def test_backfill_heals_empty_title():
    from lib.paper import _backfill_library_title
    phash = 'abcdef0000000000000000000000000b'
    _seed_library_row(phash, '')
    _backfill_library_title(phash, 'Some Real Title')
    assert _read_title(phash) == 'Some Real Title'
    _ok('backfill heals a row with an empty title')


def test_backfill_respects_user_renamed_title():
    from lib.paper import _backfill_library_title
    phash = 'abcdef0000000000000000000000000c'
    _seed_library_row(phash, 'My Custom Name')   # user-renamed → must NOT change
    out = _backfill_library_title(phash, 'Attention Is All You Need')
    assert _read_title(phash) == 'My Custom Name', 'user title was clobbered!'
    assert out == 'My Custom Name', f'returned {out!r}'
    _ok('backfill leaves a user-renamed title untouched (returns existing)')


def test_backfill_no_row_returns_new_title():
    from lib.paper import _backfill_library_title
    out = _backfill_library_title('abcdef00000000000000000000000099', 'Title X')
    assert out == 'Title X', f'returned {out!r}'
    _ok('backfill with no matching row returns the new title (no crash)')


# ─── Layer 2c: end-to-end through the report engine ─────────────

_MINI_REPORT = (
    '## ⚡ TL;DR\nThe Transformer.\n\n## 📋 Paper Card\n'
    '| Field | Detail |\n|-------|--------|\n'
    '| **Title** | Attention Is All You Need |\n'
    '| **Authors** | Vaswani et al. |\n\n'
    '## 📝 Technical Reference\nEnd.\n'
)


def _run_engine_once(phash, report_body=_MINI_REPORT):
    """Drive _run_report_task with a one-shot mocked dispatch (no network)."""
    import lib.paper.report_engine as re_mod
    from lib.paper import _new_report_task
    orig = re_mod.dispatch_stream

    def _fake_dispatch(messages, on_content=None, on_thinking=None, **kw):
        if on_content:
            on_content(report_body)
        msg = {'role': 'assistant', 'content': report_body, 'tool_calls': []}
        return msg, 'stop', {'prompt_tokens': 1, 'completion_tokens': 1, '_dispatch': {}}

    re_mod.dispatch_stream = _fake_dispatch
    try:
        task = _new_report_task(f'rpt_{phash[:8]}', phash, 'en', None)
        re_mod._run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper'},
        ], [])
        return task
    finally:
        re_mod.dispatch_stream = orig


def test_engine_backfills_and_emits_resolved_title():
    """Finished report heals an arXiv:<id> row AND carries resolvedTitle in done."""
    phash = 'abcdef0000000000000000000000aa01'
    _seed_library_row(phash, 'arXiv:1706.03762', row_id='e1')
    task = _run_engine_once(phash)
    assert task['status'] == 'done'
    assert task.get('resolved_title') == 'Attention Is All You Need', \
        f'task.resolved_title={task.get("resolved_title")!r}'
    done = [e for e in task['events'] if e.get('type') == 'done']
    assert done and done[-1].get('resolvedTitle') == 'Attention Is All You Need', \
        'done event missing resolvedTitle'
    assert _read_title(phash, 'e1') == 'Attention Is All You Need'
    _ok('engine backfills arXiv:<id> row + emits resolvedTitle in done event')


def test_engine_leaves_renamed_title_alone():
    """Finished report does NOT clobber a user-renamed title."""
    phash = 'abcdef0000000000000000000000aa02'
    _seed_library_row(phash, 'My Reading Notes', row_id='e2')
    task = _run_engine_once(phash)
    assert task['status'] == 'done'
    # backfill returns the existing (respected) title, row unchanged
    assert _read_title(phash, 'e2') == 'My Reading Notes', 'user title clobbered!'
    _ok('engine leaves a user-renamed title untouched end-to-end')


def test_engine_replaces_placeholder_h1_in_body():
    """Model baked `# arXiv:<id>` as the report's own H1 → engine swaps in the
    Paper-Card title so the rendered heading is the real paper title."""
    phash = 'abcdef0000000000000000000000aa03'
    # No library row → the placeholder must be fixed from the Card title.
    body = (
        '# arXiv:1706.03762\n\n## ⚡ TL;DR\nThe Transformer.\n\n## 📋 Paper Card\n'
        '| Field | Detail |\n|-------|--------|\n'
        '| **Title** | Attention Is All You Need |\n'
    )
    task = _run_engine_once(phash, report_body=body)
    assert task['status'] == 'done'
    done = [e for e in task['events'] if e.get('type') == 'done']
    report = done[-1]['report']
    assert report.splitlines()[0] == '# Attention Is All You Need', \
        f'placeholder H1 not replaced: {report.splitlines()[0]!r}'
    _ok('engine replaces a baked-in placeholder # arXiv:<id> H1 with the real title')


def main():
    print()
    print(_color('═══ Paper Title Recovery Tests ═══', '36'))
    print()
    _bootstrap_test_db()
    tests = [
        test_fetch_title_recovers_via_abs_page,
        test_fetch_title_api_success_no_fallback,
        test_fetch_title_all_sources_fail_returns_empty,
        test_extract_title_from_report_en,
        test_extract_title_from_report_zh,
        test_extract_title_rejects_placeholder_and_arxiv,
        test_extract_title_strips_markdown_link,
        test_ensure_heading_repairs_placeholder_h1_from_card,
        test_ensure_heading_leaves_real_h1_untouched,
        test_ensure_heading_prepends_when_missing,
        test_ensure_heading_keeps_placeholder_when_no_better_title,
        test_backfill_heals_arxiv_placeholder,
        test_backfill_heals_empty_title,
        test_backfill_respects_user_renamed_title,
        test_backfill_no_row_returns_new_title,
        test_engine_backfills_and_emits_resolved_title,
        test_engine_leaves_renamed_title_alone,
        test_engine_replaces_placeholder_h1_in_body,
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
