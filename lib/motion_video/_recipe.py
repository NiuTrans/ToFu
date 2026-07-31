"""lib/motion_video/_recipe.py — Topic → scenes.json front-half (P4).

The missing first half of the motion-video pipeline
(docs/PRODUCTION_PIPELINE_DESIGN.md §2.2): turn a bare NEWS TOPIC into a
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
    script prompt is a single bounded ``dispatch_chat`` call. Money caps live
    in the wallet layer, not here.

The heavy dependencies (web search, LLM, TTS) are reached through module-
level indirections so tests can monkeypatch them without a network.
"""

from __future__ import annotations

import json
import os
import re

from lib.log import get_logger

from lib.production.stages import Stage, run_stages

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
#: How many web results to mine per research query.
_RESEARCH_MAX_RESULTS = 6


# ── Seams (monkeypatchable) ───────────────────────────────

def _web_search(query: str, *, user_question: str = '', freshness: str = ''):
    """Run one web search through the tofu-search facade. Returns results."""
    from lib.tasks_pkg.handlers import search as _facade
    return _facade.perform_web_search(query, user_question=user_question,
                                      freshness=freshness)


def _llm_chat(messages, **kwargs):
    """Non-streaming LLM call through the dispatcher. Returns (content, usage)."""
    from lib.llm_dispatch.api import dispatch_chat
    return dispatch_chat(messages, **kwargs)


def _tts_durations(scenes: list[dict], out_dir: str, *, voice=None, speed=None,
                   alignment: str = 'loose', abort_event=None) -> dict:
    """Synthesize per-scene narration + return the alignment manifest."""
    from lib import motion_video as mv
    return mv.synthesize_scene_narrations(
        scenes, out_dir, voice=voice, speed=speed, alignment=alignment,
        abort_event=abort_event)


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
        cards.append({'point': point, 'url': url,
                      'title': (r.get('title') or '').strip()[:200]})
    return cards


# ── Stage: research ───────────────────────────────────────

def _run_research(ctx: dict) -> dict:
    topic = ctx['topic']
    lang = ctx.get('lang', 'zh')

    def _search(q: str, freshness: str) -> list[dict]:
        try:
            return _cards_from_results(
                _web_search(q, user_question=topic, freshness=freshness))
        except Exception as e:
            logger.warning('[Recipe:research] query %r (freshness=%r) failed: %s',
                           q, freshness, e)
            return []

    # The PRIMARY angle is freshness-gated to the last week — the recipe is
    # built for NEWS topics. But produce_video also serves EVERGREEN science
    # topics (owner 2026-07-26): if the week gate starves the run (<3 cards),
    # retry the SAME query unfiltered instead of failing the fact gate. The
    # background angle stays ungated in all cases.
    cards = _search(topic, 'week')
    freshness_used = 'week'
    if len(cards) < 3:
        logger.info('[Recipe:research] week-fresh primary gave %d card(s) — '
                    'retrying without freshness (evergreen topic?)',
                    len(cards))
        cards = _search(topic, '')
        freshness_used = 'none'
    seen = {c['url'] for c in cards}
    bg_query = (f'{topic} 原理 背景' if lang == 'zh'
                else f'{topic} explained background')
    for card in _search(bg_query, ''):
        if card['url'] not in seen:
            seen.add(card['url'])
            cards.append(card)
    logger.info('[Recipe:research] topic=%r → %d fact card(s) '
                '(freshness_used=%s)', topic[:60], len(cards), freshness_used)
    return {'topic': topic, 'cards': cards[:24],
            'freshness_used': freshness_used}


def _gate_research(ctx: dict, artifact: dict) -> list:
    cards = artifact.get('cards') or []
    if not cards:
        return ['research produced zero fact cards with a real source URL '
                '(fact-discipline gate: every point must be grounded)']
    if not any(c.get('url', '').lower().startswith(('http://', 'https://'))
               for c in cards):
        return ['no fact card carries a real http(s) URL']
    return []


RESEARCH = Stage('research', _run_research, gate=_gate_research, retry=1)


# ── Stage: script ─────────────────────────────────────────

def _build_script_prompt(topic: str, cards: list[dict], *, lang: str,
                         max_scenes: int) -> str:
    numbered = '\n'.join(
        f'[{i}] {c["point"]}  (来源: {c["url"]})'
        for i, c in enumerate(cards, 1))
    import datetime
    today = datetime.date.today().isoformat()
    if lang == 'zh':
        return (
            f'今天是 {today}。你是一名科普短视频编导。请把下面这些带来源的事实卡片,改写成一段'
            f'口语化、准确、适合配音的科普短视频口播稿,主题是《{topic}》。\n\n'
            '严格要求:\n'
            f'1. 输出 JSON:{{"title": "...", "segments": ["第1段口播", "第2段", ...]}}。\n'
            f'2. segments 数量在 3 到 {max_scenes - 1} 之间(不含片尾来源卡,系统会自动追加)。\n'
            '3. 每段 1~3 句,口语、连贯、可直接配音;不得出现"如图""见下"等书面语。\n'
            '4. 只依据事实卡片,不得编造;不确定就不说。\n'
            '5. 只输出 JSON 本身,不要解释、不要代码围栏。\n\n'
            f'事实卡片:\n{numbered}')
    return (
        f'Today is {today}. You are a science-explainer video writer. Rewrite the sourced fact '
        f'cards below into a spoken, accurate, voice-over-ready short-video '
        f'script about "{topic}".\n\n'
        'Strict requirements:\n'
        '1. Output JSON: {"title": "...", "segments": ["line 1", "line 2", ...]}.\n'
        f'2. Between 3 and {max_scenes - 1} segments (the sources card is '
        'appended automatically).\n'
        '3. Each segment 1-3 spoken sentences; no "as shown"/"see figure".\n'
        '4. Ground every claim in the cards; invent nothing.\n'
        '5. Output ONLY the JSON — no commentary, no fences.\n\n'
        f'Fact cards:\n{numbered}')


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
    if not isinstance(segs, list):
        raise ValueError('script JSON has no segments array')
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


def _run_script(ctx: dict) -> dict:
    topic = ctx['topic']
    lang = ctx.get('lang', 'zh')
    max_scenes = ctx.get('max_scenes', _DEFAULT_MAX_SCENES)
    cards = ctx['artifacts']['research']['cards']
    prompt = _build_script_prompt(topic, cards, lang=lang, max_scenes=max_scenes)
    content, usage = _llm_chat(
        [{'role': 'user', 'content': prompt}],
        max_tokens=4096, temperature=0.4,
        log_prefix='[Recipe:script]')
    raw = _parse_script(content)
    segments = [re.sub(r'\s+', ' ', str(s)).strip()
                for s in raw.get('segments') or [] if str(s).strip()]
    segments = segments[:max_scenes - 1]  # leave room for the sources card
    title = (raw.get('title') or topic).strip()
    # 拍板 #4: credit the sources at the end — but as a SILENT VISUAL end
    # card (owner 2026-07-26), never a narration segment. The timeline stage
    # turns this line into the final spoken=False scene; if it stayed here,
    # the TTS pass would read domain names aloud.
    sources_line = _sources_line(cards, lang)
    logger.info('[Recipe:script] topic=%r → %d segment(s), sources card %r, '
                'title=%r', topic[:60], len(segments), sources_line[:40],
                title[:60])
    return {'title': title, 'segments': segments,
            'sources_line': sources_line,
            'usage': usage if isinstance(usage, dict) else {}}


def _gate_script(ctx: dict, artifact: dict) -> list:
    segs = artifact.get('segments') or []
    if len(segs) < 2:
        return [f'script has too few segments ({len(segs)}; need ≥2)']
    if any(not s.strip() for s in segs):
        return ['script has an empty segment']
    return []


SCRIPT = Stage('script', _run_script, gate=_gate_script, retry=1)


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
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        prompt = re.sub(r'\s+', ' ', str(item.get('prompt') or '')).strip()
        if not prompt:
            continue
        role = str(item.get('role') or '').strip().lower()
        if role not in ASSET_ROLES:
            logger.info('[Recipe] asset role %r is not one of %s — treating it '
                        'as background (never required)', role, ASSET_ROLES)
            role = 'background'
        out.append({'role': role, 'prompt': prompt[:_MAX_ASSET_PROMPT]})
    return out[:3]


def script_stage_for_source(source_text: str, *, lang: str = 'zh',
                            max_scenes: int = _DEFAULT_MAX_SCENES,
                            char_budget: int = 58,
                            caption_capacity: int = 88,
                            model: str | None = None) -> list[dict]:
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
    content, _usage = _llm_chat([{'role': 'user', 'content': prompt}],
                                max_tokens=4096, temperature=0.4,
                                prefer_model=model,
                                log_prefix='[Recipe:source-beats]')
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
        })
    if len(beats) < 2:
        raise ValueError(f'source-beat reply yielded {len(beats)} usable beat(s)')
    logger.info('[Recipe:source-beats] %d beat(s) from %d chars of source',
                len(beats), len(source_text or ''))
    return beats[:max_scenes]



# ── Stage: timeline ───────────────────────────────────────

#: Fixed on-screen duration of the SILENT sources end card.
_SOURCES_CARD_S = 3.5


def _provisional_scenes(segments: list[str],
                        sources_line: str = '') -> list[dict]:
    """A first-cut storyboard (contiguous from 0) used only to drive TTS.

    Durations here are placeholders; the real durations come from the TTS
    manifest and are written back before this becomes the final scenes.json.
    ``sources_line`` becomes the final scene — a SILENT visual end card
    (spoken=False, fixed duration, never sent to TTS).
    """
    scenes: list[dict] = []
    cursor = 0.0
    for i, seg in enumerate(segments, 1):
        est = max(_MIN_SCENE_S, min(len(seg) / _FALLBACK_CHARS_PER_SECOND,
                                    _MAX_SCENE_S))
        scenes.append({'id': f'scene-{i:03d}',
                       'start': round(cursor, 3),
                       'end': round(cursor + est, 3),
                       'text': seg, 'visual': ''})
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
                                 script.get('sources_line') or '')
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
                abort_event=ctx.get('abort_event'))
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
    scenes_path = os.path.join(ctx['workdir'], 'scenes.json')
    from lib.json_store import write_json_atomic
    write_json_atomic(scenes_path, scenes)
    return {'scenes_path': scenes_path, 'scenes': len(scenes),
            'timed_from_audio': bool(manifest.get('ok')),
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
    return mv.check_storyboard(scenes, span)


TIMELINE = Stage('timeline', _run_timeline, gate=_gate_timeline, retry=0)


def video_recipe_stages() -> list:
    """The ordered front-half stage list: research → script → timeline."""
    return [RESEARCH, SCRIPT, TIMELINE]


# ── Public entry ──────────────────────────────────────────

def build_scenes_from_topic(topic: str, workdir: str, *, lang: str = 'zh',
                            max_scenes: int = _DEFAULT_MAX_SCENES,
                            narration: bool = True, voice: str = '',
                            speed=None, alignment: str = 'loose',
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
    ctx = {
        'topic': topic, 'workdir': workdir, 'lang': lang,
        'max_scenes': max_scenes, 'narration': narration, 'voice': voice,
        'speed': speed, 'alignment': alignment, 'abort_event': abort_event,
    }
    state_path = os.path.join(workdir, 'pipeline_state.json')
    artifacts = run_stages(
        video_recipe_stages(), ctx, state_path=state_path, emit=emit,
        abort_check=(lambda: bool(abort_event is not None
                                  and abort_event.is_set())))
    return artifacts['timeline']
