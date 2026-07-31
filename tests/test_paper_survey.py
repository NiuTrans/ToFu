#!/usr/bin/env python3
"""R2 — multi-paper fan-in survey + library-verified open-gap map (lib/paper/survey.py).

Owner acceptance for R2, proven here (hermetic — the LLM synthesis and the
citation verifier are monkeypatched, so no network / real model needed):

  1. SCHEMA VALID + ALL IDS LIBRARY-VERIFIABLE — build_survey produces an
     open_gaps map whose schema_version is the frozen contract, and every
     arXiv id it surfaces (clusters.papers / method_matrix.paper /
     open_gaps.evidence) resolves to a row in paper_library.

  2. NEUTER (unknown id stripped) — when the LLM's raw gap map cites an arXiv
     id that is NOT in the library, the zero-LLM structural gate strips it; a
     gap whose evidence empties out is dropped entirely and recorded in
     missing_ids.
       ↳ counter-check: an id THAT IS in the library survives untouched.

  3. NEUTER (fake citation flagged) — a survey markdown that cites a
     nonexistent paper trips build_citation_audit → the result carries a
     citation_audit card. A clean survey → citation_audit is None.

  4. ZERO-REPARSE — _load_paper_inputs reads paper_reports / paper_library
     stored text and NEVER calls parse_pdf (asserted via a parse_pdf spy that
     must record zero calls), and prefers an existing report over parsed_text.

Run standalone:  python tests/test_paper_survey.py
Under pytest:    pytest tests/test_paper_survey.py -m unit
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


try:
    import pytest
    pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('open')]
except ImportError:
    pytest = None


_APP = None


def _load_app():
    global _APP
    if _APP is not None:
        return _APP
    import tempfile
    os.environ['TOFU_DB_BACKEND'] = 'sqlite'
    if not os.environ.get('TOFU_DB_PATH'):
        _dbf = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        _dbf.close()
        os.environ['TOFU_DB_PATH'] = _dbf.name
    os.environ.setdefault('TOFU_AUTH_MODE', 'open')
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)), 'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    try:
        from lib.database import init_db
        init_db()
    except Exception as e:
        print(f'[survey_test] init_db: {e}')
    _APP = mod.app
    return _APP


def _seed_library_paper(arxiv_id, *, phash=None, parsed_text=None, report=None,
                        lang='en', user_id=1, folder_id='research_test'):
    """Insert a paper_library row (+ optional paper_reports row) directly."""
    from lib.database._core import _pool_get, _pool_put
    from lib.database._core_schema import PAPER_LIBRARY, PAPER_REPORTS, upsert
    phash = phash or ('h' + arxiv_id.replace('.', '').replace('/', '')[:20])
    parsed_text = parsed_text if parsed_text is not None else f'Full parsed text of {arxiv_id}.'
    now = int(time.time() * 1000)
    db = _pool_get()
    try:
        upsert(db, PAPER_LIBRARY, {
            'id': f'lib_{arxiv_id}', 'user_id': user_id, 'title': f'Paper {arxiv_id}',
            'pdf_url': '', 'pdf_filename': '', 'arxiv_id': arxiv_id, 'paper_hash': phash,
            'parsed_text': parsed_text, 'parser_version': '', 'qa_history': '[]', 'images': '[]',
            'babel_cache': '{}', 'page_count': 5, 'folder_id': folder_id,
            'created_at': now, 'updated_at': now,
        }, retry=True)
        if report is not None:
            upsert(db, PAPER_REPORTS, {
                'paper_hash': phash, 'lang': lang, 'report': report,
                'model': '', 'meta': '{}', 'created_at': now,
            }, retry=True)
    finally:
        _pool_put(db)
    return phash


def _patch_synthesis(survey_md, gap_map):
    """Patch survey._synthesize_survey to return a fixed (md, raw_gap_map),
    bypassing the LLM. Returns restore()."""
    import lib.paper.survey as sv
    orig = sv._synthesize_survey
    sv._synthesize_survey = lambda inputs, direction, lang, **kw: (survey_md, gap_map)
    return lambda: setattr(sv, '_synthesize_survey', orig)


def _patch_citation_audit(fn):
    """Patch the citation audit used by survey (imported inside _audit_citations
    from lib.paper.citation_audit)."""
    import lib.paper.citation_audit as ca
    orig = ca.build_citation_audit
    ca.build_citation_audit = fn
    return lambda: setattr(ca, 'build_citation_audit', orig)


# ── Test 1: schema valid + all ids library-verifiable ─────────────────────

def test_survey_schema_and_all_ids_library_verifiable():
    _load_app()
    import lib.paper.survey as sv
    folder = f'rf_{int(time.time())%100000}'
    ids = ['2305.11111', '2401.22222', '2402.33333']
    for aid in ids:
        _seed_library_paper(aid, folder_id=folder, report=f'Report on {aid}.')
    raw = {
        'schema_version': 1,
        'clusters': [{'id': 'c1', 'theme': 'low-rank KV', 'papers': ids[:2],
                      'shared_assumption': 'low rank', 'limitation': 'inference only',
                      'unexplored': ['learnable rate']}],
        'method_matrix': [{'paper': ids[0], 'task': 'summ', 'assumption': 'low rank',
                           'data_scale': '16k', 'metric': 'ROUGE', 'open_source': True}],
        'open_gaps': [{'id': 'g1', 'gap': 'no exact recall under compression',
                       'why_open': 'only ppl measured', 'evidence': ids[1:],
                       'kind_hint': 'methodology'}],
    }
    restore = _patch_synthesis('A survey citing arXiv:2305.11111.', raw)
    ra = _patch_citation_audit(lambda md: None)
    try:
        res = sv.build_survey('long-context KV compression', ids, lang='en', folder_id=folder)
        assert res['ok'], res.get('error')
        gm = res['open_gaps']
        assert gm['schema_version'] == sv.OPEN_GAPS_SCHEMA_VERSION, 'schema_version not frozen value'
        assert gm['direction'] and gm['lang'] == 'en'
        assert gm['surveyed_count'] == 3, gm['surveyed_count']
        # Every surfaced id must be library-verifiable → none stripped.
        assert gm['stripped_ids'] == [], f"clean run stripped ids: {gm['stripped_ids']}"
        surfaced = sv._extract_survey_ids(gm)
        lib_ids = {i.split('v')[0] for i in ids}
        assert surfaced <= lib_ids, f'surfaced non-library ids: {surfaced - lib_ids}'
        assert len(gm['open_gaps']) == 1, 'valid gap was dropped'
    finally:
        ra(); restore()
    _ok('schema frozen + every surfaced id resolves in paper_library (clean run: 0 stripped)')


# ── Test 2: NEUTER — unknown id stripped, unbacked gap dropped ─────────────

def test_unknown_arxiv_id_is_stripped_NEUTER():
    _load_app()
    import lib.paper.survey as sv
    folder = f'rf_{int(time.time())%100000}_n'
    real = ['2305.44444', '2401.55555']
    for aid in real:
        _seed_library_paper(aid, folder_id=folder)
    FAKE = '2499.99999'  # never seeded → not in library
    raw = {
        'schema_version': 1,
        'clusters': [{'id': 'c1', 'theme': 't', 'papers': [real[0], FAKE]}],
        'method_matrix': [{'paper': real[0], 'task': 'x'}, {'paper': FAKE, 'task': 'y'}],
        'open_gaps': [
            {'id': 'g_real', 'gap': 'backed gap', 'evidence': [real[1]]},
            {'id': 'g_fake', 'gap': 'fabricated gap', 'evidence': [FAKE]},
        ],
    }
    restore = _patch_synthesis('survey', raw)
    ra = _patch_citation_audit(lambda md: None)
    # Stub grounding so the FAKE id is a pure hallucination (not grounded): the
    # 'grounded' tier requires _fetch_arxiv_title to confirm existence — return
    # '' so FAKE is classified hallucination and stripped.
    import lib.paper.survey as _sv
    _orig_ground = _sv._fetch_arxiv_title
    _sv._fetch_arxiv_title = lambda aid: ''
    try:
        res = sv.build_survey('dir', real, lang='en', folder_id=folder)
        gm = res['open_gaps']
        # The fake id is stripped everywhere and recorded as a hallucination.
        assert FAKE in gm['stripped_ids'], f"fake id not recorded stripped: {gm['stripped_ids']}"
        # A pure hallucination is NOT a missing_id (missing_ids = grounded-not-harvested only).
        assert FAKE not in gm['missing_ids'], 'hallucination must NOT be a harvest-me signal'
        surfaced = sv._extract_survey_ids(gm)
        assert FAKE not in surfaced, f'fake id survived somewhere: {surfaced}'
        # cluster keeps only the real paper
        assert gm['clusters'][0]['papers'] == [real[0]], gm['clusters'][0]['papers']
        # method_matrix drops the fake-paper row entirely
        assert [m['paper'] for m in gm['method_matrix']] == [real[0]]
        # the fabricated gap (evidence all hallucination) is DROPPED; the backed one survives
        gap_ids = [g['id'] for g in gm['open_gaps']]
        assert gap_ids == ['g_real'], f'expected only g_real, got {gap_ids}'
    finally:
        _sv._fetch_arxiv_title = _orig_ground
        ra(); restore()
    _ok('NEUTER: hallucinated arXiv id stripped, fabricated gap dropped, real gap survives')


def test_verify_against_library_is_pure_and_biting():
    """Direct unit on the pure gate with an explicit lib_ids set (no DB)."""
    import lib.paper.survey as sv
    raw = {'clusters': [{'id': 'c', 'papers': ['1111.00001', '2222.00002']}],
           'method_matrix': [{'paper': '1111.00001'}, {'paper': '9999.00009'}],
           'open_gaps': [{'id': 'g', 'gap': 'x', 'evidence': ['9999.00009']}]}
    out = sv._verify_against_library(raw, lib_ids={'1111.00001'}, ground_fn=lambda aid: '')
    assert out['clusters'][0]['papers'] == ['1111.00001']
    assert [m['paper'] for m in out['method_matrix']] == ['1111.00001']
    assert out['open_gaps'] == [], 'gap backed only by a hallucinated id should drop'
    assert sorted(out['stripped_ids']) == ['2222.00002', '9999.00009']
    # counter: with both ids present, nothing strips
    out2 = sv._verify_against_library(raw, lib_ids={'1111.00001', '2222.00002', '9999.00009'},
                                      ground_fn=lambda aid: '')
    assert out2['stripped_ids'] == [] and len(out2['open_gaps']) == 1
    _ok('_verify_against_library is a pure, biting gate (version-normalized id set)')


# ── Test 3: NEUTER — fake citation flagged by audit ───────────────────────

def test_fake_citation_flagged():
    _load_app()
    import lib.paper.survey as sv
    folder = f'rf_{int(time.time())%100000}_c'
    _seed_library_paper('2305.66666', folder_id=folder)
    raw = {'clusters': [], 'method_matrix': [],
           'open_gaps': [{'id': 'g', 'gap': 'x', 'evidence': ['2305.66666']}]}
    # audit returns a suspicious card (simulating an unresolvable arXiv id in prose)
    fake_card = {'total': 1, 'counts': {'verified': 0, 'suspicious': 1, 'unverifiable': 0},
                 'suspicious': [{'identifier': '2499.00000', 'kind': 'arXiv',
                                 'reason': 'not found', 'checked': 'arxiv'}]}
    restore = _patch_synthesis('A survey citing arXiv:2499.00000 which does not exist.', raw)
    ra = _patch_citation_audit(lambda md: fake_card if '2499.00000' in md else None)
    try:
        res = sv.build_survey('dir', ['2305.66666'], lang='en', folder_id=folder)
        assert res['citation_audit'] is not None, 'suspicious citation not surfaced'
        assert res['citation_audit']['counts']['suspicious'] == 1
    finally:
        ra(); restore()
    _ok('NEUTER: a fabricated citation in the survey prose is flagged by citation_audit')


def test_clean_citations_no_card():
    _load_app()
    import lib.paper.survey as sv
    folder = f'rf_{int(time.time())%100000}_cc'
    _seed_library_paper('2305.77777', folder_id=folder)
    raw = {'open_gaps': [{'id': 'g', 'gap': 'x', 'evidence': ['2305.77777']}]}
    restore = _patch_synthesis('A clean survey citing arXiv:2305.77777.', raw)
    ra = _patch_citation_audit(lambda md: None)  # nothing suspicious
    try:
        res = sv.build_survey('dir', ['2305.77777'], lang='en', folder_id=folder)
        assert res['citation_audit'] is None, 'clean survey should carry no audit card'
    finally:
        ra(); restore()
    _ok('clean survey → citation_audit is None (card only on suspicion)')


# ── Test 4: zero-reparse + report-first input loading ─────────────────────

def test_load_inputs_never_reparses_and_prefers_report():
    _load_app()
    import lib.paper.survey as sv
    import lib.pdf_parser as pp
    folder = f'rf_{int(time.time())%100000}_r'
    # paper A has a generated report; paper B only parsed_text.
    _seed_library_paper('2305.88888', folder_id=folder, report='REPORT-A body', lang='en')
    _seed_library_paper('2401.99999', folder_id=folder, parsed_text='PARSED-B body')

    # Spy: parse_pdf must never be called by the survey input path.
    calls = {'n': 0}
    orig_parse = pp.parse_pdf
    pp.parse_pdf = lambda *a, **k: (calls.__setitem__('n', calls['n'] + 1) or {'text': '', 'totalPages': 0})
    try:
        inputs = sv._load_paper_inputs(['2305.88888', '2401.99999'], lang='en')
        assert calls['n'] == 0, f'survey input load must NOT reparse, parse_pdf called {calls["n"]}x'
        by_id = {p['arxiv_id']: p for p in inputs}
        assert by_id['2305.88888']['source'] == 'report', 'should prefer existing report'
        assert 'REPORT-A' in by_id['2305.88888']['content']
        assert by_id['2401.99999']['source'] == 'parsed_text', 'fallback to parsed_text'
        assert 'PARSED-B' in by_id['2401.99999']['content']
    finally:
        pp.parse_pdf = orig_parse
    _ok('input load reuses reports/parsed_text, prefers report, and NEVER reparses')


def test_grounded_tier_keeps_gap_and_flags_low_confidence():
    """R2/R3 seam v2: an evidence id NOT in the library but GROUNDED (exists on
    arXiv) keeps the gap alive, is reported in missing_ids (harvest-me), and —
    because the gap has ZERO library-tier evidence — is flagged low_confidence."""
    import lib.paper.survey as sv
    raw = {'clusters': [], 'method_matrix': [],
           'open_gaps': [{'id': 'g', 'gap': 'real but unharvested', 'evidence': ['2404.00001']}]}
    # 2404.00001 is not in lib_ids, but ground_fn confirms it exists → grounded.
    out = sv._verify_against_library(raw, lib_ids=set(),
                                     ground_fn=lambda aid: 'A Real Title')
    assert len(out['open_gaps']) == 1, 'grounded-only gap must survive (not dropped)'
    g = out['open_gaps'][0]
    assert g['evidence'] == ['2404.00001'], g['evidence']
    assert g['evidence_tiers'].get('2404.00001') == 'grounded', g['evidence_tiers']
    assert g['library_evidence_count'] == 0 and g['grounded_evidence_count'] == 1
    assert g['low_confidence'] is True, 'grounded-only gap must be low_confidence'
    assert '2404.00001' in out['missing_ids'], 'grounded-not-harvested → missing_ids'
    assert out['stripped_ids'] == [], 'grounded id is not a hallucination'
    _ok('grounded (not-in-library but real) evidence keeps gap + flags low_confidence + missing_ids')


def test_library_tier_gap_is_high_confidence():
    """A gap with at least one library-tier evidence id is NOT low_confidence."""
    import lib.paper.survey as sv
    raw = {'open_gaps': [{'id': 'g', 'gap': 'x',
                          'evidence': ['1111.00001', '2404.00002']}]}
    out = sv._verify_against_library(raw, lib_ids={'1111.00001'},
                                     ground_fn=lambda aid: 'Real')  # 2404 grounded
    g = out['open_gaps'][0]
    assert g['library_evidence_count'] == 1 and g['grounded_evidence_count'] == 1
    assert g['low_confidence'] is False, 'a library-backed gap is high confidence'
    assert g['evidence_tiers']['1111.00001'] == 'library'
    assert g['evidence_tiers']['2404.00002'] == 'grounded'
    _ok('a gap with any library-tier evidence is high-confidence (low_confidence=False)')


def test_dict_shaped_ids_are_extracted_not_crash():
    """E2E regression (research_f12ab5e8, 2026-07-27): a REAL model emitted
    dict-shaped entries in the gap map's id lists — ``{'id': '2305.x', 'note':
    '…'}`` in clusters.papers and ``{'arxiv_id': '2401.y'}`` in
    open_gaps.evidence — and ``_norm_id`` crashed with ``'dict' object has no
    attribute 'split'``, killing the whole survey stage after a successful LLM
    call. The gate must salvage the id from a dict, and treat a dict with no
    usable id as unverifiable (stripped), never crash."""
    import lib.paper.survey as sv
    raw = {
        'clusters': [{'id': 'c1', 'theme': 't',
                      'papers': ['1111.00001', {'id': '2222.00002', 'note': 'dict entry'},
                                 {'title': 'no id here'}]}],
        'method_matrix': [{'paper': {'arxiv_id': '1111.00001'}, 'task': 'x'}],
        'open_gaps': [{'id': 'g', 'gap': 'x',
                       'evidence': [{'arxiv_id': '2222.00002', 'role': 'proves gap'},
                                    '1111.00001']}],
    }
    out = sv._verify_against_library(
        raw, lib_ids={'1111.00001', '2222.00002'}, ground_fn=lambda aid: '')
    # Dict-carried ids are salvaged and kept; the id-less dict is stripped.
    assert out['clusters'][0]['papers'] == ['1111.00001', '2222.00002'], \
        out['clusters'][0]['papers']
    assert len(out['method_matrix']) == 1, 'dict-shaped matrix paper must survive'
    assert out['method_matrix'][0]['paper'] == '1111.00001', \
        'salvaged matrix paper id should be normalized to the bare string'
    g = out['open_gaps'][0]
    assert g['evidence'] == ['2222.00002', '1111.00001'], g['evidence']
    assert g['low_confidence'] is False
    assert out['stripped_ids'], 'the id-less dict must be recorded as unverifiable'
    # _extract_survey_ids must not crash on the same shapes either.
    ids = sv._extract_survey_ids(out)
    assert ids == {'1111.00001', '2222.00002'}
    _ok('dict-shaped ids salvaged (e2e crash class), id-less dicts stripped, no crash')


def test_grounding_tier_NEUTER():
    """NEUTER: if the 'grounded' fallback is removed (ground_fn always ''), the
    same real-but-unharvested gap is dropped as a hallucination — proving the
    grounded tier is what keeps it alive."""
    import lib.paper.survey as sv
    raw = {'open_gaps': [{'id': 'g', 'gap': 'real but unharvested', 'evidence': ['2404.00001']}]}
    out = sv._verify_against_library(raw, lib_ids=set(), ground_fn=lambda aid: '')
    assert out['open_gaps'] == [], \
        'NEUTER FAILED: without the grounded tier the unharvested gap should drop'
    assert '2404.00001' in out['stripped_ids']
    _ok('NEUTER: removing the grounded tier drops the real-but-unharvested gap (tier bites)')


def test_no_library_inputs_is_clean_failure():
    _load_app()
    import lib.paper.survey as sv
    res = sv.build_survey('a direction with no harvested papers', ['1234.00000'],
                          lang='en', folder_id='nonexistent_folder')
    assert not res['ok'] and 'harvest' in res['error'], res
    _ok('no library papers → clean ok=False with a run-harvest-first message')


def main():
    print()
    print(_color('═══ R2 Survey / Open-Gap Map Tests ═══', '36'))
    print()
    tests = [
        test_survey_schema_and_all_ids_library_verifiable,
        test_unknown_arxiv_id_is_stripped_NEUTER,
        test_verify_against_library_is_pure_and_biting,
        test_fake_citation_flagged,
        test_clean_citations_no_card,
        test_load_inputs_never_reparses_and_prefers_report,
        test_grounded_tier_keeps_gap_and_flags_low_confidence,
        test_library_tier_gap_is_high_confidence,
        test_grounding_tier_NEUTER,
        test_no_library_inputs_is_clean_failure,
        test_dict_shaped_ids_are_extracted_not_crash,
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
