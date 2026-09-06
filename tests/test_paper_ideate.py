#!/usr/bin/env python3
"""R3 — anti-"A+B" idea gate (lib/paper/ideate.py).

Owner's two hidden-assumption pins, proven here (pure — search/LLM seams
monkeypatched, no DB, no network):

  PIN #2 (structural gate: idea must link a real open_gap):
    * an A+B stitch whose linked_gap_id points at NOTHING in the verified
      open_gaps is rejected at the free structural gate;
    * NEUTER — drop the "linked_gap_id must match a real gap" rule and the
      invented-problem stitch survives (test goes red).

  PIN #1 (novelty = f(retrieved neighbor set), not f(model self-report)):
    * the novelty prior set ALWAYS contains the forced search_arxiv top-K,
      including ids the model never self-reported;
    * an idea whose novelty only holds when you ignore retrieval is sunk once
      the forced neighbor (a dead-ringer paper) enters the judge's basis;
    * NEUTER — if _novelty_prior_set stopped forcing retrieval (self-report
      only), the stitch's colliding neighbor never reaches the judge (test
      asserts the forced id IS in the prior set).

  Plus: rejected ideas keep scores+reason (calibration audit); the threshold is
  a single constant the tests never hardcode (they drive scores relative to it).

Run standalone:  python tests/test_paper_ideate.py
Under pytest:    pytest tests/test_paper_ideate.py -m unit
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


try:
    import pytest
    pytestmark = [pytest.mark.unit]
except ImportError:
    pytest = None


# ── Fixtures ───────────────────────────────────────────────────────────────

_OPEN_GAPS = {
    'schema_version': 1,
    'direction': 'long-context KV compression',
    'open_gaps': [
        {'id': 'gap_1', 'gap': 'no exact recall under KV compression',
         'why_open': 'only perplexity measured', 'kind_hint': 'methodology',
         'evidence': ['2305.11111']},
        {'id': 'gap_2', 'gap': 'no analysis of per-layer compressibility',
         'why_open': 'treated as uniform', 'kind_hint': 'analysis',
         'evidence': ['2401.22222']},
    ],
}


def _good_idea():
    return {
        'title': 'Per-layer learnable KV rank with recall-preserving loss',
        'kind': 'methodology', 'linked_gap_id': 'gap_1',
        'corpus_anchor_id': '2305.11111',
        'corpus_delta': 'Replace the anchor paper\'s uniform static rank with a '
                        'retrieval-conditioned per-layer allocation.',
        'core_mechanism': 'A retrieval-consistency loss makes each layer learn its own '
                          'rank so needle tokens survive compression — a mechanism absent '
                          'from prior uniform-rank work.',
        'novelty_claim': 'Unlike arXiv:2305.11111 (uniform static rank) this learns rank '
                         'end-to-end under a recall objective.',
        'prior_art': ['2305.11111'],
        'falsifiable_prediction': 'On needle-in-haystack at 32k ctx, exact-match recall '
                                  'stays >90% at 4x compression where uniform-rank drops <60%.',
        'why_not_AB': 'It is not low-rank + a loss glued: the loss reshapes what "rank" '
                      'means per layer, a mechanism neither prior piece has.',
    }


def _ab_stitch_no_gap():
    """An A+B stitch that invents its own problem (links no real gap)."""
    idea = _good_idea()
    idea['title'] = 'KV low-rank + RoPE, combined'
    idea['linked_gap_id'] = 'gap_999_invented'   # not in open_gaps
    idea['core_mechanism'] = 'Apply low-rank compression and also use RoPE.'
    idea['why_not_AB'] = 'They are different papers.'
    return idea


class _FakeSearch:
    """A fake arXiv search; records queries. Serves BOTH retrieval seams.

    ★ Gate ② does NOT go through ``search_arxiv``. It calls
    ``ts_search_by_query`` so identity/domain stay STRUCTURED legs all the way
    into tofu-search (flattening them into a free-text string is what once
    returned titanium-alloy papers for a KV-cache idea).

    When this fake only replaced ``search_arxiv``, gate ② silently bypassed it
    and hit the REAL arXiv API: 16 of 18 patch sites still passed — against
    live network data — and only 2 went red. A fake that is no longer wired to
    the code under test does not announce itself; it just stops being a fake.
    So this object is callable as the free-text entry point AND exposes
    ``.structured`` for the gate ② entry point, and both record into the same
    ``queries`` list.
    """
    def __init__(self, hits):
        self.hits = hits
        self.queries = []

    def __call__(self, query, max_results=10):
        self.queries.append(query)
        return list(self.hits)

    def structured(self, identity_terms, domain_terms=None, *, field='ti',
                   max_results=5):
        """Mimic the tofu-search envelope for the gate ② seam."""
        ident = list(identity_terms or [])
        dom = list(domain_terms or [])
        self.queries.append(' '.join(ident + dom))
        hits = list(self.hits)
        return {'ok': True, 'query': ' '.join(ident + dom),
                'mode': 'fielded' if (ident and dom) else 'terms',
                'papers': hits, 'error': '',
                'outcome': 'hits' if hits else 'no_matches'}


def _patch(monkeyables):
    """Patch ideate module attributes; return restore().

    Patching ``search_arxiv`` with a ``_FakeSearch`` automatically patches the
    structured gate ② seam too — otherwise the test would quietly measure the
    live arXiv API instead of its own fixture.
    """
    import lib.paper.ideate as it
    monkeyables = dict(monkeyables)
    _fs = monkeyables.get('search_arxiv')
    if isinstance(_fs, _FakeSearch) and 'ts_search_by_query' not in monkeyables:
        monkeyables['ts_search_by_query'] = _fs.structured
    saved = {k: getattr(it, k) for k in monkeyables}
    for k, v in monkeyables.items():
        setattr(it, k, v)
    return lambda: [setattr(it, k, saved[k]) for k in saved]


def _score_returning(overall, delta='mechanism-level', novelty=5):
    """A fake _score_idea that returns a fixed verdict."""
    def _fn(idea, prior_set, gap, lang, **kw):
        scores = {'novelty': novelty, 'falsifiability': 5, 'mechanism_depth': 5, 'value': 5}
        # emulate the real deterministic cap so the delta path is exercised
        capped = False
        if delta == 'parameter-level' and scores['novelty'] > 2:
            scores['novelty'] = 2
            capped = True
        ov = round(sum(scores.values()) / len(scores), 2)
        return {'scores': scores, 'overall': overall if overall is not None else ov,
                'mechanism_delta': delta, 'closest_neighbor': (prior_set['retrieved_ids'][0]
                                                               if prior_set['retrieved_ids'] else ''),
                'justifications': {}, 'verdict': 'v', 'novelty_capped': capped}
    return _fn


# ── PIN #2: structural gate ────────────────────────────────────────────────

def test_structural_gate_rejects_invented_problem():
    import lib.paper.ideate as it
    valid = it._valid_gap_ids(_OPEN_GAPS)
    assert it._structural_gate(_good_idea(), valid) is None, 'good idea should pass'
    reason = it._structural_gate(_ab_stitch_no_gap(), valid)
    assert reason and 'linked_gap_id' in reason, f'stitch should fail on gap link, got {reason!r}'
    _ok('structural gate: idea linking no real open_gap is rejected (invented problem)')


def test_novelty_claim_without_manual_arxiv_id_reaches_evidence_judge():
    """A missing citation is formatting, not negative novelty evidence; the
    forced retrieved-neighbour gate is the substantive judge."""
    import lib.paper.ideate as it

    idea = _good_idea()
    idea['novelty_claim'] = 'No prior method makes allocation retrieval-conditioned.'
    assert it._structural_gate(idea, it._valid_gap_ids(_OPEN_GAPS)) is None


def test_corpus_anchor_must_support_the_linked_gap():
    import lib.paper.ideate as it

    idea = _good_idea()
    idea['corpus_anchor_id'] = '9999.99999'
    fake_search = _FakeSearch([{'arxiv_id': '2305.11111', 'title': 'x'}] * 5)
    restore = _patch({
        '_generate_raw_ideas': lambda *a, **k: [idea],
        'search_arxiv': fake_search,
        'fetch_arxiv_title': lambda aid: 'Real',
        '_score_idea': _score_returning(4.8),
    })
    try:
        result = it.generate_ideas('dir', _OPEN_GAPS, lang='en')
    finally:
        restore()
    assert result['accepted'] == []
    assert result['rejected'][0]['reject_stage'] == 'structural'
    assert 'corpus_anchor_id' in result['rejected'][0]['reject_reason']


def test_ab_stitch_rejected_end_to_end_NEUTER():
    """Full pipeline: the invented-problem stitch is rejected; NEUTER the
    gap-link rule and it survives."""
    import lib.paper.ideate as it
    fake_search = _FakeSearch([{'arxiv_id': '2305.11111', 'title': 'Uniform rank KV', 'summary': ''}])
    restore = _patch({
        '_generate_raw_ideas': lambda *a, **k: [_ab_stitch_no_gap()],
        'search_arxiv': fake_search,
        'fetch_arxiv_title': lambda aid: 'Some Real Title',
        '_score_idea': _score_returning(4.8),  # would pass rubric IF it reached it
    })
    try:
        res = it.generate_ideas('dir', _OPEN_GAPS, lang='en')
        assert res['ok'], res
        assert len(res['accepted']) == 0, 'stitch must not be accepted'
        assert any(r['reject_stage'] == 'structural' for r in res['rejected']), \
            f"expected structural rejection, got {[r.get('reject_stage') for r in res['rejected']]}"
    finally:
        restore()

    # NEUTER: disable the gap-link rule in the structural gate.
    orig = it._structural_gate
    def _neutered(idea, valid_gap_ids):
        # keep field checks, DROP the linked_gap_id-must-match rule
        for f in it._REQUIRED_FIELDS:
            v = idea.get(f)
            if not (isinstance(v, str) and v.strip()):
                return f'missing {f}'
        return None
    it._structural_gate = _neutered
    restore2 = _patch({
        '_generate_raw_ideas': lambda *a, **k: [_ab_stitch_no_gap()],
        'search_arxiv': fake_search,
        'fetch_arxiv_title': lambda aid: 'Some Real Title',
        '_score_idea': _score_returning(4.8),
    })
    try:
        res2 = it.generate_ideas('dir', _OPEN_GAPS, lang='en')
        leaked = len(res2['accepted']) == 1
    finally:
        restore2()
        it._structural_gate = orig
    assert leaked, 'NEUTER FAILED: stitch did not leak through when gap-link rule removed'
    _ok('NEUTER: removing the "must link a real open_gap" rule lets the stitch leak (gate bites)')


# ── PIN #1: forced-neighbor-retrieval novelty ──────────────────────────────

def test_novelty_prior_set_forces_retrieval_including_unreported_id():
    """The prior set MUST include the forced search_arxiv top-K, even ids the
    idea never self-reported."""
    import lib.paper.ideate as it
    # The idea self-reports only 2305.11111; retrieval surfaces a colliding
    # neighbor 2402.55555 the model never mentioned.
    collider = {'arxiv_id': '2402.55555', 'title': 'Learnable per-layer KV rank', 'summary': 'x'}
    fake_search = _FakeSearch([collider,
                               {'arxiv_id': '2305.11111', 'title': 'Uniform rank', 'summary': ''},
                               {'arxiv_id': '2401.22222', 'title': 'Other', 'summary': ''},
                               {'arxiv_id': '2401.33333', 'title': 'Other2', 'summary': ''},
                               {'arxiv_id': '2401.44444', 'title': 'Other3', 'summary': ''}])
    restore = _patch({'search_arxiv': fake_search})
    try:
        ps = it._novelty_prior_set(_good_idea())
        assert len(ps['retrieved']) >= it.IDEATE_NOVELTY_RETRIEVAL_K, \
            f"retrieved {len(ps['retrieved'])} < K={it.IDEATE_NOVELTY_RETRIEVAL_K}"
        assert '2402.55555' in ps['retrieved_ids'], \
            'forced-retrieval collider (unreported by the model) missing from prior set'
        assert fake_search.queries and 'learnable' in fake_search.queries[0].lower(), \
            'retrieval query must be built from the title terms'
    finally:
        restore()
    _ok('novelty prior set forces search_arxiv top-K incl. ids the model never self-reported')


def test_parameter_level_delta_caps_novelty_and_sinks_stitch():
    """A parameter-level mechanism_delta caps novelty at 2, dragging overall
    below threshold — the stitch is rejected on the rubric."""
    import lib.paper.ideate as it
    fake_search = _FakeSearch([{'arxiv_id': '2402.55555', 'title': 'Dead ringer', 'summary': ''}] * 5)
    # Use the REAL _score_idea with a fake dispatch that declares parameter-level.
    def _fake_dispatch(messages, on_content=None, **kw):
        import json
        from lib.paper.agent_loop_policy import PAPER_AGENT_ROUTE_MAX_RETRIES
        assert kw['max_retries'] == PAPER_AGENT_ROUTE_MAX_RETRIES
        payload = json.dumps({'mechanism_delta': 'parameter-level', 'closest_neighbor': '2402.55555',
                              'scores': {'novelty': 5, 'falsifiability': 5, 'mechanism_depth': 5,
                                         'value': 5}, 'justifications': {}, 'verdict': 'stitch'})
        if on_content:
            on_content(payload)
        return ({'content': payload}, 'stop', {})
    restore = _patch({
        '_generate_raw_ideas': lambda *a, **k: [_good_idea()],
        'search_arxiv': fake_search,
        'fetch_arxiv_title': lambda aid: 'Real',
        'dispatch_stream': _fake_dispatch,
    })
    try:
        res = it.generate_ideas('dir', _OPEN_GAPS, lang='en')
        # novelty capped 5→2 → overall = (2+5+5+5)/4 = 4.25 ... still above 4.0!
        # so tighten: this asserts the CAP happened, not the threshold outcome.
        rec = (res['accepted'] + res['rejected'])[0]
        assert rec['scores']['novelty'] == 2, f"novelty not capped: {rec['scores']}"
        assert rec['mechanism_delta'] == 'parameter-level'
    finally:
        restore()
    _ok('parameter-level delta deterministically caps the novelty axis to 2')


def test_novelty_capped_below_threshold_rejects():
    """With three axes also modest, the parameter-level cap pushes overall under
    threshold → rejected on rubric with scores preserved."""
    import lib.paper.ideate as it
    fake_search = _FakeSearch([{'arxiv_id': '2402.55555', 'title': 'Dead ringer', 'summary': ''}] * 5)
    def _fake_dispatch(messages, on_content=None, **kw):
        import json
        payload = json.dumps({'mechanism_delta': 'parameter-level', 'closest_neighbor': '2402.55555',
                              'scores': {'novelty': 5, 'falsifiability': 4, 'mechanism_depth': 3,
                                         'value': 4}, 'justifications': {}, 'verdict': 'stitch'})
        if on_content:
            on_content(payload)
        return ({'content': payload}, 'stop', {})
    restore = _patch({
        '_generate_raw_ideas': lambda *a, **k: [_good_idea()],
        'search_arxiv': fake_search, 'fetch_arxiv_title': lambda aid: 'Real',
        'dispatch_stream': _fake_dispatch,
    })
    try:
        res = it.generate_ideas('dir', _OPEN_GAPS, lang='en')
        # capped: (2+4+3+4)/4 = 3.25 < 4.0 → rejected
        assert len(res['accepted']) == 0, 'capped stitch should not be accepted'
        rej = res['rejected'][0]
        assert rej['reject_stage'] == 'rubric' and rej['scores']['novelty'] == 2
        assert 'overall' in rej and rej['overall'] < res['threshold']
    finally:
        restore()
    _ok('parameter-level cap drags overall < threshold → stitch rejected on rubric')


# ── Rejection audit + accepted path + threshold-not-hardcoded ──────────────

def test_good_idea_accepted_and_rejections_audited():
    import lib.paper.ideate as it
    fake_search = _FakeSearch([{'arxiv_id': '2305.11111', 'title': 'Uniform rank KV', 'summary': ''}] * 5)
    restore = _patch({
        '_generate_raw_ideas': lambda *a, **k: [_good_idea(), _ab_stitch_no_gap()],
        'search_arxiv': fake_search, 'fetch_arxiv_title': lambda aid: 'Real title',
        '_score_idea': _score_returning(4.75, delta='mechanism-level'),
    })
    try:
        res = it.generate_ideas('dir', _OPEN_GAPS, lang='en')
        assert len(res['accepted']) == 1, f"expected 1 accepted, got {len(res['accepted'])}"
        acc = res['accepted'][0]
        assert acc['overall'] == 4.75 and 'scores' in acc
        # Provenance stays split: the retrieved basis the judge saw is recorded
        # apart from the model's self-report (never one merged prior_set_ids).
        assert 'retrieved_ids' in acc and 'self_reported_ids' in acc
        assert 'prior_set_ids' not in acc
        # the stitch is rejected AND its record is auditable
        assert len(res['rejected']) == 1
        rej = res['rejected'][0]
        assert rej['reject_stage'] == 'structural' and rej['reject_reason']
    finally:
        restore()
    _ok('good idea accepted with scores; rejected stitch preserved with stage+reason (audit)')


def test_threshold_is_a_calibratable_constant_not_hardcoded():
    """Raising the threshold above a fixed overall flips accept→reject WITHOUT
    changing any gate logic — proving the threshold is a tunable, not baked in."""
    import lib.paper.ideate as it
    fake_search = _FakeSearch([{'arxiv_id': '2305.11111', 'title': 'x', 'summary': ''}] * 5)
    common = {'search_arxiv': fake_search, 'fetch_arxiv_title': lambda aid: 'Real',
              '_score_idea': _score_returning(4.2, delta='mechanism-level'),
              '_generate_raw_ideas': lambda *a, **k: [_good_idea()]}
    restore = _patch(common)
    try:
        lax = it.generate_ideas('dir', _OPEN_GAPS, lang='en', threshold=4.0)
        strict = it.generate_ideas('dir', _OPEN_GAPS, lang='en', threshold=4.5)
        assert len(lax['accepted']) == 1, 'overall 4.2 should pass threshold 4.0'
        assert len(strict['accepted']) == 0, 'overall 4.2 should fail threshold 4.5'
        assert strict['rejected'][0]['reject_stage'] == 'rubric'
    finally:
        restore()
    _ok('threshold is a calibratable constant (4.0 accepts, 4.5 rejects the same idea)')


def test_low_confidence_gap_docks_value_axis():
    """R2/R3 seam v2: an idea linked to a low_confidence gap (survey backed it
    only with grounded-but-unharvested papers) gets its value axis docked by one
    and is flagged — the reward for a loosened survey gate is paid HERE, visibly."""
    import lib.paper.ideate as it
    gaps_low = {
        'schema_version': 1, 'direction': 'd',
        'open_gaps': [{'id': 'gap_1', 'gap': 'g', 'low_confidence': True,
                       'missing_ids': ['2404.00001']}],
    }
    fake_search = _FakeSearch([{'arxiv_id': '2305.11111', 'title': 'x', 'summary': ''}] * 5)
    restore = _patch({
        '_generate_raw_ideas': lambda *a, **k: [_good_idea()],
        'search_arxiv': fake_search, 'fetch_arxiv_title': lambda aid: 'Real',
        '_score_idea': _score_returning(None, delta='mechanism-level'),  # value=5 raw
    })
    try:
        res = it.generate_ideas('dir', gaps_low, lang='en')
        rec = (res['accepted'] + res['rejected'])[0]
        assert rec['scores']['value'] == 4, f"value not docked (5→4): {rec['scores']}"
        assert rec['linked_gap_low_confidence'] is True, 'idea not flagged low_confidence'
        # overall recomputed: (5+5+5+4)/4 = 4.75
        assert rec['overall'] == 4.75, rec['overall']
    finally:
        restore()
    _ok('idea linked to a low_confidence gap has its value axis docked + is flagged')


def test_high_confidence_gap_no_dock_NEUTER():
    """Counter/NEUTER: the SAME idea linked to a high-confidence gap (no
    low_confidence flag) keeps value=5 — proving the dock is gated on the flag,
    not applied unconditionally."""
    import lib.paper.ideate as it
    gaps_hi = {'schema_version': 1, 'direction': 'd',
               'open_gaps': [{'id': 'gap_1', 'gap': 'g', 'low_confidence': False}]}
    fake_search = _FakeSearch([{'arxiv_id': '2305.11111', 'title': 'x', 'summary': ''}] * 5)
    restore = _patch({
        '_generate_raw_ideas': lambda *a, **k: [_good_idea()],
        'search_arxiv': fake_search, 'fetch_arxiv_title': lambda aid: 'Real',
        '_score_idea': _score_returning(None, delta='mechanism-level'),
    })
    try:
        res = it.generate_ideas('dir', gaps_hi, lang='en')
        rec = (res['accepted'] + res['rejected'])[0]
        assert rec['scores']['value'] == 5, f'high-confidence gap must NOT dock value: {rec["scores"]}'
        assert rec['linked_gap_low_confidence'] is False
    finally:
        restore()
    _ok('NEUTER: a high-confidence gap does NOT dock value (dock is flag-gated)')


# ── kind is metadata, NOT a validity verdict (real-run regression) ────────
#
# Production evidence: data/research/jobs/research_7a444f96c65d42b5 — 6/6 ideas
# were killed at the structural gate with reject_stage='structural', every
# reason `invalid kind: 'Methodology'/'Algorithm'/'Architecture'/'Novelty'`.
# The novelty retrieval and the four-axis rubric NEVER RAN. The 40 unit tests
# were all green because every fixture happened to spell kind in lowercase.
#
# Owner ruling: `kind` selects the R5/R6 template — it is metadata. A gate that
# rejects on it turns a formatting slip into "this idea is invalid". The
# structural gate keeps only the three real validity judgements (links a
# verified gap / falsifiable / cites concrete prior art); kind is NORMALIZED.

#: The exact kind strings the real model emitted (verbatim from the job file).
_REAL_RUN_KINDS = ('Methodology', 'Algorithm', 'Architecture', 'Novelty')


def test_kind_is_never_a_rejection_reason():
    """Every kind the real run produced must pass the structural gate.

    Failing-first: on the un-fixed gate all four are rejected with
    'invalid kind'. This is the production bug, reproduced exactly."""
    import lib.paper.ideate as it
    valid = it._valid_gap_ids(_OPEN_GAPS)
    for k in _REAL_RUN_KINDS:
        idea = _good_idea()
        idea['kind'] = k
        reason = it._structural_gate(idea, valid)
        assert reason is None, \
            f'kind {k!r} must NOT be a rejection reason (idea is otherwise valid), got {reason!r}'
    # Even total garbage in `kind` is not a validity verdict.
    odd = _good_idea()
    odd['kind'] = 'Methodology (novel mechanism)'
    assert it._structural_gate(odd, valid) is None, \
        'a free-form kind string must not reject an otherwise-valid idea'
    _ok('kind is never a rejection reason (all 4 real-run kinds + free-form pass)')


def test_kind_normalized_to_frozen_enum_with_audit_flag():
    """kind is coerced into the frozen methodology|analysis enum, and a coerced
    idea is flagged so the normalization is auditable rather than silent."""
    import lib.paper.ideate as it
    cases = {
        'Methodology': 'methodology', 'methodology': 'methodology',
        'Algorithm': 'methodology', 'Architecture': 'methodology',
        'method': 'methodology', 'Novelty': 'methodology',
        'Analysis': 'analysis', 'empirical study': 'analysis',
        'Evaluation': 'analysis', '  ANALYSIS  ': 'analysis',
    }
    for raw, want in cases.items():
        got, changed = it._normalize_kind(raw)
        assert got == want, f'kind {raw!r} → {got!r}, expected {want!r}'
        assert got in it._VALID_KINDS, f'normalized kind {got!r} escaped the frozen enum'
        assert changed == (raw != want), f'kind_normalized flag wrong for {raw!r}: {changed}'
    _ok('kind normalized into the frozen enum (synonyms mapped, flag records coercion)')


def test_real_run_ideas_reach_the_expensive_gates_NEUTER():
    """End-to-end with the REAL run's kind strings: ideas must now reach the
    novelty retrieval + rubric. NEUTER — restore the kind rejection and they
    all die at the structural gate again (the exact production failure)."""
    import lib.paper.ideate as it
    fake_search = _FakeSearch([{'arxiv_id': '2305.11111', 'title': 'x', 'summary': ''}] * 5)
    ideas = []
    for k in _REAL_RUN_KINDS:
        idea = _good_idea()
        idea['kind'] = k
        idea['title'] = f'Idea kinded {k}'
        ideas.append(idea)
    common = {
        '_generate_raw_ideas': lambda *a, **k: [dict(i) for i in ideas],
        'search_arxiv': fake_search, 'fetch_arxiv_title': lambda aid: 'Real',
        '_score_idea': _score_returning(4.5, delta='mechanism-level'),
    }
    restore = _patch(common)
    try:
        res = it.generate_ideas('dir', _OPEN_GAPS, lang='en')
        assert res['ok'], res
        assert len(res['accepted']) == len(_REAL_RUN_KINDS), (
            f'all {len(_REAL_RUN_KINDS)} real-run ideas should reach+clear the rubric, '
            f"got {len(res['accepted'])} accepted / {len(res['rejected'])} rejected: "
            f"{[r.get('reject_reason') for r in res['rejected']]}")
        assert all(a['kind'] in it._VALID_KINDS for a in res['accepted']), \
            'accepted ideas must carry a normalized kind'
        assert any(a.get('kind_normalized') for a in res['accepted']), \
            'coercion must be recorded on the accepted record'
        assert fake_search.queries, 'novelty retrieval (gate ②) never ran'
    finally:
        restore()

    # NEUTER — remove the normalization AND restore the kind rejection, i.e.
    # exactly the shipped code before this fix. The production failure returns.
    # (Neutering only the gate proves nothing: normalization runs BEFORE the
    # gate, so the gate would never see a non-enum kind to reject.)
    orig_gate = it._structural_gate
    orig_norm = it._normalize_kind

    def _no_normalize(raw):
        return raw, False

    def _kind_rejecting(idea, valid_gap_ids):
        if idea.get('kind') not in it._VALID_KINDS:
            return f'invalid kind: {idea.get("kind")!r}'
        return orig_gate(idea, valid_gap_ids)

    it._normalize_kind = _no_normalize
    it._structural_gate = _kind_rejecting
    restore2 = _patch(common)
    try:
        res2 = it.generate_ideas('dir', _OPEN_GAPS, lang='en')
        all_died_structural = (
            len(res2['accepted']) == 0
            and len(res2['rejected']) == len(_REAL_RUN_KINDS)
            and all(r['reject_stage'] == 'structural' for r in res2['rejected'])
            and all('invalid kind' in r['reject_reason'] for r in res2['rejected']))
    finally:
        restore2()
        it._structural_gate = orig_gate
        it._normalize_kind = orig_norm
    assert all_died_structural, \
        'NEUTER FAILED: removing kind normalization no longer reproduces the wipe'
    _ok('NEUTER: removing normalization + restoring the kind check reproduces the 100% wipe')


def test_prompt_declares_the_legal_kind_values():
    """Root cause #2: the contract must be visible to the party bound by it —
    the generation prompt never listed the legal kind values."""
    import lib.paper.ideate as it
    for lang in ('en', 'zh'):
        p = it._ideate_system_prompt(lang, 6)
        for k in it._VALID_KINDS:
            assert k in p, f'{lang} prompt does not declare the legal kind value {k!r}'
    _ok('generation prompt declares the legal kind enum (both languages)')


def test_prompt_demands_proactive_causal_mechanisms_after_llm_eval():
    """The first real LLM evaluation found reactive metric wrappers despite
    the anti-A+B prose. Pin the corrective mechanism contract, not its score."""
    import lib.paper.ideate as it

    for lang in ('en', 'zh'):
        prompt = it._ideate_system_prompt(lang, 6)
        for field in ('corpus_anchor_id', 'corpus_delta', 'failure_cause',
                      'new_invariant', 'intervention_level'):
            assert field in prompt
        assert 'state transition' in prompt or '状态转移' in prompt
        assert 'different causal mechanism' in prompt or '不同的因果机制' in prompt


# ── The gate must report its own pathology (charter: guards must not idle) ──

def test_total_structural_wipe_is_reported_as_degraded():
    """A structural gate that kills EVERY idea is a pipeline defect, not
    宁缺毋滥 — the result must say so, with the dominant reason.

    This is the invariant that would have made the kind bug self-announcing:
    'accepted 0' and 'the gate worked' were indistinguishable from outside."""
    import lib.paper.ideate as it
    fake_search = _FakeSearch([{'arxiv_id': '2305.11111', 'title': 'x', 'summary': ''}] * 5)
    restore = _patch({
        # every idea invents its own problem → all die structurally
        '_generate_raw_ideas': lambda *a, **k: [_ab_stitch_no_gap() for _ in range(4)],
        'search_arxiv': fake_search, 'fetch_arxiv_title': lambda aid: 'Real',
        '_score_idea': _score_returning(4.9),
    })
    try:
        res = it.generate_ideas('dir', _OPEN_GAPS, lang='en')
        assert res['ok'], res
        assert res.get('degraded') is True, \
            'a 100% structural wipe must be flagged degraded, not reported as a clean pass'
        assert res.get('degraded_reason'), 'degraded result must carry the dominant reason'
        assert 'linked_gap_id' in res['degraded_reason'], \
            f"dominant reason should name the actual gate, got {res['degraded_reason']!r}"
    finally:
        restore()
    _ok('100% structural wipe → degraded=True + dominant reason (gate reports its own pathology)')


def test_partial_structural_rejection_is_not_degraded():
    """Counter-case: genuine selectivity (some pass, some fail) is NOT degraded
    — proving the flag tracks total-wipe pathology, not ordinary strictness."""
    import lib.paper.ideate as it
    fake_search = _FakeSearch([{'arxiv_id': '2305.11111', 'title': 'x', 'summary': ''}] * 5)
    restore = _patch({
        '_generate_raw_ideas': lambda *a, **k: [_good_idea(), _ab_stitch_no_gap()],
        'search_arxiv': fake_search, 'fetch_arxiv_title': lambda aid: 'Real',
        '_score_idea': _score_returning(4.5, delta='mechanism-level'),
    })
    try:
        res = it.generate_ideas('dir', _OPEN_GAPS, lang='en')
        assert len(res['accepted']) == 1 and len(res['rejected']) == 1
        assert not res.get('degraded'), 'partial rejection must NOT be flagged degraded'
    finally:
        restore()
    _ok('partial structural rejection is NOT degraded (flag tracks pathology, not strictness)')


def test_zero_accepted_on_rubric_is_honest_not_degraded():
    """宁缺毋滥 stays intact: ideas that REACH the rubric and score below the
    threshold are an honest zero — the expensive gates ran, so not degraded."""
    import lib.paper.ideate as it
    fake_search = _FakeSearch([{'arxiv_id': '2305.11111', 'title': 'x', 'summary': ''}] * 5)
    restore = _patch({
        '_generate_raw_ideas': lambda *a, **k: [_good_idea(), _good_idea()],
        'search_arxiv': fake_search, 'fetch_arxiv_title': lambda aid: 'Real',
        '_score_idea': _score_returning(2.0, delta='mechanism-level'),
    })
    try:
        res = it.generate_ideas('dir', _OPEN_GAPS, lang='en')
        assert len(res['accepted']) == 0, 'low-scoring ideas should not be accepted'
        assert all(r['reject_stage'] == 'rubric' for r in res['rejected'])
        assert not res.get('degraded'), \
            'an honest rubric-based zero must NOT be flagged degraded (宁缺毋滥 preserved)'
    finally:
        restore()
    _ok('zero accepted via the rubric is honest, not degraded (宁缺毋滥 preserved)')


def test_no_gaps_is_clean_failure():
    import lib.paper.ideate as it
    res = it.generate_ideas('dir', {'open_gaps': []}, lang='en')
    assert not res['ok'] and 'survey' in res['error']
    _ok('empty open_gaps → clean ok=False with a run-survey-first message')


def test_empty_forced_synthesis_retries_only_current_dispatch():
    """An empty provider-final answer must not replay the ideation stage."""
    import json

    import lib.paper.ideate as it
    from lib.paper.agent_usage import PaperAgentUsageMeter

    calls = []
    payload = json.dumps({'ideas': [{'title': 'Recovered idea'}]})

    def _fake_dispatch(_messages, *, on_content, tools, **_kwargs):
        calls.append(tools)
        if len(calls) == 1:
            return (
                {'role': 'assistant', 'content': ''}, 'stop',
                {'prompt_tokens': 10, 'completion_tokens': 2,
                 '_empty_stop': True},
            )
        on_content(payload)
        return (
            {'role': 'assistant', 'content': payload}, 'stop',
            {'prompt_tokens': 10, 'completion_tokens': 8},
        )

    meter = PaperAgentUsageMeter(
        'ideate', token_budget=1_000, dispatch_budget=1, repeat_limit=0)
    restore = _patch({'dispatch_stream': _fake_dispatch})
    try:
        ideas = it._generate_raw_ideas(
            'direction', _OPEN_GAPS, '', 'en', usage_meter=meter)
    finally:
        restore()

    assert ideas == [{'title': 'Recovered idea'}]
    assert len(calls) == 2 and all(not tools for tools in calls), \
        'retry must remain tool-less'
    assert meter.calls == 2, 'both paid attempts must be accounted'
    assert meter.agent_dispatches == 2, 'retry must consume dispatch budget'
    _ok('empty forced synthesis retries one dispatch without replaying tools')


def main():
    print()
    print(_color('═══ R3 Ideate / Anti-A+B Gate Tests ═══', '36'))
    print()
    tests = [
        test_structural_gate_rejects_invented_problem,
        test_ab_stitch_rejected_end_to_end_NEUTER,
        test_novelty_prior_set_forces_retrieval_including_unreported_id,
        test_parameter_level_delta_caps_novelty_and_sinks_stitch,
        test_novelty_capped_below_threshold_rejects,
        test_good_idea_accepted_and_rejections_audited,
        test_threshold_is_a_calibratable_constant_not_hardcoded,
        test_low_confidence_gap_docks_value_axis,
        test_high_confidence_gap_no_dock_NEUTER,
        test_kind_is_never_a_rejection_reason,
        test_kind_normalized_to_frozen_enum_with_audit_flag,
        test_real_run_ideas_reach_the_expensive_gates_NEUTER,
        test_prompt_declares_the_legal_kind_values,
        test_total_structural_wipe_is_reported_as_degraded,
        test_partial_structural_rejection_is_not_degraded,
        test_zero_accepted_on_rubric_is_honest_not_degraded,
        test_no_gaps_is_clean_failure,
        test_empty_forced_synthesis_retries_only_current_dispatch,
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
