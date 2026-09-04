"""Deterministic creative packets shared by every motion-video entry point.

The scene author used to receive a theme and one isolated beat.  It was free
to *optionally* browse the craft catalogue, which measured as ``craft_reads=0``
on the films that triggered this work.  This module turns creative direction
into input data instead of an author-side suggestion:

* every scene has a narrative role and an explicit reason to exist;
* every non-credit scene cites one proven HyperFrames blueprint;
* adjacent scenes deliberately vary their visual grammar;
* the chosen reference can be injected in full as a self-contained "frame
  packet" before the model writes any HTML.

The planner is deliberately deterministic and zero-LLM.  Source and topic
recipes may provide better fields; missing or malformed fields are repaired
here so uploads, legacy ``scenes.json`` files and crash resumes all share the
same contract.
"""

from __future__ import annotations

import re

from lib.log import get_logger
from lib.production.contracts import normalise_narrative_core
from lib.motion_video._shot_recipes import (
    SHOT_RECIPES,
    normalise_shot_contract,
)

logger = get_logger(__name__)

__all__ = [
    'NARRATIVE_ROLES', 'BLUEPRINTS', 'normalise_scene_plan',
    'normalise_film_plan', 'frame_packet',
]


NARRATIVE_ROLES = (
    'hook', 'problem', 'mechanism', 'evidence', 'comparison', 'implication',
    'cta', 'credits',
)

# Backward-compatible public name.  The registry is now a structured shot
# contract rather than the former eight one-line blueprint hints.
BLUEPRINTS = SHOT_RECIPES
_NUMBER_RE = re.compile(r'(?:\d[\d,.]*\s*%|\d+(?:\.\d+)?)')

_RENDERER_CANDIDATES = {
    'generated-still': ('hyperframes',),
    'stock-video': ('hyperframes', 'remotion'),
    'stock-gif': ('hyperframes', 'remotion'),
    'web-capture': ('hyperframes', 'remotion'),
    'native-data': ('hyperframes', 'motion-canvas', 'manim'),
    'native-diagram': ('hyperframes', 'motion-canvas', 'manim'),
    'kinetic-type': ('hyperframes', 'motion-canvas'),
    'hybrid': ('hyperframes', 'remotion', 'motion-canvas'),
}


def _text(scene: dict) -> str:
    return ' '.join(str(scene.get(k) or '')
                    for k in ('text', 'on_screen', 'visual')).lower()


def _infer_role(scene: dict, index: int, total: int) -> str:
    visual = str(scene.get('visual') or '').strip().lower()
    if visual == 'sources' or scene.get('spoken') is False:
        return 'credits'
    if index <= 1:
        return 'hook'
    if index >= total:
        return 'cta'
    blob = _text(scene)
    if any(k in blob for k in ('对比', '相比', 'versus', ' vs ', 'before',
                               'after', '提升', '下降', '差距')):
        return 'comparison'
    if any(k in blob for k in ('问题', '痛点', '瓶颈', '困境', 'risk',
                               'problem', 'challenge', 'too slow')):
        return 'problem'
    if _NUMBER_RE.search(blob) or any(k in blob for k in
                                      ('数据', '实验', '结果', 'benchmark',
                                       'metric', 'evidence')):
        return 'evidence'
    if any(k in blob for k in ('原理', '机制', '流程', '如何', 'how ',
                               'pipeline', 'architecture', 'decode')):
        return 'mechanism'
    return 'implication'


def _default_why(role: str) -> str:
    return {
        'hook': 'Earn attention by stating the value or surprise immediately.',
        'problem': 'Make the concrete tension visible before explaining it.',
        'mechanism': 'Turn the causal explanation into a spatial sequence.',
        'evidence': 'Ground the claim in one legible, truthful proof point.',
        'comparison': 'Make the relevant delta readable on a shared baseline.',
        'implication': 'Translate the mechanism into a consequence people care about.',
        'cta': 'Resolve the story into one memorable takeaway and clean hold.',
        'credits': 'Credit the evidence without competing with the ending.',
    }.get(role, 'Advance the film with one clear visual idea.')


def normalise_scene_plan(scene: dict, index: int, total: int, *,
                         previous_blueprint: str = '',
                         previous_motion_family: str = '') -> dict:
    """Fill/repair the creative-plan fields of ``scene`` in place.

    User/model-authored valid fields win.  Invalid tokens do not flow into the
    author prompt because an unresolvable blueprint recreates the old dead
    instruction under a different name.
    """
    role = str(scene.get('narrative_role') or '').strip().lower()
    if role not in NARRATIVE_ROLES:
        role = _infer_role(scene, index, total)
    normalise_narrative_core(
        scene, allowed_roles=NARRATIVE_ROLES, fallback_role=role,
        fallback_why=_default_why(role))
    role = str(scene.get('narrative_role') or role)
    normalise_shot_contract(
        scene, role, index=index, previous_recipe=previous_blueprint,
        previous_family=previous_motion_family)
    blueprint = str(scene.get('shot_recipe') or '')
    transition = str(scene.get('transition_in') or '').strip().lower()
    if index <= 1:
        transition = 'cold-open'
    elif transition not in ('cut', 'match-cut', 'push', 'wipe', 'dissolve'):
        transition = ('match-cut' if role in ('mechanism', 'comparison')
                      else 'cut')
    scene['blueprint'] = blueprint
    scene['transition_in'] = transition
    scene['signature_move'] = str(scene.get('signature_move') or '').strip() \
        or (BLUEPRINTS.get(blueprint) or {}).get('rule', '')
    modality = str(scene.get('visual_modality') or '').strip().lower()
    if modality not in _RENDERER_CANDIDATES:
        modality = 'kinetic-type' if role == 'credits' else 'generated-still'
    scene['visual_modality'] = modality
    scene['renderer_candidates'] = list(_RENDERER_CANDIDATES[modality])
    # HyperFrames remains the installed renderer. The ordered candidates are
    # a durable adapter seam for Remotion/Motion Canvas/Manim lanes without
    # making a resumed job depend on whichever optional runtime is installed.
    scene['preferred_renderer'] = scene['renderer_candidates'][0]
    return scene


def normalise_film_plan(scenes: list[dict]) -> list[dict]:
    """Apply the contract to a whole storyboard with adjacent-scene context."""
    previous = ''
    previous_family = ''
    total = len(scenes)
    for index, scene in enumerate(scenes, 1):
        normalise_scene_plan(scene, index, total,
                             previous_blueprint=previous,
                             previous_motion_family=previous_family)
        previous = str(scene.get('shot_recipe') or '')
        previous_family = str(scene.get('motion_family') or '')
    return scenes


def frame_packet(scene: dict, *, include_reference: bool = True) -> str:
    """Return the mandatory creative packet injected into scene authoring."""
    blueprint = str(scene.get('shot_recipe') or scene.get('blueprint') or '')
    spec = BLUEPRINTS.get(blueprint) or {}
    progresses = scene.get('qa_progresses') or []
    qa_labels = ' / '.join(f'{round(float(point) * 100)}%'
                           for point in progresses)
    duration = scene.get('recommended_duration_s') or []
    duration_text = (f'{duration[0]:g}–{duration[1]:g}s'
                     if len(duration) == 2 else 'unspecified')
    lines = [
        '## Mandatory frame packet / shot recipe '
        '(film plan — do not replace it)',
        f'- narrative role: {scene.get("narrative_role") or "unspecified"}',
        f'- why this frame exists: {scene.get("narrative_why") or ""}',
        f'- shot recipe: {blueprint or "credits-card / no animation recipe"}',
        f'- motion family: {scene.get("motion_family") or "unspecified"}',
        f'- visual modality: {scene.get("visual_modality") or "generated-still"}',
        f'- renderer candidates: '
        f'{" / ".join(scene.get("renderer_candidates") or ["hyperframes"])}; '
        f'current adapter: {scene.get("preferred_renderer") or "hyperframes"}',
        f'- planned energy: {scene.get("shot_energy") or 1}/5',
        f'- recipe duration range: {duration_text}; actual duration still wins',
        f'- phases: {scene.get("recipe_phases") or 1}',
        f'- minimum resolved hold: {scene.get("hold_s") or 0}s',
        f'- QA anchors: {qa_labels or "start / midpoint / resolved state"}',
        f'- transition in: {scene.get("transition_in") or "cut"}',
        f'- transition overlap: '
        f'{scene.get("transition_in_duration_s") or 0}s; '
        f'outgoing visual handle: {scene.get("outgoing_handle_s") or 0}s',
        f'- program/render duration: '
        f'{scene.get("content_duration_s") or "unspecified"}s / '
        f'{scene.get("render_duration_s") or "unspecified"}s',
        '- outgoing-handle rule: finish every narrative/action animation by '
        'program end (content_duration_s), then preserve the exact resolved '
        'state through render end. The handle is transition media, never '
        'extra story time.',
        f'- signature move: {scene.get("signature_move") or spec.get("rule", "")}',
        '- composition rule: create 2–4 focal points with one dominant anchor; '
        'fill the frame deliberately and keep captions out of the content zone.',
        '- motion rule: stage a readable start, meaningful midpoint and settled '
        'end; do not animate every element with the same ease or direction.',
    ]
    constraints = [str(item) for item in
                   (scene.get('recipe_constraints') or []) if str(item)]
    if constraints:
        lines += ['- recipe acceptance constraints:']
        lines += [f'  - {item}' for item in constraints]
    media_queries = [item for item in (scene.get('media_queries') or [])
                     if isinstance(item, dict)]
    if media_queries:
        lines += ['- real-media retrieval intents (never replace with a '
                  'semantically unrelated decorative image):']
        lines += [
            f'  - {item.get("kind")}: {item.get("query")} — must show '
            f'{item.get("semantic_target")}'
            for item in media_queries[:4]
        ]
    if include_reference and blueprint:
        try:
            from lib.motion_video._craft import craft_reference
            body = craft_reference(blueprint)
        except Exception as e:
            logger.debug('[MotionCreativePlan] craft reference unavailable for '
                         '%s: %s', blueprint, e)
            body = ''
        if body and not body.startswith(('No craft reference',
                                         'The craft corpus is not available')):
            lines += [
                '',
                '### Full cited blueprint (adapt its choreography to this beat)',
                body,
            ]
    return '\n'.join(lines) + '\n'
