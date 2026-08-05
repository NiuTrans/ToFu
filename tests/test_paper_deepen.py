#!/usr/bin/env python3
"""Deepen (on-demand section depth) backend suite — design P3.

Proves fully offline:

  1. extract_report_section — h2/h3 spans, level-aware boundaries, code-fence
     tolerance, content-hash stability;
  2. cache freshness — fresh hit (hash match) / stale miss (regenerated) /
     absent; a cache hit NEVER re-bills;
  3. start_deepen validation — bad mode 400 / no report 409 / bad section 400;
  4. spawn + dedup — first call spawns, second joins the in-flight task;
  5. worker — done event carries content+usage; cache row written; cost
     ACCUMULATES into the report meta's secondPasses.deepen (two calls sum);
  6. NEUTER: break the cache hash check → a regenerated report would serve
     stale depth (proving the validator is load-bearing).

Run standalone: ``python3 tests/test_paper_deepen.py``
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('TRADING_ENABLED', '0')

if __name__ == '__main__':
    # The engine import below freezes the DB backend from the ambient env —
    # the standalone guard must run FIRST (under pytest this branch never
    # fires, so the session DB is untouched).
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_paper_deepen.standalone')

import lib.paper.deepen_engine as de  # noqa: E402


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


_REPORT = (
    '# dUltra\n\n'
    '## ⚡ TL;DR\nshort\n\n'
    '## 💡 Method — How It Works\nThe method body.\n\n'
    '### Sub-derivation\nsub body\n\n'
    '```\n## fake heading in a fence\n```\n\n'
    '## 📊 Experimental Analysis\nNumbers.\n'
)


class _FakeDB:
    """Rows keyed by (phash, lang); supports the engine's SELECT/UPDATE + upsert."""

    def __init__(self):
        self.rows = {}
        self.updates = []

    def execute(self, sql, params=None):
        self._last_sql = sql
        if sql.strip().upper().startswith('SELECT'):
            row = self.rows.get((params[0], params[1]))
            self._result = row
        elif sql.strip().upper().startswith('UPDATE'):
            self.updates.append(params)
            key = (params[1], params[2])
            if key in self.rows:
                self.rows[key]['meta'] = params[0]
            self._result = None
        return self

    def fetchone(self):
        return self._result


class _DbPatched:
    def __init__(self):
        self.db = _FakeDB()
        self._orig = {}

    def __enter__(self):
        import lib.database as _dbmod
        import lib.database._core_schema as _schema
        self._orig['get_thread_db'] = getattr(_dbmod, 'get_thread_db', None)
        self._orig['upsert'] = _schema.upsert
        _dbmod.get_thread_db = lambda: self.db
        db = self.db

        def _fake_upsert(d, table, row, **kw):
            db.rows[(row['paper_hash'], row['lang'])] = dict(row)

        _schema.upsert = _fake_upsert
        self._dbmod, self._schema = _dbmod, _schema
        return self

    def __exit__(self, *exc):
        self._dbmod.get_thread_db = self._orig['get_thread_db']
        self._schema.upsert = self._orig['upsert']
        return False


# ── 1. section extraction ────────────────────────────────────────────────
def test_extract_report_section():
    s0 = de.extract_report_section(_REPORT, 0)
    assert s0['heading'] == '⚡ TL;DR' and s0['level'] == 2
    s1 = de.extract_report_section(_REPORT, 1)
    assert s1['heading'] == '💡 Method — How It Works'
    # The h2 section ends before its h3 child... no: an h2 spans until the
    # NEXT h2 — h3s are inside it (level-aware boundary).
    assert 'Sub-derivation' in s1['text'] and 'Experimental' not in s1['text']
    s2 = de.extract_report_section(_REPORT, 2)
    assert s2['heading'] == 'Sub-derivation' and s2['level'] == 3
    assert 'Experimental' not in s2['text']
    s3 = de.extract_report_section(_REPORT, 3)
    assert s3['heading'] == '📊 Experimental Analysis'
    # Fence heading did NOT become an index (4 real headings only).
    assert de.extract_report_section(_REPORT, 4) is None
    assert de.extract_report_section(_REPORT, -1) is None
    # Hash stable + distinct per section.
    assert s1['hash'] == de.extract_report_section(_REPORT, 1)['hash']
    assert s1['hash'] != s3['hash']
    _ok('extract_report_section:层级边界/栅栏容忍/越界 None/hash 稳定')


# ── 2. cache freshness ───────────────────────────────────────────────────
def test_cache_freshness():
    with _DbPatched() as p:
        sec = de.extract_report_section(_REPORT, 1)
        de._write_deepen_cache('h1', 'deeper', 1, 'en', sec['hash'],
                               'DEEP CONTENT', {'prompt_tokens': 10}, 'm1')
        hit = de.read_deepen_cache('h1', 'deeper', 1, 'en', sec['hash'])
        assert hit and hit['content'] == 'DEEP CONTENT', 'fresh cache not served'
        stale = de.read_deepen_cache('h1', 'deeper', 1, 'en', 'differenthash')
        assert stale is None, 'stale cache served (regeneration not detected)'
        miss = de.read_deepen_cache('h1', 'derive', 1, 'en', sec['hash'])
        assert miss is None, 'different mode must be a different cache slot'
        miss2 = de.read_deepen_cache('h1', 'deeper', 1, 'zh', sec['hash'])
        assert miss2 is None, 'different lang must be a different cache slot'
    _ok('缓存新鲜度:命中/再生失效/模式与语言分槽')


def test_neuter_hash_validation_is_load_bearing():
    """NEUTER: bypass the hash check → stale depth would be served after a
    regeneration — proving the validator is what keeps depth honest."""
    with _DbPatched():
        sec = de.extract_report_section(_REPORT, 1)
        de._write_deepen_cache('h2', 'deeper', 1, 'en', sec['hash'],
                               'STALE DEPTH', None, 'm1')
        # Neutered read: hash check removed.
        try:
            from lib.database import get_thread_db
            db = get_thread_db()
            row = db.execute(
                "SELECT report, meta FROM paper_reports WHERE paper_hash = ? AND lang = ?",
                ('h2', de.deepen_lang_key('deeper', 1, 'en'))).fetchone()
            served_stale = row['report'] if row else None
        except Exception:
            served_stale = None
        assert served_stale == 'STALE DEPTH', \
            'NEUTER precondition: without the check the stale row IS served'
        # Real read rejects it.
        assert de.read_deepen_cache('h2', 'deeper', 1, 'en', 'newhash') is None
    _ok('NEUTER:摘掉 hash 校验 → 旧深挖被误服;校验是真闸')


# ── 3. start validation + 4. spawn/dedup ─────────────────────────────────
def test_start_validation():
    with _DbPatched():
        bad = de.start_deepen('hx', 'en', 'bogus-mode', 1, 'paper')
        assert bad['error'][1] == 400, f'bad mode not rejected: {bad}'
        norep = de.start_deepen('hx', 'en', 'deeper', 1, 'paper')
        assert norep['error'][1] == 409, f'missing report not 409: {norep}'
    with _DbPatched() as p:
        p.db.rows[('hx', 'en')] = {'report': _REPORT, 'meta': '{}'}
        badsec = de.start_deepen('hx', 'en', 'deeper', 99, 'paper')
        assert badsec['error'][1] == 400, f'out-of-range section not 400: {badsec}'
    _ok('start 校验:坏模式 400/无报告 409/坏小节 400')


def test_start_cache_hit_and_spawn_dedup():
    sec = de.extract_report_section(_REPORT, 1)
    with _DbPatched() as p:
        p.db.rows[('hx', 'en')] = {'report': _REPORT, 'meta': '{}'}
        de._write_deepen_cache('hx', 'deeper', 1, 'en', sec['hash'],
                               'CACHED DEPTH', {'prompt_tokens': 5}, 'm1')
        hit = de.start_deepen('hx', 'en', 'deeper', 1, 'paper')
        assert hit.get('cached') is True and hit['content'] == 'CACHED DEPTH', \
            f'cache hit path broken: {hit}'
    # Spawn + dedup (worker patched to a no-op so no real LLM call happens).
    with _DbPatched() as p:
        p.db.rows[('hy', 'en')] = {'report': _REPORT, 'meta': '{}'}
        orig_run = de._run_deepen_task
        de._run_deepen_task = lambda *a, **k: None
        try:
            first = de.start_deepen('hy', 'en', 'deeper', 1, 'paper')
            assert 'task' in first, f'no task spawned: {first}'
            tid = first['task']['task_id']
            second = de.start_deepen('hy', 'en', 'deeper', 1, 'paper')
            assert 'joined' in second and second['joined']['task_id'] == tid, \
                f'in-flight dedup broken: {second}'
            # Different section = different task.
            third = de.start_deepen('hy', 'en', 'deeper', 3, 'paper')
            assert 'task' in third and third['task']['task_id'] != tid
            import time as _t
            _t.sleep(0.05)   # let the no-op threads finish + clear the dedup
        finally:
            de._run_deepen_task = orig_run
    _ok('start:缓存命中不再生成;首发 spawn;在飞 dedup join;异节异任务')


# ── 5. worker: done event + cache write + cost accumulation ──────────────
def test_worker_done_cache_cost():
    with _DbPatched() as p:
        p.db.rows[('hz', 'en')] = {
            'report': _REPORT,
            'meta': json.dumps({'model': 'm1', 'promptTokens': 1000,
                                'completionTokens': 200})}
        events = []
        orig_dispatch = de.dispatch_stream
        orig_append = de._append_deepen_event

        def _fake_dispatch(messages, *, on_content=None, tools=None, **kw):
            body = '## Deeper expansion\nStep by step content.'
            if on_content:
                on_content(body)
            return ({'role': 'assistant', 'content': body, 'tool_calls': None},
                    'stop', {'prompt_tokens': 500, 'completion_tokens': 120})

        de.dispatch_stream = _fake_dispatch
        de._append_deepen_event = lambda t, ev: events.append(ev)
        import lib.cost as _cost_mod
        orig_cost = _cost_mod.compute_cost
        _cost_mod.compute_cost = lambda u, **k: {'costCny': 0.001, 'costUsd': 0.0001}
        try:
            task = de._new_deepen_task('dt1', 'hz', 'en', 'm1',
                                       section_idx=1, mode='deeper',
                                       section_heading='💡 Method — How It Works')
            section = de.extract_report_section(_REPORT, 1)
            de._run_deepen_task(task, [{'role': 'user', 'content': 'x'}],
                                paper_hash='hz', section=section, ui_lang='en')
            # A SECOND deepen (another mode) accumulates ON TOP.
            task2 = de._new_deepen_task('dt2', 'hz', 'en', 'm1',
                                        section_idx=3, mode='derive',
                                        section_heading='📊 Experimental Analysis')
            section3 = de.extract_report_section(_REPORT, 3)
            de._run_deepen_task(task2, [{'role': 'user', 'content': 'x'}],
                                paper_hash='hz', section=section3, ui_lang='en')
        finally:
            de.dispatch_stream = orig_dispatch
            de._append_deepen_event = orig_append
            _cost_mod.compute_cost = orig_cost

        assert task['status'] == 'done'
        done = [e for e in events if e.get('type') == 'done']
        assert done and done[0]['usage']['prompt_tokens'] == 500
        # Cache row written for BOTH sections.
        row1 = p.db.rows.get(('hz', de.deepen_lang_key('deeper', 1, 'en')))
        row2 = p.db.rows.get(('hz', de.deepen_lang_key('derive', 3, 'en')))
        assert row1 and 'Deeper expansion' in row1['report']
        assert row2
        meta1 = json.loads(row1['meta'])
        assert meta1['kind'] == 'deep' and meta1['section_hash'] == section['hash']
        # Cost accumulated into the REPORT row: two calls summed.
        report_meta = json.loads(p.db.rows[('hz', 'en')]['meta'])
        sp = report_meta.get('secondPasses', {}).get('deepen')
        assert sp, f'deepen not accumulated: {report_meta}'
        assert sp['calls'] == 2, f'expected 2 accumulated calls: {sp}'
        assert sp['usage']['prompt_tokens'] == 1000, \
            f'usage not summed across calls: {sp["usage"]}'
        assert report_meta['totalUsage']['prompt_tokens'] == 2000, \
            f'total not body+passes: {report_meta["totalUsage"]}'
    _ok('工作线程:done 事件/双槽缓存/成本两次累计求和/总量=本体+二遍')


def main():
    print()
    print(_color('═══ Paper Deepen Backend Tests ═══', '36'))
    print()
    tests = [
        test_extract_report_section,
        test_cache_freshness,
        test_neuter_hash_validation_is_load_bearing,
        test_start_validation,
        test_start_cache_hit_and_spawn_dedup,
        test_worker_done_cache_cost,
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
