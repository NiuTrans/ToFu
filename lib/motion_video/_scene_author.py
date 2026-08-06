"""lib/motion_video/_scene_author.py — Per-scene composition author (P5).

The quality jump over the zero-LLM slide template
(docs/PRODUCTION_PIPELINE_DESIGN.md §2.2 stage 5): each scene gets its OWN
bounded agent loop that writes a bespoke ``index.html`` composition, instead
of every scene rendering as "gradient background + one centred line".

Design constraints (all deliberate):

  * **Narrow toolset.** The author sees only ``write_composition`` (the HTML
    is returned as an argument — there is deliberately no filesystem write
    tool, so a composition can never reference a local asset path),
    ``web_search`` / ``fetch_url`` (for real logos / reference material, which
    are pasted in as INLINE SVG markup), and ``composition_check``.
    It CANNOT render — rendering stays in the engine's render stage, so an
    author loop can never burn a 35s render per iteration.
  * **Per-scene isolation.** Context is one scene's text + duration + the
    composition contract. No whole-film context, so cost is linear and a
    scene's failure is local.
  * **Never fail the film.** Any failure (no composition written, gate still
    failing, token budget exhausted, LLM error, abort) degrades that ONE scene
    to :func:`lib.motion_video._template.render_scene_html`. The caller always
    gets valid HTML back.
  * **Hard cost caps** (owner 拍板 #3): ``max_rounds`` bounds the loop and
    ``token_budget`` stops it once cumulative tokens for THIS scene exceed the
    budget. There is no money cap here — that belongs to the wallet layer.

Default ON (owner 2026-07-27). The zero-LLM template cannot pass the
renderer's own lint and reads as "text flying around with no formatting", so
it is the FALLBACK, never the default deliverable. A job opts OUT per-job
(``task['scene_author'] = False``) or globally via
``TOFU_MOTION_SCENE_AUTHOR=0`` (the emergency kill switch — authoring spends
one agent loop per scene).
"""

from __future__ import annotations

import logging
import os

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['author_scene', 'scene_author_enabled', 'SCENE_AUTHOR_TOOLS',
           'is_transient_fault', 'load_draft', 'save_draft',
           'DRAFT_FILENAME']

#: Bounded rounds per scene (tool-eligible rounds; +1 final no-tools round).
_DEFAULT_MAX_ROUNDS = 4
#: Cumulative token ceiling per scene — the cost cap (拍板 #3).
#:
#: Raised from 60000 after measuring the shipped 0-of-8 job: two scenes blew
#: through it (74040 and 67941 tokens) on compositions that were otherwise
#: progressing, so the old ceiling was cutting off complex scenes mid-repair
#: rather than catching a runaway.
_DEFAULT_TOKEN_BUDGET = 90000
#: Max output tokens per dispatch (a composition is a few hundred lines).
_MAX_TOKENS_PER_ROUND = 8192

#: Attempts for the WHOLE author loop when it dies of a TRANSIENT fault.
#: A transient fault is an infrastructure event (the network, a rate limit, an
#: expired token) — not evidence that the model cannot compose this scene, so
#: retrying is the correct response and degrading is not.
_TRANSIENT_ATTEMPTS = 3
#: Base seconds for the exponential backoff between those attempts.
_TRANSIENT_BACKOFF_S = 2.0

#: Where the in-progress composition is persisted.
#:
#: It lives in a DOT-SUBDIRECTORY, not beside ``index.html``, and that is not
#: cosmetic: the renderer scans the project root and rejects the project with
#: "Multiple root-level HTML files with data-composition-id" the moment a
#: second composition-looking file sits next to it. Measured — the first draft
#: implementation put the file in the scene dir and turned every resumed scene
#: into a gate failure, i.e. it broke the very scenes it was meant to save.
DRAFT_SUBDIR = '.tofu-draft'
DRAFT_FILENAME = os.path.join(DRAFT_SUBDIR, 'composition.html')

#: Substrings that identify a TRANSIENT infrastructure fault in an exception.
#: Measured from the shipped 0-of-8 job's own log lines: an expired OAuth
#: token, a 429 from the shared gateway, and a 119s read timeout each
#: destroyed one scene's creative work permanently.
_TRANSIENT_MARKERS = (
    'read timed out', 'readtimeout', 'timed out', 'timeout',
    'connection reset', 'connection aborted', 'connection error',
    'connectionreset', 'remote end closed', 'broken pipe',
    'temporarily unavailable', 'service unavailable', 'bad gateway',
    'too many requests', 'rate limit', 'ratelimit', ' 429', 'http 429',
    ' 502', ' 503', ' 504',
    'not logged in', 'no valid oauth token', 'token expired',
    'invalid_grant', 'unauthorized', 'http 401',
    'all providers failed', 'no slot available', 'no healthy',
)


def is_transient_fault(exc: BaseException) -> bool:
    """True when ``exc`` is an infrastructure fault worth RETRYING.

    The distinction this draws is the whole point: "the model wrote a
    composition that fails the gate" is a quality verdict and degrading to the
    template is the right answer, but "the gateway timed out" says nothing
    about the composition — and treating the two identically is what turned
    four transient blips into four gradient cards in the shipped 0-of-8 job.

    Errors are matched on their TEXT rather than their class because the
    dispatcher re-raises gateway failures as plain ``RuntimeError`` (the OAuth
    case measured in that job was exactly that), so an isinstance check would
    miss the most common one.
    """
    # A cooperative abort is NOT a fault — the user asked us to stop.
    for cls_name in type(exc).__mro__:
        if getattr(cls_name, '__name__', '') in ('KeyboardInterrupt',
                                                 'SystemExit'):
            return False
    blob = f'{type(exc).__name__}: {exc}'.lower()
    return any(m in blob for m in _TRANSIENT_MARKERS)


#: Values of ``TOFU_MOTION_SCENE_AUTHOR`` that force authoring OFF fleet-wide.
_ENV_OFF = ('0', 'false', 'no', 'off')
#: …and the ones that force it ON (redundant with the default, kept so the
#: variable reads in BOTH directions rather than being a one-way switch).
_ENV_ON = ('1', 'true', 'yes', 'on')


def scene_author_enabled(task: dict | None = None) -> bool:
    """True when per-scene authoring is switched on for this job.

    **This is the single source of the default** — every entry point that
    spawns a motion job (the ``produce_video`` tool, ``POST /api/v1/motion/
    videos``, the paper Video-studio panel, the crash-resume scanner) reads
    the answer from here. A caller that wants the fast template path says so
    explicitly; a caller that says nothing gets an authored film.

    Resolution order (mirrors ``rows_write_enabled()``'s convention):

      1. ``task['scene_author']`` when the job stated a preference;
      2. ``TOFU_MOTION_SCENE_AUTHOR`` — honoured in BOTH directions, so ``0``
         is an emergency fleet-wide kill switch (authoring costs one agent
         loop per scene) and ``1`` pins it on;
      3. **ON.** Owner 2026-07-27: the zero-LLM template does not pass the
         renderer's own lint, so it must not be what a user gets by default.

    Deliberately NOT done: defaulting per call site. Four construction sites
    exist and the two that nobody thought about (paper panel, bare REST POST)
    are exactly the ones a user actually reaches — a per-caller default is a
    copy that silently stops matching.
    """
    if isinstance(task, dict) and task.get('scene_author') is not None:
        return bool(task['scene_author'])
    env = os.environ.get('TOFU_MOTION_SCENE_AUTHOR', '').strip().lower()
    if env in _ENV_OFF:
        return False
    if env in _ENV_ON:
        return True
    return True


# ── Narrow tool schemas ───────────────────────────────────

SCENE_AUTHOR_TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'write_composition',
            'description': (
                'Write this scene\'s composition HTML. Overwrites any previous '
                'attempt. Call composition_check afterwards to verify it passes '
                'the static gate.'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'html': {'type': 'string',
                             'description': 'The COMPLETE index.html document.'},
                },
                'required': ['html'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'composition_check',
            'description': (
                'Run the full static gate on the composition written so far: '
                'the contract/determinism checks PLUS the real renderer gates '
                '(lint = fonts + contract, validate = headless-Chrome runtime '
                'errors + WCAG contrast, inspect = text overflowing its '
                'container across the timeline). Returns the error list — '
                'empty means it passes.'),
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'web_search',
            'description': ('Search the web for reference material — official '
                            'brand SVG logos, product screenshots, factual '
                            'detail for the on-screen text.'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string'},
                },
                'required': ['query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'generate_asset',
            'description': (
                'Create a real image asset for this scene from a text prompt '
                '(background texture, diagram, icon, illustration) and store it '
                'on disk. Returns the scene-relative path to reference in your '
                'HTML, e.g. "assets/ab12cd.png" — use it as <img src="..."> or '
                'in CSS background-image. Assets are content-addressed and '
                'shared across scenes, so an identical prompt costs nothing the '
                'second time. Prefer this over a text-only card: a frame with a '
                'real graphic is the whole point. Say what STYLE you want (flat '
                'vector / UI illustration / paper-cut collage / photographic) '
                'or you will get a photorealistic default.\n'
                'A BACKGROUND MUST RECEDE. When the asset sits behind text or '
                'a chart, push it back so it never competes with the content: '
                'low opacity AND a darkening filter (e.g. '
                '"opacity:.35; filter:brightness(.45) saturate(.8)"), or a '
                'gradient scrim over it. Measured failure to avoid: an asset '
                'used at opacity .25 with NO filter kept its hard-edged blocks '
                'fully legible, and they cut straight through the bar chart in '
                'front of them. Opacity alone does not make a busy image '
                'recede. If the asset is the SUBJECT (a hero illustration, an '
                'icon) rather than a background, that rule does not apply — '
                'give it real size and let it read.'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'prompt': {'type': 'string',
                               'description': 'Subject + style + palette + '
                                              'negative constraints.'},
                    'width': {'type': 'integer',
                              'description': '512-2048, multiple of 8.'},
                    'height': {'type': 'integer',
                               'description': '512-2048, multiple of 8.'},
                },
                'required': ['prompt'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'fetch_url',
            'description': 'Fetch one URL (e.g. an official SVG asset page).',
            'parameters': {
                'type': 'object',
                'properties': {'url': {'type': 'string'}},
                'required': ['url'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            # The DEEP CHANNEL. The craft guide in this package is a ~20 KB
            # distillation; the corpus behind this tool is 29 atomic motion
            # rules, 13 multi-phase scene blueprints and 13 frame presets, each
            # with working GSAP code. WORKFLOW.md used to tell the author to
            # "Activate hyperframes-motion", but activate_skill is a CHAT-agent
            # tool this loop does not have — measured skill hits: 0. This is
            # that instruction made real on the engine path.
            'name': 'craft_reference',
            'description': (
                'Read the FULL text of one motion rule, scene blueprint or '
                'frame preset from the craft corpus — working GSAP code and '
                'exact parameters, far beyond the summary in the craft guide. '
                'Pass a name exactly as the craft index printed it (e.g. '
                '"3d-text-depth-layers", "biennale-yellow"). Read one before '
                'authoring a beat that needs real choreography.'),
            'parameters': {
                'type': 'object',
                'properties': {'name': {
                    'type': 'string',
                    'description': 'Entry name from the craft index.'}},
                'required': ['name'],
            },
        },
    },
]


# ── Prompt ────────────────────────────────────────────────

def _read_guide(name: str, limit: int = 12000) -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'guide', name)
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()[:limit]
    except OSError as e:
        logger.warning('[SceneAuthor] cannot read guide %s: %s', name, e)
        return ''


def save_draft(scene_dir: str, html: str) -> None:
    """Persist the best composition written so far, gate-passing or not.

    Why this exists: the loop used to hold the authored HTML in memory only, so
    a transient fault (or a blown token budget) threw away work the model had
    already done — the next run started from an empty page. A draft on disk
    turns "start over" into "continue repairing", which is the same
    crash-resume contract the engine already applies to FINISHED compositions
    via ``_existing_composition``.

    Deliberately NOT written to ``index.html``: the engine treats that file as
    the finished composition and its resume path would then serve a draft that
    never passed the gate as if it were a deliverable.
    """
    if not html:
        return
    path = os.path.join(scene_dir, DRAFT_FILENAME)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(html)
        os.replace(tmp, path)          # atomic — a torn draft is unusable
    except OSError as e:
        logger.warning('[SceneAuthor] cannot save draft to %s: %s', path, e)


def load_draft(scene_dir: str, duration: float) -> str:
    """The persisted draft for this scene iff it matches ``duration``.

    The duration check mirrors ``engine._existing_composition``: a draft from a
    run whose timeline has since changed is stale and must not be resumed, or
    the scene renders at the wrong length.
    """
    path = os.path.join(scene_dir, DRAFT_FILENAME)
    try:
        with open(path, encoding='utf-8') as f:
            html = f.read()
    except OSError as _e:
        logger.debug('[SceneAuthor] no draft at %s (%s)', path, _e)
        return ''
    import re as _re
    m = _re.search(r'data-duration="([0-9.]+)"', html)
    if not m:
        return ''
    try:
        if abs(float(m.group(1)) - float(duration)) > 0.01:
            logger.info('[SceneAuthor] discarding stale draft (%ss vs %ss)',
                        m.group(1), duration)
            return ''
    except ValueError as _e:
        logger.debug('[SceneAuthor] draft duration unparseable (%s)', _e)
        return ''
    return html


def clear_draft(scene_dir: str) -> None:
    """Remove the draft once its composition has passed the gate."""
    path = os.path.join(scene_dir, DRAFT_FILENAME)
    try:
        if os.path.isfile(path):
            os.unlink(path)
    except OSError as e:
        logger.debug('[SceneAuthor] cannot clear draft %s: %s', path, e)


def _generate_scene_asset(prompt: str, scene_dir: str, *, width: int = 1024,
                          height: int = 1024) -> str:
    """Generate an image, store it in the library, link it into the scene.

    Returns the SCENE-RELATIVE path a composition must reference (e.g.
    ``assets/ab12cd.png``). Raises on failure so the caller can tell the model
    the asset does not exist — inventing a filename is worse than having no
    graphic, because the renderer then draws a blank where the image should be.

    Reuses the project's existing image-generation chassis
    (:func:`lib.image_gen.generate_image`) rather than opening a second path to
    a provider: slot picking, 429 cycling and the retry budget are already
    solved there, and a private copy would drift.
    """
    from lib.image_gen import generate_image
    from lib.motion_video._assets import materialise, store_bytes

    ar = _aspect_ratio_for(width, height)
    res = generate_image(prompt, aspect_ratio=ar,
                         resolution='2K' if max(width, height) > 1280 else '1K')
    if not res.get('ok'):
        raise RuntimeError(res.get('error') or 'image generation failed')
    import base64
    raw = base64.b64decode(res['image_b64'])
    mime = (res.get('mime_type') or 'image/png').lower()
    suffix = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}\
        .get(mime, '.png')
    lib_path = store_bytes(raw, suffix=suffix)
    rel, tier = materialise(lib_path, scene_dir)
    logger.info('[SceneAuthor] generated asset %s (%d bytes, %s) for %s',
                rel, len(raw), tier, scene_dir)
    return rel


def _aspect_ratio_for(width: int, height: int) -> str:
    """Nearest supported aspect-ratio token for a requested pixel size."""
    if height <= 0:
        return '1:1'
    ratio = width / height
    best, best_d = '1:1', 1e9
    for token, value in (('1:1', 1.0), ('16:9', 16 / 9), ('9:16', 9 / 16),
                         ('4:3', 4 / 3), ('3:4', 3 / 4)):
        d = abs(ratio - value)
        if d < best_d:
            best, best_d = token, d
    return best


def _full_gate(html: str, scene_dir: str, *, abort_event=None,
               scene: dict | None = None,
               advisory: bool = False) -> list[str]:
    """Regex gate + the THREE REAL gates (lint / validate / inspect).

    The regex gate (:func:`~lib.motion_video._gates.check_composition_html`)
    only sees the contract fields and the determinism ban-list — it cannot see
    an unresolvable font, a WCAG contrast failure, a runtime console error, or
    text spilling its container. Those are exactly the defects that read as
    "no formatting", so the author must be judged on them BEFORE a render is
    spent. Findings are returned as plain strings for the repair prompt.

    When the CLI is unavailable the real gates report ``env_missing``; that is
    NOT a composition defect, so it degrades to the regex verdict rather than
    failing the scene.

    Also runs text fidelity, which the CLI cannot express. It is computed from
    the HTML in hand, so it is not swallowed by an ``env_missing`` outcome — a
    corrupted headline is corrupted with or without a toolchain.

    ``advisory=True`` additionally reports **vertical fill** and the **asset
    floor**. This is the author's FEEDBACK channel, not the accept/reject
    verdict, and the distinction is load-bearing: the zero-LLM template itself
    measures 58% span / 38% bottom dead-band and ships ZERO graphics, so
    treating either as a rejection would degrade the scene into a card that
    scores WORSE on the very same axis — strictly worse output, plus a wasted
    agent loop. Both are "make this better" signals; only a broken contract or
    a real renderer error is a "do not ship this" signal.

    **Why the floor lives HERE and not only in the engine** (measured
    2026-07-29): it was first written as an engine-side check, so the moment a
    new author path appeared — the zero-spend draft adoption — that path
    bypassed it silently and adopted a graphics-less draft for 0 tokens. A gate
    that must be re-attached by every future caller WILL be forgotten. Putting
    it in the author's single gate function means every author path
    (write_composition, composition_check, the adoption pre-check, the final
    verdict) inherits it by construction.
    """
    from lib.motion_video._gates import check_composition_html, check_text_fidelity

    errors = list(check_composition_html(html))
    # Text fidelity rides the SAME feedback loop as the contract errors, so a
    # corrupted headline is something the author can still repair in a later
    # round rather than a verdict delivered after the film is finished.
    if scene is not None:
        errors += list(check_text_fidelity(html, scene))
    # ── Asset + font references, checked BEFORE Chrome ──
    # Both are cheap pure-Python passes over the HTML, and both catch defects
    # the CLI would only surface after a browser boot (or not at all):
    #   * an asset reference that escapes the project root, points at an
    #     absolute path, or names a file that is not there — the renderer draws
    #     a blank where the graphic should be;
    #   * a font family the document NAMES but neither declares via @font-face
    #     nor can rely on the renderer resolving. That one is invisible by
    #     construction: fontconfig silently substitutes, so the frame renders
    #     in a face nobody chose (on this host, a serif).
    try:
        from lib.motion_video._assets import verify_asset_refs
        errors += list(verify_asset_refs(html, scene_dir))
    except Exception as e:
        logger.warning('[SceneAuthor] asset-ref check crashed: %s', e,
                       exc_info=True)
    try:
        from lib.motion_video._fonts import undeclared_font_families
        for fam in undeclared_font_families(html):
            errors.append(
                f'font-family {fam!r} is neither declared with an @font-face '
                f'in this document nor auto-resolved by the renderer — naming '
                f'an absent face does NOT get you that face, it silently '
                f'falls back to whatever the host has. Either declare it from '
                f'a stored font asset or name one the renderer resolves.')
    except Exception as e:
        logger.warning('[SceneAuthor] font-family check crashed: %s', e,
                       exc_info=True)
    if errors:
        return errors  # contract broken — no point booting Chrome
    # Vertical fill is measured in its OWN browser boot from the HTML we
    # already hold, so — like fidelity, and unlike the CLI gates — it survives
    # an env_missing / chrome outcome. Collected BEFORE the CLI call for that
    # reason: both of the early returns below are unreachable-for-fill paths,
    # and a frame that leaves a third of its height empty is still empty when
    # the hyperframes CLI is absent. The CLI cannot see this defect at all —
    # it reports content spilling OUT of the frame, never failing to fill it.
    fill: list[str] = []
    if advisory:
        try:
            from lib.motion_video._fill import check_composition_fill
            fill = list(check_composition_fill(html))
        except Exception as e:
            logger.warning('[SceneAuthor] fill gate crashed: %s', e,
                           exc_info=True)
        # The asset floor rides the SAME advisory channel, and it lives here
        # rather than in the engine so that EVERY author path inherits it (see
        # the docstring: an engine-only floor was bypassed by draft adoption).
        try:
            from lib.motion_video._quality import asset_floor_findings
            fill += list(asset_floor_findings(scene or {}, html, scene_dir,
                                              mode='authored'))
        except Exception as e:
            logger.warning('[SceneAuthor] asset floor crashed: %s', e,
                           exc_info=True)
        # Same channel again for CJK typography: a scene that ships no face of
        # its own lets the render HOST pick, so the film is inconsistent by
        # construction and the same file renders differently elsewhere.
        try:
            from lib.motion_video._fonts import cjk_fallback_findings
            fill += list(cjk_fallback_findings(html, scene_dir))
        except Exception as e:
            logger.warning('[SceneAuthor] cjk fallback check crashed: %s', e,
                           exc_info=True)
    try:
        from lib.motion_video._render import check_project, is_infra_category
        with open(os.path.join(scene_dir, 'index.html'), 'w',
                  encoding='utf-8') as f:
            f.write(html)
        res = check_project(scene_dir, abort_event=abort_event)
    except Exception as e:
        logger.warning('[SceneAuthor] real gates unavailable: %s', e)
        return fill
    if is_infra_category(res.get('category')):
        logger.info('[SceneAuthor] real gates skipped (%s)', res.get('category'))
        return fill
    out = [str(e) for e in res.get('errors', [])]
    out += [f'{h}' for h in res.get('fix_hints', []) if h]
    return out + fill


def _build_prompt(scene: dict, *, width: int, height: int, duration: float,
                  scene_index: int, total_scenes: int,
                  font_rel: str = '', theme=None,
                  font_rels: dict | None = None) -> str:
    contract = _read_guide('COMPOSITION_CONTRACT.md')
    craft = _read_guide('MOTION_CRAFT.md')
    skeleton = _read_guide('skeleton.html', limit=6000)
    text = str(scene.get('text') or '').strip()
    visual = str(scene.get('visual') or '').strip()
    # The ASSET BRIEF. Without this the prompt only ever OFFERED imagery
    # ("REAL IMAGERY IS AVAILABLE — use it when the beat calls for it"), and
    # measured across every film ever produced generate_asset was called ZERO
    # times: the model always chose the cheaper inline SVG. A capability that
    # nothing asks for is an unused capability, so the storyboard now NAMES
    # what this beat needs and the floor checks it was delivered.
    brief_block = ''
    briefed = [a for a in (scene.get('assets') or []) if isinstance(a, dict)]
    if briefed:
        lines = []
        for a in briefed:
            role = str(a.get('role') or 'background').strip().lower()
            prompt_txt = str(a.get('prompt') or '').strip()
            must = ('REQUIRED — a real generated file, inline SVG does NOT '
                    'substitute' if role in ('subject', 'diagram')
                    else 'optional texture')
            lines.append(f'  - role={role} ({must})\n    prompt: {prompt_txt}')
        brief_block = (
            '\n## Asset brief for THIS beat (from the storyboard)\n'
            'Call generate_asset for each REQUIRED item below, then reference '
            'the scene-relative path it returns. Refine the prompt if you can '
            'make it better — but do not silently skip a required item and '
            'draw an SVG instead: that role exists for imagery a composition '
            'cannot draw itself.\n'
            + '\n'.join(lines) + '\n')
    font_block = ''
    if theme is not None and font_rels:
        # Themed path: the film's font pairing is already staged. Name ONLY
        # the families that actually materialised (font_rels is the proof),
        # and bind the palette/type hierarchy through the theme block.
        from lib.design_sys.fonts import font_face_block, get_font
        from lib.design_sys.themes import theme_prompt_block
        faces = []
        for role in ('display', 'body', 'latin'):
            face = get_font(theme.fonts.get(role, ''))
            if face is not None and any(k[0] == face.id for k in font_rels):
                faces.append(face)
        css = font_face_block(faces, rel_paths=font_rels)
        body_fam = get_font(theme.fonts.get('body', ''))
        staged_ids = {f.id for f in faces}
        theme_css = theme_prompt_block(theme, for_video=True,
                                       staged_font_ids=staged_ids)
        font_block = (
            f'\n## Film theme + typefaces (already staged in this scene)\n'
            f'{theme_css}\n\n'
            f'Put these @font-face rules in your <style> verbatim, then set '
            f'the families on your text (body default: '
            f"`font-family: '{body_fam.family if body_fam else ''}', "
            f"Inter, sans-serif` — CJK glyphs resolve from the staged face):\n"
            f'```css\n{css}\n```\n'
            f'Never name a family you have not declared this way (PingFang SC, '
            f'Microsoft YaHei, Source Han Sans…): naming an absent face does '
            f'not get you that face, it silently falls back.\n')
    elif font_rel:
        from lib.motion_video._fonts import CJK_SANS_FAMILY, font_face_css
        font_block = (
            f'\n## Typeface (already staged in this scene)\n'
            f'This scene ships a CJK **sans** face. The host\'s only installed '
            f'Chinese font is a SERIF, so WITHOUT this the frame silently '
            f'renders in a serif nobody chose. Put this rule in your <style> '
            f'and set it on your text:\n'
            f'```css\n{font_face_css(font_rel)}\n'
            f"body {{ font-family: '{CJK_SANS_FAMILY}', Inter, sans-serif; }}\n"
            f'```\n'
            f'Never name a family you have not declared this way (PingFang SC, '
            f'Microsoft YaHei, Source Han Sans…): naming an absent face does '
            f'not get you that face, it silently falls back.\n')
    # The DEEP CHANNEL index. Handed over WITH the prompt because a tool the
    # model cannot enumerate is a tool it will not call: the author has to see
    # which rules and blueprints exist before craft_reference is worth reaching
    # for. Only the index travels here (~27 KB of name + summary + tags); the
    # full text of an entry costs a tool call, which is the whole point of
    # progressive disclosure. Absent corpus → empty block, and the author
    # simply works from the distilled craft guide as before.
    craft_block = ''
    try:
        from lib.motion_video._craft import craft_index
        index = craft_index()
    except Exception as e:  # never let the deep channel break authoring
        logger.warning('[SceneAuthor] craft index unavailable: %s', e)
        index = ''
    if index:
        craft_block = (
            '## Craft corpus (deep reference — READ ONE BEFORE YOU AUTHOR)\n'
            'These are complete, working techniques with real GSAP code — the '
            'craft guide above is only a summary of the same discipline. '
            'Choose the ONE entry whose tags best match this beat and call '
            'craft_reference with its name to read it in full, THEN author. '
            'Composing a proven technique beats inventing one.\n'
            f'{index}\n\n')
    return (
        f'You are authoring ONE scene of a {total_scenes}-scene motion-graphics '
        f'video. Write a single self-contained HTML composition for it.\n\n'
        f'## This scene\n'
        f'- id: {scene.get("id")}\n'
        f'- index: {scene_index} of {total_scenes}\n'
        f'- EXACT duration: {duration} seconds (data-duration MUST be this)\n'
        f'- frame: {width}x{height} px\n'
        f'- narration (spoken over this scene): {text or "(none)"}\n'
        + (f'- visual direction: {visual}\n' if visual else '')
        + brief_block
        + font_block
        + '\n## Hard requirements\n'
        '1. Call write_composition with the COMPLETE document, then '
        'composition_check. Iterate until the check returns no errors.\n'
        '2. The composition must satisfy the contract below exactly '
        '(data-composition-id / data-start / data-duration / data-width / '
        'data-height, ONE paused GSAP timeline registered on '
        'window.__timelines under the SAME key as data-composition-id).\n'
        '3. DETERMINISM: no Date.now(), Math.random(), performance.now(), '
        'requestAnimationFrame, setInterval, or infinite repeats.\n'
        '4. Visual quality is the point. Pick ONE archetype from the craft '
        'guide below that fits this beat, then build it with a real type '
        'hierarchy (eyebrow / headline / caption at clearly different sizes), '
        'STAGGERED entrances, and at least one supporting graphic (rule, bar, '
        'number, icon or divider). A single centred line on a gradient is the '
        'fallback we are replacing and is not acceptable output.\n'
        '5. REAL IMAGERY IS AVAILABLE — use it when the beat calls for it. '
        'Call generate_asset to create a background texture, an illustration '
        'or a diagram; it returns a scene-relative path like '
        '"assets/ab12cd.png" that you reference with <img src="..."> or CSS '
        'background-image. You may also paste a fetched brand SVG inline. '
        'What you may NOT do is reference a local file you did not create: '
        'every src/url() must resolve inside this scene directory, no "../", '
        'no absolute paths — the gate rejects those before rendering.\n'
        '6. Text must be HTML-escaped and fit inside the frame at the chosen '
        'font size.\n\n'
        + craft_block
        + f'## Composition contract\n{contract}\n\n'
        f'## Motion craft — how to make this LOOK designed\n{craft}\n\n'
        f'## Reference skeleton (a STARTING POINT, not the target quality)\n'
        f'```html\n{skeleton}\n```\n'
    )


# ── Author one scene ──────────────────────────────────────

def author_scene(scene: dict, scene_dir: str, *, width: int, height: int,
                 duration: float, scene_index: int, total_scenes: int,
                 max_rounds: int | None = None,
                 token_budget: int | None = None,
                 model: str | None = None,
                 abort_event=None,
                 theme=None,
                 transient_attempts: int = _TRANSIENT_ATTEMPTS) -> dict:
    """Author one scene's composition with a bounded agent loop.

    ``max_rounds`` / ``token_budget`` accept ``None`` meaning "use this
    module's default", and that is the ONLY way a caller should express
    "no preference". They used to carry the literals as defaults, so every
    call site wrote its own copy — measured 2026-07-29: ``_DEFAULT_TOKEN_BUDGET``
    was deliberately raised 60000 → 90000 after two scenes were cut off
    mid-repair, but ``engine.py`` and ``routes/api_v1/motion.py`` both passed
    ``or 60000`` explicitly, so the raise was DEAD on every production path and
    the reading-mode panel (which passes neither knob) was silently capped at
    the old ceiling. This is the same failure ``scene_author_enabled``'s
    docstring warns about: a per-caller default is a copy that stops matching.

    Returns ``{'ok', 'html', 'mode', 'rounds', 'tokens', 'detail'}`` where
    ``mode`` is ``'authored'`` (the agent's composition passed the static gate)
    or ``'template'`` (degraded — ``html`` is the zero-LLM template). NEVER
    raises: every failure path degrades, so one bad scene cannot take down the
    film.

    **Transient faults are retried, not degraded** (owner 2026-07-28). Measured
    on the shipped 0-of-8 job: half its scenes degraded for reasons that said
    nothing about composition quality — an expired OAuth token, a 429 from the
    shared gateway, and a 119s read timeout. Each cost that scene its creative
    work permanently, because the engine then wrote the fallback card to
    ``index.html`` and the resume path (which compares only ``data-duration``)
    cannot tell a fallback card from an authored composition, pinning the scene
    to the gradient card for every future run.

    So: an infrastructure fault gets ``transient_attempts`` tries with
    exponential backoff, and whatever the author already wrote is persisted as
    a DRAFT (:func:`save_draft`) and fed back in on the next attempt or the
    next run. Only a genuine quality verdict — a composition that exists and
    still fails the gate — degrades to the template.
    """
    import time as _time

    from lib.agent_loop import AbortSignal
    from lib.motion_video._template import render_scene_html

    max_rounds = _DEFAULT_MAX_ROUNDS if not max_rounds else int(max_rounds)
    token_budget = (_DEFAULT_TOKEN_BUDGET if not token_budget
                    else int(token_budget))

    def _fallback(detail: str, *, rounds: int = 0, tokens: int = 0) -> dict:
        logger.info('[SceneAuthor] %s → template fallback (%s)',
                    scene.get('id'), detail)
        return {'ok': True, 'mode': 'template', 'rounds': rounds,
                'tokens': tokens, 'detail': detail,
                'html': render_scene_html(scene, width=width, height=height,
                                          duration=duration,
                                          scene_index=scene_index,
                                          total_scenes=total_scenes)}

    abort = (AbortSignal.from_event(abort_event) if abort_event is not None
             else AbortSignal.never())
    if abort.aborted:
        return _fallback('aborted before authoring')

    # Resume from a draft left by an earlier attempt / an earlier process:
    # continuing a repair is strictly better than restarting from a blank page.
    resumed = load_draft(scene_dir, duration)
    if resumed:
        logger.info('[SceneAuthor] %s resuming from a %d-char draft',
                    scene.get('id'), len(resumed))
        # ZERO-SPEND RESCUE. A draft only survives on disk because the previous
        # run ended without adopting it — and the most common reason for that
        # was never the composition: an infrastructure verdict discarded work
        # that was already finished (measured 2026-07-29: two scenes of a
        # 6-scene film held 7.5 KB / 10.5 KB drafts that pass this very gate,
        # while index.html carried a 2-node gradient card). If the draft passes
        # NOW, adopting it costs nothing; re-entering the loop would spend a
        # full agent round to re-derive a composition we are already holding.
        #
        # advisory=True is REQUIRED here, not a nicety: the plain verdict omits
        # the asset floor, so the first version of this rescue adopted a
        # graphics-less draft for 0 tokens and shipped a text-only frame the
        # floor had just been written to reject. Adoption is the one place a
        # composition reaches the film WITHOUT the author ever seeing feedback,
        # so it must clear the STRICTER bar, not the looser one.
        try:
            pending = _full_gate(resumed, scene_dir, abort_event=abort_event,
                                 scene=scene, advisory=True)
        except Exception as e:
            logger.warning('[SceneAuthor] %s draft pre-check crashed: %s',
                           scene.get('id'), e, exc_info=True)
            pending = ['draft pre-check crashed']
        if not pending:
            clear_draft(scene_dir)
            logger.info('[SceneAuthor] %s adopted its persisted draft — it '
                        'passes the gate as written (0 rounds, 0 tokens)',
                        scene.get('id'))
            return {'ok': True, 'mode': 'authored', 'html': resumed,
                    'rounds': 0, 'tokens': 0, 'detail': 'adopted draft'}
        logger.info('[SceneAuthor] %s draft NOT adopted (%d finding(s), e.g. '
                    '%.120s) — entering the repair loop with it as the seed',
                    scene.get('id'), len(pending), pending[0])

    attempts = max(1, int(transient_attempts))
    total_tokens = 0
    last_transient = ''
    for attempt in range(1, attempts + 1):
        res = _author_once(
            scene, scene_dir, width=width, height=height, duration=duration,
            scene_index=scene_index, total_scenes=total_scenes,
            max_rounds=max_rounds, token_budget=token_budget, model=model,
            abort=abort, abort_event=abort_event, seed_html=resumed)
        total_tokens += res['tokens']

        if res['outcome'] == 'authored':
            clear_draft(scene_dir)
            logger.info('[SceneAuthor] %s authored in %d round(s), %d tokens'
                        '%s', scene.get('id'), res['rounds'], total_tokens,
                        f' (attempt {attempt})' if attempt > 1 else '')
            return {'ok': True, 'mode': 'authored', 'html': res['html'],
                    'rounds': res['rounds'], 'tokens': total_tokens,
                    'detail': ''}

        # Any HTML produced this attempt is kept for the next one / next run,
        # whatever the outcome — this is the "never throw away written work"
        # half of the contract.
        if res['html']:
            resumed = res['html']
            save_draft(scene_dir, res['html'])

        if res['outcome'] == 'transient' and attempt < attempts \
                and not abort.aborted:
            last_transient = res['detail']
            delay = _TRANSIENT_BACKOFF_S * (2 ** (attempt - 1))
            logger.warning(
                '[SceneAuthor] %s attempt %d/%d hit a TRANSIENT fault (%s) — '
                'retrying in %.1fs; %d chars of work kept as a draft',
                scene.get('id'), attempt, attempts, res['detail'][:120],
                delay, len(res['html'] or ''))
            _time.sleep(delay)
            continue

        if res['outcome'] == 'transient':
            # Out of attempts. Degrade, but the draft stays on disk so the
            # NEXT run continues instead of restarting.
            return _fallback(
                f'transient fault persisted across {attempts} attempt(s): '
                f'{res["detail"][:160]}',
                rounds=res['rounds'], tokens=total_tokens)
        # A genuine quality verdict (or an abort) — degrading is correct.
        detail = res['detail']
        if last_transient:
            detail += f' (an earlier attempt also hit: {last_transient[:80]})'
        return _fallback(detail, rounds=res['rounds'], tokens=total_tokens)

    return _fallback('author loop exhausted its attempts',
                     tokens=total_tokens)


def _author_once(scene: dict, scene_dir: str, *, width: int, height: int,
                 duration: float, scene_index: int, total_scenes: int,
                 max_rounds: int, token_budget: int, model: str | None,
                 abort, abort_event, seed_html: str = '') -> dict:
    """ONE attempt of the author loop.

    Returns ``{'outcome', 'html', 'rounds', 'tokens', 'detail'}`` where
    ``outcome`` is:

      * ``'authored'``  — the composition passed the gate;
      * ``'transient'`` — an infrastructure fault (retry is worthwhile);
      * ``'quality'``   — a composition exists but fails the gate, or none was
                          written at all (degrade is correct);
      * ``'aborted'``   — the user stopped us.

    Split out of :func:`author_scene` so the retry policy is expressed once,
    over a function whose failure MODE is part of its return value — the old
    single-function shape could only say "fell back", which is precisely why a
    network blip and a bad composition were indistinguishable.
    """
    import json as _json

    from lib.agent_loop import AbortSignal, run_agent_loop

    state = {'html': seed_html, 'tokens': 0, 'gate_ok': False}
    # Stage the CJK sans face INTO this scene before prompting, so the author
    # can declare it with a scene-local @font-face. Best-effort: an
    # unreachable network leaves font_rel empty and the prompt simply omits the
    # typeface block (pre-existing fontconfig fallback behaviour).
    font_rel = ''
    font_rels: dict = {}
    if theme is not None:
        # Themed path: stage every weight of each role face in the film's
        # pairing. Any face that fails to stage is simply absent from
        # font_rels, and the prompt block remaps its role to a staged one —
        # a missing font degrades the pairing, never the film.
        try:
            from lib.design_sys.fonts import (get_font,
                                              stage_font_into_scene)
            for role in ('display', 'body', 'latin'):
                face = get_font(theme.fonts.get(role, ''))
                if face is None:
                    continue
                for src in face.sources:
                    rel = stage_font_into_scene(scene_dir, face.id,
                                                src.weight)
                    if rel:
                        font_rels[(face.id, src.weight)] = rel
        except Exception as e:
            logger.warning('[SceneAuthor] theme font staging failed: %s', e)
            font_rels = {}
    if not font_rels:
        try:
            from lib.motion_video._assets import materialise
            from lib.motion_video._fonts import ensure_cjk_sans
            font_path = ensure_cjk_sans()
            if font_path:
                font_rel, _tier = materialise(
                    font_path, scene_dir,
                    name='cjk-sans' + os.path.splitext(font_path)[1])
        except Exception as e:
            logger.warning('[SceneAuthor] could not stage the CJK sans face: %s', e)
    # Materialise the deep craft corpus BEFORE the prompt is built — the index
    # travels inside the prompt, so a corpus fetched later would be invisible
    # to this scene. Same managed-dependency contract as the font above:
    # one-time fetch, cached under the motion root, never fatal.
    try:
        from lib.motion_video._craft import ensure_craft_corpus
        ensure_craft_corpus()
    except Exception as e:
        logger.warning('[SceneAuthor] craft corpus unavailable: %s', e)
    prompt = _build_prompt(scene, width=width, height=height,
                           duration=duration, scene_index=scene_index,
                           total_scenes=total_scenes, font_rel=font_rel,
                           theme=theme, font_rels=font_rels)
    if seed_html:
        # Hand the model its own unfinished work plus the gate's current
        # verdict, so the attempt continues the repair rather than restarting.
        pending = _full_gate(seed_html, scene_dir, abort_event=abort_event,
                             scene=scene, advisory=True)
        prompt += (
            '\n## Work in progress (resume this, do not start over)\n'
            'A previous attempt was interrupted by an infrastructure fault. '
            'Its composition is below. Continue from it: call '
            'write_composition with an IMPROVED full document, then '
            'composition_check.\n'
            + ('Outstanding gate findings:\n'
               + '\n'.join(f'- {e}' for e in pending[:8]) + '\n'
               if pending else 'It passed the static gate as written.\n')
            + f'```html\n{seed_html[:20000]}\n```\n')
    messages = [{'role': 'user', 'content': prompt}]

    def _dispatch(rnd, tools):
        from lib.llm_dispatch.api import dispatch_chat
        content, usage = dispatch_chat(
            messages, max_tokens=_MAX_TOKENS_PER_ROUND, temperature=0.3,
            tools=tools, prefer_model=model,
            log_prefix=f'[SceneAuthor:{scene.get("id")}:R{rnd}]')
        tool_calls = []
        if isinstance(usage, dict):
            state['tokens'] += int(usage.get('total_tokens') or 0)
            tool_calls = usage.get('_tool_calls') or []
        msg = {'role': 'assistant', 'content': content or None,
               'tool_calls': tool_calls}
        return msg, None, usage

    def _on_round(rnd, msg, finish, usage):
        # Cost cap (拍板 #3): once this scene's cumulative tokens exceed the
        # budget, stop asking for more rounds — the loop's abort seam is the
        # cheapest way to break out without a special-case return path.
        if state['tokens'] > token_budget:
            logger.warning('[SceneAuthor] %s hit the %d-token budget (%d used)'
                           ' — stopping (work so far is kept)',
                           scene.get('id'), token_budget, state['tokens'])
            state['budget_exhausted'] = True

    def _on_tool_round(rnd, msg):
        messages.append(msg)

    def _reply(tc_id: str, payload) -> None:
        messages.append({'role': 'tool', 'tool_call_id': tc_id,
                         'content': payload if isinstance(payload, str)
                         else _json.dumps(payload, ensure_ascii=False)})

    def _execute(rnd, tc):
        fn = tc.get('function') or {}
        name = fn.get('name') or ''
        tc_id = tc.get('id', '')
        try:
            args = _json.loads(fn.get('arguments') or '{}')
        except (_json.JSONDecodeError, TypeError) as e:
            _reply(tc_id, f'Invalid JSON arguments: {e}')
            return
        if not isinstance(args, dict):
            args = {}

        if name == 'write_composition':
            html = str(args.get('html') or '')
            if len(html) < 200:
                _reply(tc_id, 'Rejected: that is not a complete HTML document.')
                return
            state['html'] = html
            # Persist EVERY accepted write: if the next dispatch dies of a
            # network fault, this is the work that survives.
            save_draft(scene_dir, html)
            errors = _full_gate(html, scene_dir, abort_event=abort_event,
                                scene=scene, advisory=True)
            state['gate_ok'] = not errors
            _reply(tc_id, {'written_chars': len(html),
                           'gate_errors': errors[:8],
                           'passes_gate': not errors})
        elif name == 'composition_check':
            if not state['html']:
                _reply(tc_id, 'Nothing written yet — call write_composition first.')
                return
            errors = _full_gate(state['html'], scene_dir,
                                abort_event=abort_event, scene=scene,
                                advisory=True)
            state['gate_ok'] = not errors
            _reply(tc_id, {'gate_errors': errors[:8], 'passes_gate': not errors})
        elif name == 'web_search':
            query = str(args.get('query') or '').strip()
            if not query:
                _reply(tc_id, 'Error: query is required')
                return
            try:
                from lib.tasks_pkg.handlers.search import _web_search_one
                results, _diag, _eb, _v = _web_search_one(
                    query, str(scene.get('text') or ''), '', vertical='off')
                from tofu_search.search import format_search_for_tool_response
                _reply(tc_id, format_search_for_tool_response(
                    results, query=query)[:12000])
            except Exception as e:
                logger.warning('[SceneAuthor] web_search failed: %s', e)
                _reply(tc_id, f'Search failed: {e}')
        elif name == 'generate_asset':
            prompt = str(args.get('prompt') or '').strip()
            if not prompt:
                _reply(tc_id, 'Error: prompt is required')
                return
            try:
                w = int(args.get('width') or 1024)
                h = int(args.get('height') or 1024)
            except (TypeError, ValueError) as e:
                logger.debug('[SceneAuthor] bad width/height (%s) — using 1024x1024', e)
                w = h = 1024
            try:
                rel = _generate_scene_asset(prompt, scene_dir, width=w, height=h)
            except Exception as e:
                logger.warning('[SceneAuthor] generate_asset failed: %s', e)
                _reply(tc_id, f'Asset generation failed ({e}). Continue '
                              f'without it — do NOT reference a file that '
                              f'was not created.')
                return
            state.setdefault('assets', []).append(rel)
            _reply(tc_id, {
                'path': rel,
                'usage': f'<img src="{rel}"> or background-image: url(\'{rel}\')',
                'note': 'stored on disk and scene-local; safe to reference'})
        elif name == 'fetch_url':
            url = str(args.get('url') or '').strip()
            if not url:
                _reply(tc_id, 'Error: url is required')
                return
            try:
                from lib.tasks_pkg.handlers.search import _fetch_url_one
                got = _fetch_url_one(url, str(scene.get('text') or ''),
                                     fetch_reason='scene asset')
                _reply(tc_id, (got.get('page_content')
                               or f'Failed to fetch {url}')[:12000])
            except Exception as e:
                logger.warning('[SceneAuthor] fetch_url failed: %s', e)
                _reply(tc_id, f'Fetch failed: {e}')
        elif name == 'craft_reference':
            from lib.motion_video._craft import craft_reference
            ref = str(args.get('name') or '').strip()
            if not ref:
                _reply(tc_id, 'Error: name is required')
                return
            body = craft_reference(ref)
            state.setdefault('craft_reads', []).append(ref)
            _reply(tc_id, body)
        else:
            _reply(tc_id, f'Unknown tool {name!r}')

    # The budget flag participates in the loop's abort seam so a blown budget
    # exits through the same path as a user Stop.
    combined = AbortSignal(
        lambda: abort.aborted or bool(state.get('budget_exhausted')))

    rounds = 0
    try:
        outcome = run_agent_loop(
            abort=combined, max_tool_rounds=max_rounds,
            round_tools=SCENE_AUTHOR_TOOLS, dispatch=_dispatch,
            execute_tool=_execute, on_round_result=_on_round,
            on_tool_round=_on_tool_round)
        rounds = outcome.rounds
    except Exception as e:
        kind = 'transient' if is_transient_fault(e) else 'quality'
        logger.log(
            logging.WARNING if kind == 'transient' else logging.ERROR,
            '[SceneAuthor] %s loop raised (%s): %s', scene.get('id'), kind, e,
            exc_info=(kind != 'transient'))
        return {'outcome': kind, 'html': state['html'], 'rounds': rounds,
                'tokens': state['tokens'],
            'craft_reads': list(state.get('craft_reads') or []),
                'detail': f'author loop error: {type(e).__name__}: {e}'}

    if abort.aborted:
        return {'outcome': 'aborted', 'html': state['html'], 'rounds': rounds,
                'tokens': state['tokens'],
            'craft_reads': list(state.get('craft_reads') or []), 'detail': 'aborted during authoring'}
    if not state['html']:
        return {'outcome': 'quality', 'html': '', 'rounds': rounds,
                'tokens': state['tokens'],
            'craft_reads': list(state.get('craft_reads') or []),
                'detail': 'author wrote no composition'}
    # The ACCEPT/REJECT verdict deliberately omits the advisory fill check:
    # rejecting an under-filled composition degrades it to the template, which
    # measures worse on the very same axis (58% span / 38% dead). Fill reaches
    # the user through the engine's quality axis instead, where it is reported
    # rather than acted on destructively.
    errors = _full_gate(state['html'], scene_dir, abort_event=abort_event,
                        scene=scene)
    if errors:
        return {'outcome': 'quality', 'html': state['html'], 'rounds': rounds,
                'tokens': state['tokens'],
            'craft_reads': list(state.get('craft_reads') or []),
                'detail': ('authored composition still fails the static gate: '
                           + '; '.join(str(e) for e in errors[:3]))}
    return {'outcome': 'authored', 'html': state['html'], 'rounds': rounds,
            'tokens': state['tokens'],
            'craft_reads': list(state.get('craft_reads') or []), 'detail': ''}
