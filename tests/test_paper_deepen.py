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
    from tests._standalone_guard import guard_standalone_storage
    guard_standalone_storage('test_paper_deepen.standalone')

import lib.paper.deepen_engine as de  # noqa: E402

TEST_OWNER_USER_ID = 1


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
    """Rows keyed by (phash, lang) behind a fake semantic client."""

    def __init__(self):
        self.rows = {}
        self.updates = []

class _DbPatched:
    def __init__(self):
        self.db = _FakeDB()
        self._orig = {}

    def __enter__(self):
        import lib.storage as _storage_mod
        self._orig['get_storage_client'] = _storage_mod.get_storage_client
        db = self.db

        class _Client:
            @staticmethod
            def _meta(row):
                raw = row.get('meta') or {}
                return json.loads(raw) if isinstance(raw, str) else dict(raw)

            def query(self, operation, payload):
                assert operation == 'paper.report.get'
                assert payload['user_id'] == TEST_OWNER_USER_ID
                row = db.rows.get((payload['paper_hash'], payload['lang']))
                if row is None:
                    return None
                return {**row, 'meta': self._meta(row)}

            def command(self, operation, payload, command_id):
                assert command_id
                assert payload['user_id'] == TEST_OWNER_USER_ID
                key = (payload['paper_hash'], payload['lang'])
                if operation == 'paper.report.upsert':
                    db.rows[key] = dict(payload)
                    return {'saved': True}
                assert operation == 'paper.report.second_pass.accumulate'
                row = db.rows.get(key)
                if row is None:
                    return {'found': False, 'meta': None}
                meta = self._meta(row)
                passes = meta.setdefault('secondPasses', {})
                entry = passes.setdefault(payload['name'], {})
                keys = ('prompt_tokens', 'completion_tokens',
                        'cache_read_tokens', 'cache_write_tokens',
                        'reasoning_tokens')
                prior_usage = entry.get('usage') or {}
                entry['usage'] = {
                    name: int(prior_usage.get(name, 0))
                    + int((payload.get('usage') or {}).get(name, 0))
                    for name in keys
                }
                entry['calls'] = int(entry.get('calls', 0)) + 1
                total = {
                    name: int(meta.get(
                        name.split('_')[0] + ''.join(
                            part.title() for part in name.split('_')[1:]), 0))
                    for name in keys
                }
                for pass_meta in passes.values():
                    for name in keys:
                        total[name] += int(
                            (pass_meta.get('usage') or {}).get(name, 0))
                meta['totalUsage'] = total
                row['meta'] = meta
                db.updates.append((payload['paper_hash'], payload['lang'], meta))
                return {'found': True, 'meta': meta}

        _storage_mod.get_storage_client = lambda *, write=False: _Client()
        self._storage_mod = _storage_mod
        return self

    def __exit__(self, *exc):
        self._storage_mod.get_storage_client = self._orig['get_storage_client']
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
                               'DEEP CONTENT', {'prompt_tokens': 10}, 'm1',
                               user_id=TEST_OWNER_USER_ID)
        hit = de.read_deepen_cache(
            'h1', 'deeper', 1, 'en', sec['hash'],
            user_id=TEST_OWNER_USER_ID)
        assert hit and hit['content'] == 'DEEP CONTENT', 'fresh cache not served'
        stale = de.read_deepen_cache(
            'h1', 'deeper', 1, 'en', 'differenthash',
            user_id=TEST_OWNER_USER_ID)
        assert stale is None, 'stale cache served (regeneration not detected)'
        miss = de.read_deepen_cache(
            'h1', 'derive', 1, 'en', sec['hash'],
            user_id=TEST_OWNER_USER_ID)
        assert miss is None, 'different mode must be a different cache slot'
        miss2 = de.read_deepen_cache(
            'h1', 'deeper', 1, 'zh', sec['hash'],
            user_id=TEST_OWNER_USER_ID)
        assert miss2 is None, 'different lang must be a different cache slot'
    _ok('缓存新鲜度:命中/再生失效/模式与语言分槽')


def test_neuter_hash_validation_is_load_bearing():
    """NEUTER: bypass the hash check → stale depth would be served after a
    regeneration — proving the validator is what keeps depth honest."""
    with _DbPatched() as p:
        sec = de.extract_report_section(_REPORT, 1)
        de._write_deepen_cache('h2', 'deeper', 1, 'en', sec['hash'],
                               'STALE DEPTH', None, 'm1',
                               user_id=TEST_OWNER_USER_ID)
        # Neutered read: hash check removed.
        row = p.db.rows.get(('h2', de.deepen_lang_key('deeper', 1, 'en')))
        served_stale = row['report'] if row else None
        assert served_stale == 'STALE DEPTH', \
            'NEUTER precondition: without the check the stale row IS served'
        # Real read rejects it.
        assert de.read_deepen_cache(
            'h2', 'deeper', 1, 'en', 'newhash',
            user_id=TEST_OWNER_USER_ID) is None
    _ok('NEUTER:摘掉 hash 校验 → 旧深挖被误服;校验是真闸')


# ── 3. start validation + 4. spawn/dedup ─────────────────────────────────
def test_start_validation():
    with _DbPatched():
        bad = de.start_deepen('hx', 'en', 'bogus-mode', 1, 'paper', user_id=TEST_OWNER_USER_ID)
        assert bad['error'][1] == 400, f'bad mode not rejected: {bad}'
        norep = de.start_deepen('hx', 'en', 'deeper', 1, 'paper', user_id=TEST_OWNER_USER_ID)
        assert norep['error'][1] == 409, f'missing report not 409: {norep}'
    with _DbPatched() as p:
        p.db.rows[('hx', 'en')] = {'report': _REPORT, 'meta': '{}'}
        badsec = de.start_deepen('hx', 'en', 'deeper', 99, 'paper', user_id=TEST_OWNER_USER_ID)
        assert badsec['error'][1] == 400, f'out-of-range section not 400: {badsec}'
    _ok('start 校验:坏模式 400/无报告 409/坏小节 400')


def test_start_cache_hit_and_spawn_dedup():
    sec = de.extract_report_section(_REPORT, 1)
    with _DbPatched() as p:
        p.db.rows[('hx', 'en')] = {'report': _REPORT, 'meta': '{}'}
        de._write_deepen_cache('hx', 'deeper', 1, 'en', sec['hash'],
                               'CACHED DEPTH', {'prompt_tokens': 5}, 'm1',
                               user_id=TEST_OWNER_USER_ID)
        hit = de.start_deepen('hx', 'en', 'deeper', 1, 'paper', user_id=TEST_OWNER_USER_ID)
        assert hit.get('cached') is True and hit['content'] == 'CACHED DEPTH', \
            f'cache hit path broken: {hit}'
        # A request-local long-agent arm must measure a real run, not consume
        # the canonical cached answer produced by another runtime policy.
        orig_run = de._run_deepen_task
        de._run_deepen_task = lambda *a, **k: None
        isolated = {}
        try:
            isolated = de.start_deepen(
                'hx', 'en', 'deeper', 1, 'paper',
                config={'tools': {
                    'schemaBudgetTokens': 4_000,
                    'resultEnvelope': 'legacy',
                }},
                user_id=TEST_OWNER_USER_ID)
            assert 'task' in isolated and not isolated.get('cached'), isolated
            assert (isolated['task']['requestPolicyV1']['cacheMode']
                    == 'request_local')
        finally:
            de._run_deepen_task = orig_run
            with de._deepen_dedup_lock:
                de._deepen_dedup.pop(
                    isolated.get('task', {}).get('_dedupKey'), None)
    # Spawn + dedup (worker patched to a no-op so no real LLM call happens).
    with _DbPatched() as p:
        p.db.rows[('hy', 'en')] = {'report': _REPORT, 'meta': '{}'}
        orig_run = de._run_deepen_task
        de._run_deepen_task = lambda *a, **k: None
        first = third = arm = model_variant = {}
        try:
            first = de.start_deepen('hy', 'en', 'deeper', 1, 'paper', user_id=TEST_OWNER_USER_ID)
            assert 'task' in first, f'no task spawned: {first}'
            tid = first['task']['task_id']
            second = de.start_deepen('hy', 'en', 'deeper', 1, 'paper', user_id=TEST_OWNER_USER_ID)
            assert 'joined' in second and second['joined']['task_id'] == tid, \
                f'in-flight dedup broken: {second}'
            # Different section = different task.
            third = de.start_deepen('hy', 'en', 'deeper', 3, 'paper', user_id=TEST_OWNER_USER_ID)
            assert 'task' in third and third['task']['task_id'] != tid
            # Same section but a different experiment arm or model is distinct
            # work; neither may join the baseline task.
            arm = de.start_deepen(
                'hy', 'en', 'deeper', 1, 'paper', model='paper',
                config={'tools': {
                    'schemaBudgetTokens': 4_000,
                    'resultEnvelope': 'legacy',
                }}, user_id=TEST_OWNER_USER_ID)
            assert 'task' in arm and arm['task']['task_id'] != tid, arm
            model_variant = de.start_deepen(
                'hy', 'en', 'deeper', 1, 'paper', model='other-model',
                user_id=TEST_OWNER_USER_ID)
            assert ('task' in model_variant
                    and model_variant['task']['task_id'] != tid), model_variant
        finally:
            de._run_deepen_task = orig_run
            with de._deepen_dedup_lock:
                for value in (first, third, arm, model_variant):
                    de._deepen_dedup.pop(
                        value.get('task', {}).get('_dedupKey'), None)
    _ok('start:缓存命中;实验绕缓存;精确 arm/model dedup;异节异任务')


# ── 5. worker: done event + cache write + cost accumulation ──────────────
def test_worker_done_cache_cost():
    with _DbPatched() as p:
        p.db.rows[('hz', 'en')] = {
            'report': _REPORT,
            'meta': json.dumps({'model': 'm1', 'promptTokens': 1000,
                                'completionTokens': 200})}
        orig_dispatch = de.dispatch_stream

        def _fake_dispatch(messages, *, on_content=None, tools=None, **kw):
            body = '## Deeper expansion\nStep by step content.'
            if on_content:
                on_content(body)
            return ({'role': 'assistant', 'content': body, 'tool_calls': None},
                    'stop', {'prompt_tokens': 500, 'completion_tokens': 120})

        de.dispatch_stream = _fake_dispatch
        import lib.cost as _cost_mod
        orig_cost = _cost_mod.compute_cost
        _cost_mod.compute_cost = lambda u, **k: {'costCny': 0.001, 'costUsd': 0.0001}
        try:
            task = de._new_deepen_task('dt1', 'hz', 'en', 'm1',
                                       section_idx=1, mode='deeper',
                                       section_heading='💡 Method — How It Works', user_id=TEST_OWNER_USER_ID)
            section = de.extract_report_section(_REPORT, 1)
            de._run_deepen_task(task, [{'role': 'user', 'content': 'x'}],
                                paper_hash='hz', section=section, ui_lang='en')
            # A SECOND deepen (another mode) accumulates ON TOP.
            task2 = de._new_deepen_task('dt2', 'hz', 'en', 'm1',
                                        section_idx=3, mode='derive',
                                        section_heading='📊 Experimental Analysis', user_id=TEST_OWNER_USER_ID)
            section3 = de.extract_report_section(_REPORT, 3)
            de._run_deepen_task(task2, [{'role': 'user', 'content': 'x'}],
                                paper_hash='hz', section=section3, ui_lang='en')
        finally:
            de.dispatch_stream = orig_dispatch
            _cost_mod.compute_cost = orig_cost

        assert task['status'] == 'done'
        done = [e for e in task['events'] if e.get('type') == 'done']
        assert done and done[0]['usage']['prompt_tokens'] == 500
        # Cache row written for BOTH sections.
        row1 = p.db.rows.get(('hz', de.deepen_lang_key('deeper', 1, 'en')))
        row2 = p.db.rows.get(('hz', de.deepen_lang_key('derive', 3, 'en')))
        assert row1 and 'Deeper expansion' in row1['report']
        assert row2
        meta1 = (json.loads(row1['meta']) if isinstance(row1['meta'], str)
                 else row1['meta'])
        assert meta1['kind'] == 'deep' and meta1['section_hash'] == section['hash']
        # Cost accumulated into the REPORT row: two calls summed.
        raw_report_meta = p.db.rows[('hz', 'en')]['meta']
        report_meta = (json.loads(raw_report_meta)
                       if isinstance(raw_report_meta, str)
                       else raw_report_meta)
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
