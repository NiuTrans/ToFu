"""lib/motion_video/_recipe.py — Topic → scenes.json front-half (P4).

The missing first half of the motion-video pipeline
(docs/modules/production.md): turn a bare NEWS TOPIC into a
validated ``scenes.json`` the existing engine can render. Three stages,
built on the reusable stage-graph contract (:mod:`lib.production.stages`) so
every stage
is checkpointed and the whole graph is crash-resumable:

    research  → fact cards (each with ≥1 real source URL)   [web_search]
    script    → spoken narration segments (time-budgeted)   [dispatch_chat]
    timeline  → scenes.json with REAL TTS durations         [lib.tts, zero-LLM]

Design decisions (owner-ratified 2026-07-25):

  * **Fact discipline is enforced** (拍板 #4): the ``research`` gate rejects
    any run that produced zero fact cards carrying a real URL, and the
    ``script`` stage always appends a sources scene (片尾来源卡) so the
    finished video credits where its claims came from.
  * **Real durations, not 4.2 chars/s** (owner requirement): ``timeline``
    synthesizes the narration up-front and reads each segment's true audio
    length, so the SRT is measured, not estimated. TTS-degraded hosts fall
    back to a conservative char estimate but keep going (silent video).
  * **Cost is capped** (拍板 #3): ``max_scenes`` bounds scene count; the
    script prompt is one bounded ``dispatch_chat`` call per stage attempt, and
    the stage permits only one gate-driven repair. Wallets own monetary caps;
    this recipe additionally owns finite slot/429 transport attempts.

The heavy dependencies (web search, LLM, TTS) are reached through module-
level indirections so tests can monkeypatch them without a network.
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlparse

from lib.log import get_logger
from lib.production.contracts import (
    normalise_asset_briefs,
    normalise_creative_mode,
    normalise_media_queries,
    normalise_source_ids,
)
from lib.production.research import (
    RESEARCH_RESUME_TTL_S,
    current_fact_errors,
    evidence_checkpoint_version,
    format_research_cards,
    gate_research_bundle,
    research_topic,
    summarise_current_signals,
)
from lib.production.llm_policy import (
    abort_check_from_event,
    production_llm_dispatch_kwargs,
    production_llm_max_429_attempts,
)
from lib.production.stages import Stage, StageAborted, run_stages

logger = get_logger(__name__)

__all__ = ['build_scenes_from_topic', 'RESEARCH', 'SCRIPT', 'TIMELINE',
           'video_recipe_stages', 'script_stage_for_source',
           'normalise_assets', 'ASSET_ROLES', 'ASSET_ROLES_REQUIRING_FILE']

#: Hard ceilings (拍板 #3 — scene-count cap; no money cap here).
_DEFAULT_MAX_SCENES = 8
_MIN_SCENE_S = 2.5
_MAX_SCENE_S = 15.0
#: Conservative narration pace used ONLY when TTS is unavailable (degraded).
_FALLBACK_CHARS_PER_SECOND = 4.2
_RESEARCH_CHECKPOINT_VERSION = evidence_checkpoint_version(freshness='week')
_SCRIPT_CHECKPOINT_VERSION = 'motion-script-v3'
_DIRECTOR_LENSES = (
    ('evidence-motion',
     'Use native data/diagram animation and evidence-led compositions when '
     'facts support them; make every motion communicate a relationship.'),
    ('documentary-multimodal',
     'Use a documentary rhythm with purposeful stock video/GIF/web captures, '
     'spatial continuity, and contrast between live media and native graphics.'),
)
_VISUAL_MODALITIES = (
    'generated-still', 'stock-video', 'stock-gif', 'web-capture',
    'native-data', 'native-diagram', 'kinetic-type', 'hybrid',
)


# ── Seams (monkeypatchable) ───────────────────────────────

def _web_search(query: str, *, user_question: str = '', freshness: str = '',
                max_results: int = 12, deepen: bool = False):
    """Run one web search through the tofu-search service. Returns results."""
    from lib.search_runtime import ensure_search_runtime
    search_runtime = ensure_search_runtime()
    return search_runtime.perform_web_search(
        query, user_question=user_question, freshness=freshness,
        max_results=max_results, deepen=deepen)


def _llm_chat(messages, **kwargs):
    """Non-streaming LLM call through the dispatcher. Returns (content, usage)."""
    from lib.llm_dispatch.api import dispatch_chat
    return dispatch_chat(messages, **kwargs)


def _context_abort_check(ctx: dict):
    """Return the recipe's one request-level cancellation predicate."""
    callback = ctx.get('abort_check')
    if callable(callback):
        return callback
    return abort_check_from_event(ctx.get('abort_event'))


def _raise_if_script_aborted(ctx: dict, where: str) -> None:
    abort_check = _context_abort_check(ctx)
    if abort_check is not None and abort_check():
        raise StageAborted(f'aborted {where} script dispatch')


def _tts_durations(scenes: list[dict], out_dir: str, *, voice=None, speed=None,
                   alignment: str = 'loose', abort_event=None,
                   owner_user_id: int | None = None,
                   tenant_id: str | None = None) -> dict:
    """Synthesize per-scene narration + return the alignment manifest."""
    from lib import motion_video as mv
    return mv.synthesize_scene_narrations(
        scenes, out_dir, voice=voice, speed=speed, alignment=alignment,
        abort_event=abort_event, owner_user_id=owner_user_id,
        tenant_id=tenant_id)


# ── Fact-card extraction (zero-LLM) ───────────────────────

_URL_RE = re.compile(r'https?://[^\s)>\]"\']+')


def _cards_from_results(results) -> list[dict]:
    """Turn web-search results into fact cards {point, url, title}.

    A card is kept ONLY when it carries a real http(s) URL — the zero-LLM
    fact-discipline gate (拍板 #4) reads this list.
    """
    cards: list[dict] = []
    seen: set[str] = set()
    for r in results or []:
        if not isinstance(r, dict):
            continue
        url = (r.get('url') or r.get('link') or '').strip()
        if not url:
            body = str(r.get('content') or r.get('snippet') or '')
            m = _URL_RE.search(body)
            url = m.group(0) if m else ''
        if not url or not url.lower().startswith(('http://', 'https://')):
            continue
        if url in seen:
            continue
        seen.add(url)
        point = (r.get('snippet') or r.get('content') or r.get('title')
                 or '').strip()
        point = re.sub(r'\s+', ' ', point)[:400]
        if not point:
            continue
        cards.append({'id': f'S{len(cards) + 1}', 'point': point, 'url': url,
                      'host': urlparse(url).netloc.lower().removeprefix('www.'),
                      'title': (r.get('title') or '').strip()[:200],
                      'published_at': '', 'query_lane': 'background',
                      'query_lanes': ['background'], 'freshness': 'none',
                      'source_hints': []})
    return cards


# ── Stage: research ───────────────────────────────────────

def _run_research(ctx: dict) -> dict:
    topic = ctx['topic']

    def _search(query: str, **kwargs):
        # Keep the historical monkeypatch seam usable: older callers replace
        # _web_search with a three-argument function. The live function owns
        # the richer max-results/deepen knobs.
        import inspect
        params = inspect.signature(_web_search).parameters.values()
        supports_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD
                              for p in params)
        names = {p.name for p in params}
        common = {
            'user_question': kwargs.get('user_question', topic),
            'freshness': kwargs.get('freshness', ''),
        }
        if supports_kwargs or {'max_results', 'deepen'} <= names:
            common.update({
                'max_results': kwargs.get('max_results', 12),
                'deepen': kwargs.get('deepen', False),
            })
        return _web_search(query, **common)

    return research_topic(
        topic, max_cards=18, current_freshness='week',
        fallback_unfiltered_current=True, search_fn=_search)


def _gate_research(ctx: dict, artifact: dict) -> list:
    return gate_research_bundle(artifact)


RESEARCH = Stage(
    'research', _run_research, gate=_gate_research, retry=1,
    resume_ttl_s=RESEARCH_RESUME_TTL_S,
    checkpoint_version=_RESEARCH_CHECKPOINT_VERSION)


# ── Stage: script ─────────────────────────────────────────

def _build_script_prompt(topic: str, cards: list[dict], *, lang: str,
                         max_scenes: int, research: dict | None = None,
                         gate_feedback: list | None = None) -> str:
    import datetime
    research = dict(research or {})
    research.setdefault('cards', cards)
    today = (str(research.get('as_of') or '')[:10]
             or datetime.date.today().isoformat())
    numbered = format_research_cards(research)
    signals = (research.get('current_signals')
               or summarise_current_signals(cards))
    signal_doc = (
        'Automated temporal scan (not an authority verdict): '
        f'current_status_sources={",".join(signals.get("status_source_ids") or []) or "none"}; '
        f'prices corroborated by 2+ independent hosts='
        f'{",".join(signals.get("corroborated_price_values") or []) or "none"}; '
        f'single-host price candidates='
        f'{",".join(signals.get("single_source_price_values") or []) or "none"}.\n')
    feedback_doc = ''
    if gate_feedback:
        feedback_doc = (
            'Previous script attempt was rejected. Correct every item below:\n- '
            + '\n- '.join(str(item) for item in gate_feedback[:6]) + '\n')
    if lang == 'zh':
        return (
            f'今天是 {today}。你是一名品牌短片与科普短视频编导。请把下面这些带来源的事实卡片,'
            f'改写成一段口语化、准确、适合配音的短视频脚本,主题是《{topic}》。\n\n'
            '严格要求:\n'
            '1. 输出 JSON:{"title":"...","beats":[{"text":"口播",'
            '"on_screen":"画面短文案","visual":"构图与动效方向",'
            '"visual_modality":"generated-still|stock-video|stock-gif|'
            'web-capture|native-data|native-diagram|kinetic-type|hybrid",'
            '"source_ids":["S1"],'
            '"assets":[{"role":"subject|diagram|background",'
            '"semantic_target":"该素材必须在画面中支撑的具体对象/部位/判断",'
            '"prompt":"English text-free image prompt"}],'
            '"media_queries":[{"kind":"image|video|gif|webpage",'
            '"query":"可检索的具体查询","semantic_target":"必须真实可见的对象/行为"}]}]}。\n'
            f'2. beats 数量在 3 到 {max_scenes - 1} 之间(不含片尾来源卡,系统会自动追加)。\n'
            '3. text 每段 1~3 句,口语、连贯、可直接配音;on_screen 是不超过18字的标题式短句。\n'
            '4. visual 明确主体、景别、空间层次和一个可执行动效,相邻镜头不能同构。\n'
            '5. 每个内容镜头给 1~2 个 assets。具体汽车/人物/场景用 subject;解释结构用 diagram;'
            '纯转场才可为空。prompt 必须英文、无文字、无 Logo、无水印,不得要求生成图表文字。\n'
            '   semantic_target 必须明确写出素材要证明的可见对象、部位或关系;例如要讲'
            '“纯平地板”就必须要求看见地板/中央通道,不能用窗户或泛化座舱图代替。\n'
            '6. 每个包含事实陈述的 beat 必须在 source_ids 列出支持它的 [S#];'
            '不得用“片尾有来源”代替逐镜证据关联。\n'
            '7. 先处理当前状态:只要 current/official 车道出现发布、预售、价格'
            '更新,脚本必须引用对应 [S#],严格区分预售价、最终售价、传闻和估算;'
            '较新证据已公布预售价时不得说“价格尚未公布”。\n'
            '8. 精确价格/日期/参数优先第一方页面;若只有二手来源,必须两家独立'
            '来源一致才写成确定数字。只依据事实卡片,不得编造。\n'
            '9. stock-video/stock-gif/web-capture 镜头必须给 media_queries;'
            'native-data/native-diagram 不得伪造数据,优先用原生可编辑图形。\n'
            '10. 只输出 JSON 本身,不要解释、不要代码围栏。\n\n'
            f'{signal_doc}{feedback_doc}事实卡片:\n{numbered}')
    return (
        f'Today is {today}. You are a brand-film and explainer writer. Rewrite the sourced fact '
        f'cards below into a spoken, accurate, voice-over-ready short-video '
        f'script about "{topic}".\n\n'
        'Strict requirements:\n'
        '1. Output JSON: {"title":"...","beats":[{"text":"...",'
        '"on_screen":"...","visual":"...","visual_modality":"'
        'generated-still|stock-video|stock-gif|web-capture|native-data|'
        'native-diagram|kinetic-type|hybrid","source_ids":["S1"],'
        '"assets":[{"role":'
        '"subject|diagram|background","semantic_target":"visible object/detail '
        'that supports the claim","prompt":"English text-free image '
        'prompt"}],"media_queries":[{"kind":"image|video|gif|webpage",'
        '"query":"specific searchable query","semantic_target":"object or '
        'action that must be visible"}]}]}.\n'
        f'2. Between 3 and {max_scenes - 1} beats (the sources card is '
        'appended automatically).\n'
        '3. text is 1-3 spoken sentences; on_screen is a short headline; '
        'visual names composition, depth and one executable motion idea.\n'
        '4. Give each content beat 1-2 real asset briefs; prompts are English, '
        'text-free, logo-free and watermark-free. Never ask image generation '
        'to draw charts or labels. semantic_target names the visible object, '
        'part or relationship the asset must actually prove; never substitute '
        'a generic cabin/window image for a floor or rail claim.\n'
        '5. Every factual beat lists its supporting [S#] values in source_ids; '
        'end credits do not replace beat-level grounding.\n'
        '6. Address current-state evidence first. Distinguish presale price, '
        'final price, rumor and estimate; never say price is unannounced when '
        'newer evidence announces a presale price. Exact prices/dates/specs '
        'need a first-party card or agreement across two independent hosts.\n'
        '7. Ground every claim in the cards; invent no specs, prices, dates or promises.\n'
        '8. stock-video/stock-gif/web-capture beats require media_queries. '
        'Native data/diagram beats use editable graphics and never invent data.\n'
        '9. Output ONLY the JSON — no commentary, no fences.\n\n'
        f'{signal_doc}{feedback_doc}Fact cards:\n{numbered}')


_JSON_BLOCK_RE = re.compile(r'\{.*\}', re.DOTALL)


def _parse_script(content: str) -> dict:
    text = (content or '').strip()
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        raise ValueError('no JSON object in script reply')
    raw = json.loads(m.group(0))
    if not isinstance(raw, dict):
        raise ValueError('script JSON is not an object')
    segs = raw.get('segments')
    beats = raw.get('beats')
    if not isinstance(segs, list) and not isinstance(beats, list):
        raise ValueError('script JSON has neither segments nor beats array')
    return raw


def _sources_line(cards: list[dict], lang: str) -> str:
    hosts: list[str] = []
    seen: set[str] = set()
    for c in cards:
        from urllib.parse import urlparse
        try:
            host = urlparse(c['url']).netloc.replace('www.', '')
        except Exception as _e:
            logger.debug('sources line: failed (%s)', _e)
            host = ''
        if host and host not in seen:
            seen.add(host)
            hosts.append(host)
        if len(hosts) >= 4:
            break
    joined = ' · '.join(hosts) if hosts else ''
    if lang == 'zh':
        return f'资料来源:{joined}' if joined else '资料来源见简介'
    return f'Sources: {joined}' if joined else 'Sources in description'


def _normalise_visual_modality(value, *, assets: list, media: list) -> str:
    modality = str(value or '').strip().lower()
    if modality in _VISUAL_MODALITIES:
        return modality
    if any(item.get('kind') == 'video' for item in media):
        return 'stock-video'
    if any(item.get('kind') == 'gif' for item in media):
        return 'stock-gif'
    if any(item.get('role') == 'diagram' for item in assets):
        return 'native-diagram'
    return 'generated-still' if assets else 'kinetic-type'


def _script_candidate(raw: dict, *, topic: str, lang: str, research: dict,
                      max_scenes: int, usage: dict) -> dict:
    """Normalize one script draft into the checkpointed beat contract."""
    cards = research.get('cards') or []
    raw_beats = raw.get('beats') if isinstance(raw.get('beats'), list) else []
    beats = []
    valid_source_ids = [str(card.get('id') or '') for card in cards]
    for item in raw_beats:
        if not isinstance(item, dict):
            continue
        spoken = re.sub(r'\s+', ' ', str(item.get('text') or '')).strip()
        if not spoken:
            continue
        assets = normalise_assets(item.get('assets'))
        media_queries = normalise_media_queries(item.get('media_queries'))
        beats.append({
            'text': spoken,
            'on_screen': re.sub(r'\s+', ' ',
                                str(item.get('on_screen') or '')).strip()[:88],
            'visual': re.sub(r'\s+', ' ',
                             str(item.get('visual') or '')).strip()[:800],
            'source_ids': normalise_source_ids(
                item.get('source_ids'), valid_ids=valid_source_ids),
            'assets': assets,
            'media_queries': media_queries,
            'visual_modality': _normalise_visual_modality(
                item.get('visual_modality'), assets=assets,
                media=media_queries),
        })
    if beats:
        beats = beats[:max_scenes - 1]
        segments = [b['text'] for b in beats]
    else:
        # Backward-compatible with checkpoints and older/custom models.
        segments = [re.sub(r'\s+', ' ', str(s)).strip()
                    for s in raw.get('segments') or [] if str(s).strip()]
    segments = segments[:max_scenes - 1]  # leave room for the sources card
    title = (raw.get('title') or topic).strip()
    # 拍板 #4: credit the sources at the end — but as a SILENT VISUAL end
    # card (owner 2026-07-26), never a narration segment. The timeline stage
    # turns this line into the final spoken=False scene; if it stayed here,
    # the TTS pass would read domain names aloud.
    sources_line = _sources_line(cards, lang)
    source_ids = normalise_source_ids(
        [sid for beat in beats for sid in beat.get('source_ids') or []]
        + list(raw.get('source_ids') or []),
        valid_ids=valid_source_ids, limit=12)
    return {'title': title, 'segments': segments,
            'beats': beats,
            'source_ids': source_ids,
            'sources_line': sources_line,
            'research_as_of': str(research.get('as_of') or ''),
            'usage': usage if isinstance(usage, dict) else {}}


def _script_errors(ctx: dict, artifact: dict) -> list:
    """Pure script gate used before critic selection and by the Stage gate."""
    segs = artifact.get('segments') or []
    errors = []
    if len(segs) < 2:
        return [f'script has too few segments ({len(segs)}; need ≥2)']
    if any(not s.strip() for s in segs):
        return ['script has an empty segment']
    research = ctx.get('artifacts', {}).get('research') or {}
    script_text = '\n'.join(
        list(segs)
        + [str(beat.get('on_screen') or '')
           for beat in artifact.get('beats') or []])
    errors.extend(current_fact_errors(
        research, script_text, cited_ids=artifact.get('source_ids') or []))
    return errors


def _script_score(candidate: dict) -> int:
    beats = candidate.get('beats') or []
    modalities = {str(beat.get('visual_modality') or '') for beat in beats}
    grounded = sum(bool(beat.get('source_ids')) for beat in beats)
    semantic_assets = sum(
        bool(asset.get('semantic_target'))
        for beat in beats for asset in (beat.get('assets') or []))
    media = sum(len(beat.get('media_queries') or []) for beat in beats)
    visual_directions = {
        str(beat.get('visual') or '').strip().lower() for beat in beats
        if beat.get('visual')
    }
    return (len(beats) * 3 + len(modalities) * 8 + grounded * 4
            + semantic_assets * 2 + media * 3 + len(visual_directions) * 2)


def _script_digest(candidate: dict) -> dict:
    return {
        'title': candidate.get('title'),
        'beats': [{
            key: beat.get(key) for key in (
                'text', 'on_screen', 'visual', 'visual_modality',
                'source_ids', 'assets', 'media_queries') if beat.get(key)
        } for beat in (candidate.get('beats') or [])],
    }


def _script_critic_choice(ctx: dict, candidates: list[dict]) \
        -> tuple[int, str, dict]:
    prompt = (
        'You are an independent short-video creative director. Select the '
        'stronger script/storyboard. Judge factual grounding, spoken narrative '
        'flow, shot and modality diversity, asset-to-claim relevance, executable '
        'motion, and visual continuity. Penalize repeated templates, decorative '
        'motion, generic media searches, and visuals that do not prove the '
        'spoken claim. Output ONLY JSON: {"winner":1,"reason":"specific '
        'concise reason","scores":[{"content":0,"evidence":0,"motion":0,'
        '"coherence":0},{"content":0,"evidence":0,"motion":0,'
        '"coherence":0}]}. Scores are integers 0..10. Candidates:\n'
        + json.dumps([_script_digest(item) for item in candidates],
                     ensure_ascii=False, separators=(',', ':'))[:28000])
    _raise_if_script_aborted(ctx, 'before critic')
    content, usage = _llm_chat(
        [{'role': 'user', 'content': prompt}], max_tokens=1200,
        temperature=0.0, prefer_model=ctx.get('model') or None,
        strict_model=bool(ctx.get('model')),
        **production_llm_dispatch_kwargs(
            abort_check=_context_abort_check(ctx),
            max_429_attempts=ctx.get('max_429_attempts')),
        log_prefix='[Recipe:script-critic]')
    _raise_if_script_aborted(ctx, 'after critic')
    match = _JSON_BLOCK_RE.search(content or '')
    if not match:
        raise ValueError('script critic reply has no JSON object')
    raw = json.loads(match.group(0))
    winner = int(raw.get('winner') or 0) - 1
    if winner < 0 or winner >= len(candidates):
        raise ValueError('script critic selected an invalid candidate')
    reason = re.sub(r'\s+', ' ', str(raw.get('reason') or '')).strip()[:500]
    return winner, reason or 'critic selected the stronger script', {
        'usage': usage if isinstance(usage, dict) else {},
        'scores': raw.get('scores') if isinstance(raw.get('scores'), list) else [],
    }


def _run_script(ctx: dict) -> dict:
    topic = ctx['topic']
    lang = ctx.get('lang', 'zh')
    max_scenes = ctx.get('max_scenes', _DEFAULT_MAX_SCENES)
    research = ctx['artifacts']['research']
    cards = research['cards']
    mode = normalise_creative_mode(
        ctx.get('creative_mode'), default='standard')
    gate_feedback = list(ctx.pop('_script_gate_feedback', []) or [])
    base_prompt = _build_script_prompt(
        topic, cards, lang=lang, max_scenes=max_scenes, research=research,
        gate_feedback=gate_feedback)
    lenses = _DIRECTOR_LENSES if mode == 'director' else (('standard', ''),)
    candidates: list[dict] = []
    rejected: list[str] = []
    candidate_attempts: list[dict] = []
    for lens_name, lens in lenses:
        usage: dict = {}
        prompt = base_prompt
        if lens:
            prompt += ('\nDirector candidate lens (binding): ' + lens
                       + '\nDo not mention this lens in the output.')
        try:
            before_label = ('before' if mode == 'standard'
                            else f'before candidate {lens_name}')
            after_label = ('after' if mode == 'standard'
                           else f'after candidate {lens_name}')
            _raise_if_script_aborted(ctx, before_label)
            content, usage = _llm_chat(
                [{'role': 'user', 'content': prompt}],
                max_tokens=4096, temperature=0.45,
                prefer_model=ctx.get('model') or None,
                strict_model=bool(ctx.get('model')),
                **production_llm_dispatch_kwargs(
                    abort_check=_context_abort_check(ctx),
                    max_429_attempts=ctx.get('max_429_attempts')),
                log_prefix=f'[Recipe:script:{lens_name}]')
            _raise_if_script_aborted(ctx, after_label)
            candidate = _script_candidate(
                _parse_script(content), topic=topic, lang=lang,
                research=research, max_scenes=max_scenes, usage=usage)
            errors = _script_errors(ctx, candidate)
            if errors:
                candidate_attempts.append({
                    'lens': lens_name, 'passed': False,
                    'usage': usage if isinstance(usage, dict) else {},
                    'errors': errors[:3],
                })
                rejected.extend(errors)
                logger.info('[Recipe:script] rejected %s: %s', lens_name,
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
            logger.warning('[Recipe:script] candidate %s failed: %s',
                           lens_name, exc)
    if not candidates:
        ctx['_script_gate_feedback'] = rejected[:6]
        raise ValueError('no script candidate passed: '
                         + '; '.join(rejected[:4]))
    fallback_scores = [_script_score(candidate) for candidate in candidates]
    winner = max(range(len(candidates)), key=lambda index: fallback_scores[index])
    reason = 'deterministic fallback selected the richer valid script'
    critic_meta: dict = {'usage': {}, 'scores': []}
    if len(candidates) > 1:
        try:
            winner, reason, critic_meta = _script_critic_choice(ctx, candidates)
        except StageAborted:
            raise
        except Exception as exc:
            from lib.llm_errors import AbortedError
            if isinstance(exc, AbortedError):
                raise StageAborted(str(exc)) from exc
            logger.warning('[Recipe:script] critic failed; using score: %s', exc)
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
    logger.info('[Recipe:script] topic=%r → %d segment(s), sources card %r, '
                'title=%r mode=%s winner=%d', topic[:60],
                len(selected['segments']), selected['sources_line'][:40],
                selected['title'][:60], mode, winner + 1)
    return selected


def _gate_script(ctx: dict, artifact: dict) -> list:
    errors = _script_errors(ctx, artifact)
    if errors:
        ctx['_script_gate_feedback'] = list(errors)
    else:
        ctx.pop('_script_gate_feedback', None)
    return errors


SCRIPT = Stage(
    'script', _run_script, gate=_gate_script, retry=1,
    checkpoint_version=f'{_SCRIPT_CHECKPOINT_VERSION}-standard')


# ── Shared: prose → spoken beats + on-screen captions ─────

def _build_source_beat_prompt(source_text: str, *, lang: str, max_scenes: int,
                              char_budget: int, caption_capacity: int) -> str:
    """Prompt for rewriting EXISTING prose (a paper report) into beats.

    Distinct from :func:`_build_script_prompt`, which writes a script from
    sourced fact CARDS. Both produce the same beat shape, so the paper path
    and the topic path share one downstream contract.
    """
    body = source_text[:24000]
    if lang == 'zh':
        return (
            f'你是科普短视频编导。把下面这份资料改写成一条短视频的分镜口播稿。\n\n'
            '严格要求:\n'
            f'1. 输出 JSON:{{"beats": [{{"text": "...", "on_screen": "...", '
            f'"visual": "...", "assets": [{{"role": "subject", "prompt": "..."}}]}}]}}。\n'
            f'2. beats 数量 3 到 {max_scenes} 个。\n'
            f'3. text = 该镜的口播旁白,**每条不超过 {char_budget} 字**,口语、'
            '连贯、可直接配音。\n'
            f'4. on_screen = 该镜画面上出现的短文案,**每条不超过 '
            f'{caption_capacity} 字**,是标题式短句而不是旁白全文。\n'
            '5. visual = 该镜的美术方向(构图/主体/动效意象),一句话。\n'
            '6. assets = 该镜**必须真实生成的图像素材**清单,1~2 件。每件写 '
            '{"role": "subject|diagram|background", "prompt": "..."}。\n'
            '   - subject:该镜的主体插画(讲一个具体事物/场景时用);\n'
            '   - diagram:解释性图示(讲机制/流程/对比时用);\n'
            '   - background:仅作背景肌理,可选。\n'
            '   prompt 用英文写,包含:主体 + 风格(flat vector / UI illustration / '
            'paper-cut collage 等)+ 配色 + negative 约束。**不要写"图表"这类'
            '可以用代码画出来的东西当 subject**——那应该由构图代码画。\n'
            '   纯过渡/停顿镜可以给空数组 [],但必须在 visual 里说明它是过渡。\n'
            '7. 只依据资料,不得编造;只输出 JSON,不要解释或代码围栏。\n\n'
            f'资料:\n{body}')
    return (
        'You are a science-explainer video writer. Rewrite the material below '
        'into the beats of a short video.\n\n'
        'Strict requirements:\n'
        '1. Output JSON: {"beats": [{"text": "...", "on_screen": "...", '
        '"visual": "...", "assets": [{"role": "subject", "prompt": "..."}]}]}.\n'
        f'2. Between 3 and {max_scenes} beats.\n'
        f'3. text = spoken narration for that beat, AT MOST {char_budget} '
        'characters each, voice-over ready.\n'
        f'4. on_screen = the short caption drawn ON the frame, AT MOST '
        f'{caption_capacity} characters each — a title-style line, NOT the '
        'narration.\n'
        '5. visual = art direction for that beat (composition / subject / '
        'motion idea), one sentence.\n'
        '6. assets = the image assets this beat MUST really generate, 1-2 of '
        'them. Each is {"role": "subject|diagram|background", "prompt": "..."}.\n'
        '   - subject: the hero illustration (a concrete thing or scene);\n'
        '   - diagram: an explanatory graphic (a mechanism, flow, contrast);\n'
        '   - background: texture only, optional.\n'
        '   Write prompt in English: subject + style (flat vector / UI '
        'illustration / paper-cut collage) + palette + negative constraints. '
        'Do NOT ask for a bar chart as a subject — code draws those better.\n'
        '   A pure transition/hold beat may use [], but its visual must say so.\n'
        '7. Ground everything in the material; output ONLY the JSON.\n\n'
        f'Material:\n{body}')


#: Asset roles a beat may declare, and what each one means for the floor.
#:
#: ``subject`` / ``diagram`` are IMAGERY the composition cannot draw itself, so
#: the role-aware floor requires a real generated file for them. ``background``
#: is texture: nice to have, never required, because a gradient or an inline
#: SVG backdrop is a legitimate answer.
ASSET_ROLES = ('subject', 'diagram', 'background')
#: Roles that OBLIGE the scene to carry a real image file on disk.
ASSET_ROLES_REQUIRING_FILE = ('subject', 'diagram')
#: Longest asset prompt we keep (the image API caps at ~1500).
_MAX_ASSET_PROMPT = 1200


def normalise_assets(raw) -> list[dict]:
    """Validate a beat's ``assets`` array into ``[{'role', 'prompt'}]``.

    Roles are checked against :data:`ASSET_ROLES` rather than passed through:
    an unrecognised role would flow into the floor, which decides whether a
    real file is REQUIRED — so a typo or an invented role would silently
    change what the gate demands. An unknown role degrades to ``background``
    (the never-required tier) because inventing an obligation the author was
    never told about is worse than under-asking.

    A prompt-less entry is dropped: an asset request with no prompt cannot be
    generated, and keeping it would create an obligation nothing can satisfy.
    """
    return normalise_asset_briefs(
        raw, allowed_roles=ASSET_ROLES, fallback_role='background',
        max_items=3, prompt_limit=_MAX_ASSET_PROMPT,
        log_prefix='[Recipe]')


def script_stage_for_source(source_text: str, *, lang: str = 'zh',
                            max_scenes: int = _DEFAULT_MAX_SCENES,
                            char_budget: int = 58,
                            caption_capacity: int = 88,
                            model: str | None = None, abort_check=None,
                            max_429_attempts: int | None = None) -> list[dict]:
    """Rewrite existing prose into bounded beats. Returns beat dicts.

    The single implementation of "prose → spoken beats + on-screen captions +
    art direction" (charter reuse rule): the paper video-abstract path calls
    THIS instead of maintaining its own splitter. Each beat is
    ``{'text', 'on_screen', 'visual'}``.

    ``model`` is the caller's preferred dispatch model (None = dispatcher
    default) — the paper panels let the user pick it, so it must reach the
    dispatch rather than stop at this seam.

    Raises on an unusable model reply so the caller can fall back to its
    deterministic path; never returns malformed beats.
    """
    prompt = _build_source_beat_prompt(
        source_text, lang=lang, max_scenes=max_scenes,
        char_budget=char_budget, caption_capacity=caption_capacity)
    if abort_check is not None and abort_check():
        from lib.llm_errors import AbortedError
        raise AbortedError('aborted before source-beat dispatch')
    content, _usage = _llm_chat([{'role': 'user', 'content': prompt}],
                                max_tokens=4096, temperature=0.4,
                                prefer_model=model,
                                strict_model=bool(model),
                                **production_llm_dispatch_kwargs(
                                    abort_check=abort_check,
                                    max_429_attempts=max_429_attempts),
                                log_prefix='[Recipe:source-beats]')
    if abort_check is not None and abort_check():
        from lib.llm_errors import AbortedError
        raise AbortedError('aborted after source-beat dispatch')
    text = (content or '').strip()
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        raise ValueError('no JSON object in source-beat reply')
    raw = json.loads(m.group(0))
    if not isinstance(raw, dict) or not isinstance(raw.get('beats'), list):
        raise ValueError('source-beat JSON has no beats array')
    beats: list[dict] = []
    for item in raw['beats']:
        if not isinstance(item, dict):
            continue
        spoken = re.sub(r'\s+', ' ', str(item.get('text') or '')).strip()
        if not spoken:
            continue
        beats.append({
            'text': spoken,
            'on_screen': re.sub(r'\s+', ' ',
                                str(item.get('on_screen') or '')).strip(),
            'visual': re.sub(r'\s+', ' ',
                             str(item.get('visual') or '')).strip(),
            'assets': normalise_assets(item.get('assets')),
            'media_queries': normalise_media_queries(item.get('media_queries')),
        })
    if len(beats) < 2:
        raise ValueError(f'source-beat reply yielded {len(beats)} usable beat(s)')
    logger.info('[Recipe:source-beats] %d beat(s) from %d chars of source',
                len(beats), len(source_text or ''))
    return beats[:max_scenes]



# ── Stage: timeline ───────────────────────────────────────

#: Fixed on-screen duration of the SILENT sources end card.
_SOURCES_CARD_S = 3.5


def _provisional_scenes(segments: list[str], sources_line: str = '',
                        beats: list[dict] | None = None) -> list[dict]:
    """A first-cut storyboard (contiguous from 0) used only to drive TTS.

    Durations here are placeholders; the real durations come from the TTS
    manifest and are written back before this becomes the final scenes.json.
    ``sources_line`` becomes the final scene — a SILENT visual end card
    (spoken=False, fixed duration, never sent to TTS).
    """
    scenes: list[dict] = []
    cursor = 0.0
    for i, seg in enumerate(segments, 1):
        beat = beats[i - 1] if beats and i <= len(beats) else {}
        est = max(_MIN_SCENE_S, min(len(seg) / _FALLBACK_CHARS_PER_SECOND,
                                    _MAX_SCENE_S))
        scenes.append({
            'id': f'scene-{i:03d}',
            'start': round(cursor, 3),
            'end': round(cursor + est, 3),
            'text': seg,
            'on_screen': str(beat.get('on_screen') or ''),
            'visual': str(beat.get('visual') or ''),
            'source_ids': list(beat.get('source_ids') or []),
            'assets': list(beat.get('assets') or []),
            'media_queries': list(beat.get('media_queries') or []),
            'visual_modality': str(
                beat.get('visual_modality') or 'kinetic-type'),
        })
        cursor += est
    if sources_line:
        scenes.append({'id': f'scene-{len(scenes) + 1:03d}',
                       'start': round(cursor, 3),
                       'end': round(cursor + _SOURCES_CARD_S, 3),
                       'text': sources_line, 'visual': 'sources',
                       'spoken': False})
    return scenes


def _rescore_from_manifest(scenes: list[dict], manifest: dict) -> list[dict]:
    """Rewrite scene start/end from the TTS manifest's real durations."""
    by_id = {e['scene_id']: e for e in manifest.get('scenes', [])}
    cursor = 0.0
    for sc in scenes:
        entry = by_id.get(sc['id'])
        dur = (float(entry['target_duration']) if entry
               else float(sc['end']) - float(sc['start']))
        dur = max(_MIN_SCENE_S, round(dur, 3))
        sc['start'] = round(cursor, 3)
        sc['end'] = round(cursor + dur, 3)
        cursor += dur
    return scenes


def _run_timeline(ctx: dict) -> dict:
    script = ctx['artifacts']['script']
    scenes = _provisional_scenes(script['segments'],
                                 script.get('sources_line') or '',
                                 script.get('beats') or [])
    audio_dir = os.path.join(ctx['workdir'], 'audio')
    manifest = {'ok': False, 'degraded': True}
    if ctx.get('narration', True):
        # TTS only ever sees SPOKEN scenes — the silent sources card keeps its
        # fixed duration and is voiced by nothing (owner: no robot reading URLs).
        spoken = [s for s in scenes if s.get('spoken', True)]
        try:
            manifest = _tts_durations(
                spoken, audio_dir, voice=ctx.get('voice') or None,
                speed=ctx.get('speed'), alignment=ctx.get('alignment', 'loose'),
                abort_event=ctx.get('abort_event'),
                owner_user_id=ctx.get('owner_user_id'),
                tenant_id=ctx.get('tenant_id'))
        except Exception as e:
            logger.warning('[Recipe:timeline] TTS pass failed (%s) — '
                           'falling back to char-estimated durations', e)
            manifest = {'ok': False, 'degraded': True}
    if manifest.get('ok'):
        scenes = _rescore_from_manifest(scenes, manifest)
        # Persist the manifest so the engine's narrate stage REUSES this audio
        # instead of re-synthesizing (resumable + no double-TTS).
        from lib.json_store import write_json_atomic as _wja
        _wja(os.path.join(audio_dir, 'manifest.json'), manifest)
        logger.info('[Recipe:timeline] %d scene(s) timed from real TTS audio',
                    len(scenes))
    else:
        logger.info('[Recipe:timeline] %d scene(s), char-estimated durations '
                    '(TTS %s)', len(scenes),
                    'degraded' if manifest.get('degraded') else 'off')
    # Remotion's composition schema and video-shotcraft's recipe cards point
    # at the same missing layer in our former design: a scene needs a typed
    # shot contract before it reaches a renderer.  Normalize it here so the
    # checkpointed scenes.json itself is complete; the engine repeats this
    # idempotently for uploaded/legacy storyboards.
    from lib.motion_video._creative_plan import normalise_film_plan
    normalise_film_plan(scenes)
    scenes_path = os.path.join(ctx['workdir'], 'scenes.json')
    from lib.json_store import write_json_atomic
    write_json_atomic(scenes_path, scenes)
    return {'scenes_path': scenes_path, 'scenes': len(scenes),
            'timed_from_audio': bool(manifest.get('ok')),
            'creative_mode': script.get('creative_mode') or 'standard',
            'director': script.get('director') or {},
            'span_s': round(scenes[-1]['end'] - scenes[0]['start'], 3)
            if scenes else 0.0}


def _gate_timeline(ctx: dict, artifact: dict) -> list:
    path = artifact.get('scenes_path')
    if not path or not os.path.isfile(path):
        return ['timeline did not write scenes.json']
    try:
        with open(path, encoding='utf-8') as f:
            scenes = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug('gate timeline: unreadable/malformed JSON (%s)', e)
        return [f'scenes.json unreadable: {e}']
    if not scenes:
        return ['scenes.json is empty']
    from lib import motion_video as mv
    span = (float(scenes[0]['start']), float(scenes[-1]['end']))
    errors = mv.check_storyboard(scenes, span)
    from lib.motion_video._shot_recipes import shot_contract_errors
    return errors + shot_contract_errors(scenes)


TIMELINE = Stage('timeline', _run_timeline, gate=_gate_timeline, retry=0)


def video_recipe_stages(creative_mode: str = 'director') -> list:
    """The ordered front-half stage list: research → script → timeline."""
    creative_mode = normalise_creative_mode(creative_mode)
    script = Stage(
        'script', _run_script, gate=_gate_script, retry=1,
        checkpoint_version=f'{_SCRIPT_CHECKPOINT_VERSION}-{creative_mode}')
    return [RESEARCH, script, TIMELINE]


# ── Public entry ──────────────────────────────────────────

def build_scenes_from_topic(topic: str, workdir: str, *, lang: str = 'zh',
                            max_scenes: int = _DEFAULT_MAX_SCENES,
                            creative_mode: str = 'director',
                            narration: bool = True, voice: str = '',
                            speed=None, alignment: str = 'loose',
                            model: str | None = None,
                            owner_user_id: int | None = None,
                            tenant_id: str | None = None,
                            abort_event=None,
                            emit=None) -> dict:
    """Run research → script → timeline; return the timeline artifact.

    The stage graph is checkpointed at ``<workdir>/pipeline_state.json`` so a
    crash resumes at the first unfinished stage (already-synthesized audio and
    the written scenes.json are not recomputed).

    Returns ``{'scenes_path', 'scenes', 'timed_from_audio', 'span_s'}``.
    Raises StageFailed / StageAborted on unrecoverable failure.
    """
    os.makedirs(workdir, exist_ok=True)
    creative_mode = normalise_creative_mode(creative_mode)
    ctx = {
        'topic': topic, 'workdir': workdir, 'lang': lang,
        'max_scenes': max_scenes, 'narration': narration, 'voice': voice,
        'speed': speed, 'alignment': alignment, 'abort_event': abort_event,
        'model': model, 'owner_user_id': owner_user_id,
        'tenant_id': tenant_id,
        'creative_mode': creative_mode,
    }
    abort_check = (lambda: bool(abort_event is not None
                                and abort_event.is_set()))
    ctx['abort_check'] = abort_check
    ctx['max_429_attempts'] = production_llm_max_429_attempts()
    state_path = os.path.join(workdir, 'pipeline_state.json')
    artifacts = run_stages(
        video_recipe_stages(creative_mode), ctx, state_path=state_path, emit=emit,
        abort_check=abort_check)
    return artifacts['timeline']
