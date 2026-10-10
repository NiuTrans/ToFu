"""Deck-level creative contract for coherent, non-repetitive slides.

Per-page authoring remains useful for bounded repair, but isolated briefs tend
to converge on the same card grid.  This planner converts an outline into a
storyboard with explicit narrative roles, layout archetypes, density rhythm,
asset obligations and neighbouring-page context.  It is deterministic so
legacy outlines and resumed jobs receive the same contract.
"""

from __future__ import annotations

import re

from lib.log import get_logger
from lib.production.contracts import normalise_narrative_core

logger = get_logger(__name__)

__all__ = [
    'LAYOUT_ARCHETYPES', 'NARRATIVE_ROLES', 'VISUAL_MODALITIES',
    'normalise_deck_plan', 'page_packet',
]

LAYOUT_ARCHETYPES = (
    'full-bleed-hero', 'split-editorial', 'metric-focus', 'diagram-flow',
    'comparison-field', 'timeline-ribbon', 'evidence-quote', 'closing-resolve',
)
NARRATIVE_ROLES = (
    'hook', 'argument', 'explanation', 'evidence', 'contrast', 'progression',
    'resolution',
)
VISUAL_MODALITIES = (
    'hero-image', 'annotated-evidence', 'native-chart', 'native-diagram',
    'timeline', 'comparison', 'table', 'quote', 'code', 'formula', 'map',
    'minimal-type',
)

_LAYOUT_MODALITY = {
    'full-bleed-hero': 'hero-image',
    'split-editorial': 'annotated-evidence',
    'metric-focus': 'native-chart',
    'diagram-flow': 'native-diagram',
    'comparison-field': 'comparison',
    'timeline-ribbon': 'timeline',
    'evidence-quote': 'quote',
    'closing-resolve': 'minimal-type',
}

_NUMBER_RE = re.compile(r'(?:\d[\d,.]*\s*%|\d+(?:\.\d+)?)')


def _pick_layout(page: dict, index: int, total: int) -> str:
    ptype = str(page.get('pageType') or 'content').lower()
    blob = ' '.join(str(page.get(k) or '')
                    for k in ('key_message', 'purpose', 'layout_hint',
                              'content_notes')).lower()
    if index == 0 or ptype == 'cover':
        return 'full-bleed-hero'
    if index == total - 1 or ptype == 'final':
        return 'closing-resolve'
    if any(k in blob for k in ('对比', '相比', 'before', 'after', 'versus',
                               ' vs ', '差距')):
        return 'comparison-field'
    if any(k in blob for k in ('时间线', '阶段', 'timeline', 'roadmap')):
        return 'timeline-ribbon'
    if _NUMBER_RE.search(blob):
        return 'metric-focus'
    if any(k in blob for k in ('流程', '机制', '架构', '原理', 'flow',
                               'pipeline', 'architecture')):
        return 'diagram-flow'
    if any(k in blob for k in ('证据', '引用', '研究', 'source', 'quote')):
        return 'evidence-quote'
    return 'split-editorial'


def _role(page: dict, index: int, total: int) -> str:
    ptype = str(page.get('pageType') or 'content').lower()
    if index == 0 or ptype == 'cover':
        return 'hook'
    if index == total - 1 or ptype == 'final':
        return 'resolution'
    layout = str(page.get('layout_archetype') or '')
    return {
        'metric-focus': 'evidence',
        'evidence-quote': 'evidence',
        'diagram-flow': 'explanation',
        'comparison-field': 'contrast',
        'timeline-ribbon': 'progression',
        'split-editorial': 'argument',
    }.get(layout, 'argument')


def _asset_mode(layout: str, modality: str, page: dict) -> str:
    if modality in ('native-chart', 'native-diagram', 'timeline', 'comparison',
                    'table', 'code', 'formula', 'map'):
        return 'code'
    if modality in ('quote', 'minimal-type'):
        return 'none'
    if page.get('asset_mode') == 'provided':
        return 'provided'
    if modality in ('hero-image', 'annotated-evidence'):
        return 'generate'
    return 'none'


def _asset_prompt(title: str, page: dict, layout: str) -> str:
    supplied = re.sub(r'\s+', ' ', str(page.get('asset_prompt') or '')).strip()
    if supplied:
        return supplied[:1200]
    message = re.sub(r'\s+', ' ', str(page.get('key_message') or
                                      page.get('purpose') or title)).strip()
    return (
        f'Editorial hero illustration for a presentation about {title}. '
        f'Visual idea: {message}. Composition: {layout}, strong single focal '
        'subject, generous negative space reserved for slide typography, '
        'cohesive restrained palette, premium publication art direction. '
        'No words, no letters, no numbers, no logos, no watermark, no UI '
        'card grid, no blue-purple gradient, no glassmorphism.')[:1200]


def normalise_deck_plan(outline: dict) -> dict:
    """Fill storyboard fields and adjacent-page context in place."""
    pages = [p for p in (outline.get('pages') or []) if isinstance(p, dict)]
    total = len(pages)
    previous = ''
    title = str(outline.get('title') or '')
    for index, page in enumerate(pages):
        layout = str(page.get('layout_archetype') or '').strip().lower()
        if layout not in LAYOUT_ARCHETYPES:
            layout = _pick_layout(page, index, total)
        # Content pages must not fall into an accidental repeated template.
        if layout == previous and layout not in ('full-bleed-hero',
                                                 'closing-resolve'):
            alternates = ('split-editorial', 'metric-focus', 'diagram-flow',
                          'comparison-field', 'evidence-quote')
            layout = next(a for a in alternates if a != previous)
        page['layout_archetype'] = layout
        inferred_role = _role(page, index, total)
        message = re.sub(
            r'\s+', ' ', str(page.get('key_message')
                              or page.get('purpose') or '')).strip()
        normalise_narrative_core(
            page, allowed_roles=NARRATIVE_ROLES,
            fallback_role=inferred_role,
            fallback_why=(
                f'Advance the deck with this judgment: {message}'
                if message else 'Advance the deck with one clear judgment.'))
        page['density'] = ('breathing' if index % 3 == 0 or
                           layout in ('full-bleed-hero', 'closing-resolve')
                           else 'dense' if index % 3 == 1 else 'balanced')
        modality = str(page.get('visual_modality') or '').strip().lower()
        if modality not in VISUAL_MODALITIES:
            modality = _LAYOUT_MODALITY[layout]
        page['visual_modality'] = modality
        anchor = re.sub(r'\s+', ' ', str(page.get('visual_anchor') or '')).strip()
        page['visual_anchor'] = (anchor or message or title)[:280]
        handoff = re.sub(r'\s+', ' ', str(page.get('handoff') or '')).strip()
        if not handoff and index + 1 < total:
            handoff = str(pages[index + 1].get('purpose') or
                          pages[index + 1].get('key_message') or '')
        page['handoff'] = handoff[:280]
        page['asset_mode'] = _asset_mode(layout, modality, page)
        if page['asset_mode'] == 'generate':
            page['asset_prompt'] = _asset_prompt(title, page, layout)
            page['asset_semantic_target'] = message[:280]
        page['continuity'] = {
            'previous_message': str(pages[index - 1].get('key_message') or '')
                                if index else '',
            'next_message': str(pages[index + 1].get('key_message') or '')
                            if index + 1 < total else '',
        }
        previous = layout
    outline['pages'] = pages
    outline['visual_motif'] = str(outline.get('visual_motif') or '').strip() or (
        'Use one recurring accent rule and one subject crop language across '
        'the deck; vary spatial composition, not the identity system.')
    return outline


def page_packet(brief: dict, page_index: int, total: int, *,
                deck_title: str = '') -> str:
    """The deck-level context injected into one otherwise isolated author."""
    continuity = brief.get('continuity') or {}
    return (
        '## Mandatory deck storyboard packet\n'
        f'- deck: {deck_title}\n'
        f'- narrative role: {brief.get("narrative_role") or "argument"}\n'
        f'- why this page exists: {brief.get("narrative_why") or ""}\n'
        f'- layout archetype: {brief.get("layout_archetype") or "split-editorial"}\n'
        f'- visual modality: {brief.get("visual_modality") or "annotated-evidence"}\n'
        f'- recurring visual anchor: {brief.get("visual_anchor") or ""}\n'
        f'- handoff to next page: {brief.get("handoff") or "(resolution)"}\n'
        f'- density: {brief.get("density") or "balanced"}\n'
        f'- previous page message: {continuity.get("previous_message") or "(opening)"}\n'
        f'- next page message: {continuity.get("next_message") or "(ending)"}\n'
        f'- page position: {page_index + 1}/{total}\n'
        '- Use a shared alignment axis and restrained recurring chrome, but do '
        'not turn the page into a card wall. Honour the named archetype.\n'
        '- Establish one dominant visual mass occupying roughly 35–65% of the '
        'canvas on non-table pages. Repeated peers must share exact baselines, '
        'heights and gaps; avoid manually eyeballed coordinates.\n'
        '- Prefer semantic process/timeline components for sequences. If a '
        'chevron is necessary, declare adjustments: [25000]; every arrow '
        'endpoint must terminate on the named target, never float nearby.\n'
        '- This page must advance the neighbouring messages; do not restate '
        'the narration or duplicate the previous layout.\n')
