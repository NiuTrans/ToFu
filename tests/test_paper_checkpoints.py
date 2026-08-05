#!/usr/bin/env python3
"""Checkpoint second-pass backend suite (design P2).

Proves fully offline:

  1. checkpoints_enabled four-level chain (cfg > server_config > env > ON);
  2. personal_scope registration (headless fail-closed);
  3. run_report_checkpoints — strict-JSON parse, deterministic anchor
     resolution (resolved kept / unresolved DROPPED), usage surfaced,
     v2 persist (items + usage in meta);
  4. the repair re-ask recovers an unparseable first reply;
  5. the hook emits the checkpoints event + folds usage into secondPasses;
  6. read path: persisted row → structured payload, no body merge;
  7. NEUTER: without the anchor resolution the cards cannot land (dropped) —
     proving the resolver is what places them.

Run standalone: ``python3 tests/test_paper_checkpoints.py``
"""

import asyncio
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
    guard_standalone_db('test_paper_checkpoints.standalone')

import lib.paper.checkpoint_engine as ce  # noqa: E402


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


_REPORT = (
    '# dUltra\n\n'
    '## ⚡ TL;DR\nshort\n\n'
    '## 🔑 Core Terminology\nterms\n\n'
    '## 💡 Method — How It Works\nThe method section.\n\n'
    '## 📊 Experimental Analysis\nNumbers.\n'
)

_CARDS = {'checkpoints': [
    {'section': 'Method — How It Works',
     'question': 'Why does removing recurrence unlock parallelism?',
     'answer': 'Because the O(n) sequential dependency is gone.'},
    {'section': 'No Such Heading Here',
     'question': 'dropped?', 'answer': 'dropped.'},
    {'section': 'Experimental Analysis',
     'question': 'What does 54.2% mean?', 'answer': 'Resolve-rate on SWE-Bench Verified.'},
]}


# ── 1/2. enable chain + registry ─────────────────────────────────────────
def _with_saved(saved, fn):
    import lib as _lib
    orig = getattr(_lib, '_SAVED_CONFIG', {})
    _lib._SAVED_CONFIG = saved
    try:
        return fn()
    finally:
        _lib._SAVED_CONFIG = orig


def test_enable_chain():
    os.environ.pop('TOFU_PAPER_CHECKPOINTS', None)
    assert _with_saved({}, lambda: ce.checkpoints_enabled()) is True, 'default must be ON'
    os.environ['TOFU_PAPER_CHECKPOINTS'] = '0'
    try:
        assert _with_saved({}, lambda: ce.checkpoints_enabled()) is False, 'env=0 must disable'
        assert _with_saved({'paper': {'reading_experience': {'checkpoints': True}}},
                           lambda: ce.checkpoints_enabled()) is True, \
            'server_config must beat env'
        assert ce.checkpoints_enabled({'paperCheckpointsEnabled': False}) is False, \
            'cfg stamp must win'
    finally:
        os.environ.pop('TOFU_PAPER_CHECKPOINTS', None)
    _ok('checkpoints_enabled: cfg > server_config > env > 默认 ON')


def test_personal_scope_registration():
    from lib.agent_core.personal_scope import (
        PERSONAL_CAPABILITIES, apply_headless_personal_defaults)
    cap = PERSONAL_CAPABILITIES.get('paperCheckpointsEnabled')
    assert cap is not None, 'paperCheckpointsEnabled not registered'
    assert cap.headless_default is False
    cfg = {}
    apply_headless_personal_defaults(cfg)
    assert cfg['paperCheckpointsEnabled'] is False
    _ok('personal_scope: paperCheckpointsEnabled fail-closed 注册')


# ── 3/4. generation + anchors + usage + persist / repair ─────────────────
class _Patched:
    """Fake dispatch_stream: first call returns the cards JSON (or garbage when
    break_first=True); the repair re-ask returns clean JSON. Persist captured."""
    def __init__(self, *, break_first=False, break_repair=False):
        self.break_first = break_first
        self.break_repair = break_repair
        self.calls = 0
        self.persisted = None
        self._orig = {}

    def __enter__(self):
        self._orig['dispatch_stream'] = ce.dispatch_stream
        rec = self

        def _fake(messages, *, on_content=None, **kw):
            rec.calls += 1
            last = messages[-1] if messages else {}
            is_repair = (last.get('role') == 'user'
                         and 'could not be parsed as JSON' in (last.get('content') or ''))
            if is_repair:
                body = 'GARBAGE AGAIN' if rec.break_repair else json.dumps(_CARDS)
            else:
                body = 'not json at all' if rec.break_first else json.dumps(_CARDS)
            if on_content:
                on_content(body)
            return ({'role': 'assistant', 'content': body}, 'stop',
                    {'prompt_tokens': 80, 'completion_tokens': 30})

        ce.dispatch_stream = _fake
        import lib.database as _dbmod
        import lib.database._core_schema as _schema
        self._orig['get_thread_db'] = getattr(_dbmod, 'get_thread_db', None)
        self._orig['upsert'] = _schema.upsert

        class _FakeDB:
            def execute(self, *a, **k):
                return self

        def _fake_upsert(db, table, row, **kw):
            rec.persisted = row

        _dbmod.get_thread_db = lambda: _FakeDB()
        _schema.upsert = _fake_upsert
        self._dbmod = _dbmod
        self._schema = _schema
        return self

    def __exit__(self, *exc):
        ce.dispatch_stream = self._orig['dispatch_stream']
        self._dbmod.get_thread_db = self._orig['get_thread_db']
        self._schema.upsert = self._orig['upsert']
        return False


def test_generate_anchor_persist():
    with _Patched() as p:
        out = ce.run_report_checkpoints(_REPORT, 'en', phash='cp1', persist=True)
    items = out['items']
    assert len(items) == 2, f'unresolved nomination must be DROPPED: {items}'
    assert items[0]['anchor_idx'] == 2 and items[0]['section'] == '💡 Method — How It Works'
    assert items[1]['anchor_idx'] == 3
    assert items[0]['question'].startswith('Why does removing')
    u = out['usage']
    assert u and u['prompt_tokens'] == 80 and u['completion_tokens'] == 30, f'usage: {u}'
    assert out['persisted'] is True
    meta = json.loads(p.persisted['meta'])
    assert meta['kind'] == 'checkpoints' and meta['v'] == 2
    assert len(meta['items']) == 2 and meta['usage'] == u
    assert p.persisted['lang'] == 'checkpoints:en'
    _ok('生成→锚定→持久化:未解析提名丢弃、usage 透出、v2 meta 落行')


def test_repair_reask_recovers():
    with _Patched(break_first=True) as p:
        out = ce.run_report_checkpoints(_REPORT, 'en', phash='cp2', persist=True)
    assert p.calls == 2, f'repair re-ask not issued: calls={p.calls}'
    assert len(out['items']) == 2, 'repair did not recover the cards'
    _ok('修复重问:首发垃圾 → temp-0 重问救回卡片')


def test_repair_both_fail_yields_nothing():
    with _Patched(break_first=True, break_repair=True) as p:
        out = ce.run_report_checkpoints(_REPORT, 'en', phash='cp3', persist=True)
    assert p.calls == 2
    assert out['items'] == [], 'both-fail must yield nothing (never garbage to the reader)'
    assert out['persisted'] is False
    _ok('NEUTER:双发皆坏 → 零卡片(不产出垃圾)')


def test_zh_prompt_and_cards():
    with _Patched():
        out = ce.run_report_checkpoints(_REPORT, 'zh', phash='cp4', persist=True)
    assert out['persisted'] is True
    assert out['items'], 'zh run produced no cards'
    _ok('zh 路径:提示词与持久化键随语言')


# ── 5. hook wiring ───────────────────────────────────────────────────────
def test_hook_emits_and_merges():
    import lib.paper.report_engine._hooks as hooks
    events = []
    updates = []
    orig_run = ce.run_report_checkpoints
    orig_append = hooks._append_report_event
    import lib.database as _dbmod
    orig_thread = getattr(_dbmod, 'get_thread_db', None)
    import lib.cost as _cost_mod
    orig_cost = _cost_mod.compute_cost

    class _FakeDB:
        def execute(self, sql, params=None):
            updates.append((sql, params))
            return self

    ce.run_report_checkpoints = lambda *a, **k: {
        'items': [{'section': 'X', 'anchor_idx': 2, 'question': 'q', 'answer': 'a'}],
        'usage': {'prompt_tokens': 80, 'completion_tokens': 30,
                  'cache_read_tokens': 0, 'cache_write_tokens': 0,
                  'reasoning_tokens': 0},
        'persisted': True, 'llmError': False}
    hooks._append_report_event = lambda t, ev: events.append(ev)
    _dbmod.get_thread_db = lambda: _FakeDB()
    _cost_mod.compute_cost = lambda u, **k: {'costCny': 0.002, 'costUsd': 0.0002}
    task = {'lang': 'en', 'config': None,
            'report_meta': {'model': 'm', 'promptTokens': 100, 'completionTokens': 50}}
    try:
        hooks._maybe_run_checkpoints(task, 'hc', 'en', _REPORT, model='m')
    finally:
        ce.run_report_checkpoints = orig_run
        hooks._append_report_event = orig_append
        _dbmod.get_thread_db = orig_thread
        _cost_mod.compute_cost = orig_cost
    assert any(e.get('type') == 'checkpoints' and e.get('items') for e in events), \
        f'checkpoints event not emitted: {events}'
    sp = task['report_meta'].get('secondPasses', {}).get('checkpoints')
    assert sp and sp['cards'] == 1 and sp['costCny'] == 0.002, f'secondPasses: {sp}'
    assert any(e.get('type') == 'report_meta' for e in events)
    _ok('钩子:checkpoints 事件 + secondPasses 合并 + report_meta 热更')


def test_hook_gate_respects_cfg_stamp():
    import lib.paper.report_engine._hooks as hooks
    called = []
    orig_run = ce.run_report_checkpoints
    ce.run_report_checkpoints = lambda *a, **k: called.append(1) or {'items': [], 'usage': None}
    task = {'lang': 'en', 'config': {'paperCheckpointsEnabled': False},
            'report_meta': {'model': 'm'}}
    try:
        hooks._maybe_run_checkpoints(task, 'hg', 'en', _REPORT, model='m')
    finally:
        ce.run_report_checkpoints = orig_run
    assert not called, 'cfg stamp False did not suppress the pass'
    _ok('钩子门:headless 戳 False → 二遍不跑(fail-closed 端到端)')


# ── 6. read path ─────────────────────────────────────────────────────────
def _import_routes_paper():
    for modname in ('flask', 'quart'):
        try:
            mod = __import__(modname)
            if hasattr(mod, 'Blueprint') and not hasattr(mod.Blueprint, 'websocket'):
                mod.Blueprint.websocket = lambda self, *a, **k: (lambda f: f)
        except Exception:
            pass
    import routes.paper as rp
    return rp


def test_read_path_payload():
    rp = _import_routes_paper()
    phash = 'cpr'
    meta = json.dumps({'kind': 'checkpoints', 'v': 2,
                       'items': [{'section': 'X', 'anchor_idx': 1,
                                  'question': 'q', 'answer': 'a'}]})
    rows = {(phash, ce.checkpoints_lang_key('en')): {'meta': meta, 'report': 'x'}}

    async def _fake_fetchone(sql, params, **kw):
        return rows.get((params[0], params[1]))

    orig = rp.async_fetchone
    rp.async_fetchone = _fake_fetchone
    try:
        payload = asyncio.new_event_loop().run_until_complete(
            rp._load_cached_checkpoints_payload(phash, 'en'))
        none_payload = asyncio.new_event_loop().run_until_complete(
            rp._load_cached_checkpoints_payload('missing', 'en'))
    finally:
        rp.async_fetchone = orig
    assert payload and payload['items'][0]['anchor_idx'] == 1, f'payload: {payload}'
    assert none_payload is None
    _ok('读路径:持久行 → 结构化负载;无行 → None(不合并、不重算)')


def main():
    print()
    print(_color('═══ Paper Checkpoint Second-Pass Tests ═══', '36'))
    print()
    tests = [
        test_enable_chain,
        test_personal_scope_registration,
        test_generate_anchor_persist,
        test_repair_reask_recovers,
        test_repair_both_fail_yields_nothing,
        test_zh_prompt_and_cards,
        test_hook_emits_and_merges,
        test_hook_gate_respects_cfg_stamp,
        test_read_path_payload,
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
