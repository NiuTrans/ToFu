#!/usr/bin/env python3
"""Headless tests for the AGENTIC insight second-pass (lib/paper/insight_engine).

The insight pass runs AFTER the fidelity report and adds the uncovered axis —
synthesis / taste / TRANSFER. Like the recommend engine, its whole value depends
on it RESEARCHING the current frontier before writing (a frozen-memory
"future directions" list is exactly the regression we already fixed elsewhere).
This suite proves, fully offline:

  1. the synthesis is a REAL agentic loop — the model's web_search call is
     executed and fed back BEFORE it emits its final insight JSON;
  2. the system prompt is date-anchored (no stale-clock frontier guessing);
  3. the anti-hallucination GROUNDING gate holds — a name-dropped paper that
     does NOT resolve on arXiv is stripped to null (prose survives, fake link
     dies), while a real one is kept and rendered as an arXiv link;
  4. the rubric critic returns a parseable, clamped, self-recomputed verdict;
  5. NEUTER: with the tool loop broken (round 0 answers immediately, tools
     ignored), the research tool is NEVER executed — proving the loop is
     load-bearing, not decorative.

Run standalone: ``python3 tests/test_paper_insight_agentic.py``
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TRADING_ENABLED', '0')

import lib.paper.insight_engine as ie  # noqa: E402


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# One REAL arXiv paper (grounds); everything else is a hallucination (drops).
_REAL = {
    'arxiv_id': '1706.03762', 'title': 'Attention Is All You Need',
    'authors': ['Vaswani'], 'summary': '', 'published': '2017-06-12',
    'primary_category': 'cs.CL', 'pdf_url': '',
    'abs_url': 'https://arxiv.org/abs/1706.03762',
}


def _fake_search(query, max_results=10):
    q = (query or '').lower()
    if 'attention is all you need' in q or 'transformer' in q:
        return [_REAL]
    return []


# The final-round insight JSON the model returns AFTER researching. It cites one
# REAL paper (grounds) and one fabricated paper (must be dropped to null).
_FINAL_INSIGHT = {
    'thesis': 'The paper bets that removing recurrence entirely — not augmenting '
              'it — is what unlocks parallelism; it breaks when sequence length '
              'dwarfs the attention budget.',
    'connections': [
        {'kind': 'prior_paper',
         'text': 'Same move as the Transformer: replace a sequential inductive '
                 'bias with a fully-parallel attention mechanism.',
         'paper': {'title': 'Attention Is All You Need', 'arxiv_id': '1706.03762'}},
        {'kind': 'transfer',
         'text': 'This transfers to your CAD-OCR layout problem via the shared '
                 'set-to-set alignment structure.',
         'paper': {'title': 'A Totally Made Up Nonexistent Paper 9999',
                   'arxiv_id': '9999.99999'}},
    ],
    'opinion': 'The core idea is bigger than the paper frames it; the ablation on '
               'head count is under-powered so the "8 heads" claim is over-stated.',
    'open_problems': [
        {'text': 'Test whether the mechanism survives at 100k-token context.',
         'grounded_by': {'title': 'Attention Is All You Need', 'arxiv_id': '1706.03762'}},
    ],
    'provocations': ['Is attention actually necessary, or just convenient?'],
}


class _Patched:
    """Fake an agentic 2-round dispatch: round 0 issues a web_search call, round 1
    (offered no tools) returns the final insight JSON. Records executed tools so
    the test can assert real tool use. ``break_loop=True`` neuters the loop.

    Also patches the arXiv seam so grounding runs without a network, and stubs
    the reader-context DB/memory lookups so the pass runs with no live stores.
    """
    def __init__(self, *, break_loop=False):
        self.break_loop = break_loop
        self._orig = {}
        self.executed_tools = []
        self.dispatched_rounds = []   # (n_tool_msgs, had_tools)
        self.systems_seen = []

    def __enter__(self):
        for name in ('dispatch_stream', '_execute_report_tool', 'search_arxiv',
                     'fetch_arxiv_title', '_build_reader_context'):
            self._orig[name] = getattr(ie, name)
        rec = self

        def _fake_dispatch_stream(messages, *, on_content=None, tools=None, **kw):
            rec.systems_seen.append(messages[0]['content'] if messages else '')
            n_tool_msgs = sum(1 for m in messages if m.get('role') == 'tool')
            rec.dispatched_rounds.append((n_tool_msgs, bool(tools)))
            first_round = (n_tool_msgs == 0)
            if first_round and not rec.break_loop:
                return ({'role': 'assistant', 'content': '',
                         'tool_calls': [{
                             'id': 'tc1', 'type': 'function',
                             'function': {'name': 'web_search',
                                          'arguments': json.dumps(
                                              {'queries': [{'query': 'transformer follow-up frontier'}]})},
                         }]}, 'tool_calls', {'prompt_tokens': 1, 'completion_tokens': 1})
            body = json.dumps(_FINAL_INSIGHT)
            if on_content:
                on_content(body)
            return ({'role': 'assistant', 'content': body, 'tool_calls': None},
                    'stop', {'prompt_tokens': 1, 'completion_tokens': 1})

        def _fake_execute_report_tool(name, args_str, user_question='', abort=None):
            rec.executed_tools.append(name)
            return ('RESULT: the current frontier is long-context attention.',
                    [{'title': 'x'}], None, None, None)

        ie.dispatch_stream = _fake_dispatch_stream
        ie._execute_report_tool = _fake_execute_report_tool
        ie.search_arxiv = _fake_search
        ie.fetch_arxiv_title = lambda _id: ''
        # No live library/memory in the test — inject a fixed context string.
        ie._build_reader_context = lambda *a, **k: '## READER CONTEXT\n- Some prior paper'
        return self

    def __exit__(self, *exc):
        for k, v in self._orig.items():
            setattr(ie, k, v)
        return False


# NOTE: the report is about a DIFFERENT paper than the one connection[0] cites.
# connection[0] cites the real Transformer paper (1706.03762) as a legitimate
# prior-work bridge; the paper UNDER ANALYSIS here is "dUltra", so the self-ref
# guard must NOT fire on that bridge. (The dedicated self-ref test below uses a
# fixture whose report title DOES match the cited paper.)
_REPORT = '# dUltra: Ultra-Fast Diffusion Decoding\n\n## TL;DR\nA fast diffusion LM.\n'
_PAPER = 'dUltra accelerates diffusion decoding. ' * 20


def test_synthesis_actually_researches():
    """The loop executes the model's web_search call BEFORE emitting insight JSON."""
    with _Patched() as p:
        out = ie.generate_insight(_PAPER, _REPORT, 'en', phash='abc123')
    assert 'web_search' in p.executed_tools, \
        f'web_search never executed — not agentic: {p.executed_tools}'
    assert len(p.dispatched_rounds) >= 2, f'expected >=2 rounds: {p.dispatched_rounds}'
    assert p.dispatched_rounds[0][1] is True, 'first round was not offered tools'
    assert out['insight'] is not None, 'no insight produced'
    assert out['llmError'] is False
    _ok('synthesis runs a real web_search research round before writing the insight')


def test_system_prompt_is_date_anchored():
    """Every dispatch carries today's date — no stale-clock frontier guessing."""
    import re as _re
    with _Patched() as p:
        ie.generate_insight(_PAPER, _REPORT, 'en', phash='abc123')
    assert p.systems_seen, 'no dispatch happened'
    assert _re.search(r"[Tt]oday'?s? date is \d{4}-\d{2}-\d{2}", p.systems_seen[0]), \
        f'insight system prompt is not date-anchored: {p.systems_seen[0][:200]!r}'
    _ok('insight system prompt injects the current date (kills stale-clock frontier guesses)')


def test_grounding_gate_drops_hallucinated_paper():
    """A name-dropped paper that does NOT resolve on arXiv is stripped to null
    (prose survives); the REAL one is kept and rendered as an arXiv link."""
    with _Patched():
        out = ie.generate_insight(_PAPER, _REPORT, 'en', phash='abc123')
    insight = out['insight']
    conns = insight['connections']
    # Connection 0 cited the real Transformer paper → grounded card kept.
    assert isinstance(conns[0]['paper'], dict) and conns[0]['paper']['arxiv_id'].startswith('1706.03762'), \
        f'real paper was not grounded: {conns[0]["paper"]}'
    # Connection 1 cited a fabricated paper → dropped to None, but its PROSE survives.
    assert conns[1]['paper'] is None, \
        f'hallucinated paper was NOT dropped: {conns[1]["paper"]}'
    assert 'CAD-OCR' in conns[1]['text'], 'the ungrounded connection prose was lost'
    assert out['grounded'] == 2 and out['dropped'] == 1, \
        f'grounding accounting wrong: grounded={out["grounded"]} dropped={out["dropped"]}'
    # Rendered markdown links the real paper, and does NOT link the fake id.
    md = out['markdown']
    assert '1706.03762' in md and 'arxiv.org/abs/1706.03762' in md, 'real link missing from render'
    assert '9999.99999' not in md, 'fabricated arXiv id leaked into the rendered insight'
    _ok('grounding gate: hallucinated paper stripped to null (prose kept), real paper linked')


def test_rubric_parses_clamps_and_recomputes():
    """The rubric critic returns clamped 1-5 scores and a SELF-RECOMPUTED mean
    (never trusts the model's arithmetic)."""
    # Model returns out-of-range + a deliberately WRONG 'overall' — we must clamp
    # and recompute.
    rubric_json = {
        'scores': {'thesis_strength': 4, 'novelty_of_idea': 9,   # 9 → clamp to 5
                   'defensible_grounded_opinion': 3, 'transfer_concreteness': 5},
        'justifications': {'thesis_strength': 'clear bet'},
        'overall': 1.0,   # LIE — real mean of {4,5,3,5} = 4.25
        'one_line_verdict': 'Leaves the reader with a real idea.',
    }

    orig = ie.dispatch_stream
    try:
        def _fake(messages, *, on_content=None, **kw):
            body = json.dumps(rubric_json)
            if on_content:
                on_content(body)
            return ({'role': 'assistant', 'content': body}, 'stop',
                    {'prompt_tokens': 1, 'completion_tokens': 1})
        ie.dispatch_stream = _fake
        v = ie.score_report_rubric('# Some report body long enough to score.')
    finally:
        ie.dispatch_stream = orig

    assert v is not None, 'rubric returned None on a valid reply'
    assert v['scores']['novelty_of_idea'] == 5, f'score not clamped to 5: {v["scores"]}'
    assert set(v['scores']) == set(ie.RUBRIC_AXES), f'axis set drift: {v["scores"].keys()}'
    assert abs(v['overall'] - 4.25) < 1e-6, \
        f"overall not self-recomputed (got {v['overall']}, model lied 1.0)"
    _ok('rubric critic: scores clamped to 1-5 and overall recomputed (model arithmetic ignored)')


def test_neuter_confirms_agentic_loop_is_load_bearing():
    """NEUTER: with the loop broken (model answers immediately, tools ignored),
    the research tool is NEVER executed — proving the loop drives the tool use."""
    with _Patched() as p:
        ie.generate_insight(_PAPER, _REPORT, 'en', phash='abc123')
    assert 'web_search' in p.executed_tools, 'precondition failed: real loop did not research'

    with _Patched(break_loop=True) as p:
        out = ie.generate_insight(_PAPER, _REPORT, 'en', phash='abc123')
    assert 'web_search' not in p.executed_tools, \
        'NEUTER did not break the invariant — research tool ran with the loop broken ' \
        '(false-confidence test).'
    # The insight is still produced from the model's memory-only answer (that
    # path is unchanged) — so this isolates that the LOOP is what adds RESEARCH.
    assert out['insight'] is not None, 'neuter unexpectedly killed the whole feature'
    _ok('NEUTER: the tool loop is load-bearing (broken loop → no research executed)')


class _RepairPatched:
    """Simulate the residual JSON-failure mode the higher temperature causes: the
    SYNTHESIS round returns prose-wrapped / truncated garbage that the extractor
    cannot parse, and the model only emits clean JSON on the REPAIR re-ask (the
    turn whose last user message is the repair instruction).

    ``break_repair=True`` neuters the recovery by making the repair re-ask ALSO
    return garbage — proving the repair step is what saves the feature.
    """
    def __init__(self, *, break_repair=False):
        self.break_repair = break_repair
        self._orig = {}
        self.saw_repair_reask = False

    def __enter__(self):
        for name in ('dispatch_stream', '_execute_report_tool', 'search_arxiv',
                     'fetch_arxiv_title', '_build_reader_context'):
            self._orig[name] = getattr(ie, name)
        rec = self

        # A reply that is NOT parseable JSON: prose, then a truncated object.
        garbage = ('Sure! Here is my analysis of the paper.\n\n'
                   'The key insight is that {"thesis": "removing recurrence", '
                   '"connections": [{"kind": "prior_pap')  # truncated mid-string

        def _fake_dispatch_stream(messages, *, on_content=None, tools=None, **kw):
            last = messages[-1] if messages else {}
            is_repair = (last.get('role') == 'user'
                         and ie._REPAIR_INSTRUCTION[:40] in (last.get('content') or ''))
            if is_repair:
                rec.saw_repair_reask = True
                if rec.break_repair:
                    body = garbage  # neuter: repair also fails
                else:
                    body = json.dumps(_FINAL_INSIGHT)  # clean reformat
                if on_content:
                    on_content(body)
                return ({'role': 'assistant', 'content': body}, 'stop',
                        {'prompt_tokens': 1, 'completion_tokens': 1})
            # The synthesis round: emit unparseable garbage (no tool calls, so the
            # loop ends and the parse-then-repair path is exercised).
            if on_content:
                on_content(garbage)
            return ({'role': 'assistant', 'content': garbage, 'tool_calls': None},
                    'stop', {'prompt_tokens': 1, 'completion_tokens': 1})

        ie.dispatch_stream = _fake_dispatch_stream
        ie._execute_report_tool = lambda *a, **k: ('', [], None, None, None)
        ie.search_arxiv = _fake_search
        ie.fetch_arxiv_title = lambda _id: ''
        ie._build_reader_context = lambda *a, **k: '## READER CONTEXT\n- Some prior paper'
        return self

    def __exit__(self, *exc):
        for k, v in self._orig.items():
            setattr(ie, k, v)
        return False


def test_repair_reask_recovers_unparseable_json():
    """A prose-wrapped / truncated synthesis reply is recovered by the one-shot
    repair re-ask — the feature does NOT silently no-op on a parse failure."""
    with _RepairPatched() as p:
        out = ie.generate_insight(_PAPER, _REPORT, 'en', phash='abc123')
    assert p.saw_repair_reask, 'repair re-ask was never issued on unparseable JSON'
    assert out['insight'] is not None, \
        'repair did not recover the insight — feature silently no-opped'
    assert out['llmError'] is False
    # Recovered content still flows through grounding + render.
    assert '1706.03762' in out['markdown'], 'recovered insight was not grounded/rendered'
    _ok('repair re-ask recovers an unparseable synthesis reply (no silent no-op)')


def test_neuter_repair_is_load_bearing():
    """NEUTER: if the repair re-ask ALSO returns garbage, the feature yields
    nothing — proving the repair step (not some other path) is what recovers it."""
    with _RepairPatched(break_repair=True) as p:
        out = ie.generate_insight(_PAPER, _REPORT, 'en', phash='abc123')
    assert p.saw_repair_reask, 'precondition: repair re-ask must have been attempted'
    assert out['insight'] is None, \
        'NEUTER failed — insight recovered even though BOTH synthesis and repair ' \
        'returned garbage (recovery is coming from somewhere other than the repair).'
    _ok('NEUTER: repair is load-bearing (break the re-ask → feature yields nothing)')


# Fixture for the self-reference guard: a FOUNDATIONAL paper (the report is
# ABOUT "Attention Is All You Need") whose insight bridges back to ITSELF — the
# real bd79f6/Transformer failure. connection[0] cites the paper itself by id;
# connection[1] is circular prose (own title named 2×); connection[2] is a
# LEGITIMATE bridge to a different paper and MUST survive.
_SELFREF_INSIGHT = {
    'thesis': 'Removing recurrence is the bet.',
    'connections': [
        {'kind': 'prior_paper',
         'text': 'The multi-head attention here generalizes the attention primitive.',
         'paper': {'title': 'Attention Is All You Need', 'arxiv_id': '1706.03762'}},
        {'kind': 'analogy',
         'text': 'Attention in Attention Is All You Need is a generalized form of '
                 'the attention in Attention Is All You Need itself.',
         'paper': {'title': 'Some Other Title', 'arxiv_id': None}},
        {'kind': 'transfer',
         'text': 'This transfers to retrieval-augmented memory work.',
         'paper': {'title': 'EvoLM', 'arxiv_id': '2605.03871'}},
    ],
    'opinion': 'Bigger than its framing.',
    'open_problems': [],
    'provocations': ['Is attention necessary?'],
}


class _SelfRefPatched:
    """One-round dispatch returning _SELFREF_INSIGHT; report title == the paper
    the first two connections point back to. Grounds EvoLM + the self-titled
    ref (search returns the real Transformer for that title) so, WITHOUT the
    guard, the self-refs would survive as 'grounded'."""
    def __init__(self):
        self._orig = {}

    def __enter__(self):
        for name in ('dispatch_stream', '_execute_report_tool', 'search_arxiv',
                     'fetch_arxiv_title', '_build_reader_context'):
            self._orig[name] = getattr(ie, name)

        def _fake_dispatch(messages, *, on_content=None, tools=None, **kw):
            body = json.dumps(_SELFREF_INSIGHT)
            if on_content:
                on_content(body)
            return ({'role': 'assistant', 'content': body, 'tool_calls': None},
                    'stop', {'prompt_tokens': 1, 'completion_tokens': 1})

        def _fake_search(query, max_results=10):
            q = (query or '').lower()
            if 'attention is all you need' in q:
                return [_REAL]
            if 'evolm' in q:
                return [{'arxiv_id': '2605.03871', 'title': 'EvoLM',
                         'abs_url': 'https://arxiv.org/abs/2605.03871'}]
            return []

        ie.dispatch_stream = _fake_dispatch
        ie._execute_report_tool = lambda *a, **k: ('', [], None, None, None)
        ie.search_arxiv = _fake_search
        ie.fetch_arxiv_title = lambda _id: ''
        ie._build_reader_context = lambda *a, **k: '## READER CONTEXT\n- prior'
        return self

    def __exit__(self, *exc):
        for k, v in self._orig.items():
            setattr(ie, k, v)
        return False


_SELFREF_REPORT = '# Attention Is All You Need\n\n## TL;DR\nThe Transformer.\n'


def test_selfref_connections_dropped_legit_survives():
    """Fix #2: on a foundational paper, connections that bridge back to the paper
    ITSELF (by id, by title-match, or via circular prose) are DROPPED entirely,
    while a legitimate bridge to a DIFFERENT paper survives. Passing self_title
    explicitly so the test doesn't depend on report-head parsing."""
    with _SelfRefPatched():
        out = ie.generate_insight(
            'paper text', _SELFREF_REPORT, 'en', phash='selfref1',
            self_arxiv_id='1706.03762', self_title='Attention Is All You Need')
    conns = out['insight']['connections']
    titles = [(c.get('paper') or {}).get('title') if c.get('paper') else None for c in conns]
    texts = ' || '.join(c['text'] for c in conns)
    assert len(conns) == 1, f'self-ref connections not dropped: {[c["text"][:40] for c in conns]}'
    assert 'EvoLM' in (titles[0] or ''), f'legit bridge did not survive: {titles}'
    assert 'generalized form of the attention' not in texts, 'circular prose survived'
    assert out['selfref'] == 2, f'expected 2 self-refs dropped, got {out["selfref"]}'
    # The self-titled ref must NOT have been grounded+rendered as a link.
    assert out['markdown'].count('1706.03762') == 0, 'self-referential link leaked into render'
    _ok('self-reference guard: circular/self-titled bridges dropped, legit bridge kept')


def test_neuter_selfref_guard_is_load_bearing():
    """NEUTER: with no self identity supplied (guard disabled), the circular +
    self-titled connections SURVIVE and the self-titled one even grounds —
    proving the guard is what removes them."""
    with _SelfRefPatched():
        out = ie.generate_insight(
            'paper text', 'no title here', 'en', phash='',
            self_arxiv_id=None, self_title=None)
    conns = out['insight']['connections']
    assert len(conns) == 3, \
        f'guard fired without identity — should keep all 3, got {len(conns)}'
    assert out['selfref'] == 0, f'selfref should be 0 with no identity, got {out["selfref"]}'
    _ok('NEUTER: self-ref guard is load-bearing (no identity → circular bridges survive)')


def test_headroom_gate_threshold():
    """The a-priori headroom gate fires only when the report's own insight
    baseline is at/below threshold (4.0); fails OPEN on a None baseline."""
    assert ie.insight_gate_fires(3.5) is True
    assert ie.insight_gate_fires(4.0) is True
    assert ie.insight_gate_fires(4.01) is False
    assert ie.insight_gate_fires(4.75) is False
    assert ie.insight_gate_fires(None) is True, 'gate must fail OPEN on scoring failure'
    assert ie.INSIGHT_GATE_THRESHOLD == 4.0
    _ok('headroom gate: fires at baseline<=4.0, withholds above, fails open on None')


class _ReportInsightPatched:
    """Drive run_report_insight() end-to-end offline.

    Controls the gate input via a fake ``score_report_rubric`` (``baseline``),
    a one-round synthesis returning _FINAL_INSIGHT, the arXiv grounding seam,
    the reader-context builder (records whether it was CALLED — the personal
    leak signal), and the persistence upsert (records the persisted row).
    """
    def __init__(self, *, baseline):
        self.baseline = baseline
        self._orig = {}
        self.reader_context_called = False
        self.persisted = None   # {'lang':..., 'report':...} or None

    def __enter__(self):
        for name in ('dispatch_stream', '_execute_report_tool', 'search_arxiv',
                     'fetch_arxiv_title', '_build_reader_context',
                     'score_report_rubric', '_persist_insight', '_self_identity'):
            self._orig[name] = getattr(ie, name)
        rec = self

        def _fake_dispatch(messages, *, on_content=None, tools=None, **kw):
            body = json.dumps(_FINAL_INSIGHT)
            if on_content:
                on_content(body)
            return ({'role': 'assistant', 'content': body, 'tool_calls': None},
                    'stop', {'prompt_tokens': 1, 'completion_tokens': 1})

        def _fake_reader_ctx(*a, **k):
            rec.reader_context_called = True
            return '## READER CONTEXT\n- Some prior paper the operator read'

        def _fake_persist(phash, ui_lang, markdown, model):
            rec.persisted = {'lang': ie.insight_lang_key(ui_lang),
                             'report': markdown, 'phash': phash}
            return True

        ie.dispatch_stream = _fake_dispatch
        ie._execute_report_tool = lambda *a, **k: ('', [], None, None, None)
        ie.search_arxiv = _fake_search
        ie.fetch_arxiv_title = lambda _id: ''
        ie._build_reader_context = _fake_reader_ctx
        # Fake the rubric: return the desired baseline (or None to test fail-open).
        ie.score_report_rubric = lambda *a, **k: (
            None if rec.baseline is None else {'overall': rec.baseline, 'scores': {}})
        ie._persist_insight = _fake_persist
        # Report-under-analysis is 'dUltra' (distinct from cited Transformer) so
        # the self-ref guard doesn't fire on the legit bridge.
        ie._self_identity = lambda *a, **k: (None, 'dUltra')
        return self

    def __exit__(self, *exc):
        for k, v in self._orig.items():
            setattr(ie, k, v)
        return False


def test_report_insight_low_baseline_fires_generates_persists():
    """Flag-on + LOW baseline → gate FIRES, insight generated + persisted under
    the ``insight:<ui>`` key + markdown returned for the reader."""
    with _ReportInsightPatched(baseline=3.2) as p:
        out = ie.run_report_insight(_PAPER, _REPORT, 'en', phash='rpt1',
                                    allow_personal_context=True)
    assert out['fired'] is True, f'gate should fire at baseline 3.2: {out}'
    assert out['insight'] is not None and out['markdown'], 'no insight produced'
    assert p.persisted is not None, 'insight was not persisted'
    assert p.persisted['lang'] == 'insight:en', \
        f'persisted under wrong key: {p.persisted["lang"]}'
    assert p.persisted['phash'] == 'rpt1'
    assert out['persisted'] is True
    _ok('run_report_insight: low baseline → fires, generates, persists under insight:en')


def test_report_insight_high_baseline_withheld():
    """Flag-on + HIGH baseline → gate WITHHELD: no generation, no persistence."""
    with _ReportInsightPatched(baseline=4.75) as p:
        out = ie.run_report_insight(_PAPER, _REPORT, 'en', phash='rpt2',
                                    allow_personal_context=True)
    assert out['fired'] is False, f'gate should withhold at baseline 4.75: {out}'
    assert out['insight'] is None, 'insight generated despite gate withholding'
    assert out['markdown'] == '', 'markdown produced despite gate withholding'
    assert p.persisted is None, 'persisted despite gate withholding'
    assert p.reader_context_called is False, 'generation ran despite gate withholding'
    _ok('run_report_insight: high baseline → gate withholds (no gen, no persist)')


def test_report_insight_headless_suppresses_personal_context():
    """Headless surface (allow_personal_context=False): the pass still fires +
    persists, but the operator's personal reader-context is NEVER built —
    proving the personal_scope fail-closed gate is honoured end-to-end."""
    with _ReportInsightPatched(baseline=3.2) as p:
        out = ie.run_report_insight(_PAPER, _REPORT, 'en', phash='rpt3',
                                    allow_personal_context=False)
    assert out['fired'] is True, 'gate should still fire on headless (low baseline)'
    assert out['insight'] is not None, 'insight should still be produced headless'
    assert p.reader_context_called is False, \
        'PERSONAL LEAK: reader-context was built on a headless surface'
    _ok('run_report_insight: headless → personal reader-context SUPPRESSED (no leak), pass still runs')


def test_neuter_gate_wiring_is_load_bearing():
    """NEUTER: force the gate to always fire (baseline None → fail-open) on a
    HIGH-baseline report; confirm the gate decision is what withholds — i.e.
    with the gate neutered even a saturated report would generate."""
    # With baseline=None the gate fails OPEN → fires even though a real 4.75
    # would withhold. Proves insight_gate_fires is the actual lever.
    with _ReportInsightPatched(baseline=None) as p:
        out = ie.run_report_insight(_PAPER, _REPORT, 'en', phash='rpt4',
                                    allow_personal_context=True)
    assert out['fired'] is True, 'None baseline must fail OPEN (fire)'
    assert p.persisted is not None, 'fail-open path should generate + persist'
    # And the control: a high baseline withholds (already covered above), so the
    # gate — not something else — decides.
    _ok('NEUTER: gate wiring is load-bearing (None→fail-open fires; high→withholds)')


def test_flag_gate_default_off():
    """The pass is opt-in — default OFF so the report path stays byte-identical."""
    os.environ.pop('TOFU_PAPER_INSIGHT', None)
    assert ie.insight_enabled() is False, 'insight must default OFF'
    os.environ['TOFU_PAPER_INSIGHT'] = '1'
    try:
        assert ie.insight_enabled() is True, 'TOFU_PAPER_INSIGHT=1 did not enable'
    finally:
        os.environ.pop('TOFU_PAPER_INSIGHT', None)
    _ok('flag gate: default OFF, TOFU_PAPER_INSIGHT=1 enables')


def main():
    print()
    print(_color('═══ Paper Insight Agentic Second-Pass Tests ═══', '36'))
    print()
    tests = [
        test_synthesis_actually_researches,
        test_system_prompt_is_date_anchored,
        test_grounding_gate_drops_hallucinated_paper,
        test_rubric_parses_clamps_and_recomputes,
        test_neuter_confirms_agentic_loop_is_load_bearing,
        test_repair_reask_recovers_unparseable_json,
        test_neuter_repair_is_load_bearing,
        test_selfref_connections_dropped_legit_survives,
        test_neuter_selfref_guard_is_load_bearing,
        test_headroom_gate_threshold,
        test_report_insight_low_baseline_fires_generates_persists,
        test_report_insight_high_baseline_withheld,
        test_report_insight_headless_suppresses_personal_context,
        test_neuter_gate_wiring_is_load_bearing,
        test_flag_gate_default_off,
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
