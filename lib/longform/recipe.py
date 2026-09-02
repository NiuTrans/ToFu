"""lib/longform/recipe.py — topic → long-form research report (P7).

The THIRD production capability, whose real job is to TEST the substrate
abstraction (docs/modules/production.md):
"third recipe first, then extract"). It is deliberately a different SHAPE
from the video recipe so the test is meaningful:

  * deliverable is TEXT (a markdown artifact), not a binary file;
  * no TTS, no render, no per-scene fan-out;
  * a variable number of section stages, built at runtime from the outline —
    the video recipe's stage list is static, this one is DATA-DEPENDENT.

Stages:  research → outline → sections(×N) → assemble

If the substrate is the right shape, this file is the only place that knows
anything about reports. Whatever it has to duplicate from motion_video is
the evidence for what P6 should extract.
"""

from __future__ import annotations

import json
import os
import re

from lib.log import get_logger
from lib.production.stages import Stage, run_stages

logger = get_logger(__name__)

__all__ = ['build_report_from_topic', 'longform_recipe_stages']

_DEPTHS = {'brief': (3, 400), 'standard': (5, 700), 'deep': (8, 1000)}
_MAX_SECTIONS = 10


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
                                max_tokens=2048, temperature=0.3,
                                log_prefix='[Longform:outline]')
    raw = _parse_json(content)
    sections = [str(s).strip() for s in (raw.get('sections') or []) if str(s).strip()]
    return {'title': (raw.get('title') or topic).strip(),
            'sections': sections[:_MAX_SECTIONS]}


def _gate_outline(ctx: dict, art: dict) -> list:
    if len(art.get('sections') or []) < 2:
        return [f'outline has too few sections ({len(art.get("sections") or [])})']
    return []


# ── Stage: one section (built per outline entry) ──────────

def _make_section_stage(index: int, heading: str) -> Stage:
    """Build a Stage for ONE section.

    This is the shape the video recipe never exercised: the stage LIST is
    data-dependent (one stage per outline entry), so each section is its own
    checkpoint and a crash mid-report resumes at the first unwritten section
    instead of re-spending every section's tokens.
    """
    def _run(ctx: dict) -> dict:
        lang = ctx.get('lang', 'zh')
        _, words = _DEPTHS[ctx.get('depth', 'standard')]
        cards = ctx['artifacts']['research']['cards']
        title = ctx['artifacts']['outline']['title']
        facts = '\n'.join(f'[{i}] {c["point"]} ({c["url"]})'
                          for i, c in enumerate(cards, 1))
        prompt = (
            (f'你在写研究报告《{title}》。现在只写这一节:「{heading}」。'
             f'约 {words} 字,markdown 正文(不要重复小节标题),引用事实时用 [n] 角标,'
             f'只依据事实卡,不确定就不写。\n\n事实卡:\n{facts}')
            if lang == 'zh' else
            (f'You are writing the report "{title}". Write ONLY the section '
             f'"{heading}", ~{words} words of markdown body (omit the heading '
             f'itself). Cite facts as [n]. Ground everything in the cards.'
             f'\n\nCards:\n{facts}'))
        content, usage = _llm_chat([{'role': 'user', 'content': prompt}],
                                   max_tokens=4096, temperature=0.4,
                                   log_prefix=f'[Longform:sec{index}]')
        return {'heading': heading, 'body': (content or '').strip(),
                'tokens': (usage or {}).get('total_tokens', 0)
                if isinstance(usage, dict) else 0}

    def _gate(ctx: dict, art: dict) -> list:
        if len(art.get('body') or '') < 80:
            return [f'section {heading!r} came back too short']
        return []

    return Stage(f'section-{index:02d}', _run, gate=_gate, retry=1)


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


def longform_recipe_stages(sections: list | None = None) -> list:
    """Ordered stages. Section stages are appended once the outline exists."""
    stages = [Stage('research', _run_research, gate=_gate_research, retry=1),
              Stage('outline', _run_outline, gate=_gate_outline, retry=1)]
    for i, heading in enumerate(sections or [], 1):
        stages.append(_make_section_stage(i, heading))
    if sections:
        stages.append(Stage('assemble', _run_assemble, gate=_gate_assemble))
    return stages


def build_report_from_topic(topic: str, workdir: str, *, lang: str = 'zh',
                            depth: str = 'standard', abort_event=None,
                            emit=None) -> dict:
    """Run research → outline → sections(×N) → assemble; return the report.

    Two passes over the stage graph because the section stages don't exist
    until the outline does. Both passes share ONE checkpoint file, so the
    second pass skips research+outline from disk rather than re-running them —
    the data-dependent stage list rides the existing resume contract without
    any change to the substrate.
    """
    os.makedirs(workdir, exist_ok=True)
    if depth not in _DEPTHS:
        depth = 'standard'
    ctx = {'topic': topic, 'workdir': workdir, 'lang': lang, 'depth': depth,
           'abort_event': abort_event}
    state_path = os.path.join(workdir, 'pipeline_state.json')
    abort_check = (lambda: bool(abort_event is not None and abort_event.is_set()))

    run_stages(longform_recipe_stages(), ctx, state_path=state_path,
               emit=emit, abort_check=abort_check)
    sections = ctx['artifacts']['outline']['sections']
    artifacts = run_stages(longform_recipe_stages(sections), ctx,
                           state_path=state_path, emit=emit,
                           abort_check=abort_check)
    return artifacts['assemble']
