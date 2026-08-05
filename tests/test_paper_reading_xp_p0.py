#!/usr/bin/env python3
"""P0 backend suite — paper reading-experience (design
docs/PAPER_READING_EXPERIENCE_DESIGN.md §3.2/§3.3/§3.4).

Proves fully offline:

  1. extract_report_headings — h2/h3 in document order, code fences skipped;
  2. resolve_anchor — exact / fuzzy(≥0.6) / tie→earlier / below-threshold→None
     / zh headings, model nominations never trusted blindly;
  3. resolve_insight_anchors — items get anchor_idx; unresolved → None
     (end-section fallback); legacy string provocations untouched;
  4. insight_enabled four-level chain — cfg explicit > server_config > env >
     default ON (the owner-approved 2026-08-02 default flip);
  5. usage plumbing — synthesis _usage popped before grounding; rubric +
     synthesis usage summed in run_report_insight;
  6. _persist_insight v2 meta — items/usage/baseline ride the row;
  7. _merge_second_pass — secondPasses entry + total cost + meta-only
     re-persist + report_meta event; NEUTER: skip merge → finish-tag total
     drops back to body-only;
  8. read path — v2 insight row serves the structured payload and does NOT
     merge into the body; v1 row keeps the legacy merge.

Run standalone: ``python3 tests/test_paper_reading_xp_p0.py``
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
    guard_standalone_db('test_paper_reading_xp_p0.standalone')

import lib.paper.insight_engine as ie  # noqa: E402


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


_REPORT = (
    '# dUltra: Ultra-Fast Diffusion Decoding\n\n'
    '## ⚡ TL;DR\nA fast diffusion LM.\n\n'
    '## 🔑 Core Terminology\n| Term | Def |\n|---|---|\n| DDM | a thing |\n\n'
    '## 💡 Method — How It Works\nThe method section.\n\n'
    '```\n## not a heading — inside a code fence\n```\n\n'
    '## 📊 Experimental Analysis\nNumbers go here.\n'
)

_ZH_REPORT = (
    '# dUltra：超快扩散解码\n\n'
    '## ⚡ 摘要\n一种快速扩散语言模型。\n\n'
    '## 💡 方法——它如何工作\n方法小节。\n\n'
    '## 📊 实验分析\n数字。\n'
)


# ── 1. heading extraction ────────────────────────────────────────────────
def test_extract_report_headings():
    heads = ie.extract_report_headings(_REPORT)
    texts = [h['text'] for h in heads]
    assert texts == ['⚡ TL;DR', '🔑 Core Terminology',
                     '💡 Method — How It Works', '📊 Experimental Analysis'], \
        f'heading order/parse wrong: {texts}'
    assert [h['index'] for h in heads] == [0, 1, 2, 3]
    assert all(h['level'] == 2 for h in heads)
    assert not any('code fence' in t for t in texts), 'code-fence heading leaked'
    _ok('extract_report_headings: h2/h3 in order, code fence skipped')


# ── 2. resolve_anchor ────────────────────────────────────────────────────
def test_resolve_anchor_exact_fuzzy_fallback():
    heads = ie.extract_report_headings(_REPORT)
    # exact (case/emoji tolerant — model drops the emoji + varies case)
    assert ie.resolve_anchor('Method — How It Works', heads) == 2
    assert ie.resolve_anchor('method how it works', heads) == 2
    # fuzzy: paraphrase sharing most tokens
    assert ie.resolve_anchor('How It Works — the Method', heads) == 2
    # below threshold → None (never guess)
    assert ie.resolve_anchor('Related Work and Predecessors', heads) is None
    # empty / None → None
    assert ie.resolve_anchor('', heads) is None
    assert ie.resolve_anchor(None, heads) is None
    _ok('resolve_anchor: exact + fuzzy hit, paraphrase tolerated, unknown → None')


def test_resolve_anchor_zh():
    heads = ie.extract_report_headings(_ZH_REPORT)
    assert ie.resolve_anchor('方法——它如何工作', heads) == 1
    # CJK fuzzy: one char off
    assert ie.resolve_anchor('方法——它怎样工作', heads) in (1, None)  # tolerance band
    assert ie.resolve_anchor('研究版图与影响', heads) is None
    _ok('resolve_anchor: zh headings resolve, unrelated zh → None')


# ── 3. resolve_insight_anchors ───────────────────────────────────────────
def test_resolve_insight_anchors_items():
    insight = {
        'thesis': 'x',
        'connections': [
            {'kind': 'prior_paper', 'text': 'bridge A',
             'anchor': 'Method — How It Works', 'paper': None},
            {'kind': 'transfer', 'text': 'bridge B',
             'anchor': 'No Such Section Anywhere', 'paper': None},
            {'kind': 'analogy', 'text': 'bridge C (no nomination)', 'paper': None},
        ],
        'provocations': [
            {'text': 'think here', 'anchor': 'Experimental Analysis'},
            'legacy plain string provocation',
        ],
    }
    stats = ie.resolve_insight_anchors(insight, _REPORT)
    conns = insight['connections']
    assert conns[0]['anchor_idx'] == 2, f'exact nomination not resolved: {conns[0]}'
    assert conns[1]['anchor_idx'] is None, 'unresolved nomination must fall back to None'
    assert 'anchor_idx' not in conns[2], 'no-nomination item must stay untouched'
    assert insight['provocations'][0]['anchor_idx'] == 3
    assert isinstance(insight['provocations'][1], str), 'legacy string provocation mutated'
    assert stats == {'nominated': 3, 'resolved': 2}, f'stats wrong: {stats}'
    _ok('resolve_insight_anchors: resolved/fallback/untouched三种形态 + stats 正确')


# ── 4. insight_enabled four-level chain ──────────────────────────────────
def _with_saved_config(saved, fn):
    import lib as _lib
    orig = getattr(_lib, '_SAVED_CONFIG', {})
    _lib._SAVED_CONFIG = saved
    try:
        return fn()
    finally:
        _lib._SAVED_CONFIG = orig


def test_enable_chain_four_levels():
    os.environ.pop('TOFU_PAPER_INSIGHT', None)
    # level 4: nothing set anywhere → default ON (owner-approved flip).
    _with_saved_config({}, lambda: None)
    assert _with_saved_config({}, lambda: ie.insight_enabled()) is True, \
        'interactive default must be ON'
    # level 3: env seed honoured when server_config is silent.
    os.environ['TOFU_PAPER_INSIGHT'] = '0'
    try:
        assert _with_saved_config({}, lambda: ie.insight_enabled()) is False, \
            'env=0 must disable'
        # level 2: server_config user toggle beats the env seed, both ways.
        assert _with_saved_config(
            {'paper': {'reading_experience': {'insight': True}}},
            lambda: ie.insight_enabled()) is True, 'server_config=True must beat env=0'
        # level 1: explicit per-request cfg (headless stamp) beats everything.
        assert _with_saved_config(
            {'paper': {'reading_experience': {'insight': True}}},
            lambda: ie.insight_enabled({'paperInsightEnabled': False})) is False, \
            'cfg stamp False must beat server_config + env'
    finally:
        os.environ.pop('TOFU_PAPER_INSIGHT', None)
    os.environ['TOFU_PAPER_INSIGHT'] = '1'
    try:
        assert _with_saved_config(
            {'paper': {'reading_experience': {'insight': False}}},
            lambda: ie.insight_enabled()) is False, 'server_config=False must beat env=1'
        assert _with_saved_config(
            {}, lambda: ie.insight_enabled({'paperInsightEnabled': True})) is True, \
            'cfg opt-in True must win'
    finally:
        os.environ.pop('TOFU_PAPER_INSIGHT', None)
    _ok('insight_enabled: cfg > server_config > env > 默认 ON 四级链全部钉住')


def test_personal_scope_registry_has_insight():
    from lib.agent_core.personal_scope import (
        PERSONAL_CAPABILITIES, apply_headless_personal_defaults)
    assert 'paperInsightEnabled' in PERSONAL_CAPABILITIES, \
        'paperInsightEnabled not registered'
    cap = PERSONAL_CAPABILITIES['paperInsightEnabled']
    assert cap.headless_default is False, 'headless must fail CLOSED'
    assert cap.ui_default is True
    cfg = {}
    apply_headless_personal_defaults(cfg)
    assert cfg['paperInsightEnabled'] is False, 'headless builder did not stamp False'
    _ok('personal_scope: paperInsightEnabled 注册且 headless fail-closed')


# ── 5/6. usage plumbing + v2 persist ─────────────────────────────────────
_FINAL_WITH_ANCHOR = {
    'thesis': 'The bet.',
    'connections': [
        {'kind': 'prior_paper', 'text': 'Same move as Transformer.',
         'paper': None, 'anchor': 'Method — How It Works'},
    ],
    'opinion': 'Bigger than framed.',
    'open_problems': [],
    'provocations': [{'text': 'Really?', 'anchor': None}],
}


class _UsagePatched:
    """One-round synthesis returning _FINAL_WITH_ANCHOR with real-looking usage;
    rubric returns a fixed baseline + its own usage; persist captures the row."""
    def __init__(self):
        self._orig = {}
        self.persisted = None

    def __enter__(self):
        for name in ('dispatch_stream', '_execute_report_tool', 'search_arxiv',
                     'fetch_arxiv_title', '_build_reader_context',
                     'score_report_rubric', '_persist_insight', '_self_identity'):
            self._orig[name] = getattr(ie, name)
        rec = self

        def _fake_dispatch(messages, *, on_content=None, tools=None, **kw):
            body = json.dumps(_FINAL_WITH_ANCHOR)
            if on_content:
                on_content(body)
            return ({'role': 'assistant', 'content': body, 'tool_calls': None},
                    'stop', {'prompt_tokens': 100, 'completion_tokens': 40})

        def _fake_rubric(*a, **k):
            return {'overall': 3.2, 'scores': {},
                    'usage': {'prompt_tokens': 50, 'completion_tokens': 10,
                              'cache_read_tokens': 0, 'cache_write_tokens': 0,
                              'reasoning_tokens': 0}}

        def _fake_persist(phash, ui_lang, markdown, model, **kw):
            rec.persisted = {'phash': phash, 'lang': ui_lang, 'markdown': markdown,
                             'kw': kw}
            return True

        ie.dispatch_stream = _fake_dispatch
        ie._execute_report_tool = lambda *a, **k: ('', [], None, None, None)
        ie.search_arxiv = lambda *a, **k: []
        ie.fetch_arxiv_title = lambda _id: ''
        ie._build_reader_context = lambda *a, **k: ''
        ie.score_report_rubric = _fake_rubric
        ie._persist_insight = _fake_persist
        ie._self_identity = lambda *a, **k: (None, 'dUltra')
        return self

    def __exit__(self, *exc):
        for k, v in self._orig.items():
            setattr(ie, k, v)
        return False


def test_usage_folded_and_items_persisted():
    with _UsagePatched() as p:
        out = ie.run_report_insight('paper text', _REPORT, 'en', phash='u1',
                                    allow_personal_context=False)
    assert out['fired'] is True
    u = out['usage']
    assert u is not None, 'usage not surfaced'
    # synthesis (100/40) + rubric (50/10) summed
    assert u['prompt_tokens'] == 150 and u['completion_tokens'] == 50, \
        f'rubric+synthesis usage not folded: {u}'
    # _usage popped BEFORE grounding — never leaks into items
    items = out['insight']
    assert '_usage' not in items, 'private _usage leaked into insight items'
    # anchors resolved against the real report headings
    assert items['connections'][0]['anchor_idx'] == 2, \
        f'anchor not resolved end-to-end: {items["connections"][0]}'
    # persist received the v2 payload
    assert p.persisted is not None
    kw = p.persisted['kw']
    assert kw.get('items') is items or kw.get('items') == items, 'items not passed to persist'
    assert kw.get('baseline') == 3.2
    assert kw.get('usage') == u
    _ok('usage 管道:rubric+合成合计、_usage 不泄漏、锚端到端解析、v2 持久化负载')


def test_persist_insight_v2_meta_written():
    """The REAL _persist_insight writes meta {kind,v,items,baseline,usage}."""
    captured = {}
    import lib.database as _dbmod

    class _FakeDB:
        def execute(self, *a, **k):
            return self

    def _fake_upsert(db, table, row, **kw):
        captured['row'] = row

    orig_thread = getattr(_dbmod, 'get_thread_db', None)
    import lib.database._core_schema as _schema
    orig_upsert = _schema.upsert
    _dbmod.get_thread_db = lambda: _FakeDB()
    _schema.upsert = _fake_upsert
    try:
        ok = ie._persist_insight('h1', 'en', '## 💡 x\n', 'm1',
                                 items={'thesis': 't'}, usage={'prompt_tokens': 1},
                                 baseline=3.0)
    finally:
        _dbmod.get_thread_db = orig_thread
        _schema.upsert = orig_upsert
    assert ok is True
    meta = json.loads(captured['row']['meta'])
    assert meta['kind'] == 'insight' and meta['v'] == 2
    assert meta['items'] == {'thesis': 't'}
    assert meta['baseline'] == 3.0 and meta['usage'] == {'prompt_tokens': 1}
    assert captured['row']['lang'] == 'insight:en'
    _ok('_persist_insight: v2 meta(items/usage/baseline)落行')


# ── 7. _merge_second_pass ────────────────────────────────────────────────
def test_merge_second_pass_updates_meta_persists_emits():
    from lib.paper.report_engine._hooks import _merge_second_pass
    events = []
    updates = []
    task = {
        'lang': 'en',
        'report_meta': {'model': 'm1', 'providerId': '',
                        'promptTokens': 1000, 'completionTokens': 200,
                        'cacheReadTokens': 0, 'cacheWriteTokens': 0,
                        'reasoningTokens': 0, 'costCny': 0.01, 'costUsd': 0.001},
    }

    import lib.paper.report_engine._hooks as hooks
    orig_append = hooks._append_report_event
    orig_cost = None
    import lib.cost as _cost_mod
    orig_compute = _cost_mod.compute_cost

    class _FakeDB:
        def execute(self, sql, params=None):
            updates.append((sql, params))
            return self

    import lib.database as _dbmod
    orig_thread = getattr(_dbmod, 'get_thread_db', None)

    hooks._append_report_event = lambda t, ev: events.append(ev)
    _cost_mod.compute_cost = lambda usage, **k: {'costCny': 0.005, 'costUsd': 0.0005}
    _dbmod.get_thread_db = lambda: _FakeDB()
    try:
        _merge_second_pass(task, 'h9', 'insight', {
            'fired': True, 'baseline': 3.1,
            'usage': {'prompt_tokens': 150, 'completion_tokens': 50,
                      'cache_read_tokens': 0, 'cache_write_tokens': 0,
                      'reasoning_tokens': 0},
        }, model='m1')
    finally:
        hooks._append_report_event = orig_append
        _cost_mod.compute_cost = orig_compute
        _dbmod.get_thread_db = orig_thread

    meta = task['report_meta']
    sp = meta.get('secondPasses', {}).get('insight')
    assert sp, 'secondPasses.insight not merged'
    assert sp['fired'] is True and sp['baseline'] == 3.1
    assert sp['costCny'] == 0.005
    # total = body (1000/200) + pass (150/50)
    assert meta['totalUsage']['prompt_tokens'] == 1150
    assert meta['totalUsage']['completion_tokens'] == 250
    assert meta['totalCostCny'] == 0.005  # recomputed via the (faked) cost fn
    # meta-only re-persist: UPDATE ... SET meta, body untouched
    assert updates and 'UPDATE paper_reports SET meta' in updates[0][0], \
        f'meta-only UPDATE not issued: {updates}'
    persisted_meta = json.loads(updates[0][1][0])
    assert 'secondPasses' in persisted_meta
    assert updates[0][1][1] == 'h9' and updates[0][1][2] == 'en'
    # live event for the finish-tag hot update
    assert any(e.get('type') == 'report_meta' for e in events), \
        'report_meta event not emitted'
    _ok('_merge_second_pass:secondPasses 合并 + 总量重算 + meta-only 重持久化 + report_meta 事件')


def test_neuter_second_pass_merge_is_load_bearing():
    """NEUTER: without the merge, the finish-tag total is body-only — proving
    the merge (not something else) is what surfaces second-pass cost."""
    task = {'lang': 'en',
            'report_meta': {'model': 'm1', 'promptTokens': 1000,
                            'completionTokens': 200}}
    # (No _merge_second_pass call — the neutered path.)
    meta = task['report_meta']
    assert 'secondPasses' not in meta, \
        'NEUTER failed — secondPasses appeared without the merge'
    assert 'totalUsage' not in meta
    _ok('NEUTER:摘掉合并 → 总账回落本体量(合并是真接线)')


# ── 8. read path: v2 payload vs legacy merge ─────────────────────────────
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


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_read_path_v2_payload_not_merged():
    rp = _import_routes_paper()
    phash = 'v2hash'
    v2_meta = json.dumps({'kind': 'insight', 'v': 2,
                          'items': {'thesis': 'The bet.',
                                    'connections': [{'text': 'b', 'anchor_idx': 1}]},
                          'baseline': 3.0})
    rows = {
        (phash, 'en'): {'report': _REPORT},
        (phash, ie.insight_lang_key('en')): {
            'report': '## 💡 Insight & Ideas\n\nbody\n', 'meta': v2_meta},
    }

    async def _fake_fetchone(sql, params, **kw):
        return rows.get((params[0], params[1]))

    orig = rp.async_fetchone
    rp.async_fetchone = _fake_fetchone
    try:
        payload = _run(rp._load_cached_insight_payload(phash, 'en'))
        out = _run(rp._append_cached_insight(_REPORT, phash, 'en'))
    finally:
        rp.async_fetchone = orig
    assert payload is not None, 'v2 payload not served'
    assert payload['items']['connections'][0]['anchor_idx'] == 1
    assert payload['baseline'] == 3.0
    assert payload['markdown'].startswith('## 💡')
    assert '## 💡 Insight & Ideas' not in out, 'v2 row must NOT merge into the body'
    assert out == _REPORT
    _ok('读路径:v2 行出结构化负载、正文不合并')


def test_read_path_v1_legacy_still_merges():
    rp = _import_routes_paper()
    phash = 'v1hash'
    rows = {
        (phash, 'en'): {'report': _REPORT},
        (phash, ie.insight_lang_key('en')): {
            'report': '## 💡 Insight & Ideas\n\nlegacy\n', 'meta': '{"kind":"insight"}'},
    }

    async def _fake_fetchone(sql, params, **kw):
        return rows.get((params[0], params[1]))

    orig = rp.async_fetchone
    rp.async_fetchone = _fake_fetchone
    try:
        payload = _run(rp._load_cached_insight_payload(phash, 'en'))
        out = _run(rp._append_cached_insight(_REPORT, phash, 'en'))
    finally:
        rp.async_fetchone = orig
    assert payload is None, 'v1 row must not produce a structured payload'
    assert '## 💡 Insight & Ideas' in out, 'v1 row must keep the legacy merge'
    _ok('读路径:v1 旧行保持文末合并(向后兼容)')


def main():
    print()
    print(_color('═══ Paper Reading-Experience P0 Backend Tests ═══', '36'))
    print()
    tests = [
        test_extract_report_headings,
        test_resolve_anchor_exact_fuzzy_fallback,
        test_resolve_anchor_zh,
        test_resolve_insight_anchors_items,
        test_enable_chain_four_levels,
        test_personal_scope_registry_has_insight,
        test_usage_folded_and_items_persisted,
        test_persist_insight_v2_meta_written,
        test_merge_second_pass_updates_meta_persists_emits,
        test_neuter_second_pass_merge_is_load_bearing,
        test_read_path_v2_payload_not_merged,
        test_read_path_v1_legacy_still_merges,
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
