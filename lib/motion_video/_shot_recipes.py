"""Structured shot recipes for the motion-video intermediate representation.

The renderer has always known *how* to draw a scene and the creative planner
has always attached a HyperFrames blueprint name.  A name plus one sentence,
however, is not enough information for the rest of the pipeline to reason
about the shot.  It cannot tell whether adjacent scenes repeat the same motion
grammar, which frames should be inspected, or how long the resolved state must
hold.

This module makes a shot an explicit, renderer-neutral contract.  The recipe
IDs deliberately match the managed HyperFrames blueprint corpus so the HTML
author can still load the proven implementation.  The surrounding metadata is
ours: it remains useful if a future adapter renders the same shot with another
frame engine.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'SHOT_CONTRACT_VERSION', 'SHOT_RECIPES', 'choose_shot_recipe',
    'normalise_shot_contract', 'shot_contract_errors',
    'shot_plan_findings', 'shot_recipe_catalog',
]


SHOT_CONTRACT_VERSION = 'motion-shot-v1'
DEFAULT_QA_PROGRESSES = (0.08, 0.5, 0.78, 0.94)


def _recipe(role: str, craft_role: str, motion_family: str, energy: int,
            duration_s: tuple[float, float], phases: int,
            qa_progresses: tuple[float, ...], hold_s: float,
            rule: str, *, triggers=(), constraints=()) -> dict:
    return {
        'role': role,
        'craft_role': craft_role,
        'motion_family': motion_family,
        'energy': energy,
        'duration_s': duration_s,
        'phases': phases,
        'qa_progresses': qa_progresses,
        'hold_s': hold_s,
        'rule': rule,
        'triggers': tuple(str(t).lower() for t in triggers),
        'constraints': tuple(str(c) for c in constraints),
    }


# The thirteen entries are the complete blueprint set in the managed
# HyperFrames corpus.  Unlike the old eight-entry lookup, every item carries
# enough production metadata to select, inspect and compare it without opening
# the renderer implementation first.
SHOT_RECIPES: dict[str, dict] = {
    'hook-counter-burst': _recipe(
        'hook', 'opening-hook', 'metric-impact', 5, (3.0, 5.0), 4,
        (0.05, 0.46, 0.78, 0.94), 0.6,
        'Open on one oversized proof point, then burst supporting evidence '
        'around it; establish value before explanation.',
        triggers=('数据', '数字', '价格', '性能', '增长', '下降', '%',
                  'metric', 'number', 'price', 'faster'),
        constraints=('one dominant truthful number',
                     'supporting evidence must not invent data',
                     'resolved number remains readable during the hold')),
    'takeover-ticker-displace': _recipe(
        'hook', 'takeover', 'kinetic-type', 5, (5.0, 8.0), 4,
        (0.06, 0.48, 0.79, 0.94), 0.7,
        'Build a controlled phrase ticker, then let one subject displace it '
        'and own the frame; the takeover must advance the story.',
        triggers=('口号', '宣言', '一句话', '品牌', 'tagline', 'phrase',
                  'takeover', 'reveal'),
        constraints=('few short phrases only',
                     'takeover subject visibly displaces the setup',
                     'do not repeat the same tagline later')),
    'problem-mockup-overwhelm': _recipe(
        'problem', 'problem', 'card-accumulation', 3, (4.0, 6.0), 4,
        (0.08, 0.52, 0.8, 0.94), 0.55,
        'Show the concrete pain first, using controlled accumulation and one '
        'overwhelmed focal object instead of generic text.',
        triggers=('问题', '痛点', '复杂', '繁琐', '拥堵', 'overwhelm',
                  'problem', 'too many', 'fragmented'),
        constraints=('accumulation stays legible at its peak',
                     'one focal victim anchors the problem',
                     'the scene resolves instead of ending in clutter')),
    'concept-demo-decode-pan': _recipe(
        'mechanism', 'concept-demo', 'causal-camera', 3, (6.0, 10.0), 4,
        (0.07, 0.5, 0.79, 0.94), 0.65,
        'Reveal a mechanism in spatial stages: input, transformation, output. '
        'Camera movement must follow the causal chain.',
        triggers=('原理', '机制', '如何', '输入', '输出', 'decode',
                  'mechanism', 'how', 'pipeline'),
        constraints=('input, transformation and output are all visible',
                     'camera follows causality rather than wandering',
                     'final state explains the claim without narration')),
    'demo-page-scroll-spotlight': _recipe(
        'mechanism', 'demo', 'product-demo', 3, (5.0, 9.0), 4,
        (0.06, 0.5, 0.8, 0.95), 0.7,
        'Use a real product or page view, travel to the claimed feature and '
        'hold on the exact visible evidence.',
        triggers=('界面', '页面', '功能', '操作', '产品演示', 'page',
                  'screen', 'feature', 'demo', 'scroll'),
        constraints=('real interface evidence is preferred',
                     'camera lands on the exact feature being discussed',
                     'text remains sharp at the closest camera position')),
    'metric-video-text-pivot': _recipe(
        'evidence', 'metric', 'evidence-pivot', 4, (5.0, 8.0), 4,
        (0.07, 0.53, 0.8, 0.94), 0.65,
        'Let one truthful metric dominate, then pivot to the visual evidence '
        'that explains it. Never decorate invented data.',
        triggers=('数据', '结果', '实验', '提升', '下降', '%', 'metric',
                  'benchmark', 'result', 'accuracy'),
        constraints=('metric is cited and truthful',
                     'visual evidence supports the same metric',
                     'number and unit remain readable together')),
    'proof-logo-chain': _recipe(
        'evidence', 'social-proof', 'social-proof-chain', 4, (6.0, 10.0), 5,
        (0.06, 0.5, 0.8, 0.95), 0.7,
        'Thread one proof claim through a small chain of recognisable sources '
        'or adopters, then resolve on the supported conclusion.',
        triggers=('用户', '伙伴', '客户', '生态', '排名', '采用', 'users',
                  'trusted', 'partner', 'social proof'),
        constraints=('every logo or source is authentic',
                     'the chain has one readable direction',
                     'proof resolves into a claim, not a logo wall')),
    'comparison-split-cards': _recipe(
        'comparison', 'comparison', 'split-comparison', 4, (4.0, 6.0), 3,
        (0.06, 0.5, 0.79, 0.94), 0.65,
        'Use one shared baseline and a legible before/after split; animate the '
        'delta, not two unrelated card entrances.',
        triggers=('对比', '相比', '之前', '之后', '差异', 'versus', ' vs ',
                  'before', 'after', 'compare'),
        constraints=('both sides share a baseline and scale',
                     'the meaningful delta is visually explicit',
                     'labels cannot swap sides during motion')),
    'workflow-approve-press': _recipe(
        'implication', 'workflow', 'tactile-workflow', 3, (4.0, 6.0), 4,
        (0.08, 0.5, 0.8, 0.94), 0.65,
        'Stage a real workflow state change and make the consequence visible; '
        'avoid a generic dashboard with decorative widgets.',
        triggers=('流程', '审批', '确认', '完成', '交付', 'workflow',
                  'approve', 'press', 'step'),
        constraints=('the interaction causes a visible state change',
                     'cursor and target make physical contact',
                     'resolved state is held long enough to verify')),
    'messaging-multi-phrase': _recipe(
        'implication', 'messaging', 'kinetic-type', 3, (7.0, 8.0), 3,
        (0.06, 0.46, 0.8, 0.95), 0.8,
        'Sequence a small number of short phrases around one visual anchor; '
        'narration and on-screen copy must not duplicate.',
        triggers=('意味着', '带来', '让', '从此', '价值', 'message',
                  'phrase', 'statement', 'benefit'),
        constraints=('phrases are short and non-duplicative',
                     'one visual anchor persists across phrase changes',
                     'last phrase receives a clean reading hold')),
    'brand-reveal-assemble-zoom': _recipe(
        'cta', 'brand-reveal', 'brand-resolve', 4, (4.0, 6.0), 5,
        (0.06, 0.52, 0.76, 0.95), 1.0,
        'Resolve the film by assembling its established motif into one clear '
        'takeaway or mark, then hold long enough to read.',
        triggers=('品牌', '标识', '名称', '发布', 'brand', 'logo', 'reveal',
                  'launch'),
        constraints=('reuse a motif established earlier in the film',
                     'brand or takeaway lands only once',
                     'final lockup is completely still for the hold')),
    'cta-orbit-collapse': _recipe(
        'cta', 'cta', 'orbit-cta', 5, (5.0, 8.0), 5,
        (0.05, 0.5, 0.79, 0.95), 0.85,
        'Orbit a deliberately small set of possibilities, then collapse them '
        'into one action or product truth.',
        triggers=('全部', '多种', '选择', '生态', '探索', 'categories',
                  'versatile', 'orbit', 'generate'),
        constraints=('orbiting items remain individually recognisable',
                     'collapse target is the single CTA',
                     'ambient orbit uses deterministic finite motion')),
    'cta-morph-press': _recipe(
        'cta', 'cta', 'tactile-cta', 4, (4.0, 6.0), 4,
        (0.07, 0.5, 0.78, 0.95), 0.9,
        'Morph the established subject into one action target, then make one '
        'tactile press resolve the film.',
        triggers=('立即', '现在', '开始', '预约', '了解', 'cta', 'click',
                  'start', 'try', 'book'),
        constraints=('morph preserves a visible shared anchor',
                     'only one final action competes for attention',
                     'press completes before the final hold')),
}


_ROLE_RECIPES = {
    role: tuple(name for name, spec in SHOT_RECIPES.items()
                if spec['role'] == role)
    for role in {
        str(spec['role']) for spec in SHOT_RECIPES.values()
    }
}


def _scene_blob(scene: dict) -> str:
    return ' '.join(str(scene.get(k) or '')
                    for k in ('text', 'on_screen', 'visual')).lower()


def choose_shot_recipe(scene: dict, role: str, *, index: int = 1,
                       previous_recipe: str = '',
                       previous_family: str = '') -> str:
    """Choose a resolvable recipe using semantics and film-level diversity.

    A valid explicit ``shot_recipe`` or legacy ``blueprint`` always wins.  For
    old or model-authored storyboards, deterministic trigger matching selects
    among recipes for the inferred narrative role.  Equal candidates prefer a
    different motion family from the previous scene.
    """
    explicit = str(scene.get('shot_recipe') or scene.get('blueprint') or '') \
        .strip().lower()
    spec = SHOT_RECIPES.get(explicit)
    if spec and spec['role'] == role:
        return explicit

    candidates = _ROLE_RECIPES.get(role) or ()
    if not candidates:
        return ''
    blob = _scene_blob(scene)
    scored: list[tuple[int, int, str]] = []
    for order, name in enumerate(candidates):
        candidate = SHOT_RECIPES[name]
        trigger_hits = sum(1 for token in candidate['triggers']
                           if token and token in blob)
        score = trigger_hits * 6
        if previous_family:
            # Repeating a motion grammar in adjacent automatically-planned
            # shots is the stronger amateur tell.  A caller that genuinely
            # wants repetition can pin an explicit recipe, which always wins
            # above; otherwise diversity beats a weak keyword coincidence.
            score += (4 if candidate['motion_family'] != previous_family
                      else -10)
        if name == previous_recipe:
            score -= 3
        # ``order`` is the stable role default.  ``index`` only rotates an
        # exact tie after semantics and family diversity have spoken.
        tie = -((order - max(0, index - 1)) % max(1, len(candidates)))
        scored.append((score, tie, name))
    return max(scored)[2]


def _normalise_progresses(value, fallback) -> list[float]:
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        try:
            points = [round(float(item), 4) for item in value]
        except (TypeError, ValueError) as e:
            logger.debug('[ShotRecipes] invalid QA points %r: %s', value, e)
        else:
            if _progresses_valid(points):
                return points
    return [float(item) for item in fallback]


def _progresses_valid(value) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) not in (3, 4):
        return False
    try:
        points = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        logger.debug('[ShotRecipes] invalid progress points %r: %s', value, exc)
        return False
    return (points == sorted(points) and 0 < points[0]
            and points[-1] < 1 and len(set(points)) == len(points))


def normalise_shot_contract(scene: dict, role: str, *, index: int = 1,
                            previous_recipe: str = '',
                            previous_family: str = '') -> dict:
    """Attach the complete shot contract to one scene in place."""
    if role == 'credits':
        scene.update({
            'shot_contract_version': SHOT_CONTRACT_VERSION,
            'shot_recipe': '',
            'blueprint': '',
            'motion_family': 'credits',
            'shot_energy': 1,
            'recommended_duration_s': [2.5, 5.0],
            'recipe_phases': 1,
            'qa_progresses': _normalise_progresses(
                scene.get('qa_progresses'), (0.1, 0.55, 0.8, 0.95)),
            'hold_s': max(0.8, _positive_float(scene.get('hold_s'), 0.8)),
            'recipe_constraints': [
                'source text remains readable and silent',
                'credits do not compete with the final brand resolve',
            ],
        })
        return scene

    name = choose_shot_recipe(
        scene, role, index=index, previous_recipe=previous_recipe,
        previous_family=previous_family)
    spec = SHOT_RECIPES.get(name)
    if spec is None:
        logger.warning('[ShotRecipes] no recipe for role=%s scene=%s',
                       role, scene.get('id'))
        return scene
    energy = scene.get('shot_energy')
    try:
        energy = int(energy)
    except (TypeError, ValueError) as exc:
        logger.debug('[ShotRecipes] invalid energy %r: %s', energy, exc)
        energy = int(spec['energy'])
    if not 1 <= energy <= 5:
        energy = int(spec['energy'])
    scene.update({
        'shot_contract_version': SHOT_CONTRACT_VERSION,
        'shot_recipe': name,
        # Compatibility with finished scenes and the existing craft channel.
        'blueprint': name,
        'motion_family': spec['motion_family'],
        'shot_energy': energy,
        'recommended_duration_s': [float(v) for v in spec['duration_s']],
        'recipe_phases': int(spec['phases']),
        'qa_progresses': _normalise_progresses(
            scene.get('qa_progresses'), spec['qa_progresses']),
        'hold_s': max(float(spec['hold_s']),
                      _positive_float(scene.get('hold_s'), spec['hold_s'])),
        'recipe_constraints': list(spec['constraints']),
    })
    return scene


def _positive_float(value, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        logger.debug('[ShotRecipes] invalid positive float %r: %s', value, exc)
        return float(fallback)
    return parsed if parsed > 0 else float(fallback)


def shot_contract_errors(scenes) -> list[str]:
    """Return structural errors in the normalized shot-plan IR."""
    if not isinstance(scenes, list) or not scenes:
        return ['shot plan must be a non-empty list']
    errors: list[str] = []
    for index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            errors.append(f'shot #{index}: not an object')
            continue
        label = str(scene.get('id') or f'#{index}')
        if scene.get('shot_contract_version') != SHOT_CONTRACT_VERSION:
            errors.append(f'shot {label}: missing current shot contract version')
        role = str(scene.get('narrative_role') or '')
        name = str(scene.get('shot_recipe') or '')
        if role == 'credits':
            if name:
                errors.append(f'shot {label}: credits must not claim a recipe')
            continue
        spec = SHOT_RECIPES.get(name)
        if spec is None:
            errors.append(f'shot {label}: unknown shot_recipe {name!r}')
            continue
        if spec['role'] != role:
            errors.append(
                f'shot {label}: recipe {name!r} is for {spec["role"]}, '
                f'not {role or "an unspecified role"}')
        if scene.get('blueprint') != name:
            errors.append(f'shot {label}: blueprint/shot_recipe drift')
        if scene.get('motion_family') != spec['motion_family']:
            errors.append(f'shot {label}: motion_family drift')
        points = scene.get('qa_progresses')
        if not _progresses_valid(points):
            errors.append(f'shot {label}: invalid qa_progresses')
    return errors


def shot_plan_findings(scenes) -> list[dict]:
    """Return advisory film-level findings after structural normalization.

    These do not reject a render: an explicit user-selected recipe may repeat
    for a good reason, and narration can legitimately push a shot outside the
    recipe's suggested duration.  They are persisted so the author and quality
    summary can see the trade-off instead of silently losing it.
    """
    findings: list[dict] = []
    previous: dict | None = None
    energy_run: list[dict] = []
    for scene in scenes or []:
        if not isinstance(scene, dict) or scene.get('narrative_role') == 'credits':
            continue
        if previous and scene.get('motion_family') == previous.get('motion_family'):
            findings.append({
                'scene_id': scene.get('id'),
                'issue': f'adjacent shots repeat motion family '
                         f'{scene.get("motion_family")!r}',
                'fix': 'choose a semantically valid recipe from another '
                       'motion family or document why repetition is intentional',
            })
        energy = int(scene.get('shot_energy') or 0)
        if energy_run and int(energy_run[-1].get('shot_energy') or 0) != energy:
            energy_run = []
        energy_run.append(scene)
        if len(energy_run) == 3:
            findings.append({
                'scene_id': scene.get('id'),
                'issue': f'three consecutive shots hold energy {energy}/5',
                'fix': 'introduce a breath or escalation so the film has an '
                       'energy curve instead of a flat montage',
            })
        previous = scene
    return findings


def shot_recipe_catalog() -> list[dict]:
    """Return the public, renderer-neutral recipe catalog.

    Callers receive fresh JSON-safe values so neither an API consumer nor a
    future gallery can mutate the production registry by accident.
    """
    return [
        {
            'id': name,
            'contract_version': SHOT_CONTRACT_VERSION,
            'role': spec['role'],
            'craft_role': spec['craft_role'],
            'motion_family': spec['motion_family'],
            'energy': int(spec['energy']),
            'duration_s': [float(value) for value in spec['duration_s']],
            'phases': int(spec['phases']),
            'qa_progresses': [float(value)
                              for value in spec['qa_progresses']],
            'hold_s': float(spec['hold_s']),
            'rule': spec['rule'],
            'constraints': list(spec['constraints']),
        }
        for name, spec in SHOT_RECIPES.items()
    ]
