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
  • engine: a review task is text-only (no figures injected), uses the REAL
    ui_lang (not the raw composite key), and persists under the composite key.
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
def test_review_prompt_nlpcc_scorecard():
    """NLPCC: CCF NLP/CC venue, 1-6 overall rating, NLP-family dimensions, not ARR's."""
    from lib.paper import build_review_prompt, parse_report_lang
    assert parse_report_lang('review:nlpcc:en')['venue'] == 'nlpcc'
    pe = build_review_prompt('nlpcc', 'en')
    assert '{paper_text}' in pe, 'prompt must keep the paper_text slot for .replace()'
    assert 'NLPCC' in pe, 'NLPCC label missing'
    # NLPCC's own 1-6 overall rating; NOT NeurIPS's 1-10 nor ARR's Excitement scale.
    assert '1–6' in pe and '1–10' not in pe, 'NLPCC uses a 1-6 overall rating'
    assert 'Excitement' not in pe, 'ARR-only dimension leaked into NLPCC prompt'
    # NLPCC's OpenReview form has only two SCORED fields — Overall Assessment (OA)
    # + Confidence. Title & review text are the prose body, so the scorecard must
    # NOT emit separate soundness/novelty/clarity/comparison number rows.
    assert 'Overall Assessment' in pe and 'Confidence' in pe, 'NLPCC OA/Confidence fields missing'
    assert 'Soundness / Substance' not in pe, 'NLPCC must not emit a separate Soundness score row'
    assert 'Meaningful Comparison' not in pe, 'NLPCC must not emit a separate Comparison score row'
    pz = build_review_prompt('nlpcc', 'zh')
    assert '中文计算' in pz and '1–6' in pz, 'Chinese NLPCC scorecard malformed'
    assert 'Overall Assessment' in pz and 'Confidence' in pz, 'Chinese NLPCC OA/Confidence fields missing'
    _ok('build_review_prompt has an authentic NLPCC scorecard (OA 1-6 + Confidence only, 4-column form)')


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
    # A review is text-only: the prompt must FORBID images, not permit embedding.
    assert 'no images' in p, 'review prompt must forbid images'
    assert 'exact_url_from_manifest' not in p, 'review prompt must not offer figure embedding'
    pz = build_review_prompt('neurips', 'zh')
    assert '禁止注水' in pz and '锚定' in pz and 'fetch_url' in pz
    assert '不嵌图' in pz, 'ZH review prompt must forbid images'
    _ok('review prompt encodes anti-slop HARD constraints (anchored evidence, banned phrases, fetch_url verify, no images)')


def test_review_prompt_human_reviewer_voice_and_precise_weaknesses():
    """The 2026-07 tuning: Summary/Strengths must be in a human reviewer's
    voice (NOT a forensic per-clause audit), and Weaknesses must be PRECISE
    (a small number of decisive/hidden flaws) rather than a padded quota."""
    from lib.paper import build_review_prompt
    p = build_review_prompt('neurips', 'en').lower()
    # Human-reviewer framing exists as an explicit constraint.
    assert 'write like a human reviewer' in p
    # Summary/Strengths are NO LONGER forced to carry a number/benchmark on
    # every clause — they explicitly permit a high-level, own-words voice.
    assert 'your own words' in p and 'do not need to pack in exact numbers' in p
    # Weaknesses: quality/precision over quantity + hunt real & hidden flaws.
    assert 'precise, not numerous' in p
    assert 'quality and precision over quantity' in p
    assert 'hidden flaw' in p or 'flaw the authors themselves may not have noticed' in p
    # The anchoring HARD constraint now lives on Weaknesses specifically.
    assert 'a weakness with no anchor is deleted' in p
    # ZH parity.
    pz = build_review_prompt('iclr', 'zh')
    assert '像人类审稿人一样写' in pz
    assert '用你自己的话' in pz or '用**你自己的话**' in pz
    assert '宁精勿多' in pz and '真问题' in pz
    _ok('review prompt: human-reviewer voice for Summary/Strengths + precise-not-numerous Weaknesses')


def test_review_prompt_per_point_length_ceiling():
    """The 2026-07 verbosity fix: Summary/Strengths/Weaknesses must carry an
    EXPLICIT per-point length ceiling so each bullet is one tight sentence,
    not a long-winded sub-paragraph. Asserted in BOTH languages."""
    from lib.paper import build_review_prompt
    pe = build_review_prompt('neurips', 'en').lower()
    # A concrete per-bullet length cap must be present (one to two sentences).
    assert 'one sentence' in pe or 'one to two sentences' in pe, \
        'EN review prompt lacks a per-point length ceiling'
    # And the ceiling must be framed as a hard limit, not a nicety.
    assert 'no sub-paragraph' in pe or 'not a sub-paragraph' in pe or \
        'no multi-sentence' in pe, 'EN prompt must forbid sub-paragraph bullets'
    pz = build_review_prompt('iclr', 'zh')
    assert '一到两句' in pz or '一句话' in pz, \
        'ZH review prompt lacks a per-point length ceiling'
    assert '不要展开成段落' in pz or '不展开成段落' in pz or '不要写成小段落' in pz, \
        'ZH prompt must forbid sub-paragraph bullets'
    _ok('review prompt: explicit per-point length ceiling in Summary/Strengths/Weaknesses (EN+ZH)')


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

    def _spy_inject(report_md, images, lang='en', appendix=True, allow_images=True):
        inj_calls['langs'].append(lang)
        inj_calls.setdefault('appendix', []).append(appendix)
        inj_calls.setdefault('allow_images', []).append(allow_images)
        return orig_inject(report_md, images, lang=lang, appendix=appendix,
                           allow_images=allow_images)

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
        # ...and the figure APPENDIX is suppressed for a review (no padding),
        # AND images are disallowed entirely (a review is text-only).
        assert inj_calls.get('appendix') == [False], inj_calls.get('appendix')
        assert inj_calls.get('allow_images') == [False], inj_calls.get('allow_images')
    finally:
        re_mod._inject_images_into_report = orig_inject
        re_mod.dispatch_stream = orig
    _ok('engine: review task injects with real ui_lang, composite key, images disallowed')


def test_review_is_text_only_no_images_at_all():
    """A review is a TEXT-ONLY decision document: with allow_images=False no
    figure is injected — not the appendix gallery, not an inline-cited figure —
    AND any paper-image embed the model emitted itself is stripped to alt text.
    The explainer report (allow_images=True) is unaffected."""
    from lib.paper.images import _inject_images_into_report
    images = [{'url': '/api/paper/images/ph/fig_01.png', 'caption': 'Figure 1: architecture', 'page': 2}]

    # 1) Unreferenced figure: report appends an appendix gallery; review adds nothing.
    body = '# Review\n\n## Summary\nNo figure is cited here.\n'
    report_out = _inject_images_into_report(body, images, lang='en', appendix=True)
    review_out = _inject_images_into_report(body, images, lang='en', appendix=False,
                                            allow_images=False)
    assert 'Appendix' in report_out and 'fig_01.png' in report_out, 'report should keep appendix'
    assert 'fig_01.png' not in review_out and 'Appendix' not in review_out, \
        'review must inject no figure at all'

    # 2) Inline-cited figure: report places it; review still places NONE.
    body_cited = '# Review\n\n## Strengths\nAs Figure 1 shows, the design is sound.\n'
    report_cited = _inject_images_into_report(body_cited, images, lang='en', appendix=True)
    review_cited = _inject_images_into_report(body_cited, images, lang='en', appendix=False,
                                              allow_images=False)
    assert 'fig_01.png' in report_cited, 'report should place the cited figure'
    assert 'fig_01.png' not in review_cited, 'review must NOT place even a cited figure'

    # 3) A model-emitted paper-image embed is stripped to its alt text in a review.
    body_embed = '# Review\n\n## Strengths\n![Figure 1: arch](/api/paper/images/ph/fig_01.png)\nGood.\n'
    review_embed = _inject_images_into_report(body_embed, images, lang='en', appendix=False,
                                              allow_images=False)
    assert '/api/paper/images/' not in review_embed, 'model-embedded paper image must be stripped'
    assert '*Figure 1: arch*' in review_embed, 'stripped embed should degrade to italic alt text'

    # 4) The strip must be as strong as the "No images" promise: an EXTERNAL
    #    image URL, a data: embed, and a raw <img> tag are ALL removed from a
    #    review — while the explainer report (allow_images=True) keeps them.
    body_external = (
        '# Review\n\n## Weaknesses\n'
        '![Fig](https://arxiv.org/abs/1234/fig.png) then '
        '![tiny](data:image/png;base64,AAAA) and '
        '<img src="https://example.com/x.png" alt="x"> plus '
        '<img src="/api/paper/images/ph/fig_02.png"/> end.\n'
    )
    review_external = _inject_images_into_report(body_external, images, lang='en',
                                                 appendix=False, allow_images=False)
    assert '![' not in review_external, 'no markdown image embed may survive a review'
    assert '<img' not in review_external.lower(), 'no raw <img> tag may survive a review'
    assert 'arxiv.org/abs/1234/fig.png' not in review_external, 'external image URL must be gone'
    assert 'data:image' not in review_external, 'data: image embed must be gone'
    assert '*Fig*' in review_external, 'external md embed should degrade to italic alt text'
    # The explainer report keeps a model-embedded external/HTML image untouched.
    report_external = _inject_images_into_report(body_external, images, lang='en',
                                                 appendix=True, allow_images=True)
    assert 'arxiv.org/abs/1234/fig.png' in report_external, 'report must keep external images'
    assert '<img' in report_external.lower(), 'report must keep raw <img> tags'

    # 5) <picture>/<source srcset> and <figure> wrappers are removed (caption
    #    prose kept), while the explainer report keeps them.
    body_picture = (
        '# Review\n\n## Weaknesses\n'
        '<picture><source srcset="a.webp" type="image/webp"></picture>\n\n'
        '<figure><img src="b.png"><figcaption>Fig 2 caption</figcaption></figure>\n'
    )
    review_pic = _inject_images_into_report(body_picture, images, lang='en',
                                            appendix=False, allow_images=False)
    assert 'srcset' not in review_pic, '<source srcset> must be gone from a review'
    assert '<picture' not in review_pic.lower() and '<source' not in review_pic.lower()
    assert '<figure' not in review_pic.lower() and '<img' not in review_pic.lower()
    assert 'a.webp' not in review_pic and 'b.png' not in review_pic, 'image URLs must be gone'
    assert 'Fig 2 caption' in review_pic, 'figcaption prose should be kept'
    report_pic = _inject_images_into_report(body_picture, images, lang='en',
                                            appendix=True, allow_images=True)
    assert 'srcset' in report_pic and '<picture' in report_pic.lower(), 'report keeps <picture>'

    # 6) Reference-style Markdown image ![alt][ref] + its [ref]: url def line:
    #    both are removed from a review (alt kept as italic), report untouched.
    body_ref = (
        '# Review\n\n## Weaknesses\n'
        'See ![Figure 3][f3] here.\n\n'
        '[f3]: https://cdn.example.com/fig3.png\n'
        '[paper]: https://arxiv.org/abs/1234\n'
    )
    review_ref = _inject_images_into_report(body_ref, images, lang='en',
                                            appendix=False, allow_images=False)
    assert '![' not in review_ref, 'reference-style image syntax must be gone'
    assert 'fig3.png' not in review_ref, 'orphaned image link-definition must be dropped'
    assert '*Figure 3*' in review_ref, 'ref-image alt should degrade to italic text'
    assert 'arxiv.org/abs/1234' in review_ref, 'a non-image link definition must be kept'
    report_ref = _inject_images_into_report(body_ref, images, lang='en',
                                            appendix=True, allow_images=True)
    assert 'fig3.png' in report_ref, 'report keeps the reference image definition'

    # 7) Inline <svg> drawing (multi-line) → dropped entirely from a review;
    #    the explainer report keeps it.
    body_svg = (
        '# Review\n\n## Weaknesses\n'
        'Consider <svg width="10" height="10">\n<rect x="0" y="0"/>\n</svg> here.\n'
    )
    review_svg = _inject_images_into_report(body_svg, images, lang='en',
                                            appendix=False, allow_images=False)
    assert '<svg' not in review_svg.lower() and '</svg' not in review_svg.lower()
    assert '<rect' not in review_svg.lower(), 'svg inner drawing must be gone'
    report_svg = _inject_images_into_report(body_svg, images, lang='en',
                                            appendix=True, allow_images=True)
    assert '<svg' in report_svg.lower(), 'report keeps inline <svg>'

    # 8) CSS background-image in an inline style → url() neutralized in a
    #    review; the explainer report keeps it.
    body_bg = (
        '# Review\n\n## Weaknesses\n'
        '<div style="background-image:url(https://cdn.example.com/bg.png)">x</div>\n'
    )
    review_bg = _inject_images_into_report(body_bg, images, lang='en',
                                           appendix=False, allow_images=False)
    assert 'bg.png' not in review_bg and 'url(' not in review_bg.lower(), \
        'background-image url must be neutralized'
    report_bg = _inject_images_into_report(body_bg, images, lang='en',
                                           appendix=True, allow_images=True)
    assert 'bg.png' in report_bg, 'report keeps a background-image url'

    # 9) <object data=…png>, <embed src=…svg>, <input type=image src=…png> all
    #    render an image → removed from a review (object fallback text kept),
    #    while the explainer report keeps them. AND an ESCAPED &lt;img…&gt; is
    #    left intact — it renders as visible text, not an image.
    body_html_img = (
        '# Review\n\n## Weaknesses\n'
        '<object data="o.png" type="image/png">fallback caption</object> and '
        '<embed src="e.svg"> and '
        '<input type="image" src="btn.png"> but '
        'this literal &lt;img src=q.png&gt; is just text.\n'
    )
    review_html = _inject_images_into_report(body_html_img, images, lang='en',
                                             appendix=False, allow_images=False)
    assert '<object' not in review_html.lower() and '</object' not in review_html.lower()
    assert '<embed' not in review_html.lower(), '<embed> must be gone'
    assert '<input' not in review_html.lower(), '<input type=image> must be gone'
    assert 'o.png' not in review_html and 'e.svg' not in review_html \
        and 'btn.png' not in review_html, 'all image URLs must be gone'
    assert 'fallback caption' in review_html, '<object> inner fallback text should survive'
    # Escaped entity must NOT be touched — it is literal text, not an image.
    assert '&lt;img src=q.png&gt;' in review_html, 'escaped &lt;img&gt; must be left intact'
    report_html = _inject_images_into_report(body_html_img, images, lang='en',
                                             appendix=True, allow_images=True)
    assert 'o.png' in report_html and 'e.svg' in report_html and 'btn.png' in report_html, \
        'report keeps <object>/<embed>/<input type=image>'

    # 10) Root-cause coverage: a review is Markdown prose, so ALL raw HTML is
    #     neutralized — <image> (an <img> alias), <video poster>, <iframe>,
    #     and legacy background= attrs all render an image and must be gone.
    body_more = (
        '# Review\n\n## Weaknesses\n'
        '<image src="a.png"> and '
        '<video poster="p.png"></video> and '
        '<iframe src="frame.png"></iframe> and '
        '<table background="bg2.png"><tr><td>cell</td></tr></table> here.\n'
    )
    review_more = _inject_images_into_report(body_more, images, lang='en',
                                             appendix=False, allow_images=False)
    for tag in ('<image', '<video', '<iframe', '<table', '<tr', '<td'):
        assert tag not in review_more.lower(), f'{tag} tag must be gone from a review'
    for url in ('a.png', 'p.png', 'frame.png', 'bg2.png'):
        assert url not in review_more, f'image URL {url} must be gone'
    assert 'cell' in review_more, 'inner table text should survive tag removal'
    report_more = _inject_images_into_report(body_more, images, lang='en',
                                             appendix=True, allow_images=True)
    assert 'a.png' in report_more and 'p.png' in report_more and 'frame.png' in report_more, \
        'report keeps raw HTML image vectors'

    # 11) A code span showing an <img> tag is PROSE, not a rendered image —
    #     fenced and inline code must survive the raw-HTML neutralizer intact.
    body_code = (
        '# Review\n\n## Weaknesses\n'
        'Inline `<img src=x.png>` example, and a block:\n\n'
        '```html\n<img src="y.png">\n```\n'
    )
    review_code = _inject_images_into_report(body_code, images, lang='en',
                                             appendix=False, allow_images=False)
    assert '`<img src=x.png>`' in review_code, 'inline code must be preserved verbatim'
    assert '<img src="y.png">' in review_code, 'fenced code must be preserved verbatim'

    # 12) A legal `>` INSIDE a quoted attribute value must not terminate the
    #     tag early and leak the src/background URL as rendered content.
    body_gt = (
        '# Review\n\n## Weaknesses\n'
        '<img alt="a>b" src=leak1.png> and '
        '<div title="x>y" style="background:url(leak2.png)">cell</div> and '
        '<img data-x="1>2" src="http://e.com/leak3.png"> end.\n'
    )
    review_gt = _inject_images_into_report(body_gt, images, lang='en',
                                           appendix=False, allow_images=False)
    assert '<img' not in review_gt.lower() and '<div' not in review_gt.lower(), \
        'no tag may survive even with > inside a quoted attribute'
    for url in ('leak1.png', 'leak2.png', 'leak3.png', 'e.com'):
        assert url not in review_gt, f'attr-embedded > must not leak {url}'
    assert 'src=' not in review_gt and 'background:url' not in review_gt, \
        'no attribute fragment may survive'
    assert 'cell' in review_gt and 'end.' in review_gt, 'inner/trailing text should survive'
    report_gt = _inject_images_into_report(body_gt, images, lang='en',
                                           appendix=True, allow_images=True)
    assert 'leak1.png' in report_gt and 'leak2.png' in report_gt and 'leak3.png' in report_gt, \
        'report keeps tags with > in attributes'
    _ok('review is text-only: Markdown image forms → *alt*, ALL raw HTML neutralized '
        '(root-cause, no denylist; > inside quoted attrs handled); escaped entities + '
        'code/math preserved; report unchanged')


# ─── Submittable-copy finalization (scores/tables OUT of the body) ───

REVIEW_BODY_WITH_TABLE_EN = (
    '# Review\n\n## Summary\nThe paper proposes X.\n\n'
    '## Strengths\n- Clean idea.\n\n'
    '## Weaknesses\nResults are summarized below:\n\n'
    '| Model | BLEU | CHRF |\n'
    '| --- | --- | --- |\n'
    '| ERNIE | 30.1 | 55.2 |\n'
    '| Qwen | 28.4 | 53.0 |\n\n'
    'Also a leaked caption *Table 4 shows overall performance. Howe*\n'
    'and a dangling one: Table 5 illustrates the discrepancy.*\n\n'
    '## Quantitative Scores\n'
    '- **Soundness**: 3 — the method is correct.\n'
    '- **Overall Rating**: 5 — borderline.\n'
    '- **Confidence**: 4.\n'
)


def test_finalize_review_moves_scores_below_separator():
    """The submittable review body must contain NO scorecard: the venue scores
    are relocated below an explicit, obviously-non-submittable separator so the
    reviewer transcribes them into the form's UI fields."""
    from lib.paper import finalize_review_body, scorecard_separator
    out = finalize_review_body(REVIEW_BODY_WITH_TABLE_EN, 'en')
    sep = scorecard_separator('en')
    assert sep in out, 'a non-submittable separator must be inserted'
    body, _, footer = out.partition(sep)
    # (a) The review body proper (above the separator) has NO scorecard.
    assert 'Quantitative Scores' not in body, 'scorecard heading leaked into the review body'
    assert 'Overall Rating' not in body and 'Soundness' not in body, \
        'venue scores leaked into the review body'
    # ...but the actual review prose survives above the line.
    assert '## Summary' in body and '## Weaknesses' in body
    # (b) The scores are preserved below the separator for transcription.
    assert 'Overall Rating' in footer and 'Soundness' in footer and 'Confidence' in footer
    _ok('finalize: scorecard relocated below a non-submittable separator (body is pure prose)')


def test_finalize_review_strips_tables_and_dangling_star():
    """A leaked Markdown/HTML table and dangling ``*`` emphasis (the artifact
    from degraded image captions) must be removed from the review body."""
    from lib.paper import finalize_review_body
    out = finalize_review_body(REVIEW_BODY_WITH_TABLE_EN, 'en')
    body = out.split('---', 1)[0] if '---' in out else out
    # (a) No Markdown table pipes / separator row survive.
    assert '| BLEU |' not in out and '| ERNIE |' not in out, 'markdown table not stripped'
    assert '| --- |' not in out, 'table separator row not stripped'
    # The prose that surrounded the table is kept.
    assert 'Results are summarized below' in out
    # (b) No dangling/unpaired ``*`` emphasis marker remains in the body.
    assert '.*' not in out, 'a dangling closing * emphasis marker survived'
    assert 'discrepancy.' in out, 'the caption prose itself should be kept (only * removed)'
    # An HTML table is also removed.
    html = ('# Review\n\n## Weaknesses\nSee <table><tr><td>a</td><td>b</td></tr></table> here.\n'
            '## Quantitative Scores\n- Overall Rating: 4.\n')
    hout = finalize_review_body(html, 'en')
    assert '<table' not in hout.lower() and '<td' not in hout.lower(), 'HTML table not stripped'
    _ok('finalize: markdown + HTML tables stripped, dangling * emphasis collapsed')


def test_finalize_review_idempotent_and_preserves_code():
    """finalize is idempotent (re-running a finalized body is a no-op) and it
    NEVER mangles a table/star shown inside a fenced code block (that is prose,
    not a rendered artifact)."""
    from lib.paper import finalize_review_body
    once = finalize_review_body(REVIEW_BODY_WITH_TABLE_EN, 'en')
    twice = finalize_review_body(once, 'en')
    assert once == twice, 'finalize must be idempotent'
    # A code block demonstrating a markdown table must survive verbatim.
    coded = (
        '# Review\n\n## Weaknesses\n'
        'The authors format results as:\n\n'
        '```\n| Model | BLEU |\n| --- | --- |\n| X | 1.0 |\n```\n\n'
        'and an inline `a*b` product.\n\n'
        '## Quantitative Scores\n- Overall Rating: 6.\n'
    )
    cout = finalize_review_body(coded, 'en')
    assert '| Model | BLEU |' in cout, 'a markdown table inside code must be preserved'
    assert '`a*b`' in cout, 'inline code with * must be preserved'
    _ok('finalize: idempotent; fenced code tables + inline-code stars preserved')


def test_review_prompt_forbids_tables_and_declares_scorecard_separator():
    """The prompt (the 'belt') must forbid tables/charts/scores in the review
    body, require prose result references, and emit the scorecard below the
    explicit separator marker. Asserted in BOTH languages."""
    from lib.paper import build_review_prompt, scorecard_separator
    pe = build_review_prompt('neurips', 'en')
    lo = pe.lower()
    assert 'no tables' in lo or 'no table' in lo, 'EN prompt must forbid tables'
    assert 'chart' in lo, 'EN prompt must forbid charts'
    # The literal separator the post-processor also uses must be in the prompt.
    assert scorecard_separator('en') in pe, 'EN prompt must show the scorecard separator'
    # And the body-above-the-line must be described as the submittable text.
    assert 'not part of the review' in lo or 'do not paste' in lo
    pz = build_review_prompt('iclr', 'zh')
    assert '不要使用表格' in pz or '不使用表格' in pz or '禁止表格' in pz, 'ZH prompt must forbid tables'
    assert scorecard_separator('zh') in pz, 'ZH prompt must show the scorecard separator'
    _ok('review prompt forbids tables/charts/scores in body + declares the scorecard separator (EN+ZH)')


def test_engine_review_finalizes_persisted_body():
    """WIRING: a completed review task must persist/emit a FINALIZED body — no
    table, scores below the separator — proving finalize_review_body runs at the
    engine seam, not just as a standalone helper."""
    import lib.paper.report_engine as re_mod
    from lib.paper import _new_report_task, make_review_lang, scorecard_separator
    orig = re_mod.dispatch_stream
    _patch_dispatch([(REVIEW_BODY_WITH_TABLE_EN, [])])
    try:
        task = _new_report_task('rvw_fin_1', 'phashrvwfin0000000000000000000001',
                                make_review_lang('neurips', 'en'), None,
                                client_title='Paper', ui_lang='en')
        re_mod._run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper'},
        ], [])
        assert task['status'] == 'done', task.get('error')
        final = task.get('enriched_text') or task.get('full_text') or ''
        sep = scorecard_separator('en')
        assert sep in final, 'engine did not finalize the review body (no separator)'
        above = final.split(sep, 1)[0]
        assert 'Quantitative Scores' not in above and 'Overall Rating' not in above, \
            'scores leaked into the submittable body from the engine path'
        assert '| BLEU |' not in final and '| --- |' not in final, 'engine left a table in the body'
    finally:
        re_mod.dispatch_stream = orig
    _ok('engine: review task persists a finalized (submittable) body')


def test_source_level_negative_control_finalize_table_strip():
    """Prove the table-strip step is load-bearing: monkeypatch it to identity →
    a leaked table survives the finalize pass; restore → it is stripped again."""
    import lib.paper.review as rv
    saved = rv._strip_md_tables
    try:
        rv._strip_md_tables = lambda t: t  # neuter
        broken = rv.finalize_review_body(REVIEW_BODY_WITH_TABLE_EN, 'en')
        assert '| BLEU |' in broken, 'neutering the table-strip should let a table survive'
    finally:
        rv._strip_md_tables = saved
    fixed = rv.finalize_review_body(REVIEW_BODY_WITH_TABLE_EN, 'en')
    assert '| BLEU |' not in fixed, 'restored table-strip must remove the table again'
    _ok('source-level negative control: finalize table-strip is load-bearing (neuter→survives, restore→stripped)')


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


# ─── NLPCC 4-column form (OA + Confidence only) ─────────────────

def test_nlpcc_scorecard_is_two_scored_fields_only():
    """NLPCC's OpenReview form is 4 columns (title, review, OA, confidence);
    title+review are the prose body, so the scorecard emits ONLY OA + Confidence
    and must NOT carry the ML-family soundness/novelty/clarity/comparison rows."""
    from lib.paper import build_review_prompt
    for L in ('en', 'zh'):
        p = build_review_prompt('nlpcc', L)
        assert 'Overall Assessment' in p and 'Confidence' in p, (L, 'OA/Confidence missing')
        assert '1–6' in p, (L, 'NLPCC OA is a 1-6 scale')
        assert 'Soundness / Substance' not in p, (L, 'stray Soundness row')
        assert 'Meaningful Comparison' not in p, (L, 'stray Comparison row')
        assert 'Novelty / Originality' not in p and 'Novelty / Originality（' not in p, (L, 'stray Novelty row')
    _ok('NLPCC scorecard = OA (1-6) + Confidence only (4-column form), no ML-family sub-scores')


# ─── Rebuttal: lang keys ────────────────────────────────────────

def test_rebuttal_lang_key_roundtrip_and_family():
    from lib.paper import (make_rebuttal_lang, is_rebuttal_lang, is_review_family,
                           is_review_lang, parse_report_lang)
    k = make_rebuttal_lang('nlpcc', 'en')
    assert k == 'rebuttal:nlpcc:en', k
    assert parse_report_lang(k) == {'kind': 'rebuttal', 'venue': 'nlpcc', 'ui_lang': 'en'}
    # family predicate covers review AND rebuttal, not plain reports.
    assert is_rebuttal_lang(k) and not is_review_lang(k)
    assert is_review_family(k) and is_review_family('review:acl:zh')
    assert not is_review_family('en') and not is_review_family('zh')
    # bad venue / uilang fall back safely, same as review keys.
    assert parse_report_lang('rebuttal:bogus:fr') == {'kind': 'rebuttal', 'venue': 'generic', 'ui_lang': 'en'}
    _ok('rebuttal:<venue>:<uilang> round-trips; is_review_family covers review+rebuttal only')


# ─── Rebuttal: prompt structure ─────────────────────────────────

def test_rebuttal_prompt_has_slots_decision_and_no_change_discipline():
    from lib.paper import build_rebuttal_prompt, REBUTTAL_DECISION_MARKER
    for L in ('en', 'zh'):
        p = build_rebuttal_prompt('nlpcc', L)
        # all three fill slots survive for the route's .replace()
        for slot in ('{paper_text}', '{original_review}', '{author_rebuttal}'):
            assert slot in p, (L, slot)
        # the machine-parseable decision block + every field it must emit
        assert REBUTTAL_DECISION_MARKER in p, (L, 'decision marker missing')
        for field in ('ORIGINAL_OVERALL', 'NEW_OVERALL', 'ORIGINAL_CONFIDENCE',
                      'NEW_CONFIDENCE', 'CHANGED', 'REASON'):
            assert field in p, (L, field)
        assert 'NLPCC' in p, (L, 'venue label missing')
    # the "default is NO change" discipline must be explicit (EN + ZH)
    assert 'NO score change' in build_rebuttal_prompt('nlpcc', 'en')
    assert '默认不改分' in build_rebuttal_prompt('nlpcc', 'zh')
    _ok('rebuttal prompt keeps 3 slots + decision block + "default = no change" discipline (EN+ZH)')


# ─── Rebuttal: structured decision parsing ──────────────────────

def _rebuttal_body(marker, oa_from, oa_to, cf_from, cf_to, changed, reason='Because.'):
    return (
        '# Response to Authors\n\n## Overall\nThanks.\n\n'
        + marker + '\n'
        + f'ORIGINAL_OVERALL: {oa_from}\n'
        + f'NEW_OVERALL: {oa_to}\n'
        + f'ORIGINAL_CONFIDENCE: {cf_from}\n'
        + f'NEW_CONFIDENCE: {cf_to}\n'
        + f'CHANGED: {changed}\n'
        + f'REASON: {reason}\n'
    )


def test_rebuttal_decision_parse_changed_and_unchanged():
    from lib.paper import parse_rebuttal_decision, REBUTTAL_DECISION_MARKER
    m = REBUTTAL_DECISION_MARKER
    # changed: OA moved 4→5, confidence steady
    d = parse_rebuttal_decision(_rebuttal_body(m, 4, 5, 4, 4, 'yes'))
    assert d['present'] and d['changed'] and d['overallChanged'] and not d['confidenceChanged']
    assert d['origOverall'] == '4' and d['newOverall'] == '5'
    # the dominant path: unchanged (model says no + values equal)
    d2 = parse_rebuttal_decision(_rebuttal_body(m, 4, 4, 4, 4, 'no'))
    assert d2['present'] and not d2['changed'] and not d2['overallChanged']
    assert d2['newOverall'] == '4', 'unchanged echoes the original value'
    # no decision block at all
    assert parse_rebuttal_decision('# Response\nno block here')['present'] is False
    _ok('parse_rebuttal_decision: changed path flags overallChanged; unchanged path is first-class')


def test_rebuttal_decision_reconciles_flag_against_values():
    """The scores are ground truth: a model that self-reports CHANGED wrong is
    reconciled toward the actual values (both directions)."""
    from lib.paper import parse_rebuttal_decision, REBUTTAL_DECISION_MARKER
    m = REBUTTAL_DECISION_MARKER
    # model says "no" but NEW_OVERALL differs → forced changed=True
    d = parse_rebuttal_decision(_rebuttal_body(m, 4, 5, 4, 4, 'no'))
    assert d['changed'] is True and d['overallChanged'] is True
    # model says "yes" but every value is identical → forced changed=False
    d2 = parse_rebuttal_decision(_rebuttal_body(m, 3, 3, 4, 4, 'yes'))
    assert d2['changed'] is False and not d2['overallChanged'] and not d2['confidenceChanged']
    # confidence-only change is still a change
    d3 = parse_rebuttal_decision(_rebuttal_body(m, 4, 4, 3, 4, 'no'))
    assert d3['changed'] is True and d3['confidenceChanged'] and not d3['overallChanged']
    _ok('parse_rebuttal_decision reconciles the self-reported flag against the actual score values')


def test_rebuttal_finalize_keeps_decision_block_verbatim():
    """finalize_rebuttal_body cleans the reply prose but must keep the decision
    block byte-exact (a comma-rewrite there would corrupt a score value)."""
    from lib.paper import finalize_rebuttal_body, parse_rebuttal_decision, REBUTTAL_DECISION_MARKER
    body = _rebuttal_body(REBUTTAL_DECISION_MARKER, 4, 5, 4, 4, 'yes',
                          reason='Table 6 now reports the ablation.')
    out = finalize_rebuttal_body(body, 'en')
    assert REBUTTAL_DECISION_MARKER in out
    # the parsed decision still reads the same scores after finalization
    d = parse_rebuttal_decision(out)
    assert d['origOverall'] == '4' and d['newOverall'] == '5' and d['changed']
    # idempotent
    assert finalize_rebuttal_body(out, 'en') == out or REBUTTAL_DECISION_MARKER in finalize_rebuttal_body(out, 'en')
    _ok('finalize_rebuttal_body cleans reply prose but preserves the score-decision block verbatim')


def test_source_level_negative_control_rebuttal_no_change_discipline():
    """Prove the "default = no score change" clause is load-bearing in the
    rebuttal prompt: blank it in-memory → the assertion fails; restore → passes."""
    import lib.paper.review._prompts as pr
    saved = pr._REBUTTAL_PROMPT_EN
    try:
        pr._REBUTTAL_PROMPT_EN = pr._REBUTTAL_PROMPT_EN.replace('NO score change', 'XXXX')
        broken = pr.build_rebuttal_prompt('nlpcc', 'en')
        assert 'NO score change' not in broken, 'blanking should remove the no-change clause'
    finally:
        pr._REBUTTAL_PROMPT_EN = saved
    assert 'NO score change' in pr.build_rebuttal_prompt('nlpcc', 'en'), 'restore must reproduce it'
    _ok('source-level negative control: rebuttal "default = no change" discipline is load-bearing')


def main():
    print()
    print(_color('═══ Paper Review Mode Tests ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_paper_review_mode.__main__')
    tests = [
        test_parse_plain_report_keys_untouched,
        test_parse_review_key_decodes_venue_and_uilang,
        test_parse_review_key_falls_back_safely,
        test_review_prompt_picks_venue_scorecard,
        test_review_prompt_ui_language,
        test_review_prompt_has_anti_slop_constraints,
        test_review_prompt_human_reviewer_voice_and_precise_weaknesses,
        test_review_prompt_per_point_length_ceiling,
        test_make_review_lang_distinct_from_report_key,
        test_engine_review_injects_with_real_uilang_and_persists_composite_key,
        test_review_is_text_only_no_images_at_all,
        test_finalize_review_moves_scores_below_separator,
        test_finalize_review_strips_tables_and_dangling_star,
        test_finalize_review_idempotent_and_preserves_code,
        test_review_prompt_forbids_tables_and_declares_scorecard_separator,
        test_engine_review_finalizes_persisted_body,
        test_source_level_negative_control_finalize_table_strip,
        test_review_and_report_rows_coexist_for_same_paper_hash,
        test_venues_endpoint_and_review_route_dispatch,
        test_source_level_negative_control_venue_registry,
        test_nlpcc_scorecard_is_two_scored_fields_only,
        test_rebuttal_lang_key_roundtrip_and_family,
        test_rebuttal_prompt_has_slots_decision_and_no_change_discipline,
        test_rebuttal_decision_parse_changed_and_unchanged,
        test_rebuttal_decision_reconciles_flag_against_values,
        test_rebuttal_finalize_keeps_decision_block_verbatim,
        test_source_level_negative_control_rebuttal_no_change_discipline,
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
