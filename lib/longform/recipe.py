"""lib/longform/recipe.py — topic → long-form research report (P7).

The THIRD production capability, whose real job is to TEST the substrate
abstraction (docs/modules/production.md):
"third recipe first, then extract"). It is deliberately a different SHAPE
from the video recipe so the test is meaningful:

  * deliverable is TEXT (a markdown artifact), not a binary file;
  * no TTS or render; independent text sections use bounded sibling fan-out;
  * a variable number of section stages, built at runtime from the outline —
    the video recipe's stage list is static, this one is DATA-DEPENDENT.

Stages:  research → outline → sections(×N) → assemble

If the substrate is the right shape, this file is the only place that knows
anything about reports. Whatever it has to duplicate from motion_video is
the evidence for what P6 should extract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from lib.log import get_logger
from lib.production.research import RESEARCH_RESUME_TTL_S
from lib.production.stages import Stage, run_independent_stages, run_stages

logger = get_logger(__name__)

__all__ = ['build_report_from_topic', 'longform_recipe_stages']

_DEPTHS = {'brief': (3, 400), 'standard': (5, 700), 'deep': (8, 1000)}
_MAX_SECTIONS = 10
_OUTLINE_MAX_TOKENS = 2048
_OUTLINE_TEMPERATURE = 0.3
_SECTION_MAX_TOKENS = 4096
_SECTION_TEMPERATURE = 0.4


def _checkpoint_version(revision: str, payload: dict) -> str:
    """Bind a checkpoint contract to the exact recipe inputs it consumes."""
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    digest = hashlib.sha256(encoded).hexdigest()[:20]
    return f'{revision}:{digest}'


def _research_checkpoint_version(ctx: dict) -> str:
    return _checkpoint_version('longform-research-v2', {
        'topic': str(ctx.get('topic') or ''),
        'lang': str(ctx.get('lang') or 'zh'),
    })


def _outline_checkpoint_version(ctx: dict) -> str:
    return _checkpoint_version('longform-outline-v3', {
        'topic': str(ctx.get('topic') or ''),
        'lang': str(ctx.get('lang') or 'zh'),
        'depth': str(ctx.get('depth') or 'standard'),
        'max_tokens': _OUTLINE_MAX_TOKENS,
        'temperature': _OUTLINE_TEMPERATURE,
    })


def _section_prompt_cards(ctx: dict) -> list[dict]:
    """Project cards to exactly the fields interpolated into section prompts."""
    cards = ctx['artifacts']['research']['cards']
    return [
        {'point': str(card.get('point') or ''),
         'url': str(card.get('url') or '')}
        for card in cards
    ]


def _section_checkpoint_version(ctx: dict, heading: str) -> str:
    _sections, words = _DEPTHS[ctx.get('depth', 'standard')]
    return _checkpoint_version('section-body-v4', {
        'title': str(ctx['artifacts']['outline']['title']),
        'heading': str(heading),
        'lang': str(ctx.get('lang') or 'zh'),
        'words': words,
        'cards': _section_prompt_cards(ctx),
        'max_tokens': _SECTION_MAX_TOKENS,
        'temperature': _SECTION_TEMPERATURE,
    })


def _assemble_checkpoint_version(ctx: dict) -> str:
    outline = ctx['artifacts']['outline']
    cards = ctx['artifacts']['research']['cards']
    sections = []
    for index, heading in enumerate(outline['sections'], 1):
        artifact = ctx['artifacts'].get(f'section-{index:02d}') or {}
        sections.append({
            'heading': str(heading),
            'artifact_heading': str(artifact.get('heading') or ''),
            'body': str(artifact.get('body') or ''),
        })
    return _checkpoint_version('longform-markdown-v3', {
        'title': str(outline['title']),
        'lang': str(ctx.get('lang') or 'zh'),
        'depth': str(ctx.get('depth') or 'standard'),
        'sections': sections,
        'sources': [
            {'title': str(card.get('title') or ''),
             'url': str(card.get('url') or '')}
            for card in cards
        ],
    })


# ── Seams (monkeypatchable, same pattern as the video recipe) ──

def _web_search(query: str, *, user_question: str = ''):
    from lib.search_runtime import ensure_search_runtime
    search_runtime = ensure_search_runtime()
    return search_runtime.perform_web_search(
        query, user_question=user_question)


def _llm_chat(messages, **kwargs):
    from lib.llm_dispatch.api import dispatch_chat
    return dispatch_chat(messages, **kwargs)


_JSON_BLOCK_RE = re.compile(r'[\[{].*[\]}]', re.DOTALL)


def _parse_json(content: str):
    m = _JSON_BLOCK_RE.search((content or '').strip())
    if not m:
        raise ValueError('no JSON in reply')
    return json.loads(m.group(0))


# ── Stage: research ───────────────────────────────────────

def _run_research(ctx: dict) -> dict:
    from lib.motion_video._recipe import _cards_from_results
    topic, lang = ctx['topic'], ctx.get('lang', 'zh')
    queries = [topic,
               f'{topic} 最新 进展' if lang == 'zh' else f'{topic} latest research']
    cards, seen = [], set()
    for q in queries:
        try:
            for c in _cards_from_results(_web_search(q, user_question=topic)):
                if c['url'] not in seen:
                    seen.add(c['url'])
                    cards.append(c)
        except Exception as e:
            logger.warning('[Longform:research] query %r failed: %s', q, e)
    logger.info('[Longform:research] %r → %d sourced card(s)', topic[:60], len(cards))
    return {'topic': topic, 'cards': cards[:30]}


def _gate_research(ctx: dict, art: dict) -> list:
    if not art.get('cards'):
        return ['research produced zero sourced cards — every claim in the '
                'report must be grounded in a real URL']
    return []


# ── Stage: outline ────────────────────────────────────────

def _run_outline(ctx: dict) -> dict:
    topic, lang = ctx['topic'], ctx.get('lang', 'zh')
    n_sections, _ = _DEPTHS[ctx.get('depth', 'standard')]
    cards = ctx['artifacts']['research']['cards']
    facts = '\n'.join(f'[{i}] {c["point"]} ({c["url"]})'
                      for i, c in enumerate(cards, 1))
    prompt = (
        (f'为主题《{topic}》拟一份研究报告大纲。只输出 JSON:'
         f'{{"title":"...","sections":["小节标题1",...]}},'
         f'恰好 {n_sections} 个小节,顺序自洽,不要编造事实卡以外的内容。\n\n事实卡:\n{facts}'
         ) if lang == 'zh' else
        (f'Draft a research-report outline for "{topic}". Output ONLY JSON: '
         f'{{"title":"...","sections":["Section 1",...]}} with exactly '
         f'{n_sections} sections grounded in the cards.\n\nCards:\n{facts}'))
    content, _usage = _llm_chat([{'role': 'user', 'content': prompt}],
                                max_tokens=_OUTLINE_MAX_TOKENS,
                                temperature=_OUTLINE_TEMPERATURE,
                                abort_check=ctx.get('abort_check'),
                                max_retries=2,
                                max_429_attempts=ctx.get('max_429_attempts'),
                                log_prefix='[Longform:outline]')
    raw = _parse_json(content)
    raw_sections = raw.get('sections')
    if not isinstance(raw_sections, list):
        raw_sections = []
    sections = [str(s).strip()[:240] for s in raw_sections
                if str(s).strip()]
    return {'title': str(raw.get('title') or topic).strip()[:300],
            'sections': sections[:min(_MAX_SECTIONS, n_sections)]}


def _gate_outline(ctx: dict, art: dict) -> list:
    required, _words = _DEPTHS[ctx.get('depth', 'standard')]
    actual = len(art.get('sections') or [])
    if actual != required:
        return [f'outline has {actual} section(s); depth '
                f'{ctx.get("depth", "standard")!r} requires exactly {required}']
    normalized = [str(section).strip().casefold()
                  for section in art.get('sections') or []]
    if len(set(normalized)) != actual:
        return ['outline contains duplicate section headings']
    return []


def _section_prompt_prefix(ctx: dict) -> str:
    """Build the immutable evidence-first prefix shared by every section."""
    lang = ctx.get('lang', 'zh')
    _, words = _DEPTHS[ctx.get('depth', 'standard')]
    title = ctx['artifacts']['outline']['title']
    cards = ctx['artifacts']['research']['cards']
    facts = '\n'.join(f'[{i}] {card["point"]} ({card["url"]})'
                      for i, card in enumerate(cards, 1))
    if lang == 'zh':
        return (
            f'你在写研究报告《{title}》。每一节约 {words} 字，使用 markdown '
            '正文且不重复小节标题；引用事实时用 [n] 角标。只依据下面的事实卡，'
            f'不确定就不写。\n\n事实卡:\n{facts}\n\n')
    return (
        f'You are writing the report "{title}". Each section is about '
        f'{words} words of markdown body and omits its heading. Cite facts as '
        '[n]. Ground everything in the cards; omit uncertain claims.'
        f'\n\nCards:\n{facts}\n\n')


def _section_prompt(ctx: dict, heading: str, *, prefix: str | None = None) -> str:
    """Append only the section-specific task after the shared evidence."""
    stable_prefix = prefix if prefix is not None else _section_prompt_prefix(ctx)
    if ctx.get('lang', 'zh') == 'zh':
        return stable_prefix + f'现在只写这一节:「{heading}」。'
    return stable_prefix + f'Write ONLY the section "{heading}".'


# ── Stage: one section (built per outline entry) ──────────

def _make_section_stage(index: int, heading: str, *,
                        checkpoint_version: str = '',
                        section_prompt_prefix: str | None = None) -> Stage:
    """Build a Stage for ONE section.

    This is the shape the video recipe never exercised: the stage LIST is
    data-dependent (one stage per outline entry), so each section is its own
    checkpoint and a crash mid-report resumes at the first unwritten section
    instead of re-spending every section's tokens.
    """
    def _run(ctx: dict) -> dict:
        prompt = _section_prompt(
            ctx, heading, prefix=section_prompt_prefix)
        content, usage = _llm_chat([{'role': 'user', 'content': prompt}],
                                   max_tokens=_SECTION_MAX_TOKENS,
                                   temperature=_SECTION_TEMPERATURE,
                                   abort_check=ctx.get('abort_check'),
                                   max_retries=2,
                                   max_429_attempts=ctx.get('max_429_attempts'),
                                   log_prefix=f'[Longform:sec{index}]')
        return {'heading': heading, 'body': (content or '').strip(),
                'tokens': (usage or {}).get('total_tokens', 0)
                if isinstance(usage, dict) else 0}

    def _gate(ctx: dict, art: dict) -> list:
        if len(art.get('body') or '') < 80:
            return [f'section {heading!r} came back too short']
        return []

    if not checkpoint_version:
        heading_digest = hashlib.sha256(
            heading.encode('utf-8')).hexdigest()[:16]
        checkpoint_version = f'section-body-v4:{heading_digest}'
    return Stage(
        f'section-{index:02d}', _run, gate=_gate, retry=1,
        checkpoint_version=checkpoint_version)


# ── Stage: assemble ───────────────────────────────────────

def _run_assemble(ctx: dict) -> dict:
    outline = ctx['artifacts']['outline']
    cards = ctx['artifacts']['research']['cards']
    lang = ctx.get('lang', 'zh')
    parts = [f'# {outline["title"]}\n']
    written = 0
    for i, heading in enumerate(outline['sections'], 1):
        sec = ctx['artifacts'].get(f'section-{i:02d}')
        if not sec:
            continue
        written += 1
        parts.append(f'\n## {heading}\n\n{sec["body"]}\n')
    parts.append('\n## ' + ('参考来源' if lang == 'zh' else 'Sources') + '\n\n')
    for i, c in enumerate(cards, 1):
        parts.append(f'{i}. [{c["title"] or c["url"]}]({c["url"]})\n')
    markdown = ''.join(parts)
    path = os.path.join(ctx['workdir'], 'report.md')
    from lib.json_store import write_text_atomic
    write_text_atomic(path, markdown)
    logger.info('[Longform:assemble] %d chars, %d section(s), %d source(s)',
                len(markdown), len(outline['sections']), len(cards))
    # ``sections`` is what the outline PROMISED; ``sections_written`` is what
    # actually made it into the markdown. They diverge when a section stage's
    # artifact is missing (the skip above), which produces a well-formed but
    # incomplete report — the engine turns the gap into the task's quality
    # verdict rather than shipping it as a clean success.
    return {'path': path, 'chars': len(markdown),
            'sections': len(outline['sections']),
            'sections_written': written,
            'sections_requested': _DEPTHS[ctx.get('depth', 'standard')][0],
            'sources': len(cards),
            'title': outline['title']}


def _gate_assemble(ctx: dict, art: dict) -> list:
    if not os.path.isfile(art.get('path') or ''):
        return ['assemble did not write report.md']
    if art.get('chars', 0) < 200:
        return ['assembled report is implausibly short']
    return []


def _section_stages(ctx: dict, sections: list | None = None) -> list:
    headings = list(sections if sections is not None else
                    ctx['artifacts']['outline']['sections'])
    prompt_prefix = _section_prompt_prefix(ctx)
    return [
        _make_section_stage(
            index, heading,
            checkpoint_version=_section_checkpoint_version(ctx, heading),
            section_prompt_prefix=prompt_prefix,
        )
        for index, heading in enumerate(headings, 1)
    ]


def _assemble_stage(ctx: dict | None = None) -> Stage:
    checkpoint_version = (
        _assemble_checkpoint_version(ctx) if ctx is not None
        else 'longform-markdown-v3'
    )
    return Stage('assemble', _run_assemble, gate=_gate_assemble,
                 checkpoint_version=checkpoint_version)


def longform_recipe_stages(sections: list | None = None, *,
                           ctx: dict | None = None) -> list:
    """Ordered stages. Section stages are appended once the outline exists."""
    research_version = (
        _research_checkpoint_version(ctx) if ctx is not None
        else 'longform-research-v2'
    )
    outline_version = (
        _outline_checkpoint_version(ctx) if ctx is not None
        else 'longform-outline-v3'
    )
    stages = [Stage(
                  'research', _run_research, gate=_gate_research, retry=1,
                  resume_ttl_s=RESEARCH_RESUME_TTL_S,
                  checkpoint_version=research_version),
              Stage('outline', _run_outline, gate=_gate_outline, retry=1,
                    checkpoint_version=outline_version)]
    prompt_prefix = (
        _section_prompt_prefix(ctx) if sections and ctx is not None else None)
    for i, heading in enumerate(sections or [], 1):
        version = (_section_checkpoint_version(ctx, heading)
                   if ctx is not None else '')
        stages.append(_make_section_stage(
            i, heading, checkpoint_version=version,
            section_prompt_prefix=prompt_prefix))
    if sections:
        stages.append(_assemble_stage(ctx))
    return stages


def build_report_from_topic(topic: str, workdir: str, *, lang: str = 'zh',
                            depth: str = 'standard', abort_event=None,
                            emit=None) -> dict:
    """Run research → outline → sections(×N) → assemble; return the report.

    The outline creates a data-dependent sibling batch. Those sections run
    behind one launch-probed fan-out budget and commit separately, then the
    deterministic assemble stage consumes their durable artifacts.
    """
    os.makedirs(workdir, exist_ok=True)
    if depth not in _DEPTHS:
        depth = 'standard'
    ctx = {'topic': topic, 'workdir': workdir, 'lang': lang, 'depth': depth,
           'abort_event': abort_event}
    state_path = os.path.join(workdir, 'pipeline_state.json')
    abort_check = (lambda: bool(abort_event is not None and abort_event.is_set()))
    ctx['abort_check'] = abort_check
    from runtime_guards import resolve_resource_budget
    ctx['max_429_attempts'] = resolve_resource_budget(
        'TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS', maximum=64)
    fanout = resolve_resource_budget(
        'TOFU_PRODUCTION_LLM_FANOUT', maximum=8)

    run_stages(longform_recipe_stages(ctx=ctx), ctx, state_path=state_path,
               emit=emit, abort_check=abort_check)
    section_stages = _section_stages(ctx)
    run_independent_stages(
        section_stages, ctx, state_path=state_path, max_workers=fanout,
        dependent_stage_names=('assemble',), emit=emit,
        abort_check=abort_check)
    artifacts = run_stages(
        [_assemble_stage(ctx)], ctx, state_path=state_path, emit=emit,
        abort_check=abort_check)
    return artifacts['assemble']
