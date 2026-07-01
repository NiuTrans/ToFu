#!/usr/bin/env python3
"""Headless tests for Paper Review Mode (2026-06-30).

Review Mode reuses the EXISTING paper-report engine/runtime/tools; the only
review-specific pieces are (1) a venue-aware peer-review prompt + scorecard and
(2) a COMPOSITE cache key ``review:<venue>:<uilang>`` so reviews never collide
with the plain (paper_hash, 'en') explainer report.

Coverage:
  • compound-key parsing: review:neurips:en → review prompt + real UI lang;
    plain 'en'/'zh' untouched; bad venue/uilang fall back safely.
  • prompt selection picks the right venue scorecard + UI language, and the
    anti-"AI-slop" hard constraints are actually present in the prompt.
  • cache-key non-pollution: a review key is distinct from the plain report
    key for the same paper_hash.
  • engine: a review task injects images with the REAL ui_lang (not the raw
    composite key) and persists under the composite lang key.
  • venues endpoint returns the registry.
  • route dispatch: /report/start with a review key feeds the REVIEW prompt
    (not the explainer) to the engine.
  • SOURCE-LEVEL NEGATIVE CONTROL: disabling the venue-registry lookup in
    build_review_prompt makes the venue-scorecard assertion fail; restored
    byte-identical afterwards.

dispatch_stream is mocked so the agent loop runs deterministically offline.
Run standalone: ``python3 tests/test_paper_review_mode.py``
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


REVIEW_BODY_EN = (
    '# Review\n\n## Summary\nThe paper proposes X, reaching 90% on Y.\n\n'
    '## Strengths\n- Table 3 shows +2 F1.\n\n## Weaknesses\n- §4 lacks an ablation.\n\n'
    '## Questions to the Authors\n1. What is the result with λ=0 in Eq. 4?\n\n'
    '## Reproducibility (evidence-based — verify, do not parrot)\n'
    '- Code: ❌ No — the printed URL 404s.\n\n'
    '## Related Work & Novelty Check\nPrior work [Foo 2022] already did Z.\n\n'
    '## Quantitative Scores\n- Overall Rating: 5 — borderline.\n'
)


# ─── Compound-key parsing ────────────────────────────────────────

def test_parse_plain_report_keys_untouched():
    from lib.paper import parse_report_lang
    assert parse_report_lang('en') == {'kind': 'report', 'venue': None, 'ui_lang': 'en'}
    assert parse_report_lang('zh') == {'kind': 'report', 'venue': None, 'ui_lang': 'zh'}
    # Empty / None defaults to an English report.
    assert parse_report_lang('')['kind'] == 'report'
    assert parse_report_lang(None)['ui_lang'] == 'en'
    _ok('parse_report_lang leaves plain report keys (en/zh) as kind=report')


def test_parse_review_key_decodes_venue_and_uilang():
    from lib.paper import parse_report_lang
    p = parse_report_lang('review:neurips:en')
    assert p == {'kind': 'review', 'venue': 'neurips', 'ui_lang': 'en'}, p
    pz = parse_report_lang('review:acl:zh')
    assert pz['kind'] == 'review' and pz['venue'] == 'acl' and pz['ui_lang'] == 'zh'
    _ok('parse_report_lang decodes review:<venue>:<uilang> correctly')


def test_parse_review_key_falls_back_safely():
    from lib.paper import parse_report_lang
    # Unknown venue → generic; non en/zh uilang → en; missing segment → en.
    assert parse_report_lang('review:bogus:en')['venue'] == 'generic'
    assert parse_report_lang('review:cvpr:fr')['ui_lang'] == 'en'
    assert parse_report_lang('review:iclr')['ui_lang'] == 'en'
    _ok('parse_report_lang falls back: bad venue→generic, bad uilang→en, missing→en')


# ─── Prompt + scorecard selection ───────────────────────────────

def test_review_prompt_picks_venue_scorecard():
    from lib.paper import build_review_prompt
    p = build_review_prompt('neurips', 'en')
    assert '{paper_text}' in p, 'prompt must keep the paper_text slot for .replace()'
    # NeurIPS-specific scale must be present; ACL-specific must NOT.
    assert 'Soundness' in p and '1–10' in p, 'NeurIPS scorecard missing'
    assert 'Excitement' not in p, 'ACL-only dimension leaked into NeurIPS prompt'
    # CVPR uses a recommendation band, not a 1–10 rating.
    pc = build_review_prompt('cvpr', 'en')
    assert 'Strong Accept' in pc and '1–10' not in pc, 'CVPR scorecard wrong'
    # ACL/ARR has Excitement + Reproducibility.
    pa = build_review_prompt('acl', 'en')
    assert 'Excitement' in pa and 'Reproducibility' in pa
    _ok('build_review_prompt selects the venue-authentic scorecard (NeurIPS/CVPR/ACL differ)')


def test_review_prompt_ui_language():
    from lib.paper import build_review_prompt
    pz = build_review_prompt('iclr', 'zh')
    assert '评审意见' in pz and '量化评分' in pz, 'Chinese review prompt malformed'
    pe = build_review_prompt('iclr', 'en')
    assert '# Review' in pe and 'Quantitative Scores' in pe
    _ok('build_review_prompt honours UI language (zh vs en templates)')


def test_review_prompt_has_anti_slop_constraints():
    """The anti-'AI-slop' rules must be HARD constraints in the prompt, not vibes."""
    from lib.paper import build_review_prompt
    p = build_review_prompt('neurips', 'en').lower()
    # Each strength/weakness anchored to concrete evidence.
    assert 'anchored to concrete evidence' in p
    # Summarize once; forbidden to re-narrate elsewhere.
    assert 'summarize the paper once' in p
    # Banned vague phrases.
    assert 'significantly improves' in p and 'banned phrases' in p
    # Actionable reviewer questions.
    assert 'actionable' in p
    # Rating + confidence must be justified.
    assert 'rating and confidence must be justified' in p
    # Reproducibility must be verified via fetch_url, not parroted.
    assert 'fetch_url' in p and 'not proof' in p
    pz = build_review_prompt('neurips', 'zh')
    assert '禁止注水' in pz and '锚定' in pz and 'fetch_url' in pz
    _ok('review prompt encodes anti-slop HARD constraints (anchored evidence, banned phrases, fetch_url verify)')


# ─── Cache-key non-pollution ─────────────────────────────────────

def test_make_review_lang_distinct_from_report_key():
    from lib.paper import make_review_lang
    k = make_review_lang('neurips', 'en')
    assert k == 'review:neurips:en'
    # Crucially distinct from the plain report key for the SAME paper_hash:
    assert k != 'en' and k != 'zh'
    # generic + bad inputs are normalized, never raising.
    assert make_review_lang('nope', 'fr') == 'review:generic:en'
    _ok("make_review_lang yields a key distinct from the plain report 'en'/'zh' cache key")


# ─── Engine: review task uses real ui_lang for image injection ───

def _patch_dispatch(plan):
    import lib.paper.report_engine as re_mod
    seq = list(plan)
    cap = {'msgs': []}

    def _fake(messages, on_content=None, on_thinking=None, **kw):
        cap['msgs'].append(messages)
        content, tool_calls = seq.pop(0)
        if content and on_content:
            on_content(content)
        msg = {'role': 'assistant', 'content': content, 'tool_calls': tool_calls}
        return msg, ('tool_calls' if tool_calls else 'stop'), {'_dispatch': {}}

    re_mod.dispatch_stream = _fake
    return cap


def test_engine_review_injects_with_real_uilang_and_persists_composite_key():
    """A review task: image injection uses ui_lang='zh' (not the composite key),
    and the persisted/enriched body uses the real language appendix."""
    import lib.paper.report_engine as re_mod
    from lib.paper import _new_report_task, make_review_lang
    orig = re_mod.dispatch_stream
    inj_calls = {'langs': []}
    orig_inject = re_mod._inject_images_into_report

    def _spy_inject(report_md, images, lang='en', appendix=True):
        inj_calls['langs'].append(lang)
        inj_calls.setdefault('appendix', []).append(appendix)
        return orig_inject(report_md, images, lang=lang, appendix=appendix)

    _patch_dispatch([(REVIEW_BODY_EN, [])])
    re_mod._inject_images_into_report = _spy_inject
    try:
        lang_key = make_review_lang('neurips', 'zh')   # review:neurips:zh
        task = _new_report_task('rvw_eng_1', 'phashrvw000000000000000000000001',
                                lang_key, None, client_title='Paper', ui_lang='zh')
        re_mod._run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper'},
        ], [])
        assert task['status'] == 'done', task.get('error')
        # The engine stored the COMPOSITE key as lang (so the DB row is keyed
        # review:neurips:zh — not plain 'zh').
        assert task['lang'] == 'review:neurips:zh'
        # Image injection saw the REAL ui_lang 'zh', NOT the composite key.
        assert inj_calls['langs'] == ['zh'], inj_calls['langs']
        # ...and the figure APPENDIX is suppressed for a review (no padding).
        assert inj_calls.get('appendix') == [False], inj_calls.get('appendix')
    finally:
        re_mod._inject_images_into_report = orig_inject
        re_mod.dispatch_stream = orig
    _ok('engine: review task injects with real ui_lang, composite key, appendix suppressed')


def test_review_appendix_suppressed_report_keeps_it():
    """The figure-appendix gallery is the explainer-report behaviour; a review
    must NOT be padded with every extracted figure. Same manifest, two modes."""
    from lib.paper.images import _inject_images_into_report
    # An unreferenced figure (the body never says "Figure 1").
    images = [{'url': '/api/paper/images/ph/fig_01.png', 'caption': 'Figure 1: architecture', 'page': 2}]
    body = '# Review\n\n## Summary\nNo figure is cited here.\n'
    report_out = _inject_images_into_report(body, images, lang='en', appendix=True)
    review_out = _inject_images_into_report(body, images, lang='en', appendix=False)
    # Report mode appends the appendix gallery + the image; review mode does not.
    assert 'Appendix' in report_out and 'fig_01.png' in report_out, 'report should keep appendix'
    assert 'Appendix' not in review_out and 'fig_01.png' not in review_out, \
        'review must NOT append the unreferenced-figure gallery'
    # But an INLINE-cited figure is still placed in BOTH modes.
    body_cited = '# Review\n\n## Strengths\nAs Figure 1 shows, the design is sound.\n'
    review_cited = _inject_images_into_report(body_cited, images, lang='en', appendix=False)
    assert 'fig_01.png' in review_cited, 'a cited figure must still be placed inline in review mode'
    _ok('review suppresses the figure appendix but still places inline-cited figures')


def test_review_and_report_rows_coexist_for_same_paper_hash():
    """DB-level isolation: persisting a review under review:<venue>:en and a
    plain report under 'en' for the SAME paper_hash yields TWO distinct rows
    (composite PK (paper_hash, lang)), neither clobbering the other."""
    import time as _time

    from lib.database import get_thread_db
    from lib.database._core_schema import PAPER_REPORTS, upsert

    # paper_reports is a core table, bootstrapped when the app is constructed.
    # Load the app first so the table exists regardless of test ordering /
    # active backend (PG or SQLite).
    _load_app()
    db = get_thread_db()
    phash = 'phashiso00000000000000000000isolation'[:32]
    now = int(_time.time())
    upsert(db, PAPER_REPORTS, {
        'paper_hash': phash, 'lang': 'en', 'report': 'PLAIN REPORT BODY',
        'model': 'm', 'meta': '{}', 'created_at': now,
    }, retry=True)
    upsert(db, PAPER_REPORTS, {
        'paper_hash': phash, 'lang': 'review:neurips:en', 'report': 'REVIEW BODY',
        'model': 'm', 'meta': '{}', 'created_at': now,
    }, retry=True)
    rep = db.execute('SELECT report FROM paper_reports WHERE paper_hash=? AND lang=?',
                     (phash, 'en')).fetchone()
    rev = db.execute('SELECT report FROM paper_reports WHERE paper_hash=? AND lang=?',
                     (phash, 'review:neurips:en')).fetchone()
    assert rep and rep['report'] == 'PLAIN REPORT BODY', 'plain report clobbered by review'
    assert rev and rev['report'] == 'REVIEW BODY', 'review row missing/clobbered'
    cnt = db.execute('SELECT COUNT(*) AS c FROM paper_reports WHERE paper_hash=?',
                     (phash,)).fetchone()
    assert cnt['c'] == 2, f'expected 2 distinct rows, got {cnt["c"]}'
    # Cleanup.
    db.execute('DELETE FROM paper_reports WHERE paper_hash=?', (phash,))
    db.commit()
    _ok('DB isolation: review row and plain-report row coexist for the same paper_hash')


# ─── Venues endpoint + route dispatch (real Quart app) ───────────

def _load_app():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)), 'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    return mod.app


def test_venues_endpoint_and_review_route_dispatch():
    """/review/venues lists the registry; /report/start with a review key feeds
    the REVIEW prompt (not the explainer report prompt) to the engine."""
    import asyncio
    import lib.paper.report_engine as re_mod
    from lib.paper import _report_runtime

    app = _load_app()
    orig = re_mod.dispatch_stream
    cap = {'system': None, 'user': None}

    def _fake(messages, on_content=None, on_thinking=None, **kw):
        # Capture the prompt the route built for the review task.
        if cap['system'] is None:
            cap['system'] = messages[0]['content']
            cap['user'] = messages[1]['content'] if len(messages) > 1 else ''
        if on_content:
            on_content(REVIEW_BODY_EN)
        return ({'role': 'assistant', 'content': REVIEW_BODY_EN, 'tool_calls': []},
                'stop', {'_dispatch': {}})

    re_mod.dispatch_stream = _fake

    async def _t():
        async with app.test_client() as client:
            # venues
            rv = await client.get('/api/v1/paper/review/venues')
            assert rv.status_code == 200
            vd = await rv.get_json()
            assert vd['ok'] and any(v['key'] == 'neurips' for v in vd['venues'])

            # start a REVIEW task via the composite key
            paper = ('# Intro\nWe propose a transformer variant with 90% accuracy '
                     'on GLUE.\n\n# Method\nIt uses attention.\n\n# Results\nIt wins.') * 3
            r = await client.post('/api/v1/paper/report/start', json={
                'paper_text': paper,
                'lang': 'review:neurips:en',
                'force': True,
            })
            assert r.status_code == 200, r.status_code
            data = await r.get_json()
            assert data['ok'] and data['task_id'], data
            assert data['task_id'].startswith('rvw_'), data['task_id']
            tid = data['task_id']

            for _ in range(60):
                t = _report_runtime.get(tid)
                if t and t['status'] in ('done', 'error'):
                    break
                await asyncio.sleep(0.05)

            # The engine must have received the REVIEW prompt, not the explainer.
            assert cap['system'] is not None, 'dispatch never called'
            assert '# Review' in cap['user'], 'review structure missing from prompt'
            assert 'peer reviewer' in cap['user'].lower(), 'not a peer-review prompt'
            assert 'Soundness' in cap['user'], 'NeurIPS scorecard missing from prompt'
            # Anti-slop discipline present in the system tool-instruction.
            assert 'Novelty check' in cap['system'] or 'novelty' in cap['system'].lower()

    try:
        asyncio.run(_t())
    finally:
        re_mod.dispatch_stream = orig
    _ok('venues endpoint lists registry; review key routes the REVIEW prompt to the engine')


# ─── SOURCE-LEVEL NEGATIVE CONTROL ──────────────────────────────

def test_source_level_negative_control_venue_registry():
    """Prove the venue-registry lookup in build_review_prompt is load-bearing.

    Temporarily monkeypatch REVIEW_VENUES so the NeurIPS entry's scorecard is
    blanked → the NeurIPS-specific assertion must FAIL. Then restore and prove
    it passes again. This mirrors a source-level negative control without
    editing the .py on disk (we mutate + restore the in-memory registry,
    asserting byte-identical restoration).
    """
    import copy
    import lib.paper.review as rv
    saved = copy.deepcopy(rv.REVIEW_VENUES['neurips'])

    # 1) Break the registry: blank NeurIPS's EN scorecard.
    rv.REVIEW_VENUES['neurips']['scorecard_en'] = ''
    broken = rv.build_review_prompt('neurips', 'en')
    assert 'Soundness' not in broken or '1–10' not in broken, \
        'blanking the scorecard should remove the NeurIPS-specific scale'

    # 2) Restore byte-identical and re-prove the positive assertion.
    rv.REVIEW_VENUES['neurips'] = saved
    fixed = rv.build_review_prompt('neurips', 'en')
    assert 'Soundness' in fixed and '1–10' in fixed, \
        'restored registry must reproduce the NeurIPS scorecard'
    # Confirm exact restoration.
    assert rv.REVIEW_VENUES['neurips']['scorecard_en'] == saved['scorecard_en']
    _ok('source-level negative control: venue registry is load-bearing (break→fail, restore→pass)')


def main():
    print()
    print(_color('═══ Paper Review Mode Tests ═══', '36'))
    print()
    tests = [
        test_parse_plain_report_keys_untouched,
        test_parse_review_key_decodes_venue_and_uilang,
        test_parse_review_key_falls_back_safely,
        test_review_prompt_picks_venue_scorecard,
        test_review_prompt_ui_language,
        test_review_prompt_has_anti_slop_constraints,
        test_make_review_lang_distinct_from_report_key,
        test_engine_review_injects_with_real_uilang_and_persists_composite_key,
        test_review_appendix_suppressed_report_keeps_it,
        test_review_and_report_rows_coexist_for_same_paper_hash,
        test_venues_endpoint_and_review_route_dispatch,
        test_source_level_negative_control_venue_registry,
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
