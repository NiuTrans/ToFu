"""lib/slides/recipe.py — topic → finished deck, as a checkpointed stage graph.

Rides ``lib.production.stages`` (docs/modules/ingest_media.md §4.5):

    research  → URL-grounded fact cards (degradable)                  [search]
    outline   → deck plan: sources + layout/asset rhythm + briefs     [LLM]
    design    → manifest + theme tokens + page skeletons               [zero-LLM]
    author    → asset preflight + one .page per brief                  [image+LLM]
    assets    → remote images downloaded into media/ + rewritten       [network]
    render    → per-page PNG previews (headless Chrome)                [browser]
    layout_qa → glyph-level overflow/collision check + minimal repair  [browser+LLM]
    visual_qa → per-page + whole-deck contact-sheet review             [VLM]
    export    → native editable PPTX + fade transitions                [zero-LLM]

Cost posture (owner 拍板 lineage): pages bounded (3..20, default 12), page
authors overlap only behind the production LLM budget and checkpoint exactly,
and QA admits one repair round per page. Images have separate call/byte/count
budgets and exact reconstructible caches. Failed page calls leave a valid
diagnostic fallback and exact successful-page caches; the author stage retries
only those misses, then blocks publication if any fallback remains.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from lib.log import get_logger
from lib.slides.contracts import (
    DEFAULT_SLIDE_PAGES,
    MAX_SLIDE_PAGES,
    MIN_SLIDE_PAGES,
    normalise_slide_briefs,
    normalise_slide_image_references,
    normalise_slide_model,
    normalise_slide_page_count,
    normalise_slide_size,
    normalise_slide_style,
    normalise_slide_topic,
)
from lib.production.llm_policy import (
    abort_check_from_event,
    production_llm_dispatch_kwargs,
    production_llm_max_429_attempts,
)
from lib.production.contracts import normalise_creative_mode
from lib.production.research import (
    RESEARCH_RESUME_TTL_S,
    evidence_checkpoint_version,
)
from lib.production.stages import Stage, StageAborted, run_stages

logger = get_logger(__name__)

__all__ = ['build_deck_from_topic', 'slides_recipe_stages']

_DEFAULT_PAGES = DEFAULT_SLIDE_PAGES
_MIN_PAGES = MIN_SLIDE_PAGES
_MAX_PAGES = MAX_SLIDE_PAGES
_RESEARCH_RESUME_TTL_S = RESEARCH_RESUME_TTL_S
_RESEARCH_CHECKPOINT_VERSION = evidence_checkpoint_version(freshness='month')
_AUTHOR_CACHE_NAME = '.tofu-slide-author.json'
_AUTHOR_CACHE_VERSION = 'slide-page-cache-v1'
_MAX_AUTHOR_CACHE_BYTES = 256 * 1024
_MAX_PAGE_YAML_BYTES = 512 * 1024
_MAX_INPUT_IMAGE_REFERENCES = 64
_MAX_INPUT_IMAGES = 20
_MAX_INPUT_IMAGE_TOTAL_BYTES = 192 * 1024 * 1024
_MAX_REMOTE_IMAGES = 40
_MAX_REMOTE_IMAGE_TOTAL_BYTES = 192 * 1024 * 1024
_REMOTE_ASSET_CACHE_NAME = '.tofu-slide-remote-assets.json'
_REMOTE_ASSET_CACHE_VERSION = 'slide-remote-assets-v1'
_MAX_REMOTE_ASSET_CACHE_BYTES = 256 * 1024
_MAX_REMOTE_ASSET_CACHE_ENTRIES = 80
_MAX_REMOTE_URL_CHARS = 4096
_REMOTE_ASSET_PATH_RE = re.compile(
    r'^media/remote_[0-9a-f]{16}\.(?:png|jpe?g|gif|webp|svg)$')
_VISUAL_QA_CACHE_NAME = '.tofu-slide-visual-qa.json'
_VISUAL_QA_CACHE_VERSION = 'slide-visual-qa-v1'
_MAX_VISUAL_QA_CACHE_BYTES = 512 * 1024
_MAX_VISUAL_QA_FINDINGS = 64
_OUTLINE_CHECKPOINT_VERSION = 'slide-outline-v3'
_DIRECTOR_LENSES = (
    ('evidence-editorial',
     'Prioritize concrete evidence, native charts/diagrams, annotated imagery, '
     'and meaningful visual modalities. Avoid generic card grids.'),
    ('spatial-narrative',
     'Prioritize a strong opening-to-resolution spatial story, recurring '
     'visual anchors, page handoffs, and deliberately contrasting layouts.'),
)


# ── Seams (monkeypatchable) ───────────────────────────────

def _llm_chat(messages, **kwargs):
    from lib.llm_dispatch.api import dispatch_chat
    return dispatch_chat(messages, **kwargs)


def _context_abort_check(ctx: dict):
    callback = ctx.get('abort_check')
    if callable(callback):
        return callback
    return abort_check_from_event(ctx.get('abort_event'))


def _raise_if_aborted(ctx: dict, stage: str) -> None:
    abort_check = _context_abort_check(ctx)
    if abort_check is not None and abort_check():
        raise StageAborted(f'aborted during slides {stage}')


# ── Stage: research ───────────────────────────────────────

def _run_research(ctx: dict) -> dict:
    from lib.production.research import research_topic
    return research_topic(ctx['topic'])


# ── Stage: outline ────────────────────────────────────────

_OUTLINE_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)


def _build_outline_prompt(topic: str, *, lang: str, max_pages: int,
                          scenarios_doc: str, style_hint: str,
                          research_doc: str = '', research_as_of: str = '') -> str:
    research_block = ((
        '## Grounded research cards (cite source ids in content_notes)\n'
        + research_doc + '\n\n') if research_doc else (
        '## Research status\nNo grounded cards were available. Do not invent '
        'specific facts, dates, quotations or statistics.\n\n'))
    if lang == 'zh':
        return (
            f'你是一名演示文稿总编。为主题《{topic}》设计一份演示 deck 的大纲。\n'
            f'{("用户风格要求:" + style_hint) if style_hint else ""}\n\n'
            f'研究截止时间: {research_as_of or "未知"}\n'
            f'{research_block}'
            '输出严格 JSON(无围栏无解释):\n'
            '{"title": "deck 标题", "scenario": "场景id", "theme_id": "主题id或空",\n'
            ' "pages": [{"pageType": "cover|table_of_contents|chapter|content|final",\n'
            '  "purpose": "本页读者任务(一句话)", "key_message": "本页核心信息/判断句",\n'
            '  "layout_hint": "版式提示(如: 左图右文/大数字+三行支撑/时间线)",\n'
            '  "content_notes": "本页要点素材(事实/数据/要点,供页作者展开)",\n'
            '  "layout_archetype": "full-bleed-hero|split-editorial|metric-focus|'
            'diagram-flow|comparison-field|timeline-ribbon|evidence-quote|'
            'closing-resolve", "asset_mode": "generate|code|none",'
            ' "visual_modality": "hero-image|annotated-evidence|native-chart|'
            'native-diagram|timeline|comparison|table|quote|code|formula|map|'
            'minimal-type", "visual_anchor": "贯穿前后页的可见对象/图形",'
            ' "handoff": "本页如何把视觉或问题交给下一页",'
            ' "asset_prompt": "若 generate,用英文写无文字的主视觉提示词"}]}\n'
            f'要求:\n1. pages 数量 {_MIN_PAGES} 到 {max_pages};\n'
            f'2. scenario 从以下选择: {scenarios_doc};\n'
            '3. 首页 cover,末页 final(回答开篇问题/给出行动,禁止孤悬"谢谢");\n'
            '4. 每页 key_message 必须是完整判断句,不是栏目名;\n'
            '5. 节奏:密页与呼吸页交替;内容页不连续同构;\n'
            '6. 事实/数字/引语必须在 content_notes 标注支持它的 [S#];'
            '没有来源卡支持时不得编造;\n'
            '7. 先回答“截至研究截止时间，最新状态是什么”：只要 current 或 '
            'official 检索车道出现发布、预售、价格等状态更新，至少一页必须引用'
            '对应 [S#]，不得只使用 background 旧材料;\n'
            '8. 冲突时优先时间更近且真正的第一方官方来源；official 只是“官方来源'
            '候选检索车道”，仍须根据域名和正文判断，不能因检索词含“官方”就认定'
            '为官方;\n'
            '9. 严格区分预售价、最终售价、传闻和估算；较新证据已给出预售价时，'
            '不得继续写“价格尚未公布”或省略已经公布的价格状态;\n'
            '10. 精确价格/日期/参数优先直接引用第一方页面；若只有二手来源，至少'
            '两家独立来源一致才写成确定数字，单一二手摘要只能写成待核实状态，不能'
            '擅自修正或四舍五入;\n'
            '11. generate 只用于照片感/编辑插画主视觉;数据图表、流程和对比必须'
            '由 PPTD 原生元素绘制(asset_mode=code),避免图生文字和虚构数据;\n'
            '12. 每页选择一个 visual_modality。整套至少使用 4 种,并用 visual_anchor'
            '和 handoff 形成跨页连续性；只有证据充分时才能选择 chart/table/map。\n')
    return (
        f'You are a presentation editor. Design the outline of a deck about '
        f'"{topic}".\n'
        f'{("Style request: " + style_hint) if style_hint else ""}\n\n'
        f'Research cutoff: {research_as_of or "unknown"}\n'
        f'{research_block}'
        'Output strict JSON (no fences):\n'
        '{"title": "...", "scenario": "scenario-id", "theme_id": "or empty",\n'
        ' "pages": [{"pageType": "cover|table_of_contents|chapter|content|final",'
        ' "purpose": "reader task, one sentence", "key_message": "the judgment",'
        ' "layout_hint": "e.g. image-left-text-right / big number + supports",'
        ' "content_notes": "facts/points the page author expands",'
        ' "layout_archetype": "full-bleed-hero|split-editorial|metric-focus|'
        'diagram-flow|comparison-field|timeline-ribbon|evidence-quote|'
        'closing-resolve", "asset_mode": "generate|code|none",'
        ' "visual_modality": "hero-image|annotated-evidence|native-chart|'
        'native-diagram|timeline|comparison|table|quote|code|formula|map|'
        'minimal-type", "visual_anchor": "visible cross-page anchor",'
        ' "handoff": "visual/question handoff to the next page",'
        ' "asset_prompt": "English, text-free hero visual prompt if generate"}]}\n'
        f'Rules: {_MIN_PAGES}..{max_pages} pages; scenario one of '
        f'{scenarios_doc}; first page cover, last page final (answer the '
        'opening question, never a bare "thank you"); every key_message is a '
        'complete judgment sentence; alternate dense and breathing pages; '
        'cite [S#] in content_notes for every fact, number or quote and invent '
        'nothing unsupported; if current/official-search cards contain a '
        'launch, availability, presale, or price update, at least one page '
        'must cite that current-state evidence instead of relying only on '
        'background cards; prefer the newest genuine first-party source when '
        'sources conflict (the official lane contains candidates, not an '
        'authority guarantee); distinguish presale price, final price, rumor, '
        'and estimate; never say price is unannounced when newer evidence '
        'already announces a presale price; exact prices, dates, and specs '
        'must come from a first-party page or agree across two independent '
        'secondary sources—never silently round or repair one secondary '
        'snippet; use generate only for photographic/editorial '
        'hero art, while charts, flows and comparisons use native PPTD '
        'elements (asset_mode=code); choose one visual_modality per page, use '
        'at least four across the deck, and connect pages with visual_anchor '
        'and handoff. Use chart/table/map only when evidence supports it.')


def _outline_candidate(content: str, *, topic: str, research: dict,
                       maximum: int, usage: dict) -> dict:
    """Parse and enrich one candidate without mutating stage feedback."""
    from lib.design_sys.themes import SCENARIOS, classify_scenario
    match = _OUTLINE_JSON_RE.search(content or '')
    if not match:
        raise ValueError('outline reply has no JSON object')
    raw = json.loads(match.group(0))
    pages = raw.get('pages')
    if not isinstance(pages, list) or len(pages) < _MIN_PAGES:
        raise ValueError(f'outline has {len(pages or [])} pages '
                         f'(need ≥{_MIN_PAGES})')
    pages = normalise_slide_briefs(pages, maximum=maximum)
    if len(pages) < _MIN_PAGES:
        raise ValueError(f'outline has {len(pages)} usable page briefs '
                         f'(need ≥{_MIN_PAGES})')
    scenario = str(raw.get('scenario') or '')
    if scenario not in SCENARIOS:
        scenario = classify_scenario(topic + ' '
                                     + str(raw.get('title') or ''))
    cards = research.get('cards') or []
    out = {
        'title': str(raw.get('title') or topic).strip()[:120],
        'scenario': scenario,
        'theme_id': str(raw.get('theme_id') or '').strip(),
        'visual_motif': str(raw.get('visual_motif') or '').strip()[:600],
        'pages': [page for page in pages if isinstance(page, dict)],
        'source_cards': cards,
        'research_as_of': str(research.get('as_of') or ''),
        'usage': usage if isinstance(usage, dict) else {},
    }
    from lib.slides._creative_plan import normalise_deck_plan
    normalise_deck_plan(out)
    cards_by_id = {str(card.get('id') or ''): card for card in cards}
    for page in out['pages']:
        source_ids = re.findall(
            r'\bS\d+\b', str(page.get('content_notes') or ''))
        page['sources'] = [cards_by_id[source_id] for source_id in source_ids
                           if source_id in cards_by_id][:4]
    return out


def _outline_errors(ctx: dict, artifact: dict) -> list:
    """Pure outline gate used both by director screening and the Stage gate."""
    pages = artifact.get('pages') or []
    errors = []
    if len(pages) < _MIN_PAGES:
        return [f'outline too thin ({len(pages)} pages)']
    for i, p in enumerate(pages):
        if not (p.get('key_message') or p.get('purpose')):
            return [f'outline page {i + 1} has neither key_message nor purpose']
    if len(pages) >= 3:
        minimum_variety = min(4, len(pages))
        layouts = {str(page.get('layout_archetype') or '') for page in pages}
        modalities = {str(page.get('visual_modality') or '') for page in pages}
        layouts.discard('')
        modalities.discard('')
        if len(layouts) < minimum_variety:
            errors.append(
                f'layout variety too low ({len(layouts)}; need '
                f'{minimum_variety})')
        if len(modalities) < minimum_variety:
            errors.append(
                f'visual modality variety too low ({len(modalities)}; need '
                f'{minimum_variety})')
    research = ctx.get('artifacts', {}).get('research') or {}
    cards = research.get('cards') or []
    if not cards:
        return errors
    outline_text = '\n'.join(
        str(page.get(field) or '')
        for page in pages
        for field in ('purpose', 'key_message', 'content_notes'))
    cited_ids = set(re.findall(r'\bS\d+\b', outline_text))
    from lib.production.research import current_fact_errors
    errors.extend(current_fact_errors(
        research, outline_text, cited_ids=cited_ids))
    return errors


def _outline_score(candidate: dict) -> int:
    """Deterministic director fallback; higher means richer and less repetitive."""
    pages = candidate.get('pages') or []
    layouts = [str(page.get('layout_archetype') or '') for page in pages]
    modalities = [str(page.get('visual_modality') or '') for page in pages]
    cited = sum(bool(page.get('sources')) for page in pages)
    handoffs = sum(bool(page.get('handoff')) for page in pages[:-1])
    consecutive_repeats = sum(
        layouts[index] == layouts[index - 1]
        for index in range(1, len(layouts)))
    return (len(pages) * 2 + len(set(layouts)) * 5
            + len(set(modalities)) * 6 + cited * 3 + handoffs * 2
            - consecutive_repeats * 8)


def _outline_digest(candidate: dict) -> dict:
    """Bounded critic input: plan semantics only, never full research cards."""
    fields = (
        'pageType', 'purpose', 'key_message', 'content_notes',
        'layout_archetype', 'visual_modality', 'visual_anchor', 'handoff',
        'asset_mode', 'narrative_role',
    )
    return {
        'title': candidate.get('title'),
        'scenario': candidate.get('scenario'),
        'visual_motif': candidate.get('visual_motif'),
        'pages': [
            {field: page.get(field) for field in fields if page.get(field)}
            for page in (candidate.get('pages') or [])
        ],
    }


def _critic_choice(ctx: dict, candidates: list[dict]) -> tuple[int, str, dict]:
    prompt = (
        'You are an independent presentation creative director. Select the '
        'stronger outline. Judge: evidence grounding, narrative progression, '
        'visual-modality and layout diversity, cross-page continuity, content '
        'richness, and feasibility as editable native slides. Penalize generic '
        'card grids, unsupported charts, repeated compositions, and decorative '
        'imagery that proves nothing. Output ONLY JSON: '
        '{"winner":1,"reason":"specific concise reason","scores":['
        '{"content":0,"evidence":0,"design":0,"coherence":0},'
        '{"content":0,"evidence":0,"design":0,"coherence":0}]}. '
        'Scores are integers 0..10. Candidates:\n'
        + json.dumps([_outline_digest(item) for item in candidates],
                     ensure_ascii=False, separators=(',', ':'))[:28000])
    _raise_if_aborted(ctx, 'outline critic')
    content, usage = _llm_chat(
        [{'role': 'user', 'content': prompt}], max_tokens=1200,
        temperature=0.0, prefer_model=ctx.get('model') or None,
        strict_model=bool(ctx.get('model')),
        owner_user_id=ctx.get('owner_user_id'),
        **production_llm_dispatch_kwargs(
            abort_check=_context_abort_check(ctx),
            max_429_attempts=ctx.get('max_429_attempts')),
        log_prefix='[Slides:outline-critic]')
    _raise_if_aborted(ctx, 'outline critic')
    match = _OUTLINE_JSON_RE.search(content or '')
    if not match:
        raise ValueError('outline critic reply has no JSON object')
    raw = json.loads(match.group(0))
    winner = int(raw.get('winner') or 0) - 1
    if winner < 0 or winner >= len(candidates):
        raise ValueError('outline critic selected an invalid candidate')
    reason = re.sub(r'\s+', ' ', str(raw.get('reason') or '')).strip()[:500]
    return winner, reason or 'critic selected the stronger plan', {
        'usage': usage if isinstance(usage, dict) else {},
        'scores': raw.get('scores') if isinstance(raw.get('scores'), list) else [],
    }


def _run_outline(ctx: dict) -> dict:
    topic = ctx['topic']
    lang = ctx.get('lang', 'zh')
    mode = normalise_creative_mode(
        ctx.get('creative_mode'), default='standard')
    gate_feedback = list(ctx.pop('_outline_gate_feedback', []) or [])
    from lib.design_sys.themes import SCENARIOS
    scenarios_doc = ', '.join(f'{scenario_id}({meta["label"]})'
                              for scenario_id, meta in SCENARIOS.items())
    research = ctx.get('artifacts', {}).get('research') or {}
    cards = research.get('cards') or []
    from lib.production.research import (
        format_research_cards,
        summarise_current_signals,
    )
    signals = research.get('current_signals') or summarise_current_signals(cards)
    corroborated = ', '.join(signals.get('corroborated_price_values') or [])
    single_source = ', '.join(signals.get('single_source_price_values') or [])
    signal_doc = (
        'Automated temporal scan (not an authority verdict): '
        f'current_status_sources={",".join(signals.get("status_source_ids") or []) or "none"}; '
        f'prices corroborated by 2+ independent hosts={corroborated or "none"}; '
        f'single-host price candidates={single_source or "none"}.\n')
    if gate_feedback:
        signal_doc += (
            'Previous outline attempt was rejected. Correct every item below:\n- '
            + '\n- '.join(str(item) for item in gate_feedback[:6]) + '\n')
    base_prompt = _build_outline_prompt(
        topic, lang=lang, max_pages=ctx.get('max_pages', _DEFAULT_PAGES),
        scenarios_doc=scenarios_doc, style_hint=ctx.get('style') or '',
        research_doc=signal_doc + format_research_cards(research),
        research_as_of=str(research.get('as_of') or ''))
    lenses = _DIRECTOR_LENSES if mode == 'director' else (('standard', ''),)
    candidates: list[dict] = []
    rejected: list[str] = []
    candidate_attempts: list[dict] = []
    for lens_name, lens in lenses:
        usage: dict = {}
        prompt = base_prompt
        if lens:
            prompt += (
                '\nDirector candidate lens (binding): ' + lens
                + '\nDo not mention this lens in the output.')
        try:
            _raise_if_aborted(ctx, f'outline candidate {lens_name}')
            content, usage = _llm_chat(
                [{'role': 'user', 'content': prompt}], max_tokens=4096,
                temperature=0.45, prefer_model=ctx.get('model') or None,
                strict_model=bool(ctx.get('model')),
                owner_user_id=ctx.get('owner_user_id'),
                **production_llm_dispatch_kwargs(
                    abort_check=_context_abort_check(ctx),
                    max_429_attempts=ctx.get('max_429_attempts')),
                log_prefix=f'[Slides:outline:{lens_name}]')
            _raise_if_aborted(ctx, f'outline candidate {lens_name}')
            candidate = _outline_candidate(
                content, topic=topic, research=research,
                maximum=ctx.get('max_pages', _DEFAULT_PAGES), usage=usage)
            errors = _outline_errors(ctx, candidate)
            if errors:
                candidate_attempts.append({
                    'lens': lens_name, 'passed': False,
                    'usage': usage if isinstance(usage, dict) else {},
                    'errors': errors[:3],
                })
                rejected.extend(errors)
                logger.info('[Slides:outline] rejected %s: %s', lens_name,
                            '; '.join(errors[:3]))
                continue
            candidate['_director_lens'] = lens_name
            candidates.append(candidate)
            candidate_attempts.append({
                'lens': lens_name, 'passed': True,
                'usage': usage if isinstance(usage, dict) else {},
                'errors': [],
            })
        except StageAborted:
            raise
        except Exception as exc:
            from lib.llm_errors import AbortedError
            if isinstance(exc, AbortedError):
                raise StageAborted(str(exc)) from exc
            if mode == 'standard':
                raise
            candidate_attempts.append({
                'lens': lens_name, 'passed': False,
                'usage': usage if isinstance(usage, dict) else {},
                'errors': [str(exc)[:400]],
            })
            rejected.append(f'{lens_name}: {exc}')
            logger.warning('[Slides:outline] candidate %s failed: %s',
                           lens_name, exc)
    if not candidates:
        ctx['_outline_gate_feedback'] = rejected[:6]
        raise ValueError('no outline candidate passed: '
                         + '; '.join(rejected[:4]))

    fallback_scores = [_outline_score(candidate) for candidate in candidates]
    winner = max(range(len(candidates)), key=lambda index: fallback_scores[index])
    reason = 'deterministic fallback selected the richer valid plan'
    critic_meta: dict = {'usage': {}, 'scores': []}
    if len(candidates) > 1:
        try:
            winner, reason, critic_meta = _critic_choice(ctx, candidates)
        except StageAborted:
            raise
        except Exception as exc:
            from lib.llm_errors import AbortedError
            if isinstance(exc, AbortedError):
                raise StageAborted(str(exc)) from exc
            logger.warning('[Slides:outline] critic failed; using score: %s', exc)
            reason += f' (critic unavailable: {type(exc).__name__})'
    selected = candidates[winner]
    winner_lens = str(selected.pop('_director_lens', '') or 'standard')
    selected['creative_mode'] = mode
    selected['director'] = {
        'candidate_count': len(candidates),
        'rejected_count': len(lenses) - len(candidates),
        'winner_index': winner + 1,
        'winner_lens': winner_lens,
        'reason': reason,
        'fallback_scores': fallback_scores,
        'critic_scores': critic_meta.get('scores') or [],
        'critic_usage': critic_meta.get('usage') or {},
        'candidate_attempts': candidate_attempts,
    }
    logger.info('[Slides:outline] %r → %d pages, scenario=%s mode=%s winner=%d',
                selected['title'][:50], len(selected['pages']),
                selected['scenario'], mode, winner + 1)
    return selected


def _gate_outline(ctx: dict, artifact: dict) -> list:
    errors = _outline_errors(ctx, artifact)
    if errors:
        ctx['_outline_gate_feedback'] = list(errors)
    else:
        ctx.pop('_outline_gate_feedback', None)
    return errors


def _effective_outline(artifacts: dict) -> dict:
    """The outline the deck is actually authored from.

    The interactive gate's checkpoint carries the user-approved (possibly
    revised) outline; on both the live path and a crash-resume it outranks
    the outline stage's original artifact.
    """
    gate = (artifacts or {}).get('outline_gate') or {}
    return gate.get('outline') or artifacts['outline']


# ── Stage: outline_gate (interactive human confirmation) ──

from lib.tasks_pkg.human_guidance import APPROVAL_REPLIES as _APPROVAL_REPLIES


def _revise_outline_with_feedback(ctx: dict, outline: dict,
                                  feedback: str) -> dict:
    """ONE bounded revision pass: current outline JSON + the user's change
    request → a re-validated outline. Falls back to the ORIGINAL outline
    when the revision cannot be parsed or fails the same gate the outline
    stage enforces — a confusing revision is never better than the plan
    the user just saw.
    """
    lang = ctx.get('lang', 'zh')
    import json as _json
    current = _json.dumps(
        {'title': outline.get('title'), 'scenario': outline.get('scenario'),
         'theme_id': outline.get('theme_id'),
         'pages': outline.get('pages')}, ensure_ascii=False)
    if lang == 'zh':
        prompt = (
            '这是 PPT 大纲的当前 JSON:\n```json\n' + current + '\n```\n\n'
            '用户的修改意见:\n' + feedback[:1200] + '\n\n'
            '请按意见修改大纲,保持相同的 JSON 结构(title/scenario/theme_id/'
            'pages,每页含 pageType/key_message/content_notes 等字段),'
            '只输出修改后的完整 JSON。')
    else:
        prompt = (
            'Current slide-deck outline JSON:\n```json\n' + current + '\n```\n\n'
            'The user requests these changes:\n' + feedback[:1200] + '\n\n'
            'Apply them, keep the exact same JSON structure (title/scenario/'
            'theme_id/pages with pageType/key_message/content_notes per '
            'page), and output ONLY the full revised JSON.')
    try:
        content, usage = _llm_chat(
            [{'role': 'user', 'content': prompt}], max_tokens=4096,
            temperature=0.3, prefer_model=ctx.get('model') or None,
            strict_model=bool(ctx.get('model')),
            owner_user_id=ctx.get('owner_user_id'),
            **production_llm_dispatch_kwargs(
                abort_check=_context_abort_check(ctx),
                max_429_attempts=ctx.get('max_429_attempts')),
            log_prefix='[Slides:outline-gate]')
        research = ctx.get('artifacts', {}).get('research') or {}
        revised = _outline_candidate(
            content, topic=ctx['topic'], research=research,
            maximum=ctx.get('max_pages', _DEFAULT_PAGES),
            usage=usage if isinstance(usage, dict) else {})
        errors = _outline_errors(ctx, revised)
        if errors:
            logger.warning('[Slides:outline-gate] revision failed validation '
                           '(%s) — keeping the original outline',
                           '; '.join(errors[:3]))
            return outline
        revised['human_revised'] = True
        return revised
    except Exception as exc:
        from lib.llm_errors import AbortedError
        if isinstance(exc, AbortedError):
            raise StageAborted(str(exc)) from exc
        logger.warning('[Slides:outline-gate] revision pass failed (%s) — '
                       'keeping the original outline', exc)
        return outline


def _run_outline_gate(ctx: dict) -> dict:
    """Interactive outline confirmation for chat-initiated decks.

    The user reviews the plan BEFORE the expensive per-page authoring
    fan-out: the stage emits the outline as an ``outline_gate`` event (the
    workUnit card renders it with an answer box), blocks on the shared
    human-gate registry, then proceeds unchanged or feeds the change request
    into ONE bounded revision pass. Headless jobs skip the stage with zero
    behavioral difference — no event, no wait, no artifact footprint.
    """
    if not ctx.get('interactive_outline'):
        return {'skipped': True}
    outline = ctx['artifacts']['outline']
    guidance_id = f"hg_{ctx.get('task_id') or 'slides'}_outline"
    emit = ctx.get('emit')
    pages_brief = [{'page': i + 1,
                    'pageType': str(p.get('pageType') or ''),
                    'key_message': str(p.get('key_message') or '')[:160]}
                   for i, p in enumerate(outline.get('pages') or [])][:24]

    def publish_request():
        if emit:
            emit({'type': 'outline_gate', 'guidance_id': guidance_id,
                  'title': str(outline.get('title') or '')[:160],
                  'pages': pages_brief})
    from lib.tasks_pkg.human_guidance import request_human_guidance
    response = request_human_guidance(
        guidance_id, owner_user_id=ctx.get('owner_user_id'),
        on_registered=publish_request, abort_check=_context_abort_check(ctx))
    if response is None or _context_abort_check(ctx)():
        raise StageAborted('cancelled or unavailable outline gate')
    feedback = str(response or '').strip()
    if emit:
        emit({'type': 'outline_gate_resolved',
              'guidance_id': guidance_id,
              'revised': bool(feedback
                              and feedback not in _APPROVAL_REPLIES)})
    if not feedback or feedback in _APPROVAL_REPLIES:
        logger.info('[Slides:outline-gate] approved as-is')
        return {'approved': True, 'outline': outline, 'revised': False}
    logger.info('[Slides:outline-gate] revision requested: %.120s', feedback)
    revised = _revise_outline_with_feedback(ctx, outline, feedback)
    return {'approved': True, 'outline': revised,
            'revised': revised is not outline,
            'feedback': feedback[:400]}


# ── Stage: design (zero-LLM) ──────────────────────────────

def _run_design(ctx: dict) -> dict:
    from lib.design_sys.themes import default_theme_id, get_theme
    outline = ctx['artifacts']['outline']
    # User template (A7): the uploaded deck's reverse-engineered tokens
    # replace the curated theme; the outline's scenario still classifies
    # the CONTENT, the template owns the visual language.
    template_id = str(ctx.get('template_id') or '').strip()
    if template_id:
        from lib.slides.templates import load_template_theme
        record = load_template_theme(
            template_id, owner_user_id=int(ctx.get('owner_user_id') or 0))
        if record is not None:
            # Stage every substituted registry face (the export gate embeds
            # them; an unstaged face would block on a download mid-export).
            import lib.design_sys.fonts as _fonts
            staged = []
            families = {
                str(style.get('fontFamily') or '')
                for style in (record['theme'].get('textStyles') or {}).values()
                if isinstance(style, dict)}
            for family in sorted(families):
                face = _fonts.resolve_font(family)
                if face:
                    for src in face.sources:
                        if _fonts.ensure_font(face.id, src.weight):
                            staged.append(f'{face.id}-w{src.weight}')
            tpl_size = [int(v) for v in (record.get('size')
                                         or list(ctx['size']))]
            ctx['size'] = (tpl_size[0], tpl_size[1])
            logger.info('[Slides:design] template=%s (%s) fonts=%s size=%s',
                        template_id, record['title'][:40], staged,
                        ctx['size'])
            return {'theme_id': f'template:{template_id}',
                    'theme_tokens': record['theme'],
                    'staged_fonts': staged,
                    'scenario': outline['scenario'],
                    'font_substitutions': dict(
                        record.get('font_substitutions') or {})}
        logger.warning('[Slides:design] template %s unknown/foreign — '
                       'falling back to the curated theme path', template_id)
    theme_id = outline.get('theme_id') or ''
    theme = get_theme(theme_id)
    if theme is None or theme.scenario != outline['scenario']:
        theme = get_theme(default_theme_id(outline['scenario']))
    theme_id = theme.id
    # Pre-warm the theme's faces so later stages never block on a download.
    from lib.slides.theme_ops import build_theme_tokens, stage_theme_fonts
    staged = stage_theme_fonts(theme)
    theme_tokens = build_theme_tokens(theme)
    out = {'theme_id': theme_id, 'theme_tokens': theme_tokens,
           'staged_fonts': staged, 'scenario': theme.scenario}
    logger.info('[Slides:design] theme=%s fonts=%s', theme_id, staged)
    return out


# ── Stage: author (per page) ──────────────────────────────

def _author_page_name(index: int, brief: dict) -> str:
    return f'pages/{index + 1:02d}_{_slug(brief.get("pageType"))}.page'


def _load_author_cache(deck_dir: str) -> dict:
    """Load the reconstructible page cache without trusting unbounded input."""
    path = os.path.join(deck_dir, _AUTHOR_CACHE_NAME)
    try:
        with open(path, 'rb') as fh:
            raw = fh.read(_MAX_AUTHOR_CACHE_BYTES + 1)
    except FileNotFoundError:
        raw = b''
    except OSError as exc:
        logger.warning('[Slides:author] page cache read failed: %s', exc)
        raw = b''
    if len(raw) > _MAX_AUTHOR_CACHE_BYTES:
        logger.warning('[Slides:author] oversized page cache ignored: %s', path)
        raw = b''
    try:
        parsed = json.loads(raw.decode('utf-8')) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning('[Slides:author] malformed page cache ignored: %s', exc)
        parsed = {}
    if (not isinstance(parsed, dict)
            or parsed.get('version') != _AUTHOR_CACHE_VERSION
            or not isinstance(parsed.get('pages'), dict)):
        return {'version': _AUTHOR_CACHE_VERSION, 'pages': {}}
    return {'version': _AUTHOR_CACHE_VERSION,
            'pages': {str(key): value
                      for key, value in parsed['pages'].items()
                      if isinstance(value, dict)}}


def _cached_author_result(deck, brief: dict, index: int, total: int, name: str,
                          input_sha256: str, row: dict | None) -> dict | None:
    """Return metadata for one exact, bounded, still-valid authored page."""
    if (not isinstance(row, dict) or row.get('mode') != 'authored'
            or row.get('input_sha256') != input_sha256
            or row.get('path') != name):
        return None
    declared_bytes = row.get('yaml_bytes')
    if (isinstance(declared_bytes, bool)
            or not isinstance(declared_bytes, int)
            or not 0 < declared_bytes <= _MAX_PAGE_YAML_BYTES):
        return None
    expected_sha256 = row.get('yaml_sha256')
    if (not isinstance(expected_sha256, str)
            or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)):
        return None

    root = os.path.realpath(deck.root)
    path = os.path.realpath(os.path.join(root, *name.split('/')))
    if not path.startswith(root + os.sep):
        return None
    try:
        with open(path, 'rb') as fh:
            data = fh.read(_MAX_PAGE_YAML_BYTES + 1)
            file_size = os.fstat(fh.fileno()).st_size
    except OSError:
        return None
    if (file_size != declared_bytes or len(data) != declared_bytes
            or len(data) > _MAX_PAGE_YAML_BYTES
            or hashlib.sha256(data).hexdigest() != expected_sha256):
        return None
    try:
        yaml_text = data.decode('utf-8')
    except UnicodeDecodeError:
        return None

    from lib.slides.author import (
        _brief_fidelity_findings,
        _validate_page_text,
    )
    if (_validate_page_text(deck, name, yaml_text)
            or _brief_fidelity_findings(
                brief, yaml_text, page_index=index,
                total=total)):
        return None
    required_assets = [str(path) for path in
                       (brief.get('resolved_assets') or []) if path]
    if any(path not in yaml_text for path in required_assets):
        return None
    rounds = row.get('rounds')
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 0:
        rounds = 0
    return {'ok': True, 'mode': 'authored', 'rounds': rounds,
            'findings': [], 'reused': True}


def _bounded_author_result(deck, brief: dict, index: int, total: int, *,
                           theme, image_urls: list, lang: str,
                           model: str | None, max_rounds: int,
                           abort_check, max_429_attempts: int | None,
                           prompt_context, owner_user_id: int | None,
                           provider_pin_id: str) -> dict:
    """Run one isolated author call and contain page-local failures."""
    from lib.slides.author import author_page, fallback_page

    try:
        result = author_page(
            deck, brief, index, total, theme=theme, image_urls=image_urls,
            lang=lang, model=model, max_rounds=max_rounds,
            abort_check=abort_check, max_429_attempts=max_429_attempts,
            owner_user_id=owner_user_id,
            provider_pin_id=provider_pin_id,
            prompt_context=prompt_context)
    except Exception as exc:
        if abort_check is not None and abort_check():
            return {'ok': False, 'yaml': '', 'mode': 'aborted', 'rounds': 0,
                    'findings': ['aborted']}
        logger.warning('[Slides:author] page %d crashed; using fallback: %s',
                       index + 1, exc, exc_info=True)
        result = {'ok': True, 'yaml': fallback_page(deck, brief, theme=theme),
                  'mode': 'fallback', 'rounds': 0,
                  'findings': [f'page author crashed: {exc}']}

    if result.get('mode') == 'aborted':
        return result
    yaml_text = result.get('yaml')
    if not isinstance(yaml_text, str) or not yaml_text:
        result = {'ok': True, 'yaml': fallback_page(deck, brief, theme=theme),
                  'mode': 'fallback', 'rounds': result.get('rounds', 0),
                  'findings': ['page author returned empty YAML']}
        yaml_text = result['yaml']
    if len(yaml_text.encode('utf-8')) > _MAX_PAGE_YAML_BYTES:
        logger.warning('[Slides:author] page %d exceeded %d bytes; using fallback',
                       index + 1, _MAX_PAGE_YAML_BYTES)
        result = {'ok': True, 'yaml': fallback_page(deck, brief, theme=theme),
                  'mode': 'fallback', 'rounds': result.get('rounds', 0),
                  'findings': ['page author output exceeded byte limit']}
    if len(result['yaml'].encode('utf-8')) > _MAX_PAGE_YAML_BYTES:
        raise ValueError('bounded fallback page exceeded YAML byte limit')
    return result


def _author_cache_row(input_sha256: str, name: str, result: dict) -> dict:
    data = result['yaml'].encode('utf-8')
    rounds = result.get('rounds')
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 0:
        rounds = 0
    return {'input_sha256': input_sha256, 'path': name, 'mode': 'authored',
            'rounds': rounds, 'yaml_bytes': len(data),
            'yaml_sha256': hashlib.sha256(data).hexdigest()}

def _run_author(ctx: dict) -> dict:
    from lib.design_sys.themes import get_theme
    from lib.slides.author import (
        author_page_input_sha256, prepare_author_prompt_context)
    outline = _effective_outline(ctx['artifacts'])
    design = ctx['artifacts']['design']
    theme = get_theme(design['theme_id'])
    deck_dir = ctx['deck_dir']
    os.makedirs(os.path.join(deck_dir, 'pages'), exist_ok=True)

    # A stub deck for validation context (pages filled as they land).
    from lib.slides.pptd import Deck
    deck = Deck(title=outline['title'], size=ctx['size'],
                theme=design['theme_tokens'], pages=[], root=deck_dir)

    briefs = outline['pages']
    total = len(briefs)
    authored = 0
    fallback_pages: list[int] = []
    fallback_findings: list[str] = []
    page_files: list[str | None] = [None] * total
    _raise_if_aborted(ctx, 'authoring')
    image_urls, input_findings = _materialise_input_images(
        ctx.get('image_urls') or [], deck_dir)
    asset_preflight = {'by_page': {}, 'records': [], 'findings': []}
    try:
        from lib.slides._asset_preflight import prepare_deck_assets
        asset_preflight = prepare_deck_assets(
            outline, deck_dir, abort_check=_context_abort_check(ctx),
            max_429_attempts=ctx.get('image_max_429_attempts'),
            owner_user_id=ctx.get('owner_user_id'),
            tenant_id=ctx.get('tenant_id'))
    except Exception as e:
        logger.warning('[Slides:author] asset preflight crashed: %s', e,
                       exc_info=True)
        asset_preflight['findings'] = [f'asset preflight crashed: {e}']
    asset_preflight['findings'] = (list(asset_preflight.get('findings') or [])
                                   + list(ctx.get('input_image_findings') or [])
                                   + input_findings)
    _raise_if_aborted(ctx, 'authoring')
    emit = ctx.get('emit')
    abort_check = _context_abort_check(ctx)
    max_429_attempts = ctx.get('max_429_attempts')
    lang = ctx.get('lang', 'zh')
    model = ctx.get('model') or None
    max_rounds = int(ctx.get('author_rounds') or 3)
    prompt_context = prepare_author_prompt_context(deck, theme)
    cache_path = os.path.join(deck_dir, _AUTHOR_CACHE_NAME)
    cache = _load_author_cache(deck_dir)
    cache_pages = cache['pages']
    page_inputs: list[str] = []
    pending: list[int] = []

    for i, brief in enumerate(briefs):
        _raise_if_aborted(ctx, 'authoring')
        author_images = (list(asset_preflight['by_page'].get(i, []))
                         + image_urls)
        input_sha256 = author_page_input_sha256(
            deck, brief, i, total, theme=theme, image_urls=author_images,
            lang=lang, model=model, max_rounds=max_rounds,
            prompt_context=prompt_context)
        page_inputs.append(input_sha256)
        name = _author_page_name(i, brief)
        cached = _cached_author_result(
            deck, brief, i, total, name, input_sha256,
            cache_pages.get(str(i)))
        if cached is None:
            pending.append(i)
            continue
        page_files[i] = name
        authored += 1
        if emit:
            emit({'type': 'page_authored', 'page': i + 1, 'total': total,
                  'mode': 'authored', 'rounds': cached['rounds'],
                  'reused': True})

    from lib.json_store import write_json_atomic, write_text_atomic

    def _commit(index: int, result: dict) -> None:
        nonlocal authored
        brief = briefs[index]
        name = _author_page_name(index, brief)
        write_text_atomic(os.path.join(deck_dir, name), result['yaml'])
        page_files[index] = name
        if result.get('mode') == 'authored':
            authored += 1
            cache_pages[str(index)] = _author_cache_row(
                page_inputs[index], name, result)
        else:
            cache_pages.pop(str(index), None)
            fallback_pages.append(index + 1)
            fallback_findings.extend(
                f'page {index + 1}: {finding}'
                for finding in (result.get('findings') or [])[:3])
        write_json_atomic(cache_path, cache, sort_keys=True)
        if emit:
            emit({'type': 'page_authored', 'page': index + 1, 'total': total,
                  'mode': result['mode'], 'rounds': result.get('rounds', 0),
                  'reused': False})

    if pending:
        from lib.production.concurrency import owned_results
        abort_requested = False
        fatal_error: Exception | None = None
        def stopped():
            return abort_requested or fatal_error is not None or bool(
                abort_check is not None and abort_check())
        def run_page(index):
            brief = briefs[index]
            author_images = list(asset_preflight['by_page'].get(index, [])) + image_urls
            return _bounded_author_result(deck, brief, index, total,
                theme=theme, image_urls=author_images, lang=lang, model=model,
                max_rounds=max_rounds, abort_check=abort_check,
                max_429_attempts=max_429_attempts,
                owner_user_id=ctx.get('owner_user_id'),
                provider_pin_id=ctx.get('provider_pin_id') or '',
                prompt_context=prompt_context)
        with owned_results(pending, run_page, name='slides-author', should_stop=stopped) as outcomes:
            for index, future in outcomes:
                try:
                    result = future.result()
                    if result.get('mode') == 'aborted':
                        abort_requested = True
                    else:
                        _commit(index, result)
                except Exception as exc:
                    logger.error('[Slides:author] page %d failed: %s',
                                 index + 1, exc, exc_info=True)
                    fatal_error = fatal_error or exc
        if fatal_error is not None:
            raise fatal_error
        if abort_requested or (abort_check is not None and abort_check()):
            raise StageAborted('aborted during slides authoring')

    _raise_if_aborted(ctx, 'authoring')
    if any(name is None for name in page_files):
        raise RuntimeError('slides author left an uncommitted page')
    return {'page_files': list(page_files), 'authored': authored, 'total': total,
            'assets_by_page': asset_preflight['by_page'],
            'input_images': image_urls,
            'asset_findings': asset_preflight['findings'],
            'fallback_pages': fallback_pages,
            'fallback_findings': fallback_findings[:24]}


def _materialise_input_images(values: list, deck_dir: str) -> tuple[list, list]:
    """Copy caller-supplied local images into ``deck/media``.

    Page authors and PPTX export both require deck-relative references.  The
    old path handed absolute caller paths to the author verbatim, so a model
    could either fail validation or shorten one to ``media/<basename>`` even
    though no such file had ever been copied.  HTTP URLs stay remote until the
    dedicated assets stage downloads and rewrites them.
    """
    out: list[str] = []
    findings: list[str] = []
    root = os.path.realpath(deck_dir)
    media_dir = os.path.join(root, 'media')
    accepted_local_bytes = 0
    local_by_digest: dict[str, str] = {}
    allowed_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')
    from lib.slides._media_io import (copy_file_bounded,
                                      hash_file_bounded)

    for index, raw in enumerate((values or [])[:_MAX_INPUT_IMAGE_REFERENCES]):
        src = str(raw or '').strip()
        if not src:
            continue
        if len(out) >= _MAX_INPUT_IMAGES:
            findings.append(
                f'caller image limit {_MAX_INPUT_IMAGES} reached; remaining '
                'references were omitted')
            break
        if src.startswith(('http://', 'https://')):
            if src not in out:
                out.append(src)
            continue

        ext = os.path.splitext(src.split('?', 1)[0])[1].lower()
        if ext not in allowed_extensions:
            findings.append(
                f'caller image has unsupported type and was omitted: {src}')
            continue

        # Already-valid deck paths need no copy, but still consume the same
        # bounded per-job source budget as external files.
        joined = os.path.realpath(os.path.join(root, src))
        already_staged = (not os.path.isabs(src)
                          and joined.startswith(root + os.sep)
                          and os.path.isfile(joined))
        local = joined if already_staged else os.path.realpath(src)
        if not os.path.isfile(local):
            findings.append(f'caller image does not exist and was omitted: {src}')
            continue
        try:
            digest, size = hash_file_bounded(local)
            if digest in local_by_digest:
                rel = local_by_digest[digest]
                if rel not in out:
                    out.append(rel)
                continue
            if accepted_local_bytes + size > _MAX_INPUT_IMAGE_TOTAL_BYTES:
                findings.append(
                    f'caller image aggregate limit '
                    f'{_MAX_INPUT_IMAGE_TOTAL_BYTES} reached; omitted: {src}')
                continue
            if already_staged:
                rel = os.path.relpath(local, root).replace(os.sep, '/')
            else:
                rel = f'media/input_{index + 1:02d}_{digest[:12]}{ext}'
                dest = os.path.join(root, *rel.split('/'))
                os.makedirs(media_dir, exist_ok=True)
                reusable = False
                if os.path.isfile(dest):
                    try:
                        existing_digest, existing_size = hash_file_bounded(dest)
                        reusable = (existing_digest == digest
                                    and existing_size == size)
                    except (OSError, ValueError):
                        reusable = False
                if not reusable:
                    copy_file_bounded(
                        local, dest, expected_sha256=digest,
                        expected_bytes=size)
            local_by_digest[digest] = rel
            accepted_local_bytes += size
            if rel not in out:
                out.append(rel)
        except (OSError, ValueError) as e:
            findings.append(f'caller image could not be copied and was omitted: '
                            f'{src} ({e})')
    if len(values or []) > _MAX_INPUT_IMAGE_REFERENCES:
        findings.append(
            f'caller image reference scan limit {_MAX_INPUT_IMAGE_REFERENCES} '
            'reached; remaining references were omitted')
    return out, findings


def _slug(value) -> str:
    s = re.sub(r'[^a-z0-9]+', '_', str(value or 'content').lower())
    return s.strip('_') or 'content'


def _gate_author(ctx: dict, artifact: dict) -> list:
    if not artifact.get('page_files'):
        return ['author produced zero pages']
    total = int(artifact.get('total') or 0)
    authored = int(artifact.get('authored') or 0)
    if total and authored < total:
        pages = artifact.get('fallback_pages') or []
        suffix = f' (pages {", ".join(str(page) for page in pages[:12])})' \
            if pages else ''
        detail = (artifact.get('fallback_findings') or [''])[0]
        return [
            f'{total - authored} of {total} pages used a fallback{suffix}; '
            'designer-quality publication requires every page to pass the '
            f'author contract. {detail}'.strip()
        ]
    return []


# ── Stage: assets ─────────────────────────────────────────

def _remote_asset_name(url: str) -> str:
    ext = os.path.splitext(url.split('?', 1)[0])[1].lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'):
        ext = '.jpg'
    identity = hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]
    return f'media/remote_{identity}{ext}'


def _load_remote_asset_cache(deck_dir: str) -> dict:
    path = os.path.join(deck_dir, _REMOTE_ASSET_CACHE_NAME)
    try:
        with open(path, 'rb') as source:
            data = source.read(_MAX_REMOTE_ASSET_CACHE_BYTES + 1)
        if len(data) > _MAX_REMOTE_ASSET_CACHE_BYTES:
            raise ValueError('remote asset cache exceeds byte limit')
        parsed = json.loads(data.decode('utf-8'))
    except FileNotFoundError:
        parsed = {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        logger.warning('[Slides:assets] remote cache ignored: %s', exc)
        parsed = {}
    if (not isinstance(parsed, dict)
            or parsed.get('version') != _REMOTE_ASSET_CACHE_VERSION
            or not isinstance(parsed.get('assets'), dict)):
        return {'version': _REMOTE_ASSET_CACHE_VERSION, 'assets': {}}
    assets = {}
    for url, row in parsed['assets'].items():
        if len(assets) >= _MAX_REMOTE_ASSET_CACHE_ENTRIES:
            break
        if (isinstance(url, str) and len(url) <= _MAX_REMOTE_URL_CHARS
                and isinstance(row, dict)):
            assets[url] = row
    return {'version': _REMOTE_ASSET_CACHE_VERSION, 'assets': assets}


def _reusable_remote_asset(deck_dir: str, url: str,
                           row: dict | None) -> tuple[str, int] | None:
    if not isinstance(row, dict):
        return None
    expected_path = _remote_asset_name(url)
    declared_bytes = row.get('bytes')
    expected_sha256 = row.get('sha256')
    if (row.get('path') != expected_path
            or not _REMOTE_ASSET_PATH_RE.fullmatch(expected_path)
            or isinstance(declared_bytes, bool)
            or not isinstance(declared_bytes, int)
            or not isinstance(expected_sha256, str)
            or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)):
        return None
    from lib.slides._media_io import (MAX_SLIDE_IMAGE_BYTES,
                                      hash_file_bounded)
    if not 0 < declared_bytes <= MAX_SLIDE_IMAGE_BYTES:
        return None
    root = os.path.realpath(deck_dir)
    path = os.path.realpath(os.path.join(root, *expected_path.split('/')))
    if not path.startswith(root + os.sep):
        return None
    try:
        actual_sha256, actual_bytes = hash_file_bounded(path)
    except (OSError, ValueError):
        return None
    if actual_bytes != declared_bytes or actual_sha256 != expected_sha256:
        return None
    return expected_path, actual_bytes


def _remote_paths_in_deck(deck) -> set[str]:
    paths: set[str] = set()
    for page in deck.pages:
        for element in page.elements:
            if isinstance(element, dict):
                src = str(element.get('src') or '')
                if _REMOTE_ASSET_PATH_RE.fullmatch(src):
                    paths.add(src)
        background = page.background
        if isinstance(background, dict):
            src = str(background.get('src') or '')
            if _REMOTE_ASSET_PATH_RE.fullmatch(src):
                paths.add(src)
    return paths

def _run_assets(ctx: dict) -> dict:
    """Download remote images into media/ and REWRITE refs to local paths —
    the deck directory must be self-contained before render/export."""
    from lib.slides.pptd import parse_deck
    deck_dir = ctx['deck_dir']
    _write_manifest(deck_dir, ctx)
    deck = parse_deck(os.path.join(deck_dir, 'deck.pptd'))
    media_dir = os.path.join(deck_dir, 'media')
    downloaded = 0
    downloaded_bytes = 0
    reused = 0
    url_map: dict = {}
    findings: list[str] = []
    cache_path = os.path.join(deck_dir, _REMOTE_ASSET_CACHE_NAME)
    cache = _load_remote_asset_cache(deck_dir)
    cached_assets = cache['assets']
    cache_rows_before = list(cached_assets.values())
    validated_cache_urls: set[str] = set()
    initially_referenced_paths = _remote_paths_in_deck(deck)
    from lib.json_store import write_json_atomic

    def _localize(src: str) -> str:
        nonlocal downloaded, downloaded_bytes, reused
        if not src.startswith(('http://', 'https://')):
            return src
        if src in url_map:
            return url_map[src]
        _raise_if_aborted(ctx, 'asset localisation')
        if len(src) > _MAX_REMOTE_URL_CHARS:
            findings.append('remote image URL exceeded the 4096-character '
                            'limit and was kept remote')
            url_map[src] = src
            return src
        if len(url_map) >= _MAX_REMOTE_IMAGES:
            findings.append(
                f'remote image limit {_MAX_REMOTE_IMAGES} reached; kept {src}')
            url_map[src] = src
            return src
        from lib.slides._media_io import (MAX_SLIDE_IMAGE_BYTES,
                                          download_file_bounded)
        cached = _reusable_remote_asset(
            deck_dir, src, cached_assets.get(src))
        if cached is not None:
            rel, _size = cached
            url_map[src] = rel
            validated_cache_urls.add(src)
            reused += 1
            return rel
        cached_assets.pop(src, None)
        remaining = _MAX_REMOTE_IMAGE_TOTAL_BYTES - downloaded_bytes
        if remaining < 1024:
            findings.append(
                f'remote image aggregate limit '
                f'{_MAX_REMOTE_IMAGE_TOTAL_BYTES} reached; kept {src}')
            url_map[src] = src
            return src
        os.makedirs(media_dir, exist_ok=True)
        rel = _remote_asset_name(src)
        path = os.path.join(deck_dir, *rel.split('/'))
        if (src not in cached_assets
                and len(cached_assets) >= _MAX_REMOTE_ASSET_CACHE_ENTRIES):
            for stale_url, stale_row in list(cached_assets.items()):
                if str(stale_row.get('path') or '') not in initially_referenced_paths:
                    cached_assets.pop(stale_url, None)
                    break
        if len(cached_assets) >= _MAX_REMOTE_ASSET_CACHE_ENTRIES:
            findings.append('remote image cache entry limit reached; kept '
                            f'{src}')
            url_map[src] = src
            return src
        try:
            size, _content_type, sha256 = download_file_bounded(
                src, path, max_bytes=min(MAX_SLIDE_IMAGE_BYTES, remaining),
                min_bytes=1024, timeout=60,
                abort_check=_context_abort_check(ctx))
        except InterruptedError as exc:
            raise StageAborted('aborted during slides asset localisation') from exc
        except Exception as e:
            logger.warning('[Slides:assets] fetch failed %s: %s', src, e)
            findings.append(f'remote image fetch failed; kept {src}: {e}')
            url_map[src] = src          # keep remote; renderer/exporter retry
            return src
        cached_assets[src] = {'path': rel, 'bytes': size, 'sha256': sha256}
        validated_cache_urls.add(src)
        write_json_atomic(cache_path, cache, sort_keys=True)
        url_map[src] = rel
        downloaded += 1
        downloaded_bytes += size
        return rel

    changed = False
    for page in deck.pages:
        for el in page.elements:
            if not isinstance(el, dict):
                continue
            if el.get('elementType') == 'image' and el.get('src'):
                new = _localize(str(el['src']))
                if new != el['src']:
                    el['src'] = new
                    changed = True
        bg = page.background
        if isinstance(bg, dict) and bg.get('type') == 'image' and bg.get('src'):
            new = _localize(str(bg['src']))
            if new != bg['src']:
                bg['src'] = new
                changed = True
    if changed:
        import yaml
        from lib.json_store import write_text_atomic
        for page in deck.pages:
            text = yaml.safe_dump(page.raw | {
                'pageType': page.page_type,
                'background': page.background,
                'elements': page.elements,
            }, allow_unicode=True, sort_keys=False)
            write_text_atomic(os.path.join(deck_dir, page.path), text)
    retained_paths = _remote_paths_in_deck(deck)
    previous_rows = cache_rows_before + list(cached_assets.values())
    cache['assets'] = {
        url: row for url, row in cached_assets.items()
        if str(row.get('path') or '') in retained_paths
        and (url in validated_cache_urls
             or _reusable_remote_asset(deck_dir, url, row) is not None)
    }
    write_json_atomic(cache_path, cache, sort_keys=True)
    retained_cache_paths = {
        str(row.get('path') or '') for row in cache['assets'].values()}
    for row in previous_rows:
        stale_path = str(row.get('path') or '')
        if (stale_path not in retained_cache_paths
                and _REMOTE_ASSET_PATH_RE.fullmatch(stale_path)):
            try:
                os.unlink(os.path.join(deck_dir, *stale_path.split('/')))
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.debug('[Slides:assets] stale remote cleanup failed: %s',
                             exc)
    logger.info('[Slides:assets] %d remote image(s) localised', downloaded)
    return {'downloaded': downloaded, 'downloaded_bytes': downloaded_bytes,
            'reused': reused, 'pages': len(deck.pages), 'findings': findings}


def _write_manifest(deck_dir: str, ctx: dict) -> None:
    """Write deck.pptd from the outline + design artifacts (idempotent)."""
    import yaml
    outline = _effective_outline(ctx['artifacts'])
    design = ctx['artifacts']['design']
    author = ctx['artifacts'].get('author') or {}
    page_files = author.get('page_files') or []
    manifest = {
        'version': 'v2',
        'title': outline['title'],
        'size': list(ctx['size']),
        'theme': design['theme_tokens'],
        'pages': page_files,
    }
    text = yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False)
    from lib.json_store import write_text_atomic
    write_text_atomic(os.path.join(deck_dir, 'deck.pptd'), text)


# ── Stage: render ─────────────────────────────────────────

def _run_render(ctx: dict) -> dict:
    from lib.slides.pptd import parse_deck
    from lib.slides.render_png import render_previews
    deck = parse_deck(os.path.join(ctx['deck_dir'], 'deck.pptd'))
    emit = ctx.get('emit')

    def _on_page(index: int, total: int) -> None:
        if emit:
            emit({'type': 'page_rendered', 'page': index + 1, 'total': total})

    manifest = render_previews(deck, os.path.join(ctx['deck_dir'], 'preview'),
                               scale=2.0,
                               abort_check=_context_abort_check(ctx),
                               on_page=_on_page)
    return {'previews': [p['png'] for p in manifest['pages']],
            'failed': manifest['failed']}


def _gate_render(ctx: dict, artifact: dict) -> list:
    if artifact.get('skipped') is True:
        return []
    expected = int((ctx['artifacts'].get('author') or {}).get('total') or 0)
    previews = artifact.get('previews') or []
    failed = artifact.get('failed') or []
    errors = []
    if failed:
        errors.append(f'{len(failed)} preview page(s) failed to render')
    if expected and len(previews) != expected:
        errors.append(
            f'render produced {len(previews)} of {expected} page previews')
    return errors


# ── Stage: layout_qa ──────────────────────────────────────

def _run_layout_qa(ctx: dict) -> dict:
    """Measure actual glyph line boxes and minimally repair bad pages.

    Unlike the VLM pass this is deterministic and cheap: Chromium reports
    the same rectangles used by preview rendering. Candidate repairs are
    accepted only when they reduce that page's finding count; otherwise the
    exact original YAML is restored.
    """
    render = ctx['artifacts'].get('render') or {}
    if not (render.get('previews') or []):
        return {'ran': False, 'ok': False,
                'reason': 'no rendered previews; layout QA skipped',
                'findings': [], 'repaired': 0}
    from lib.slides.layout_qa import findings_text, inspect_deck_layout
    from lib.slides.pptd import parse_deck
    manifest = os.path.join(ctx['deck_dir'], 'deck.pptd')
    deck = parse_deck(manifest)
    initial = inspect_deck_layout(deck)
    if not initial.get('ran') or initial.get('ok'):
        return {**initial, 'initialFindings': len(initial.get('findings') or []),
                'repaired': 0}

    from lib.design_sys.themes import get_theme
    from lib.json_store import write_text_atomic
    from lib.slides.author import author_page
    outline = _effective_outline(ctx['artifacts'])
    design = ctx['artifacts']['design']
    author_artifact = ctx['artifacts'].get('author') or {}
    assets_by_page = author_artifact.get('assets_by_page') or {}
    theme = get_theme(design['theme_id'])
    by_page: dict = {}
    for finding in initial.get('findings') or []:
        by_page.setdefault(int(finding['page']) - 1, []).append(finding)

    originals = {}
    candidates = set()
    abort_check = _context_abort_check(ctx)
    max_429_attempts = ctx.get('max_429_attempts')
    for index, findings in sorted(by_page.items()):
        _raise_if_aborted(ctx, 'layout QA')
        page_path = os.path.join(ctx['deck_dir'], deck.pages[index].path)
        with open(page_path, encoding='utf-8') as fh:
            seed = fh.read()
        originals[index] = seed
        fix = author_page(
            deck, outline['pages'][index], index, len(deck.pages),
            theme=theme,
            image_urls=(list(assets_by_page.get(index, []) or
                              assets_by_page.get(str(index), []))
                        + list(author_artifact.get('input_images') or [])),
            lang=ctx.get('lang', 'zh'), model=ctx.get('model') or None,
            max_rounds=2, seed_yaml=seed,
            extra_findings=[findings_text(findings)],
            abort_check=abort_check,
            max_429_attempts=max_429_attempts,
            owner_user_id=ctx.get('owner_user_id'),
            provider_pin_id=ctx.get('provider_pin_id') or '')
        _raise_if_aborted(ctx, 'layout QA')
        if fix.get('mode') != 'authored':
            continue
        write_text_atomic(page_path, fix['yaml'])
        candidates.add(index)

    if not candidates:
        return {**initial, 'initialFindings': len(initial['findings']),
                'repaired': 0}

    deck = parse_deck(manifest)
    candidate_result = inspect_deck_layout(deck)
    if not candidate_result.get('ran'):
        for index in candidates:
            write_text_atomic(os.path.join(ctx['deck_dir'], deck.pages[index].path),
                              originals[index])
        return {**initial, 'reason': candidate_result.get('reason') or
                'repair could not be remeasured',
                'initialFindings': len(initial['findings']), 'repaired': 0}

    initial_counts = {i: len(findings) for i, findings in by_page.items()}
    final_counts = {p['index']: len(p.get('findings') or [])
                    for p in candidate_result.get('pages') or []}
    accepted = set()
    for index in candidates:
        if final_counts.get(index, 10**6) < initial_counts[index]:
            accepted.add(index)
        else:
            page_path = os.path.join(ctx['deck_dir'], deck.pages[index].path)
            write_text_atomic(page_path, originals[index])
    if accepted != candidates:
        deck = parse_deck(manifest)
        candidate_result = inspect_deck_layout(deck)

    from lib.slides.render_png import render_page_png
    emit = ctx.get('emit')
    for index in accepted:
        preview = os.path.join(ctx['deck_dir'], 'preview', 'pages',
                               f'{index + 1:02d}.png')
        try:
            render_page_png(deck, index, preview, scale=2.0)
            if emit:
                emit({'type': 'page_rendered', 'page': index + 1,
                      'total': len(deck.pages)})
        except Exception as exc:
            logger.warning('[Slides:layout-qa] page %d preview refresh failed: '
                           '%s', index + 1, exc)
    remaining = candidate_result.get('findings') or []
    logger.info('[Slides:layout-qa] %d initial, %d repaired page(s), %d remain',
                len(initial['findings']), len(accepted), len(remaining))
    return {**candidate_result,
            'initialFindings': len(initial['findings']),
            'repaired': len(accepted),
            'repairedPages': [i + 1 for i in sorted(accepted)]}


def _gate_layout_qa(ctx: dict, artifact: dict) -> list:
    if artifact.get('ran') is not True:
        return []
    findings = artifact.get('findings') or []
    if artifact.get('ok') is True and not findings:
        return []
    examples = [str(item.get('message') or item)
                for item in findings[:3] if isinstance(item, dict)]
    detail = '; '.join(examples)
    return [
        f'layout QA still has {len(findings)} unresolved finding(s)'
        + (f': {detail}' if detail else '')
    ]


# ── Stage: visual_qa ──────────────────────────────────────

def _load_visual_qa_cache(deck_dir: str) -> dict:
    path = os.path.join(deck_dir, _VISUAL_QA_CACHE_NAME)
    import lib.design_sys.visual_qa as vqa
    return vqa.load_visual_qa_cache(
        path, version=_VISUAL_QA_CACHE_VERSION,
        max_entries=_MAX_PAGES + 1,
        max_bytes=_MAX_VISUAL_QA_CACHE_BYTES)


def _cached_visual_result(row: dict | None,
                          input_sha256: str) -> dict | None:
    import lib.design_sys.visual_qa as vqa
    return vqa.cached_visual_qa_result(
        row, input_sha256, max_findings=_MAX_VISUAL_QA_FINDINGS)


def _remember_visual_result(cache: dict, path: str, key: str,
                            input_sha256: str, result: dict) -> None:
    import lib.design_sys.visual_qa as vqa
    vqa.remember_visual_qa_result(
        cache, path, key, input_sha256, result,
        max_entries=_MAX_PAGES + 1,
        max_bytes=_MAX_VISUAL_QA_CACHE_BYTES,
        max_findings=_MAX_VISUAL_QA_FINDINGS)


def _run_page_visual_reviews(ctx: dict, previews: list, *, theme, vqa,
                             qa_model: str, cache: dict,
                             cache_path: str) -> list:
    """Review independent page previews on owned demand workers."""
    if not previews:
        return []
    abort_check = _context_abort_check(ctx)
    max_429_attempts = ctx.get('max_429_attempts')
    results: list[dict | None] = [None] * len(previews)
    identities: list[str | None] = [None] * len(previews)
    pending_items = []
    for index, png in enumerate(previews):
        _raise_if_aborted(ctx, 'visual QA')
        try:
            identity = vqa.qa_frame_input_sha256(
                png, theme=theme, subject='幻灯片页面', model=qa_model)
        except (OSError, ValueError) as exc:
            logger.debug('[Slides:qa] page %d cache identity unavailable: %s',
                         index + 1, exc)
            identity = None
        identities[index] = identity
        cached = (_cached_visual_result(
            cache['entries'].get(f'page:{index}'), identity)
                  if identity is not None else None)
        if cached is not None:
            results[index] = cached
        else:
            pending_items.append((index, png))
    if not pending_items:
        return list(results)

    from lib.production.concurrency import owned_results
    abort_requested = False

    def _review(index: int, png: str) -> dict:
        try:
            return vqa.qa_frame(
                png, theme=theme, label=f'page-{index + 1:02d}',
                subject='幻灯片页面', model=qa_model,
                abort_check=abort_check,
                max_429_attempts=max_429_attempts,
                owner_user_id=ctx.get('owner_user_id'),
                provider_pin_id=ctx.get('provider_pin_id') or '')
        except Exception as exc:
            logger.warning('[Slides:qa] page %d review crashed: %s',
                           index + 1, exc, exc_info=True)
            return {'ok': False, 'skipped': False,
                    'reason': f'page visual QA crashed: {exc}', 'findings': []}

    def stopped():
        return bool(abort_check is not None and abort_check())
    with owned_results(pending_items, lambda item: _review(*item),
                       name='slides-visual-qa', should_stop=stopped) as outcomes:
        for (index, _png), future in outcomes:
            result = future.result()
            results[index] = result
            identity = identities[index]
            if identity is not None:
                _remember_visual_result(cache, cache_path, f'page:{index}', identity, result)
        abort_requested = stopped()
    if abort_requested:
        raise StageAborted('aborted during slides visual QA')
    if any(result is None for result in results):
        raise RuntimeError('slides visual QA left an unreviewed page')
    return list(results)


def _actionable_visual_findings(result: dict) -> list:
    return [item for item in result.get('findings') or []
            if item.get('severity') in ('blocker', 'major')]


def _visual_score(findings: list) -> tuple:
    """Blockers dominate majors; a repair must strictly improve this score."""
    return (sum(item.get('severity') == 'blocker' for item in findings),
            sum(item.get('severity') == 'major' for item in findings))


def _run_visual_qa(ctx: dict) -> dict:
    """One bounded repair per page, adopted only after measured improvement.

    Candidate pages render in private scratch. Schema, real glyph layout and
    fresh pixels must pass before the retained YAML/preview pair is changed.
    Missing reviewers degrade explicitly; known defects remain gate failures.
    """
    from dataclasses import replace

    import lib.design_sys.visual_qa as vqa
    from lib.design_sys.themes import get_theme
    from lib.slides.author import author_page, _validate_page_text
    from lib.slides.layout_qa import inspect_deck_layout
    from lib.slides.pptd import parse_deck, parse_page_text
    from lib.slides.render_png import render_page_png
    from lib.json_store import write_text_atomic
    from lib.design_sys.contact_sheet import build_contact_sheet
    from lib.slides.visual_repair import recover_visual_repair, VisualRepairTransaction

    recover_visual_repair(ctx['deck_dir'])
    avail, reason = vqa.visual_qa_available()
    if not avail:
        return {'ran': False, 'reason': reason}
    qa_model = vqa.resolve_visual_qa_model(ctx.get('qa_model') or '')
    if not qa_model:
        return {'ran': False, 'reason': 'no vision-capable model slot'}
    outline = _effective_outline(ctx['artifacts'])
    theme = get_theme(ctx['artifacts']['design']['theme_id'])
    deck = parse_deck(os.path.join(ctx['deck_dir'], 'deck.pptd'))
    previews = (ctx['artifacts'].get('render') or {}).get('previews') or []
    if len(previews) != len(deck.pages):
        raise ValueError('visual QA requires one preview for every page')
    author_artifact = ctx['artifacts'].get('author') or {}
    assets_by_page = author_artifact.get('assets_by_page') or {}
    abort_check = _context_abort_check(ctx)
    cache_path = os.path.join(ctx['deck_dir'], _VISUAL_QA_CACHE_NAME)
    cache = _load_visual_qa_cache(ctx['deck_dir'])
    allowed_keys = {'deck'} | {f'page:{i}' for i in range(len(previews))}
    cache['entries'] = {k: v for k, v in cache['entries'].items() if k in allowed_keys}
    qa_directory = os.path.join(ctx['deck_dir'], '.tofu-qa')
    os.makedirs(qa_directory, exist_ok=True)

    def review(path, *, key, label, subject):
        _raise_if_aborted(ctx, 'visual QA')
        try:
            identity = vqa.qa_frame_input_sha256(
                path, theme=theme, subject=subject, model=qa_model)
        except (OSError, ValueError):
            identity = None
        result = (_cached_visual_result(cache['entries'].get(key), identity)
                  if identity is not None else None)
        if result is None:
            result = vqa.qa_frame(
                path, theme=theme, label=label, subject=subject, model=qa_model,
                abort_check=abort_check, max_429_attempts=ctx.get('max_429_attempts'),
                owner_user_id=ctx.get('owner_user_id'),
                provider_pin_id=ctx.get('provider_pin_id') or '')
            if identity is not None:
                _remember_visual_result(cache, cache_path, key, identity, result)
        _raise_if_aborted(ctx, 'visual QA')
        return result

    def review_deck():
        contact = os.path.join(qa_directory, 'deck-contact.png')
        try:
            build_contact_sheet(previews, contact, label_prefix='Page')
            return review(contact, key='deck', label='deck-coherence',
                          subject='整套幻灯片接触表（按页码从左到右、从上到下）')
        except StageAborted:
            raise
        except Exception as exc:
            logger.warning('[Slides:qa] deck review unavailable: %s', exc)
            return {'ok': False, 'reason': str(exc), 'findings': []}

    initial_deck_review = review_deck()
    deck_findings = _actionable_visual_findings(initial_deck_review)
    explicit_page_pattern = re.compile(r'\bpage\s*\d+\b|第\s*\d+\s*页|页面\s*\d+', re.IGNORECASE)
    global_findings = [f for f in deck_findings
                       if not explicit_page_pattern.search(str(f.get('element') or ''))]
    results = _run_page_visual_reviews(
        ctx, previews, theme=theme, vqa=vqa, qa_model=qa_model,
        cache=cache, cache_path=cache_path)
    repaired = []
    rejected = []
    with VisualRepairTransaction(ctx['deck_dir']) as visual_repair:
        scratch = str(visual_repair.scratch)
        originals = visual_repair.rows
        rollback = visual_repair.rollback
        try:
            for index, result in enumerate(results):
                _raise_if_aborted(ctx, 'visual QA')
                page_pattern = re.compile(
                    rf'(?:\bpage\s*{index + 1}\b|第\s*{index + 1}\s*页|页面\s*{index + 1}(?!\d))',
                    re.IGNORECASE)
                coherence = [f for f in deck_findings
                             if page_pattern.search(str(f.get('element') or ''))]
                actionable = _actionable_visual_findings(result) + coherence + global_findings
                if not result.get('ok') or not actionable:
                    continue
                page_path = os.path.join(ctx['deck_dir'], deck.pages[index].path)
                with open(page_path, encoding='utf-8') as handle:
                    seed = handle.read(_MAX_PAGE_YAML_BYTES + 1)
                if len(seed.encode('utf-8')) > _MAX_PAGE_YAML_BYTES:
                    raise ValueError('visual repair source exceeds YAML byte limit')
                candidate = author_page(
                    deck, outline['pages'][index], index, len(deck.pages), theme=theme,
                    image_urls=(list(assets_by_page.get(index, []) or
                                     assets_by_page.get(str(index), []))
                                + list(author_artifact.get('input_images') or [])),
                    lang=ctx.get('lang', 'zh'), model=ctx.get('model') or None,
                    max_rounds=2, seed_yaml=seed,
                    extra_findings=[vqa.findings_text(actionable)], abort_check=abort_check,
                    max_429_attempts=ctx.get('max_429_attempts'),
                    owner_user_id=ctx.get('owner_user_id'),
                    provider_pin_id=ctx.get('provider_pin_id') or '')
                _raise_if_aborted(ctx, 'visual QA')
                yaml_text = candidate.get('yaml') or ''
                if (candidate.get('mode') != 'authored' or yaml_text == seed
                        or len(yaml_text.encode('utf-8')) > _MAX_PAGE_YAML_BYTES
                        or _validate_page_text(deck, deck.pages[index].path, yaml_text)):
                    rejected.append(index + 1)
                    continue
                trial = replace(deck, pages=list(deck.pages))
                trial.pages[index] = parse_page_text(yaml_text, path=deck.pages[index].path)
                layout = inspect_deck_layout(trial, page_indexes=[index], abort_check=abort_check)
                if not layout.get('ran') or not layout.get('ok') or layout.get('findings'):
                    rejected.append(index + 1)
                    continue
                trial_png = os.path.join(scratch, f'candidate-{index}.png')
                try:
                    render_page_png(trial, index, trial_png, scale=2.0)
                    checked = review(trial_png, key=f'page:{index}',
                                     label=f'page-{index + 1:02d}', subject='幻灯片页面')
                except StageAborted:
                    raise
                except Exception as exc:
                    logger.warning('[Slides:qa] page %d repair unverified: %s', index + 1, exc)
                    rejected.append(index + 1)
                    continue
                if (not checked.get('ok')
                        or _visual_score(_actionable_visual_findings(checked))
                        >= _visual_score(actionable)):
                    rejected.append(index + 1)
                    continue
                visual_repair.remember(index, page_path, previews[index], seed)
                try:
                    _raise_if_aborted(ctx, 'visual QA')
                    write_text_atomic(page_path, yaml_text)
                    os.replace(trial_png, previews[index])
                except BaseException:
                    rollback(index)
                    raise
                deck = trial
                results[index] = checked
                repaired.append(index + 1)

            if repaired:
                final_deck_review = review_deck()
                final_findings = _actionable_visual_findings(final_deck_review)
                # An unreviewable or worsened whole-deck result cannot certify
                # changes made to otherwise valid retained pages.
                if (not final_deck_review.get('ok')
                        or _visual_score(final_findings) > _visual_score(deck_findings)):
                    for index in originals:
                        rollback(index)
                    rejected.extend(repaired)
                    repaired = []
                    deck = parse_deck(os.path.join(ctx['deck_dir'], 'deck.pptd'))
                    results = _run_page_visual_reviews(
                        ctx, previews, theme=theme, vqa=vqa, qa_model=qa_model,
                        cache=cache, cache_path=cache_path)
                    final_deck_review = initial_deck_review
                    final_findings = deck_findings
                deck_findings = final_findings
                initial_deck_review = final_deck_review
            _raise_if_aborted(ctx, 'visual QA')
            visual_repair.commit()
        except BaseException:
            # The transaction restores every retained YAML/preview pair.
            raise

    remaining = deck_findings + [finding for result in results
                                for finding in _actionable_visual_findings(result)]
    return {'ran': True,
            'clean': sum(bool(r.get('ok')) and not _actionable_visual_findings(r) for r in results),
            'repaired': len(repaired), 'repairedPages': repaired,
            'rejectedPages': sorted(set(rejected)),
            'reviewed': sum(bool(r.get('ok')) for r in results),
            'unreviewed': sum(not r.get('ok') for r in results),
            'coherenceReviewed': bool(initial_deck_review.get('ok')),
            'coherence_findings': len(deck_findings), 'coherence': deck_findings,
            'remaining': remaining}


def _gate_visual_qa(ctx: dict, artifact: dict) -> list:
    remaining = artifact.get('remaining') or []
    if not remaining:
        return []
    return [f'visual QA has {len(remaining)} unresolved major/blocker finding(s): '
            + '; '.join(str(f.get('issue') or f) for f in remaining[:3])]


# ── Stage: export ─────────────────────────────────────────

def _run_export(ctx: dict) -> dict:
    from lib.slides.pptd import parse_deck
    from lib.slides.export_pptx import export_pptx
    deck = parse_deck(os.path.join(ctx['deck_dir'], 'deck.pptd'))
    out_path = os.path.join(ctx['deck_dir'], f'{_safe_name(deck.title)}.pptx')
    summary = export_pptx(deck, out_path,
                          transition=ctx.get('transition') or 'fade',
                          require_embedded_fonts=True)
    return {'pptx_path': out_path, **summary}


def _safe_name(title: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', '_', (title or 'deck').strip())
    return s[:80] or 'deck'


def _gate_export(ctx: dict, artifact: dict) -> list:
    path = artifact.get('pptx_path') or ''
    if not path or not os.path.isfile(path) or os.path.getsize(path) < 4096:
        return ['export produced no usable PPTX']
    return []


# ── Public entry ──────────────────────────────────────────

def slides_recipe_stages(creative_mode: str = 'director') -> list:
    """Fresh Stage objects on EVERY call.

    Module-level ``Stage('render', _run_render, …)`` constants froze their
    function references at import time, silently defeating the documented
    monkeypatch seams (``_llm_chat`` / ``_run_render``): the unit tests patch
    ``recipe._run_render`` yet the REAL chromium render still ran — green on
    dev machines (browser present), red on CI (no chromium, 2026-08-06).
    Building the graph here resolves each seam at call time, after any patch.
    """
    creative_mode = normalise_creative_mode(creative_mode)
    return [Stage('research', _run_research,
                  resume_ttl_s=_RESEARCH_RESUME_TTL_S,
                  checkpoint_version=_RESEARCH_CHECKPOINT_VERSION),
            Stage('outline', _run_outline, gate=_gate_outline, retry=1,
                  checkpoint_version=(
                      f'{_OUTLINE_CHECKPOINT_VERSION}-{creative_mode}')),
            Stage('design', _run_design),
            Stage('outline_gate', _run_outline_gate),
            Stage('author', _run_author, gate=_gate_author, retry=1),
            Stage('assets', _run_assets),
            Stage('render', _run_render, gate=_gate_render, retry=1),
            Stage('layout_qa', _run_layout_qa, gate=_gate_layout_qa,
                  retry=1),
            Stage('visual_qa', _run_visual_qa, gate=_gate_visual_qa,
                  checkpoint_version='slide-visual-repair-v2'),
            Stage('export', _run_export, gate=_gate_export, retry=1,
                  checkpoint_version='slide-native-export-v2')]


def build_deck_from_topic(topic: str, workdir: str, *, lang: str = 'zh',
                          style: str = '', size=(1280, 720),
                          max_pages: int = _DEFAULT_PAGES,
                          creative_mode: str = 'director',
                          model: str | None = None,
                          owner_user_id: int | None = None,
                          tenant_id: str | None = None,
                          image_urls: list | None = None,
                          interactive_outline: bool = False,
                          task_id: str = '',
                          template_id: str = '',
                          abort_event=None, emit=None) -> dict:
    """Run the full stage graph; returns the export artifact (+ friends).

    Checkpointed at ``<workdir>/pipeline_state.json`` — a crash resumes at
    the first unfinished stage.
    """
    # Chromium receives a file:// URL during preview rendering.  A relative
    # workdir turns into the invalid ``file://relative/path`` form (two slashes
    # mean the first component is parsed as a host), so make the production
    # root absolute once at the public boundary.
    workdir = os.path.abspath(workdir)
    topic = normalise_slide_topic(topic)
    style = normalise_slide_style(style)
    model = normalise_slide_model(model)
    creative_mode = normalise_creative_mode(creative_mode)
    image_urls, input_image_findings = normalise_slide_image_references(
        image_urls)
    max_pages = normalise_slide_page_count(max_pages)
    size = normalise_slide_size(size)
    deck_dir = os.path.join(workdir, 'deck')
    os.makedirs(deck_dir, exist_ok=True)
    ctx = {'topic': topic, 'workdir': workdir, 'deck_dir': deck_dir,
           'lang': lang, 'style': style, 'size': size,
           'max_pages': max_pages, 'model': model or None,
           'creative_mode': creative_mode,
           'owner_user_id': owner_user_id,
           'tenant_id': str(tenant_id or ''),
           'qa_model': model,
           'image_urls': image_urls,
           'input_image_findings': input_image_findings,
           'interactive_outline': bool(interactive_outline),
           'task_id': str(task_id or ''),
           'template_id': str(template_id or ''),
           'abort_event': abort_event, 'emit': emit}
    abort_check = abort_check_from_event(abort_event)
    ctx['abort_check'] = abort_check
    ctx['max_429_attempts'] = production_llm_max_429_attempts()
    from lib.production.image_policy import (
        production_image_max_429_attempts,
    )
    ctx['image_max_429_attempts'] = production_image_max_429_attempts()
    state_path = os.path.join(workdir, 'pipeline_state.json')
    from lib.production.owner_routing import owner_chat_route
    with owner_chat_route(
            owner_user_id, tenant_id=tenant_id, prefer_model=model or '',
            owner_tag=(f'slides:{owner_user_id}' if owner_user_id is not None
                       else 'slides')) as route:
        ctx['provider_pin_id'] = route.pin_id
        if not ctx.get('qa_model'):
            ctx['qa_model'] = route.routed_model
        artifacts = run_stages(
            slides_recipe_stages(creative_mode), ctx, state_path=state_path,
            emit=emit, abort_check=abort_check)
    export = artifacts['export']
    author = artifacts.get('author') or {}
    qa = artifacts.get('visual_qa') or {}
    layout_qa = artifacts.get('layout_qa') or {}
    render = artifacts.get('render') or {}
    outline = _effective_outline(artifacts)
    research = artifacts.get('research') or {}
    quality_issues: list[str] = []
    fallback_pages = author.get('fallback_pages') or []
    if fallback_pages:
        quality_issues.append(
            f'fallback pages remained: {fallback_pages[:12]}')
    asset_findings = author.get('asset_findings') or []
    if asset_findings:
        quality_issues.append(
            f'{len(asset_findings)} planned visual asset(s) were unavailable')
    if research.get('degraded'):
        quality_issues.append(
            str(research.get('reason') or 'research evidence was degraded'))
    if layout_qa.get('ran') is not True:
        quality_issues.append(
            str(layout_qa.get('reason') or 'layout QA did not run'))
    if qa.get('ran') is not True:
        quality_issues.append(str(qa.get('reason') or 'visual QA did not run'))
    elif int(qa.get('unreviewed') or 0):
        quality_issues.append(
            f'{int(qa["unreviewed"])} page(s) were not reviewed by visual QA')
    if qa.get('coherenceReviewed') is False:
        quality_issues.append('whole-deck visual coherence was not reviewed')
    quality_reason = '; '.join(quality_issues[:6])
    return {
        'pptx_path': export['pptx_path'],
        'title': outline['title'],
        'scenario': outline['scenario'],
        'theme_id': artifacts['design']['theme_id'],
        'creative_mode': creative_mode,
        'director': outline.get('director') or {},
        'pages': author.get('total', len(outline['pages'])),
        'authored_pages': author.get('authored', 0),
        'generated_assets': sum(
            len(v) for v in (author.get('assets_by_page') or {}).values()),
        'asset_findings': asset_findings,
        'previews': render.get('previews') or [],
        'qa': qa,
        'layout_qa': layout_qa,
        'package_validation': export.get('packageValidation') or {},
        'quality': {'degraded': bool(quality_issues),
                    'reason': quality_reason,
                    'issues': quality_issues},
        'bytes': export.get('bytes', 0),
        'deck_dir': deck_dir,
    }
