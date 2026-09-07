#!/usr/bin/env python3
"""R3 gate ② — the novelty prior set must actually retrieve NEIGHBOURS.

Owner pin #1 is "novelty = f(retrieved set), not f(model self-report)". That
only holds if the retrieval genuinely finds papers in the idea's own field.
Production evidence (research_7a444f96c65d42b5, 6 real ideas) says it did not:

  * `_novelty_prior_set` sent `title + the whole core_mechanism` — 473-558 chars
    of prose, complete with `*asterisks*`, commas and parentheses — as arXiv's
    `all:` query. 25 neighbours came back and **not one** was about KV caches,
    attention, LLMs or compression: they were gravitational-wave catalogues,
    neutrino searches and a `B⁰ₛ→μ⁺μ⁻` measurement.
  * The SAME papers (GWTC-5.0, "Deep Search for Joint Sources…") appeared in
    5 of the 6 ideas' neighbour sets — a long prose query degrades `all:` into
    near-unconstrained matching that keeps returning the most-cited
    collaboration papers. The judge compared every mechanism against the same
    handful of astrophysics papers, so the novelty axis was a CONSTANT.
  * One idea's `*difference*` tripped arXiv's parser into HTTP 500, which
    `search_arxiv` swallows into `[]`; the judge prompt then just says
    "(retrieval returned nothing)" and scores the idea anyway — pin #1 silently
    switched off while the verdict still counted.

The existing ideate suite stayed green through all of this because it injects
neighbours via `_FakeSearch` and therefore never exercised query construction.
These tests close that hole: they drive the SHIPPED code with the REAL ideas.

Corpus discipline (charter: never hand-copy production data into a harness):
the ideas are read from the real job's ``pipeline_state.json`` at run time. If
that file is gone the tests SKIP loudly rather than silently passing on a
fabricated substitute.

Network discipline: the relevance/overlap tests hit the real arXiv API and are
marked ``slow``; the sanitizer and empty-basis tests are pure and always run.

Run standalone:  python tests/test_paper_ideate_retrieval.py
Under pytest:    pytest tests/test_paper_ideate_retrieval.py -m unit
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


try:
    import pytest
    # ★ `slow`, NOT `unit`: three tests in this file hit the real arXiv API.
    # Marking them `unit` put live network calls into `make test-unit` and CI,
    # where a rate-limit burst or a flaky link turns them red for reasons that
    # have nothing to do with the code under test. A guard that goes red at
    # random is indistinguishable from noise, and noise gets ignored — which is
    # precisely how a real defect slips past a "failing" suite.
    pytestmark = [pytest.mark.slow]
except ImportError:
    pytest = None


# ── The real corpus (read at run time, never copied into this file) ─────────

#: The production job whose ideate pass exposed the defect.
_REAL_JOB = 'research_7a444f96c65d42b5'


def _real_ideas():
    """The 6 real ideas from the production job.

    Read from the TRACKED fixture ``tests/fixtures/r3_real_ideas.json``, which
    is a verbatim extract of the production job's ideate artifact.

    ★ Why vendored rather than read from ``data/``: the live job dir is
    untracked, so in a clean checkout (CI, a fresh worktree, any sibling's
    machine) every test here SKIPPED — all 8 of them, silently, reporting
    green. A guard that does not run is the charter's "守卫绿着空转" failure,
    which is the very family this epic exists to close; shipping the fix with
    a guard that only runs on one machine would reproduce it.

    This is NOT the banned "hand-copy production data into the harness": the
    fixture is machine-extracted, carries its source job id, and is the frozen
    EVIDENCE of a specific measured defect — it must not drift with the live
    job dir, or the regression it pins stops being reproducible.

    Falls back to the live job dir when the fixture is absent.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fixture = os.path.join(repo, 'tests', 'fixtures', 'r3_real_ideas.json')
    if os.path.exists(fixture):
        try:
            with open(fixture, encoding='utf-8') as f:
                ideas = json.load(f).get('ideas') or []
            return [i for i in ideas
                    if isinstance(i, dict) and i.get('core_mechanism')]
        except Exception as e:                                # pragma: no cover
            print(f'   (fixture unreadable: {e})')
    path = os.path.join(repo, 'data', 'research', 'jobs', _REAL_JOB,
                        'pipeline_state.json')
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            state = json.load(f)
        art = (state.get('stages') or {}).get('ideate', {}).get('artifact') or {}
    except Exception as e:                                    # pragma: no cover
        print(f'   (corpus unreadable: {e})')
        return []
    ideas = list(art.get('accepted') or []) + list(art.get('rejected') or [])
    return [i for i in ideas if isinstance(i, dict) and i.get('core_mechanism')]


def _require_corpus():
    """The real ideas. A missing corpus FAILS — it never silently skips.

    The fixture is tracked, so its absence means someone deleted the evidence
    this guard is built on, not that the environment is merely unlucky.
    """
    ideas = _real_ideas()
    if not ideas:
        msg = ('real-idea corpus missing (tests/fixtures/r3_real_ideas.json) — '
               'this guard cannot verify retrieval without it, and skipping '
               'would report green while checking nothing')
        if pytest is not None:
            pytest.fail(msg)
        print('   (FAIL)', msg)
        raise AssertionError(msg)
    return ideas


#: Vocabulary of the corpus's field (long-context KV-cache compression). A
#: neighbour whose title contains none of these is off-topic for every one of
#: the 6 ideas.
_FIELD_WORDS = re.compile(
    r'KV|cache|caching|attention|transformer|LLM|language model|token|'
    r'quantiz|compress|context|inference|decoding|memory|retriev|embedding|'
    r'prompt|sequence|sparsit|prun', re.I)

#: Characters that must never survive into a search query — they are what turned
#: one real mechanism into an arXiv HTTP 500.
_FORBIDDEN_IN_QUERY = ('*', '(', ')', ',', '\n', '"')

#: A query longer than this is prose, not a term list.
_MAX_QUERY_CHARS = 160

#: At least this share of retrieved neighbours must be on-topic. Deliberately
#: lenient — production scored 0.0, so anything that actually searches the right
#: field clears it comfortably.
_MIN_FIELD_HIT_RATE = 0.5

#: Two different ideas may share some neighbours (they are in one field), but
#: near-identical sets mean the query is not discriminating between them.
_MAX_PAIRWISE_OVERLAP = 0.6



# ── Retrieval seam helpers ─────────────────────────────────────────
#
# Gate ② retrieves through the STRUCTURED seam (`ts_search_by_query`), which
# takes identity/domain LEGS and returns an envelope. It deliberately does NOT
# use the free-text `search_arxiv` — routing built syntax through that path is
# what produced the titanium-alloy regression, and these harnesses must drive
# the seam production actually uses.

def _envelope(papers, outcome=None, query='(ti:a OR ti:b) AND all:"d"'):
    return {'ok': True, 'papers': list(papers), 'query': query, 'mode': 'fielded',
            'outcome': outcome or ('hits' if papers else 'no_matches'), 'error': ''}


def _patch_retrieval(it, fn):
    """Patch the structured retrieval seam; returns a restore callable."""
    saved = it.ts_search_by_query
    it.ts_search_by_query = fn
    return lambda: setattr(it, 'ts_search_by_query', saved)


def _hit_rate(neighbours):
    if not neighbours:
        return 0.0
    hits = sum(1 for n in neighbours if _FIELD_WORDS.search(n.get('title') or ''))
    return hits / len(neighbours)


# ── Test 1: the query sent to the API is a term list, never prose ──────────

def test_retrieval_query_is_sanitized_never_raw_prose():
    """No idea may put prose (or `*`, brackets, commas) on the wire.

    Failing-first: the shipped code sends `title + core_mechanism` verbatim, so
    every real idea produces a 400+ char query full of forbidden characters."""
    ideas = _require_corpus()
    if not ideas:
        return
    import lib.paper.ideate as it

    captured = []

    def _capture(identity, domain=None, *, field='ti', max_results=5):
        # Legs are what production sends now; join them to assert the same
        # "no prose, no parser-breaking characters" property on the payload.
        captured.append(' '.join(list(identity or []) + list(domain or [])))
        return _envelope([])

    restore = _patch_retrieval(it, _capture)
    try:
        for idea in ideas:
            it._novelty_prior_set(idea, k=5)
    finally:
        restore()

    assert len(captured) == len(ideas), \
        f'expected one retrieval per idea, got {len(captured)} for {len(ideas)}'
    for q, idea in zip(captured, ideas):
        assert len(q) <= _MAX_QUERY_CHARS, (
            f'query for {idea.get("title","?")[:40]!r} is {len(q)} chars '
            f'(> {_MAX_QUERY_CHARS}) — that is prose, not search terms: {q[:120]!r}')
        for ch in _FORBIDDEN_IN_QUERY:
            assert ch not in q, (
                f'query contains {ch!r}, which breaks the arXiv parser '
                f'(one real mechanism\'s `*` caused HTTP 500): {q[:120]!r}')
        assert q.strip(), 'query must not be empty'
    _ok(f'all {len(ideas)} real ideas produce sanitized term-list queries '
        f'(<= {_MAX_QUERY_CHARS} chars, no {"".join(_FORBIDDEN_IN_QUERY[:3])})')


# ── Test 2: an empty basis is reported, never silently scored ──────────────

def test_empty_neighbour_set_marks_novelty_basis_none():
    """When retrieval yields nothing, the prior set must SAY so.

    pin #1 cannot hold on an empty basis; the pass must be able to tell that it
    could not judge, instead of scoring against '(retrieval returned nothing)'.
    Failing-first: the shipped prior set carries no such marker at all."""
    ideas = _require_corpus()
    if not ideas:
        return
    import lib.paper.ideate as it

    restore = _patch_retrieval(
        it, lambda i, d=None, *, field='ti', max_results=5: _envelope([]))
    try:
        ps = it._novelty_prior_set(ideas[0], k=5)
    finally:
        restore()

    assert ps.get('novelty_basis') == 'none', (
        "an empty retrieval must set novelty_basis='none' so the judge's "
        f'verdict can be discounted; got {ps.get("novelty_basis")!r}')
    _ok("empty retrieval marks novelty_basis='none' (pin #1 failure is visible)")


def test_idea_with_unjudgeable_novelty_cannot_be_accepted():
    """An idea whose novelty basis is empty must never reach `accepted`.

    宁可判不了,不许假装判过 — a high rubric score on an empty basis is not
    evidence of novelty. Failing-first: today such an idea is accepted on the
    judge's score alone."""
    ideas = _require_corpus()
    if not ideas:
        return
    import lib.paper.ideate as it

    gaps = {'schema_version': 1, 'open_gaps': [
        {'id': 'gap_1', 'gap': 'g', 'why_open': 'w', 'kind_hint': 'methodology',
         'evidence': ['2305.11111']}]}
    idea = dict(ideas[0])
    idea['linked_gap_id'] = 'gap_1'
    idea['corpus_anchor_id'] = '2305.11111'
    idea['corpus_delta'] = 'Change the anchor mechanism at representation level.'
    idea.setdefault('novelty_claim', 'Unlike arXiv:2305.11111 this is new.')
    idea.setdefault('falsifiable_prediction', 'Recall stays >90% at 4x.')
    idea.setdefault('why_not_AB', 'The mechanism is new, not glued.')

    def _verdict(idea_, prior_set, gap, lang, **kw):
        return {'scores': {'novelty': 5, 'falsifiability': 5,
                           'mechanism_depth': 5, 'value': 5},
                'overall': 5.0, 'mechanism_delta': 'mechanism-level',
                'closest_neighbor': '', 'justifications': {}, 'verdict': 'v',
                'novelty_capped': False}

    saved = {k: getattr(it, k) for k in
             ('_generate_raw_ideas', 'ts_search_by_query', 'fetch_arxiv_title', '_score_idea')}
    it._generate_raw_ideas = lambda *a, **k: [dict(idea)]
    it.ts_search_by_query = (lambda i, d=None, *, field='ti', max_results=5:
                            _envelope([]))          # empty basis
    it.fetch_arxiv_title = lambda aid: 'Real Title'
    it._score_idea = _verdict                                # would score 5.0
    try:
        res = it.generate_ideas('long-context KV cache compression', gaps, lang='en')
    finally:
        for k, v in saved.items():
            setattr(it, k, v)

    assert len(res['accepted']) == 0, (
        'an idea judged against an EMPTY neighbour set must not be accepted '
        f'even with a perfect rubric score; accepted={len(res["accepted"])}')
    assert res['rejected'], 'the unjudgeable idea must be recorded, not dropped'
    _ok('idea with an empty novelty basis cannot be accepted (pin #1 enforced)')



def test_audit_fields_keep_retrieved_and_self_reported_separate():
    """Provenance ratchet: NO record may carry a merged prior-art field.

    Production shipped ``prior_set_ids = retrieved + self_reported`` as one flat
    list. That union was what landed in the audit record while the judge only
    ever saw the RETRIEVED papers — with one real idea self-reporting 257 ids
    against 5 retrieved, the record claimed a 41-paper novelty basis for a
    5-paper judgement. Two different provenances with two different
    trustworthiness levels must never be squashed into one field, or a later
    reader cannot tell what novelty was actually measured against.

    Asserts the RESULT shape (charter: behaviour guards assert results), so it
    keeps biting if the fields are ever re-merged under any new name.
    """
    ideas = _require_corpus()
    if not ideas:
        return
    import lib.paper.ideate as it

    gaps = {'schema_version': 1, 'open_gaps': [
        {'id': 'gap_1', 'gap': 'g', 'why_open': 'w', 'kind_hint': 'methodology',
         'evidence': ['2305.11111']}]}
    idea = dict(ideas[0])
    idea['linked_gap_id'] = 'gap_1'
    idea['corpus_anchor_id'] = '2305.11111'
    idea['corpus_delta'] = 'Change the anchor mechanism at representation level.'
    idea['prior_art'] = ['2305.11111', '2306.22222']
    idea.setdefault('novelty_claim', 'Unlike arXiv:2305.11111 this is new.')
    idea.setdefault('falsifiable_prediction', 'Recall stays >90% at 4x.')
    idea.setdefault('why_not_AB', 'The mechanism is new, not glued.')

    def _verdict(idea_, prior_set, gap, lang, **kw):
        return {'scores': {'novelty': 5, 'falsifiability': 5,
                           'mechanism_depth': 5, 'value': 5},
                'overall': 5.0, 'mechanism_delta': 'mechanism-level',
                'closest_neighbor': '', 'justifications': {}, 'verdict': 'v',
                'novelty_capped': False}

    saved = {k: getattr(it, k) for k in
             ('_generate_raw_ideas', 'ts_search_by_query', 'fetch_arxiv_title', '_score_idea')}
    it._generate_raw_ideas = lambda *a, **k: [dict(idea)]
    it.ts_search_by_query = lambda i, d=None, *, field='ti', max_results=5: _envelope([
        {'arxiv_id': '2401.00001', 'title': 'KV cache compression', 'summary': 's'},
        {'arxiv_id': '2401.00002', 'title': 'Attention sparsity', 'summary': 's'},
    ])
    it.fetch_arxiv_title = lambda aid: 'Real Title'
    it._score_idea = _verdict
    try:
        res = it.generate_ideas('long-context KV cache compression', gaps, lang='en')
    finally:
        for k, v in saved.items():
            setattr(it, k, v)

    records = list(res['accepted']) + list(res['rejected'])
    assert records, 'expected at least one judged record to inspect'
    for rec in records:
        assert 'prior_set_ids' not in rec, (
            "'prior_set_ids' merged the retrieved basis with the model's "
            'self-report into one flat list — the audit then overstates what '
            'novelty was judged against. Keep the two provenances in separate '
            'fields.')
        assert 'retrieved_ids' in rec, (
            "every judged record must record the RETRIEVED basis it was judged "
            "against ('retrieved_ids')")
        assert 'self_reported_ids' in rec, (
            "every judged record must record the model's self-reported prior "
            "art separately ('self_reported_ids')")
        # And the two must genuinely be distinct sets, not aliases of a union.
        assert set(rec['retrieved_ids']).isdisjoint(rec['self_reported_ids']), (
            'retrieved_ids and self_reported_ids overlap — the two provenances '
            'are aliased to one union under separate names')
        assert '2305.11111' not in rec['retrieved_ids'], (
            'a self-reported id leaked into retrieved_ids — the fields are '
            'separate in name only')
    _ok(f'{len(records)} judged record(s) keep retrieved/self-reported '
        'provenance separate (no merged prior_set_ids)')


# ── Test 3: retrieval is on-topic AND discriminates between ideas ──────────

_LIVE_CACHE = {}


def _skip_if_retrieval_unreachable():
    """Skip the live measurements when arXiv is unreachable RIGHT NOW.

    ★ Deliberately narrow. This skips ONLY on a transport verdict
    (``request_failed`` — 429 / timeout / DNS), never on a measurement result.
    An on-topic rate that is genuinely too low, or neighbour sets that are
    genuinely identical, MUST still fail: those are the defects this file
    exists to catch, and letting the skip swallow them would produce the
    quietest possible failure — a green suite that measured nothing.

    The probe is one cheap request whose answer we already know (a real arXiv
    id), so "can we reach the API" is decided separately from "what does the
    corpus retrieve".
    """
    if pytest is None:
        return
    from tofu_search.search.vertical import arxiv as ts_arxiv
    probe = ts_arxiv.search_by_query(['attention'], ['transformer'],
                                     max_results=1)
    if probe.get('outcome') == 'request_failed':
        pytest.skip(
            'arXiv is unreachable right now '
            f"({probe.get('error')}) — skipping the LIVE retrieval "
            'measurement. This is a transport gate only: a real on-topic or '
            'overlap regression still fails.')


def _live_neighbour_sets():
    """Retrieve real neighbours for every real idea (hits the network).

    Drives the BATCH path: the fielded query needs every idea's terms to tell
    shared domain background from this idea's identity, which is exactly how
    generate_ideas calls it.

    ★ Memoized for the whole session. Three tests interrogate the SAME
    retrieval from different angles (on-topic rate / empty basis / pairwise
    overlap), and re-running it per test tripled the request volume against an
    API that rate-limits — measured: the third run got HTTP 429 on every rung,
    then hard read-timeouts, and reported an empty basis for ideas that had
    real neighbours minutes earlier. The suite was throttling the very API it
    was trying to measure, and the resulting red looked like a product defect.
    """
    if 'v' in _LIVE_CACHE:
        return _LIVE_CACHE['v']
    ideas = _require_corpus()
    if not ideas:
        _LIVE_CACHE['v'] = (None, None)
        return _LIVE_CACHE['v']
    import time
    import lib.paper.ideate as it
    batch = []
    for idea in ideas:
        t, _src = it.build_retrieval_query(idea)
        if t:
            batch.append(t)
    sets = []
    for idea in ideas:
        ps = it._novelty_prior_set(idea, k=5, batch_terms=batch)
        sets.append(ps.get('retrieved') or [])
        time.sleep(3)          # be polite to the arXiv API
    _LIVE_CACHE['v'] = (ideas, sets)
    return _LIVE_CACHE['v']


def test_retrieved_neighbours_are_in_the_ideas_field():
    """The neighbour set must be about the idea's field.

    Failing-first: production scored 0/25 — every neighbour was astrophysics."""
    if pytest is not None:
        pytest.importorskip('requests')
    _skip_if_retrieval_unreachable()
    ideas, sets = _live_neighbour_sets()
    if ideas is None:
        return
    all_n = [n for s in sets for n in s]
    assert all_n, ('retrieval returned nothing for ALL ideas — one real '
                   "mechanism's `*` used to cause HTTP 500; the query must be "
                   'sanitized so at least some ideas retrieve neighbours')
    rate = _hit_rate(all_n)
    per_idea = ', '.join(f'{len(s)}@{_hit_rate(s):.0%}' for s in sets)
    assert rate >= _MIN_FIELD_HIT_RATE, (
        f'only {rate:.0%} of {len(all_n)} retrieved neighbours are in the '
        f'corpus field (need >= {_MIN_FIELD_HIT_RATE:.0%}); per-idea: {per_idea}. '
        'Production measured 0% — the neighbours were gravitational-wave papers.')
    _ok(f'{rate:.0%} of {len(all_n)} retrieved neighbours are on-topic '
        f'(per-idea: {per_idea})')


def test_every_idea_retrieves_a_non_empty_basis():
    """No idea may silently end up with an empty basis (the HTTP-500 case)."""
    if pytest is not None:
        pytest.importorskip('requests')
    _skip_if_retrieval_unreachable()
    ideas, sets = _live_neighbour_sets()
    if ideas is None:
        return
    empty = [ideas[i].get('title', '?')[:50] for i, s in enumerate(sets) if not s]
    assert not empty, (
        f'{len(empty)}/{len(ideas)} ideas retrieved ZERO neighbours: {empty}. '
        'A sanitized query must not break the API (the real failure was an '
        'unescaped `*` in one mechanism → HTTP 500 → silent [] ).')
    _ok(f'all {len(ideas)} ideas retrieve a non-empty neighbour set')


def test_neighbour_sets_discriminate_between_ideas():
    """Different ideas must get different neighbours.

    Failing-first: GWTC-5.0 and "Deep Search for Joint Sources…" appeared in
    5/6 real ideas — the query was not discriminating, so the novelty axis was
    effectively a constant."""
    if pytest is not None:
        pytest.importorskip('requests')
    _skip_if_retrieval_unreachable()
    ideas, sets = _live_neighbour_sets()
    if ideas is None:
        return
    ids = [{n.get('arxiv_id') for n in s if n.get('arxiv_id')} for s in sets]
    worst = 0.0
    worst_pair = None
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if not ids[i] or not ids[j]:
                continue
            ov = len(ids[i] & ids[j]) / min(len(ids[i]), len(ids[j]))
            if ov > worst:
                worst, worst_pair = ov, (i + 1, j + 1)
    assert worst <= _MAX_PAIRWISE_OVERLAP, (
        f'ideas {worst_pair} share {worst:.0%} of their neighbours '
        f'(max {_MAX_PAIRWISE_OVERLAP:.0%}) — the retrieval is not '
        'discriminating between ideas, so the novelty axis is a constant')
    _ok(f'neighbour sets discriminate between ideas (worst pairwise overlap '
        f'{worst:.0%} <= {_MAX_PAIRWISE_OVERLAP:.0%})')


def test_batch_query_is_fielded_and_splits_identity_from_domain():
    """The wire query must be FIELDED, splitting identity from domain.

    `all:` is an unordered bag of words: it cannot tell the terms that NAME an
    idea from the terms that merely place it in a field, which is why two ideas
    ending in "KV Cache Compression" retrieved byte-identical neighbours. The
    fix expresses the real intent in syntax arXiv already has —
    ``ti:"<identity>" AND all:"<domain>"``.

    NEUTER: collapse the fielded query back to one flat term string (the shape
    that shipped) and the identity/domain distinction disappears — the two legs
    fuse into the bag of words that made the novelty axis a constant."""
    ideas = _require_corpus()
    if not ideas:
        return
    import lib.paper.ideate as it

    batch = []
    for idea in ideas:
        t, _src = it.build_retrieval_query(idea)
        if t:
            batch.append(t)

    captured = []

    def _capture(identity, domain=None, *, field='ti', max_results=5):
        # Build the wire query through the SHARED constructor, so this asserts
        # what actually goes out rather than a string the harness invented.
        from tofu_search.search.vertical.arxiv import build_query
        q, _m = build_query(identity, domain, field=field)
        captured.append(q)
        return _envelope(
            [{'arxiv_id': '2401.00001', 'title': 'KV cache', 'summary': 's'}],
            query=q)

    restore = _patch_retrieval(it, _capture)
    try:
        modes = []
        for idea in ideas:
            ps = it._novelty_prior_set(idea, k=5, batch_terms=batch)
            modes.append(ps.get('query_mode'))
    finally:
        restore()

    fielded = [q for q in captured if q.startswith('(ti:') and ' AND all:' in q]
    assert fielded, (
        'no query used the fielded (ti:a OR ti:b) AND all:"domain" form — an '
        'unordered `all:` bag cannot separate identity from domain. '
        f'Queries: {captured[:3]!r}')
    assert modes.count('fielded_t1') == len(fielded), \
        f'query_mode must record the fielded form; modes={modes}, fielded={len(fielded)}'

    # The two legs must carry DIFFERENT terms — identity in ti:, domain in all:.
    import re as _re
    for q in fielded:
        m = _re.match(r'\(((?:ti:\S+(?: OR )?)+)\) AND all:"([^"]*)"', q)
        assert m, f'malformed fielded query: {q!r}'
        ident = [t.split(':', 1)[1].lower()
                 for t in m.group(1).split(' OR ')]
        dom = m.group(2).lower().split()
        assert ident and dom, f'both legs must be populated: {q!r}'
        assert not (set(ident) & set(dom)), (
            f'a term appears in BOTH legs — the split is not a split: {q!r}')
        # Identity terms must be SEPARATE unquoted terms. A quoted multi-word
        # ti:"a b" is an exact PHRASE match, and a novel idea's identity phrase
        # is by construction in no existing title — measured 0 hits for all 6
        # real ideas, which silently collapsed the whole ladder to flat all:.
        assert '"' not in m.group(1), (
            f'identity leg is phrase-quoted, which can never match a novel '
            f'idea: {q!r}')

    # NEUTER: no batch context => nothing to compare against => flat all:.
    restore = _patch_retrieval(it, _capture)
    captured.clear()
    try:
        it._novelty_prior_set(ideas[0], k=5, batch_terms=None)
    finally:
        restore()
    assert captured and not captured[0].startswith('(ti:'), (
        'NEUTER FAILED: without batch context the query should degrade to a '
        f'flat all: string, got {captured[0]!r}')
    _ok(f'{len(fielded)}/{len(ideas)} queries are fielded (ti:a OR ti:b) AND '
        'all:"domain" with disjoint legs (NEUTER: no batch context degrades '
        'to flat all:)')


def test_novel_identity_terms_are_or_ed_not_all_required():
    """Identity terms must be OR-ed, and the ladder must widen, not collapse.

    Two syntax mistakes measured against the live arXiv API, each of which
    returned ZERO for all six real ideas while looking correct in a mock:

      * ``ti:"predictive delta"`` — quoted = exact phrase; a novel idea's
        identity phrase is in no existing title.
      * ``ti:predictive AND ti:delta`` — demands EVERY identity word in one
        title; also 0/6.

    ``(ti:predictive OR ti:delta) AND all:"KV cache compression"`` measured
    8/9 on-topic at 0% pairwise overlap. This test pins the operator choice
    without touching the network.
    """
    import lib.paper.ideate as it

    q, mode = it.assemble_arxiv_query(['predictive', 'delta'],
                                      ['KV', 'cache', 'compression'])
    assert mode == 'fielded_t1', f'expected a tier-1 fielded query, got {mode!r}'
    assert ' OR ' in q, (
        'identity terms are AND-ed (or phrase-quoted) — measured 0 hits for '
        f'every real idea: {q!r}')
    assert ' AND all:"' in q, f'domain leg must stay a phrase constraint: {q!r}'

    # Tier 2 widens the SAME terms from title to abstract, so an idea whose
    # terms are too new for any title still gets a basis instead of a false
    # "no prior art" (2 of the 6 real ideas were exactly this case).
    q2, mode2 = it.assemble_arxiv_query(['predictive', 'delta'],
                                        ['KV', 'cache', 'compression'], tier=2)
    assert mode2 == 'fielded_t2' and q2.startswith('(abs:'), \
        f'tier 2 must widen the identity leg to the abstract: {q2!r}'

    # Tier 3: domain only, still a PHRASE constraint (not the unquoted bag of
    # words whose near-unconstrained matching started this whole epic).
    q3, mode3 = it.assemble_arxiv_query([], ['KV', 'cache', 'compression'])
    assert mode3 == 'domain' and q3 == 'all:"KV cache compression"', \
        f'tier 3 must be a quoted domain phrase: {q3!r}'
    _ok('identity terms are OR-ed; ladder widens ti → abs → domain phrase')


def main():
    print()
    print(_color('═══ R3 gate ② — novelty retrieval (real-corpus) ═══', '36'))
    print()
    tests = [
        test_retrieval_query_is_sanitized_never_raw_prose,
        test_empty_neighbour_set_marks_novelty_basis_none,
        test_idea_with_unjudgeable_novelty_cannot_be_accepted,
        test_audit_fields_keep_retrieved_and_self_reported_separate,
        test_retrieved_neighbours_are_in_the_ideas_field,
        test_every_idea_retrieves_a_non_empty_basis,
        test_neighbour_sets_discriminate_between_ideas,
        test_batch_query_is_fielded_and_splits_identity_from_domain,
        test_novel_identity_terms_are_or_ed_not_all_required,
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
