"""arXiv query-syntax contract — the three facts that cost six zero-recall runs.

These assertions are the executable form of knowledge that previously lived only
in a comment in a downstream application. A comment cannot go red, so the next
person to write arXiv retrieval here would have re-learned all three the hard
way. Each was MEASURED against the live API (2026-07-28) on six real research
ideas; each looked correct in a mock and returned ZERO for all six.

★ Deliberately OFFLINE — zero network. These pin the QUERY STRING we construct,
not what arXiv happens to index today. A networked version would go randomly red
on index drift or an HTTP 429, and a randomly-red guard is indistinguishable
from noise, which is how a broken guard gets ignored for two weeks. Real recall
behaviour belongs in a comparison script, not in a resident guard.

Run:  pytest tests/test_arxiv_query_syntax.py
"""

import re

from tofu_search.search.vertical import arxiv

# ── Fact 1: a quoted multi-word value is an exact PHRASE match ──

def test_identity_terms_are_never_phrase_quoted():
    """`ti:"predictive delta"` requires those words ADJACENT in a title.

    A novel idea's distinguishing phrase is, by construction, in no existing
    title — so a quoted identity leg matches nothing, always. Measured: 0/6
    real ideas got any result from the quoted form.
    """
    q, mode = arxiv.build_query(['predictive', 'delta'],
                               ['KV', 'cache', 'compression'])
    assert mode == 'fielded', f'expected a fielded query, got {mode!r}'

    m = re.match(r'\((?P<identity>[^)]*)\) AND all:"(?P<domain>[^"]*)"', q)
    assert m, f'unexpected query shape: {q!r}'
    identity = m.group('identity')
    assert '"' not in identity, (
        'the identity leg is phrase-quoted, which can never match a novel '
        f'idea (measured 0/6 on real ideas): {q!r}')


# ── Fact 2: identity terms must be OR-ed, never AND-ed ──

def test_identity_terms_are_or_ed_not_and_ed():
    """`ti:predictive AND ti:delta` demands EVERY identity word in one title.

    Also measured at 0/6. OR-ing asks the question we actually mean: "a paper
    whose title touches ANY of this idea's distinguishing terms, inside our
    field" — which is what a neighbour IS.
    """
    q, _ = arxiv.build_query(['predictive', 'delta'],
                             ['KV', 'cache', 'compression'])
    identity = re.match(r'\(([^)]*)\)', q).group(1)
    assert ' OR ' in identity, f'identity terms are not OR-ed: {q!r}'
    assert ' AND ' not in identity, (
        'identity terms are AND-ed — requires all words in one title, '
        f'measured 0 hits for every real idea: {q!r}')
    # Each term must be its own fielded clause.
    assert identity.count('ti:') == 2, (
        f'expected one ti: clause per identity term: {identity!r}')


# ── Fact 3: the DOMAIN leg stays a quoted phrase ──

def test_domain_leg_is_phrase_quoted_to_hold_the_field():
    """The domain leg is shared field vocabulary; phrase-matching it is what
    stops the search drifting out of the field.

    Contrast: a flat unquoted prose `all:` query degrades into
    near-unconstrained matching (measured 0% on-topic, 80% cross-item overlap —
    it kept returning the same most-cited papers regardless of the query).
    """
    q, _ = arxiv.build_query(['quantum', 'entanglement'],
                             ['KV', 'cache', 'compression'])
    assert 'all:"KV cache compression"' in q, (
        f'domain leg must be a quoted phrase: {q!r}')


# ── Widening ladder: title → abstract ──

def test_free_text_terms_leg_is_not_phrase_quoted():
    """★ Fact 1 applies to the FREE-TEXT leg too, not just the identity leg.

    Regression this pins: the first version of `build_query` quoted the
    no-domain path as `all:"a b c d"`, which demands those words ADJACENT.
    Measured against the live API — the quoted form returned 0 papers for a real
    5-term idea title that the unquoted form answers with 5. Unquoted terms are
    AND-ed by arXiv, which is what a free-text caller expects.

    The original guard set asserted Fact 1 only on the identity leg, so this
    trap came back in a second leg and two live-retrieval tests caught it
    downstream. One fact, every leg that could carry it.
    """
    q, mode = arxiv.build_query(
        ['Predictive', 'Delta', 'Compression', 'KV', 'Cache'], None)
    assert mode == 'terms', f'expected the free-text mode, got {mode!r}'
    assert '"' not in q, (
        'the free-text leg is phrase-quoted, which demands adjacency and '
        f'matches almost nothing (measured 0 vs 5 papers): {q!r}')
    assert q.startswith('all:'), q


def test_domain_only_leg_stays_quoted():
    """Counter-case: the DOMAIN leg must KEEP its quotes even alone.

    Without this the obvious "fix" for the test above — drop all quoting
    everywhere — would stay green while silently removing the field constraint
    that stops the search drifting out of the domain.
    """
    q, mode = arxiv.build_query([], ['KV', 'cache', 'compression'])
    assert mode == 'domain', f'expected domain-only mode, got {mode!r}'
    assert q == 'all:"KV cache compression"', (
        f'domain leg must stay a quoted phrase even with no identity terms: {q!r}')


def test_field_widens_from_title_to_abstract():
    """2 of 6 real ideas had identity terms too new for ANY title and only found
    neighbours via the abstract leg. Without widening they would have falsely
    reported "no prior art" — the worst false negative for a novelty check."""
    q1, m1 = arxiv.build_query(['predictive', 'delta'], ['KV', 'cache'])
    q2, m2 = arxiv.build_query(['predictive', 'delta'], ['KV', 'cache'],
                               field='abs')
    assert m1 == m2 == 'fielded'
    assert q1.startswith('(ti:'), q1
    assert q2.startswith('(abs:'), q2
    assert q1 != q2, 'widening the field did not change the query'


# ── Sanitization: prose and markup never reach the wire ──

def test_parser_breaking_characters_are_stripped():
    """An unescaped `*` in one real idea caused an HTTP 500, which a bare
    `except` then swallowed into "no prior art"."""
    terms = arxiv.sanitize_terms('the *difference* between (a) and [b], c;')
    assert terms, 'everything was stripped'
    joined = ' '.join(terms)
    for ch in '*()[]{},;"\'':
        assert ch not in joined, f'{ch!r} survived sanitization: {joined!r}'


def test_prose_query_cannot_be_sent_as_a_single_term():
    """Sanitization splits on whitespace, so a whole sentence becomes terms —
    it can never go on the wire as one opaque blob."""
    terms = arxiv.sanitize_terms(
        'Instead of storing full KV pairs, only store a compressed delta')
    assert len(terms) > 5, terms
    assert all(' ' not in t for t in terms), terms


# ── The empty-recall distinction ──

def test_unusable_query_is_distinguishable_from_no_matches(monkeypatch):
    """★ "asked properly, nothing matched" MUST NOT look like "could not ask".

    Collapsing both into [] strips the caller of the signal it needs to decide
    whether to widen — and a novelty check that cannot tell them apart reports
    "no prior art" for a query that was merely too narrow.
    """
    called = []
    monkeypatch.setattr(arxiv.base, 'http_get',
                        lambda *a, **k: called.append(1))

    res = arxiv.search_by_query(['***'], ['((('])
    assert res['outcome'] == 'unusable_query', res
    assert res['ok'] is False
    assert res['papers'] == []
    assert not called, 'no request should be made for an unusable query'


def test_empty_result_set_reports_no_matches_not_failure(monkeypatch):
    """A successful request that matched nothing is `no_matches` — real evidence
    that this query is too narrow, distinct from a transport failure."""
    class _Resp:
        ok = True
        status_code = 200
        text = ('<feed xmlns="http://www.w3.org/2005/Atom">'
                '<title>ArXiv Query</title></feed>')

    monkeypatch.setattr(arxiv.base, 'http_get', lambda *a, **k: _Resp())
    res = arxiv.search_by_query(['predictive', 'delta'], ['KV', 'cache'])
    assert res['ok'] is True
    assert res['outcome'] == 'no_matches', res
    assert res['papers'] == []
    assert res['error'] == ''


def test_request_failure_is_not_evidence_about_the_literature(monkeypatch):
    class _Boom:
        ok = False
        status_code = 500
        text = ''

    monkeypatch.setattr(arxiv.base, 'http_get', lambda *a, **k: _Boom())
    res = arxiv.search_by_query(['predictive'], ['KV', 'cache'])
    assert res['ok'] is False
    assert res['outcome'] == 'request_failed', res
    assert 'HTTP 500' in res['error']


def test_hits_are_parsed_into_papers(monkeypatch):
    class _Resp:
        ok = True
        status_code = 200
        text = '''<feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>http://arxiv.org/abs/2401.00001v2</id>
            <title>KV Cache
            Compression Works</title>
            <summary>An   abstract.</summary>
            <published>2024-01-02T00:00:00Z</published>
            <author><name>A. Author</name></author>
          </entry>
        </feed>'''

    monkeypatch.setattr(arxiv.base, 'http_get', lambda *a, **k: _Resp())
    res = arxiv.search_by_query(['compression'], ['KV', 'cache'])
    assert res['outcome'] == 'hits', res
    assert len(res['papers']) == 1
    p = res['papers'][0]
    assert p['arxiv_id'] == '2401.00001'
    # Newlines inside Atom titles must be collapsed, not preserved.
    assert p['title'] == 'KV Cache Compression Works'
    assert p['authors'] == ['A. Author']
    assert p['published'] == '2024-01-02'
    assert p['abs_url'].endswith('/abs/2401.00001')


# ── Transient failures must not read as "no prior art" ──

_FEED_ONE = '''<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <title>A Paper</title>
  </entry>
</feed>'''


class _Resp:
    def __init__(self, status, text=''):
        self.status_code = status
        self.ok = status == 200
        self.text = text


def test_rate_limited_request_is_retried_and_can_recover(monkeypatch):
    """★ arXiv answers bursts with HTTP 429. Retry lives in THIS function, not
    in any one caller.

    The novelty gate calls `search_by_query` DIRECTLY (it has structured legs
    to pass and must not flatten them through a free-text adapter). Backoff
    placed in an adapter therefore protects every caller EXCEPT the one whose
    correctness depends on retrieval — measured: a 429 burst left 3 of 6 real
    ideas with an empty basis, which reads identically to "no prior art
    exists", the worst false positive a novelty check can emit.
    """
    calls = []
    monkeypatch.setattr(arxiv.time, 'sleep', lambda s: None)

    def _flaky(*a, **k):
        calls.append(1)
        return _Resp(429) if len(calls) < 3 else _Resp(200, _FEED_ONE)

    monkeypatch.setattr(arxiv.base, 'http_get', _flaky)
    res = arxiv.search_by_query(['predictive', 'delta'], ['KV', 'cache'])

    assert len(calls) == 3, f'expected retries after 429, got {len(calls)} call(s)'
    assert res['ok'] is True
    assert res['outcome'] == 'hits', res
    assert res['error'] == '', 'a recovered request must not report a stale error'


def test_exhausted_retries_still_report_request_failed_not_no_matches(monkeypatch):
    """Counter-case: when every attempt fails the verdict must stay
    `request_failed`. Degrading it to `no_matches` would tell the caller the
    literature is empty — a statement the failed request never supported."""
    calls = []
    monkeypatch.setattr(arxiv.time, 'sleep', lambda s: None)

    def _always_429(*a, **k):
        calls.append(1)
        return _Resp(429)

    monkeypatch.setattr(arxiv.base, 'http_get', _always_429)
    res = arxiv.search_by_query(['predictive'], ['KV', 'cache'])

    assert len(calls) == arxiv._SEARCH_RETRIES, calls
    assert res['ok'] is False
    assert res['outcome'] == 'request_failed', res
    assert res['papers'] == []


def test_a_clean_empty_result_is_not_retried(monkeypatch):
    """`no_matches` is a real answer about the literature, so retrying it just
    burns the rate limit that caused the problem in the first place."""
    calls = []
    monkeypatch.setattr(arxiv.time, 'sleep', lambda s: None)

    def _empty(*a, **k):
        calls.append(1)
        return _Resp(200, '<feed xmlns="http://www.w3.org/2005/Atom"/>')

    monkeypatch.setattr(arxiv.base, 'http_get', _empty)
    res = arxiv.search_by_query(['predictive'], ['KV', 'cache'])

    assert len(calls) == 1, f'a legitimate zero was retried {len(calls)}×'
    assert res['outcome'] == 'no_matches', res


# ── Single-source-of-truth: no third arXiv HTTP path ──

def test_query_path_reuses_the_shared_http_helper():
    """`search_by_query` must go through `base.http_get`, like the id lookup.

    Asserted via AST on the real call nodes rather than by counting substring
    occurrences — a comment mentioning the name would satisfy a text search,
    and `verify/citations.py` legitimately calls arXiv by id_list for citation
    verification, so a repo-wide count is the wrong criterion.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(arxiv.search_by_query))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    http_calls = [
        c for c in calls
        if isinstance(c.func, ast.Attribute) and c.func.attr == 'http_get'
        and isinstance(c.func.value, ast.Name) and c.func.value.id == 'base'
    ]
    assert len(http_calls) == 1, (
        f'expected exactly one base.http_get call, found {len(http_calls)} — '
        'a second transport path here means the arXiv client got duplicated')

    # And it must not construct its own HTTP client.
    src = inspect.getsource(arxiv.search_by_query)
    for forbidden in ('requests.get', 'urlopen', 'httpx.', 'http.client'):
        assert forbidden not in src, (
            f'{forbidden!r} bypasses the shared helper (timeout/UA/retry '
            'conventions live there)')
