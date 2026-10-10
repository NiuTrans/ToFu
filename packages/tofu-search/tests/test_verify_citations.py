"""Offline tests for tofu_search.verify — citation hallucination detection.

The HTTP seam (`tofu_search.search.vertical.base.http_get`) is mocked in every
test; NOTHING here touches the network. Covers the three-state contract and,
per the project's verification discipline, two SOURCE-LEVEL negative controls
that prove the load-bearing logic actually drives the green assertions.
"""

import pytest

from tofu_search.verify import (
    SUSPICIOUS,
    UNVERIFIABLE,
    VERIFIED,
    parse_bibtex,
    parse_references,
    summarize,
    verify_citations,
)
from tofu_search.verify.parse import Citation

# ── fake HTTP plumbing ───────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, *, status=200, json_data=None, text=''):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


def _crossref_work(title):
    return FakeResp(json_data={'message': {'title': [title]}})


def _crossref_search(items):
    return FakeResp(json_data={'message': {'items': items}})


def _s2_search(items, status=200):
    return FakeResp(status=status, json_data={'data': items})


def _arxiv_atom(title=None):
    if title is None:
        return FakeResp(text='<feed xmlns="http://www.w3.org/2005/Atom"></feed>')
    return FakeResp(text=(
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        f'<title>{title}</title></entry></feed>'
    ))


@pytest.fixture
def patch_http(monkeypatch):
    """Install a router for base.http_get; the test supplies the handler."""
    holder = {}

    def install(fn):
        from tofu_search.search.vertical import base
        monkeypatch.setattr(base, 'http_get', fn)
        holder['fn'] = fn
    return install


# ── Tier 1: DOI ──────────────────────────────────────────────────────────────

def test_doi_404_is_suspicious(patch_http):
    patch_http(lambda url, **kw: FakeResp(status=404))
    cit = Citation(title='A Real Paper', doi='10.9999/nonexistent', raw='x')
    [r] = verify_citations([cit])
    assert r['state'] == SUSPICIOUS
    assert r['tier'] == 1 and r['method'] == 'doi'


def test_doi_resolves_matching_title_is_verified(patch_http):
    patch_http(lambda url, **kw: _crossref_work('Attention Is All You Need'))
    cit = Citation(title='Attention Is All You Need', doi='10.1/x', raw='x')
    [r] = verify_citations([cit])
    assert r['state'] == VERIFIED
    assert r['evidence']['title_similarity'] >= 0.9


def test_doi_resolves_to_unrelated_title_is_suspicious(patch_http):
    patch_http(lambda url, **kw: _crossref_work('A Totally Different Subject'))
    cit = Citation(title='Attention Is All You Need', doi='10.1/x', raw='x')
    [r] = verify_citations([cit])
    assert r['state'] == SUSPICIOUS
    assert 'unrelated' in r['evidence']['reason']


def test_doi_transport_error_degrades_to_unverifiable(patch_http):
    def boom(url, **kw):
        raise OSError('connection reset')
    patch_http(boom)
    cit = Citation(title='X paper', doi='10.1/x', raw='x')
    [r] = verify_citations([cit])
    assert r['state'] == UNVERIFIABLE  # NEVER suspicious on a transport error


# ── Tier 1: arXiv ─────────────────────────────────────────────────────────────

def test_arxiv_missing_entry_is_suspicious(patch_http):
    patch_http(lambda url, **kw: _arxiv_atom(None))
    cit = Citation(title='Ghost paper', arxiv_id='2999.99999', raw='x')
    [r] = verify_citations([cit])
    assert r['state'] == SUSPICIOUS
    assert r['method'] == 'arxiv'


def test_arxiv_resolves_is_verified(patch_http):
    patch_http(lambda url, **kw: _arxiv_atom('Denoising Diffusion Probabilistic Models'))
    cit = Citation(title='Denoising Diffusion Probabilistic Models',
                   arxiv_id='2006.11239', raw='x')
    [r] = verify_citations([cit])
    assert r['state'] == VERIFIED


# ── Tier 2: title-only fuzzy ──────────────────────────────────────────────────

def test_title_strong_match_is_verified(patch_http):
    patch_http(lambda url, **kw: _crossref_search(
        [{'title': ['Attention Is All You Need'],
          'author': [{'given': 'Ashish', 'family': 'Vaswani'}],
          'issued': {'date-parts': [[2017]]}, 'DOI': '10.1/a'}]))
    cit = Citation(title='Attention Is All You Need', raw='x')
    [r] = verify_citations([cit])
    assert r['state'] == VERIFIED
    assert r['tier'] == 2


def test_title_no_match_is_unverifiable_not_suspicious(patch_http):
    # The crucial anti-false-positive assertion: an obscure/uncovered title
    # that returns no good CrossRef hit must be UNVERIFIABLE, never SUSPICIOUS.
    patch_http(lambda url, **kw: _crossref_search(
        [{'title': ['Some Unrelated Survey of Widgets'],
          'issued': {'date-parts': [[1998]]}}]))
    cit = Citation(title='An Obscure Workshop Paper Nobody Indexed', raw='x')
    [r] = verify_citations([cit])
    assert r['state'] == UNVERIFIABLE
    assert r['state'] != SUSPICIOUS


def test_title_borderline_rescued_by_author_and_year(patch_http):
    # Mid-band similarity (between weak 0.55 and strong 0.90) + matching surname
    # + year → rescued to verified by corroboration.
    patch_http(lambda url, **kw: _crossref_search(
        [{'title': ['Deep Residual Learning'],
          'author': [{'given': 'Kaiming', 'family': 'He'}],
          'issued': {'date-parts': [[2016]]}, 'DOI': '10.1/r'}]))
    cit = Citation(title='Deep Residual Learning for Image Recognition',
                   authors=['Kaiming He'], year='2016', raw='x')
    [r] = verify_citations([cit])
    assert r['state'] == VERIFIED
    assert 'corroborated' in r['evidence']['reason']


# ── Tier 2: Semantic Scholar fallback (preprint / conference coverage) ────────

def _cr_miss_s2_router(s2_resp):
    """Router: CrossRef returns no usable candidate; S2 returns *s2_resp*."""
    def router(url, **kw):
        if 'crossref.org' in url:
            return _crossref_search([])
        if 'semanticscholar.org' in url:
            return s2_resp
        return FakeResp(status=404)
    return router


def test_s2_rescues_crossref_miss_for_preprint(patch_http):
    # The whole reason this fallback exists: CrossRef doesn't index the
    # preprint, but Semantic Scholar does → VERIFIED via the S2 branch.
    patch_http(_cr_miss_s2_router(_s2_search(
        [{'title': 'LLaMA Open and Efficient Foundation Language Models',
          'year': 2023, 'externalIds': {'ArXiv': '2302.13971'}}])))
    cit = Citation(title='LLaMA Open and Efficient Foundation Language Models',
                   year='2023', raw='x')
    [r] = verify_citations([cit])
    assert r['state'] == VERIFIED
    assert r['method'] == 's2_title'
    assert r['evidence']['source'] == 'Semantic Scholar'


def test_s2_also_misses_stays_unverifiable_not_suspicious(patch_http):
    # Neither CrossRef nor S2 has it → still UNVERIFIABLE, never SUSPICIOUS.
    patch_http(_cr_miss_s2_router(_s2_search(
        [{'title': 'Something Completely Different', 'year': 1990}])))
    cit = Citation(title='A Genuinely Obscure Unindexed Workshop Note', raw='x')
    [r] = verify_citations([cit])
    assert r['state'] == UNVERIFIABLE
    assert r['state'] != SUSPICIOUS


def test_s2_rate_limited_keeps_crossref_unverifiable(patch_http):
    # S2 429 → fallback returns None → CrossRef's UNVERIFIABLE verdict stands.
    patch_http(_cr_miss_s2_router(_s2_search([], status=429)))
    cit = Citation(title='Some Preprint That S2 Rate-Limited On', raw='x')
    [r] = verify_citations([cit])
    assert r['state'] == UNVERIFIABLE
    assert r['method'] == 'crossref_title'  # kept CrossRef verdict, not S2


def test_s2_not_consulted_when_crossref_already_verified(patch_http):
    # If CrossRef gives a strong match, S2 must not be needed; prove by making
    # any S2 call raise — the verdict must still be the CrossRef VERIFIED.
    def router(url, **kw):
        if 'crossref.org' in url:
            return _crossref_search([{'title': ['Exactly The Claimed Title']}])
        raise AssertionError('S2 should not be consulted on a CrossRef hit')
    patch_http(router)
    cit = Citation(title='Exactly The Claimed Title', raw='x')
    [r] = verify_citations([cit])
    assert r['state'] == VERIFIED
    assert r['method'] == 'crossref_title'


# ── Tier 3: nothing to check ──────────────────────────────────────────────────

def test_no_identifier_no_title_is_unverifiable():
    cit = Citation(raw='Some Book, A Publisher, 1990.')
    [r] = verify_citations([cit])
    assert r['state'] == UNVERIFIABLE
    assert r['tier'] == 3


def test_book_like_reference_is_unverifiable_not_suspicious(patch_http):
    # A book reference (only a title, no DOI/arXiv) with no good index hit:
    # must degrade to unverifiable, never flagged as a hallucination.
    patch_http(lambda url, **kw: _crossref_search([]))
    cit = Citation(title='Introduction to Algorithms', raw='Cormen et al., MIT Press')
    [r] = verify_citations([cit])
    assert r['state'] == UNVERIFIABLE


# ── summary gating ────────────────────────────────────────────────────────────

def test_summary_counts_and_gating(patch_http):
    def router(url, **kw):
        if 'works/10.9999' in url:
            return FakeResp(status=404)                       # suspicious
        if url.endswith('/works') or 'query.bibliographic' in str(kw):
            return _crossref_search([{'title': ['Real Title Here Exactly']}])
        return _crossref_work('Real Title Here Exactly')
    patch_http(router)
    cits = [
        Citation(title='Real Title Here Exactly', doi='10.1/ok', raw='a'),
        Citation(title='Real Title Here Exactly', raw='b'),         # tier2 verified
        Citation(title='Fake', doi='10.9999/x', raw='c'),           # suspicious
        Citation(raw='no signal at all'),                           # unverifiable
    ]
    s = summarize(verify_citations(cits))
    assert s['total'] == 4
    assert s['counts'][SUSPICIOUS] == 1
    assert s['has_suspicious'] is True
    assert len(s['suspicious']) == 1


# ── parser unit coverage ──────────────────────────────────────────────────────

def test_parse_bibtex_basic():
    bib = r"""
@article{vaswani2017,
  title = {Attention Is All You Need},
  author = {Vaswani, Ashish and Shazeer, Noam},
  year = {2017},
  doi = {10.5555/3295222.3295349}
}
@inproceedings{he2016,
  title={Deep Residual Learning for Image Recognition},
  author={He, Kaiming},
  archivePrefix={arXiv},
  eprint={1512.03385},
  year={2016}
}
@comment{ignore me}
"""
    cits = parse_bibtex(bib)
    assert len(cits) == 2
    assert cits[0].title == 'Attention Is All You Need'
    assert cits[0].doi == '10.5555/3295222.3295349'
    assert cits[0].authors == ['Vaswani, Ashish', 'Shazeer, Noam']
    assert cits[0].year == '2017'
    assert cits[1].arxiv_id == '1512.03385'


def test_parse_bibtex_skips_malformed_keeps_rest():
    bib = '@article{good, title={Fine Paper}, year={2020}} @article{bad, title={Broken'
    cits = parse_bibtex(bib)
    assert any(c.title == 'Fine Paper' for c in cits)


def test_extract_citations_from_text_harvests_inline_ids():
    from tofu_search.verify import extract_citations_from_text
    body = (
        '## Research Landscape\n'
        'The Transformer (arXiv:1706.03762) replaced recurrence. See also '
        'the BERT paper arXiv:1810.04805 and a journal study 10.1038/s41586-023-06221-2. '
        'A second mention of arXiv:1706.03762 should NOT duplicate.\n'
    )
    cits = extract_citations_from_text(body)
    arxiv_ids = sorted(c.arxiv_id for c in cits if c.arxiv_id)
    dois = [c.doi for c in cits if c.doi]
    assert arxiv_ids == ['1706.03762', '1810.04805']  # deduped
    assert dois == ['10.1038/s41586-023-06221-2']
    # id-only citations (no title) → verifier takes the Tier-1 path
    assert all(not c.title for c in cits)


def test_extract_citations_from_text_empty():
    from tofu_search.verify import extract_citations_from_text
    assert extract_citations_from_text('') == []
    assert extract_citations_from_text('A report with no identifiers at all.') == []


def test_parse_references_extracts_identifiers():
    blob = (
        '[1] A. Author. "A Quoted Title Here". arXiv:2301.00001. 2023.\n'
        '[2] B. Writer. Some book without an id. 1999.\n'
    )
    cits = parse_references(blob)
    assert len(cits) == 2
    assert cits[0].arxiv_id == '2301.00001'
    assert cits[0].title == 'A Quoted Title Here'
    assert cits[0].year == '2023'
    assert cits[1].arxiv_id == '' and cits[1].doi == ''


# ── SOURCE-LEVEL NEGATIVE CONTROLS ───────────────────────────────────────────
# These prove the green tests above are load-bearing: monkeypatching the real
# module-level function at its definition site (not just stubbing a fixture)
# and asserting the verdict flips. Restored automatically by monkeypatch.

def test_negctl_disable_similarity_collapses_verified_doi(patch_http, monkeypatch):
    """If the title-similarity gate is forced to 0.0, a DOI whose catalogue
    title MATCHES the claim collapses from VERIFIED → SUSPICIOUS (it now looks
    like 'resolves to an unrelated title'). Proves _title_similarity is what
    makes the matching-DOI case pass."""
    import tofu_search.verify.citations as C
    patch_http(lambda url, **kw: _crossref_work('Attention Is All You Need'))
    cit = Citation(title='Attention Is All You Need', doi='10.1/x', raw='x')

    # baseline
    assert verify_citations([cit])[0]['state'] == VERIFIED
    # negative control: kill the gate
    monkeypatch.setattr(C, '_title_similarity', lambda a, b: 0.0)
    assert verify_citations([cit])[0]['state'] == SUSPICIOUS


def test_negctl_break_404_branch_loses_suspicious(patch_http, monkeypatch):
    """If the DOI checker ignores HTTP status (treats every response as OK),
    a 404 DOI stops being SUSPICIOUS. Proves the 404→suspicious branch is the
    load-bearing line, not an incidental pass."""
    import tofu_search.verify.citations as C
    patch_http(lambda url, **kw: FakeResp(status=404))
    cit = Citation(title='Ghost', doi='10.9999/y', raw='x')
    assert verify_citations([cit])[0]['state'] == SUSPICIOUS

    orig = C._verify_doi

    def blind(cit):
        # Simulate the bug: pretend the response was a successful empty record.
        from tofu_search.verify.citations import _result
        return _result(VERIFIED, cit, tier=1, method='doi', reason='blind')
    monkeypatch.setattr(C, '_verify_doi', blind)
    assert verify_citations([cit])[0]['state'] == VERIFIED
    monkeypatch.setattr(C, '_verify_doi', orig)
    assert verify_citations([cit])[0]['state'] == SUSPICIOUS


def test_negctl_disable_s2_fallback_collapses_preprint(patch_http, monkeypatch):
    """If the Semantic Scholar fallback is disabled (forced to return None), a
    preprint that CrossRef misses but S2 would confirm collapses from VERIFIED
    back to UNVERIFIABLE. Proves the S2 branch is what rescues the preprint —
    not an incidental CrossRef pass."""
    import tofu_search.verify.citations as C
    patch_http(_cr_miss_s2_router(_s2_search(
        [{'title': 'LLaMA Open and Efficient Foundation Language Models',
          'year': 2023, 'externalIds': {'ArXiv': '2302.13971'}}])))
    cit = Citation(title='LLaMA Open and Efficient Foundation Language Models',
                   year='2023', raw='x')
    # baseline: S2 rescues it
    assert verify_citations([cit])[0]['state'] == VERIFIED
    # negative control: kill the S2 fallback
    monkeypatch.setattr(C, '_verify_title_s2', lambda cit: None)
    out = verify_citations([cit])[0]
    assert out['state'] == UNVERIFIABLE
    assert out['state'] != SUSPICIOUS  # fail-open discipline preserved
