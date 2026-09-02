#!/usr/bin/env python3
"""Headless tests for the describe-to-recommend grounding gate.

The whole point of ``lib/paper/recommend_engine.py`` is: an LLM interprets a
fuzzy paper description, but **no card may surface unless its arXiv ID resolves
through the real ``search_arxiv`` / ``fetch_arxiv_title`` path to an addable
paper.** A title the model invents but cannot ground is dropped, logged at
debug, never returned. That gate is what prevents surfacing a hallucinated
paper (the exact "a diffusion LM won an award" conflation this feature exists
to catch).

The engine imports ``dispatch_stream`` / ``search_arxiv`` / ``fetch_arxiv_title``
at module scope, so we monkeypatch them ON THE MODULE to run fully offline. The
interpretation is now an AGENTIC tool loop (``dispatch_stream`` + web_search /
fetch_url); the fake ``dispatch_stream`` below returns the model's final JSON
in ONE no-tool round, so these tests drive the real loop + grounding gate
without a network. (Tool-calling behaviour itself is proven separately in
``test_paper_recommend_agentic.py``.)

DOUBLE-NEUTER: ``test_neuter_confirms_gate_is_load_bearing`` proves the
grounding gate is what does the work — it runs the "hallucination is dropped"
invariant against the REAL engine (must hold), then NEUTERS the gate (replaces
``_ground_candidate`` with a pass-through fabricator) and asserts the invariant
now BREAKS (the hallucinated paper leaks), then RESTORES and re-confirms.

Run standalone: ``python3 tests/test_paper_recommend_grounding.py``
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TRADING_ENABLED', '0')

import lib.paper.recommend_engine._events as re_mod  # noqa: E402
import lib.paper.recommend_engine._ground as ground_mod  # noqa: E402
import lib.paper.recommend_engine._research as research_mod  # noqa: E402


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ── A tiny fake arXiv: title → real metadata. Anything not here is "not on
#    arXiv" (search returns []), i.e. a hallucinated / ungroundable title. ──
_FAKE_ARXIV = {
    '2502.09992': {
        'arxiv_id': '2502.09992',
        'title': 'Large Language Diffusion Models',
        'authors': ['Shen Nie', 'Fengqi Zhu'],
        'summary': 'LLaDA, a diffusion model trained from scratch.',
        'published': '2025-02-14', 'primary_category': 'cs.CL',
        'pdf_url': 'https://arxiv.org/pdf/2502.09992.pdf',
        'abs_url': 'https://arxiv.org/abs/2502.09992',
    },
    '2504.12216': {
        'arxiv_id': '2504.12216',
        'title': 'd1: Scaling Reasoning in Diffusion Large Language Models via Reinforcement Learning',
        'authors': ['Siyan Zhao', 'Devaansh Gupta'],
        'summary': 'diffu-GRPO for reasoning in dLLMs.',
        'published': '2025-04-16', 'primary_category': 'cs.CL',
        'pdf_url': 'https://arxiv.org/pdf/2504.12216.pdf',
        'abs_url': 'https://arxiv.org/abs/2504.12216',
    },
    '2505.17638': {
        'arxiv_id': '2505.17638',
        'title': "Why Diffusion Models Don't Memorize: The Role of Implicit Dynamical Regularization in Training",
        'authors': ['Tony Bonnaire'],
        'summary': 'Two timescales of generalization vs memorization.',
        'published': '2025-05-23', 'primary_category': 'cs.LG',
        'pdf_url': 'https://arxiv.org/pdf/2505.17638.pdf',
        'abs_url': 'https://arxiv.org/abs/2505.17638',
    },
    # A real-but-tangential paper, used to prove the top-hit title-plausibility
    # guard rejects an unrelated hit for a half-remembered title.
    '2301.00001': {
        'arxiv_id': '2301.00001',
        'title': 'Diffusion Models for High-Fidelity Image Synthesis',
        'authors': ['A. Researcher'], 'summary': 'Images.',
        'published': '2023-01-01', 'primary_category': 'cs.CV',
        'pdf_url': 'https://arxiv.org/pdf/2301.00001.pdf',
        'abs_url': 'https://arxiv.org/abs/2301.00001',
    },
}


def _fake_search_by_title(title):
    """Map a claimed title to a fake-arXiv hit list (title-token based)."""
    tl = (title or '').lower()
    if 'large language diffusion' in tl or ('llada' in tl):
        return [_FAKE_ARXIV['2502.09992']]
    if tl.startswith('d1') or ('scaling reasoning' in tl and 'diffusion' in tl):
        return [_FAKE_ARXIV['2504.12216']]
    if "don't memorize" in tl or 'dont memorize' in tl or 'implicit dynamical' in tl:
        return [_FAKE_ARXIV['2505.17638']]
    # A half-remembered, wrong title still returns a real-but-UNRELATED top hit
    # → the engine's _title_grounded guard must reject it.
    if 'poetry' in tl or 'quantum' in tl:
        return [_FAKE_ARXIV['2301.00001']]
    return []  # not on arXiv → ungroundable


class _Patched:
    """Context manager: install fake LLM + arXiv seam on the engine module."""
    def __init__(self, llm_reply, *, fetch_titles=None):
        self.llm_reply = llm_reply
        self.fetch_titles = fetch_titles or {}
        self._orig = {}

    def __enter__(self):
        self._orig['dispatch_stream'] = research_mod.dispatch_stream
        self._orig['search_arxiv'] = ground_mod.search_arxiv
        self._orig['fetch_arxiv_title'] = ground_mod.fetch_arxiv_title

        reply = self.llm_reply

        def _fake_dispatch_stream(messages, *, on_content=None, tools=None, **kw):
            # Emulate the interpretation agent finishing in ONE round: no tool
            # calls, the final JSON streamed as content. A raised exception
            # simulates a hard dispatch failure. Returns dispatch_stream's
            # (msg, finish, usage) triple.
            if isinstance(reply, Exception):
                raise reply
            body = reply if isinstance(reply, str) else json.dumps(reply)
            if on_content:
                on_content(body)
            return ({'role': 'assistant', 'content': body, 'tool_calls': None},
                    'stop', {'prompt_tokens': 1, 'completion_tokens': 1})

        def _fake_search(query, max_results=10):
            return _fake_search_by_title(query)[:max_results]

        def _fake_fetch(arxiv_id):
            return self.fetch_titles.get((arxiv_id or '').split('v')[0], '')

        research_mod.dispatch_stream = _fake_dispatch_stream
        ground_mod.search_arxiv = _fake_search
        ground_mod.fetch_arxiv_title = _fake_fetch
        return self

    def __exit__(self, *exc):
        owners = {
            'dispatch_stream': research_mod,
            'search_arxiv': ground_mod,
            'fetch_arxiv_title': ground_mod,
        }
        for k, v in self._orig.items():
            setattr(owners[k], k, v)
        return False


def _ids(out):
    return [c['arxiv_id'].split('v')[0] for c in out['results']]


# ── The LLM interpretation the feature is designed around: user's fuzzy
#    "diffusion LM papers won an award at NeurIPS" → 2 real dLLM papers, 1
#    HALLUCINATED title with no id, plus a correction (no dLLM won; the real
#    diffusion Best Paper is 'Why Diffusion Models Don't Memorize'). ──
_CONFLATION_REPLY = {
    'candidates': [
        {'title': 'Large Language Diffusion Models', 'arxiv_id': '2502.09992',
         'venue': 'NeurIPS 2025 Oral', 'why': 'The flagship diffusion-LM paper you likely mean.'},
        {'title': 'd1: Scaling Reasoning in Diffusion Large Language Models via Reinforcement Learning',
         'arxiv_id': None, 'venue': 'NeurIPS 2025 Poster', 'why': 'RL for reasoning in dLLMs.'},
        # Pure hallucination: no such paper, no id → must be dropped.
        {'title': 'Diffusion Transformers Win the NeurIPS 2025 Best Paper Award',
         'arxiv_id': None, 'venue': 'NeurIPS 2025 Best Paper', 'why': 'Sounds like what you saw.'},
    ],
    'correction': {
        'note': "No diffusion language model won a NeurIPS 2025 award. The only "
                "diffusion Best Paper was about memorization theory, not LMs.",
        'paper': {'title': "Why Diffusion Models Don't Memorize", 'arxiv_id': '2505.17638'},
    },
}


def test_hallucinated_dropped_real_surface():
    with _Patched(_CONFLATION_REPLY):
        out = re_mod.recommend_papers('diffusion LM papers won best paper at NeurIPS this year', 6)
    ids = _ids(out)
    assert '2502.09992' in ids, f'real LLaDA missing: {ids}'
    assert '2504.12216' in ids, f'real d1 missing (title-only ground): {ids}'
    assert len(out['results']) == 2, f'expected exactly 2 grounded, got {ids}'
    # The hallucinated title has no fake-arXiv entry → never surfaces.
    for c in out['results']:
        assert 'Win the NeurIPS' not in c['title'], f'hallucination leaked: {c["title"]!r}'
    assert out['llmError'] is False
    _ok('hallucinated candidate dropped; both real papers grounded & surfaced')


def test_grounded_cards_carry_why_and_venue():
    with _Patched(_CONFLATION_REPLY):
        out = re_mod.recommend_papers('diffusion LM award papers', 6)
    by_id = {c['arxiv_id'].split('v')[0]: c for c in out['results']}
    llada = by_id['2502.09992']
    assert llada['why'] and 'flagship' in llada['why'], f'why missing: {llada.get("why")!r}'
    assert llada['venue'] == 'NeurIPS 2025 Oral', f'venue missing: {llada.get("venue")!r}'
    # Grounded metadata comes from REAL arXiv (title/authors), not the model.
    assert llada['title'] == 'Large Language Diffusion Models'
    assert llada['authors'], 'authors not populated from arXiv metadata'
    _ok('grounded cards carry model why/venue + real arXiv title/authors')


def test_correction_block_grounds_actual_winner():
    with _Patched(_CONFLATION_REPLY):
        out = re_mod.recommend_papers('a diffusion LM won a NeurIPS award', 6)
    corr = out['correction']
    assert corr and corr['note'], 'correction note missing'
    assert corr['paper'] and corr['paper']['arxiv_id'].split('v')[0] == '2505.17638', \
        f'correction paper not grounded: {corr.get("paper")}'
    assert "Memorize" in corr['paper']['title']
    _ok('correction block grounds the actual award winner as an addable card')


def test_correction_note_kept_when_paper_ungroundable():
    reply = {
        'candidates': [],
        'correction': {'note': 'That premise is mistaken.',
                       'paper': {'title': 'A Paper That Does Not Exist On Arxiv', 'arxiv_id': None}},
    }
    with _Patched(reply):
        out = re_mod.recommend_papers('some false premise', 6)
    assert out['correction'] and out['correction']['note'], 'note dropped'
    assert out['correction']['paper'] is None, 'ungroundable correction paper should be dropped'
    _ok('correction note survives even when its offered paper cannot be grounded')


def test_tangential_top_hit_rejected():
    reply = {'candidates': [
        {'title': 'Quantum Diffusion Poetry Generator', 'arxiv_id': None, 'why': 'x'}],
        'correction': None}
    with _Patched(reply):
        out = re_mod.recommend_papers('a paper about quantum poetry diffusion', 6)
    # search returns a REAL but unrelated image-synthesis paper; the
    # title-plausibility guard must reject it → nothing grounded.
    assert out['results'] == [], f'tangential hit was wrongly grounded: {_ids(out)}'
    _ok('a real-but-tangential arXiv top hit is rejected (title-plausibility guard)')


def test_direct_id_verify_fallback():
    """Model gives a correct id that search misses → grounded via fetch_arxiv_title."""
    reply = {'candidates': [
        {'title': 'Some Obscure Title Search Wont Find', 'arxiv_id': '2508.19982',
         'venue': 'NeurIPS 2025', 'why': 'the prophet paper'}],
        'correction': None}
    with _Patched(reply, fetch_titles={'2508.19982': 'Diffusion Language Models Know the Answer Before Decoding'}):
        out = re_mod.recommend_papers('the early-convergence dLLM decoding paper', 6)
    assert _ids(out) == ['2508.19982'], f'direct-id fallback failed: {_ids(out)}'
    assert out['results'][0]['title'].startswith('Diffusion Language Models Know'), \
        'title not resolved via fetch_arxiv_title'
    _ok('a real id search misses is still grounded via direct fetch_arxiv_title verify')


def test_llm_failure_flagged():
    with _Patched(RuntimeError('all slots exhausted')):
        out = re_mod.recommend_papers('anything', 6)
    assert out['llmError'] is True, 'llmError not flagged on dispatch failure'
    assert out['results'] == [] and out['correction'] is None
    _ok('LLM interpretation failure → llmError=True, empty results (no crash)')


def test_unparseable_reply():
    with _Patched('sorry I cannot help with that, here is some prose'):
        out = re_mod.recommend_papers('describe', 6)
    assert out['results'] == [] and out['llmError'] is False, 'unparseable reply mishandled'
    _ok('unparseable LLM reply → empty results, llmError stays False (parse miss)')


def test_empty_description():
    out = re_mod.recommend_papers('   ', 6)
    assert out['results'] == [] and out['correction'] is None
    _ok('empty description short-circuits to empty result')


def test_neuter_confirms_gate_is_load_bearing():
    """DOUBLE-NEUTER: prove the grounding gate is what blocks hallucination.

    1. REAL engine → hallucinated candidate is absent (invariant holds).
    2. NEUTER _ground_candidate to a pass-through fabricator (simulates 'no
       grounding') → the SAME hallucinated candidate now leaks (invariant
       BREAKS) — proving the gate does the work.
    3. RESTORE → invariant holds again.
    """
    def _hallucination_blocked():
        with _Patched(_CONFLATION_REPLY):
            out = re_mod.recommend_papers('diffusion LM won best paper', 6)
        titles = [c['title'] for c in out['results']]
        return not any('Win the NeurIPS' in tt for tt in titles)

    # 1. Real gate.
    assert _hallucination_blocked(), 'real engine let a hallucination through!'

    # 2. Neuter: replace the grounding gate with a pass-through that fabricates
    #    a card from the raw model candidate (no arXiv verification at all).
    orig_ground = re_mod._ground_candidate

    def _passthrough(cand):
        title = (cand.get('title') or '').strip()
        if not title:
            return None
        # Mint a UNIQUE id per candidate (id-less candidates otherwise collide
        # on one fallback id and get deduped — which would mask the leak behind
        # dedup instead of exercising the grounding gate we're neutering).
        rid = ground_mod._extract_arxiv_id(cand.get('arxiv_id') or '')
        if not rid:
            rid = '9999.%05d' % (abs(hash(title)) % 100000)
        return {'arxiv_id': rid, 'title': title, 'authors': [], 'summary': '',
                'published': '', 'primary_category': '', 'pdf_url': '', 'abs_url': '',
                'why': cand.get('why') or '', 'venue': cand.get('venue') or ''}

    re_mod._ground_candidate = _passthrough
    try:
        leaked = not _hallucination_blocked()
    finally:
        re_mod._ground_candidate = orig_ground

    assert leaked, 'NEUTER did not break the invariant — the test is not actually ' \
                   'exercising the grounding gate (false-confidence test).'

    # 3. Restore confirmed.
    assert _hallucination_blocked(), 'gate not restored after neuter'
    _ok('DOUBLE-NEUTER: grounding gate is load-bearing (neuter leaks, restore blocks)')


def main():
    print()
    print(_color('═══ Paper Recommend Grounding Tests ═══', '36'))
    print()
    tests = [
        test_hallucinated_dropped_real_surface,
        test_grounded_cards_carry_why_and_venue,
        test_correction_block_grounds_actual_winner,
        test_correction_note_kept_when_paper_ungroundable,
        test_tangential_top_hit_rejected,
        test_direct_id_verify_fallback,
        test_llm_failure_flagged,
        test_unparseable_reply,
        test_empty_description,
        test_neuter_confirms_gate_is_load_bearing,
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
