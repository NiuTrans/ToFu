"""lib/slides/recipe.py — topic → finished deck, as a checkpointed stage graph.

Rides ``lib.production.stages`` (docs/SLIDES_CAPABILITY_DESIGN.md §4.5):

    outline   → deck plan: title + scenario + theme + per-page briefs  [LLM]
    design    → manifest + theme tokens + page skeletons               [zero-LLM]
    author    → one .page per brief (validation inner loop)            [LLM/page]
    assets    → remote images downloaded into media/ + rewritten       [network]
    render    → per-page PNG previews (headless Chrome)                [browser]
    visual_qa → VLM checklist per page + ONE repair round              [VLM]
    export    → native editable PPTX + fade transitions                [zero-LLM]

Cost posture (owner 拍板 lineage): pages bounded (3..20, default 12), one
bounded author loop per page, one QA repair round per page. A page that
fails degrades to a clean fallback page — the DECK never fails because one
page did.
"""

from __future__ import annotations

import json
import os
import re

from lib.log import get_logger
from lib.production.stages import Stage, run_stages

logger = get_logger(__name__)

__all__ = ['build_deck_from_topic', 'slides_recipe_stages']

_DEFAULT_PAGES = 12
_MIN_PAGES = 3
_MAX_PAGES = 20


# ── Seams (monkeypatchable) ───────────────────────────────

def _llm_chat(messages, **kwargs):
    from lib.llm_dispatch.api import dispatch_chat
    return dispatch_chat(messages, **kwargs)


# ── Stage: outline ────────────────────────────────────────

_OUTLINE_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)


def _build_outline_prompt(topic: str, *, lang: str, max_pages: int,
                          scenarios_doc: str, style_hint: str) -> str:
    if lang == 'zh':
        return (
            f'你是一名演示文稿总编。为主题《{topic}》设计一份演示 deck 的大纲。\n'
            f'{("用户风格要求:" + style_hint) if style_hint else ""}\n\n'
            '输出严格 JSON(无围栏无解释):\n'
            '{"title": "deck 标题", "scenario": "场景id", "theme_id": "主题id或空",\n'
            ' "pages": [{"pageType": "cover|table_of_contents|chapter|content|final",\n'
            '  "purpose": "本页读者任务(一句话)", "key_message": "本页核心信息/判断句",\n'
            '  "layout_hint": "版式提示(如: 左图右文/大数字+三行支撑/时间线)",\n'
            '  "content_notes": "本页要点素材(事实/数据/要点,供页作者展开)"}]}\n'
            f'要求:\n1. pages 数量 {_MIN_PAGES} 到 {max_pages};\n'
            f'2. scenario 从以下选择: {scenarios_doc};\n'
            '3. 首页 cover,末页 final(回答开篇问题/给出行动,禁止孤悬"谢谢");\n'
            '4. 每页 key_message 必须是完整判断句,不是栏目名;\n'
            '5. 节奏:密页与呼吸页交替;内容页不连续同构。\n')
    return (
        f'You are a presentation editor. Design the outline of a deck about '
        f'"{topic}".\n'
        f'{("Style request: " + style_hint) if style_hint else ""}\n\n'
        'Output strict JSON (no fences):\n'
        '{"title": "...", "scenario": "scenario-id", "theme_id": "or empty",\n'
        ' "pages": [{"pageType": "cover|table_of_contents|chapter|content|final",'
        ' "purpose": "reader task, one sentence", "key_message": "the judgment",'
        ' "layout_hint": "e.g. image-left-text-right / big number + supports",'
        ' "content_notes": "facts/points the page author expands"}]}\n'
        f'Rules: {_MIN_PAGES}..{max_pages} pages; scenario one of '
        f'{scenarios_doc}; first page cover, last page final (answer the '
        'opening question, never a bare "thank you"); every key_message is a '
        'complete judgment sentence; alternate dense and breathing pages.')


def _run_outline(ctx: dict) -> dict:
    topic = ctx['topic']
    lang = ctx.get('lang', 'zh')
    from lib.design_sys.themes import SCENARIOS, classify_scenario
    scenarios_doc = ', '.join(f'{sid}({m["label"]})'
                              for sid, m in SCENARIOS.items())
    prompt = _build_outline_prompt(
        topic, lang=lang, max_pages=ctx.get('max_pages', _DEFAULT_PAGES),
        scenarios_doc=scenarios_doc, style_hint=ctx.get('style') or '')
    content, usage = _llm_chat([{'role': 'user', 'content': prompt}],
                               max_tokens=4096, temperature=0.4,
                               prefer_model=ctx.get('model') or None,
                               log_prefix='[Slides:outline]')
    m = _OUTLINE_JSON_RE.search(content or '')
    if not m:
        raise ValueError('outline reply has no JSON object')
    raw = json.loads(m.group(0))
    pages = raw.get('pages')
    if not isinstance(pages, list) or len(pages) < _MIN_PAGES:
        raise ValueError(f'outline has {len(pages or [])} pages '
                         f'(need ≥{_MIN_PAGES})')
    pages = pages[:ctx.get('max_pages', _DEFAULT_PAGES)]
    scenario = str(raw.get('scenario') or '')
    if scenario not in SCENARIOS:
        scenario = classify_scenario(topic + ' '
                                     + str(raw.get('title') or ''))
    out = {'title': str(raw.get('title') or topic).strip()[:120],
           'scenario': scenario,
           'theme_id': str(raw.get('theme_id') or '').strip(),
           'pages': [p for p in pages if isinstance(p, dict)],
           'usage': usage if isinstance(usage, dict) else {}}
    logger.info('[Slides:outline] %r → %d pages, scenario=%s',
                out['title'][:50], len(out['pages']), scenario)
    return out


def _gate_outline(ctx: dict, artifact: dict) -> list:
    pages = artifact.get('pages') or []
    if len(pages) < _MIN_PAGES:
        return [f'outline too thin ({len(pages)} pages)']
    for i, p in enumerate(pages):
        if not (p.get('key_message') or p.get('purpose')):
            return [f'outline page {i + 1} has neither key_message nor purpose']
    return []


# ── Stage: design (zero-LLM) ──────────────────────────────

def _run_design(ctx: dict) -> dict:
    from lib.design_sys import fonts as _fonts
    from lib.design_sys.themes import default_theme_id, get_theme
    outline = ctx['artifacts']['outline']
    theme_id = outline.get('theme_id') or ''
    theme = get_theme(theme_id)
    if theme is None or theme.scenario != outline['scenario']:
        theme = get_theme(default_theme_id(outline['scenario']))
    theme_id = theme.id
    # Pre-warm the theme's faces so later stages never block on a download.
    staged = []
    for role in ('display', 'body', 'latin'):
        face = _fonts.get_font(theme.fonts.get(role, ''))
        if face:
            for src in face.sources:
                if _fonts.ensure_font(face.id, src.weight):
                    staged.append(f'{face.id}-w{src.weight}')
    c = theme.colors
    display_f = _fonts.get_font(theme.fonts['display'])
    body_f = _fonts.get_font(theme.fonts['body'])
    latin_f = _fonts.get_font(theme.fonts['latin'])
    theme_tokens = {
        'colors': {'bg': c['bg'], 'ink': c['ink'], 'primary': c['primary'],
                   'accent': c['accent'], 'muted': c['muted'],
                   'hairline': c['hairline']},
        'textStyles': {
            'title': {'fontSize': 40, 'color': '$primary', 'bold': True,
                      'fontFamily': display_f.family if display_f else 'MiSans',
                      'lineHeight': 1.2},
            'body': {'fontSize': 18, 'color': '$ink',
                     'fontFamily': body_f.family if body_f else 'MiSans',
                     'lineHeight': 1.5},
            'caption': {'fontSize': 12, 'color': '$muted', 'letterSpacing': 2,
                        'fontFamily': body_f.family if body_f else 'MiSans'},
            'bignum': {'fontSize': 88, 'color': '$accent', 'bold': True,
                       'fontFamily': latin_f.family if latin_f else 'MiSans'},
        },
        'tableStyles': {
            'default': {
                'firstRowStyle': {
                    'fill': {'type': 'solid', 'color': '$primary'},
                    'color': c['bg'], 'bold': True},
                'cellStyle': {'border': {'style': 'solid', 'width': 1,
                                         'color': '$hairline'},
                              'align': ['left', 'middle']},
                'bodyStyles': [],
            },
        },
    }
    out = {'theme_id': theme_id, 'theme_tokens': theme_tokens,
           'staged_fonts': staged, 'scenario': theme.scenario}
    logger.info('[Slides:design] theme=%s fonts=%s', theme_id, staged)
    return out


# ── Stage: author (per page) ──────────────────────────────

def _run_author(ctx: dict) -> dict:
    from lib.design_sys.themes import get_theme
    from lib.slides.author import author_page
    outline = ctx['artifacts']['outline']
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
    page_files = []
    image_urls = ctx.get('image_urls') or []
    emit = ctx.get('emit')
    for i, brief in enumerate(briefs):
        if ctx.get('abort_event') is not None and ctx['abort_event'].is_set():
            raise InterruptedError('aborted during authoring')
        res = author_page(deck, brief, i, total, theme=theme,
                          image_urls=image_urls,
                          lang=ctx.get('lang', 'zh'),
                          model=ctx.get('model') or None,
                          max_rounds=int(ctx.get('author_rounds') or 3))
        name = f'pages/{i + 1:02d}_{_slug(brief.get("pageType"))}.page'
        path = os.path.join(deck_dir, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(res['yaml'])
        page_files.append(name)
        if res['mode'] == 'authored':
            authored += 1
        if emit:
            emit({'type': 'page_authored', 'page': i + 1, 'total': total,
                  'mode': res['mode'], 'rounds': res['rounds']})
    return {'page_files': page_files, 'authored': authored, 'total': total}


def _slug(value) -> str:
    s = re.sub(r'[^a-z0-9]+', '_', str(value or 'content').lower())
    return s.strip('_') or 'content'


def _gate_author(ctx: dict, artifact: dict) -> list:
    if not artifact.get('page_files'):
        return ['author produced zero pages']
    return []


# ── Stage: assets ─────────────────────────────────────────

def _run_assets(ctx: dict) -> dict:
    """Download remote images into media/ and REWRITE refs to local paths —
    the deck directory must be self-contained before render/export."""
    from lib.slides.pptd import parse_deck
    deck_dir = ctx['deck_dir']
    _write_manifest(deck_dir, ctx)
    deck = parse_deck(os.path.join(deck_dir, 'deck.pptd'))
    media_dir = os.path.join(deck_dir, 'media')
    downloaded = 0
    url_map: dict = {}

    def _localize(src: str) -> str:
        nonlocal downloaded
        if not src.startswith(('http://', 'https://')):
            return src
        if src in url_map:
            return url_map[src]
        from lib.http_client import http_get
        try:
            resp = http_get(src, timeout=60)
            data = getattr(resp, 'content', b'') or b''
            if getattr(resp, 'status_code', 0) != 200 or len(data) < 1024:
                raise ValueError(f'HTTP {getattr(resp, "status_code", "?")} '
                                 f'{len(data)}B')
        except Exception as e:
            logger.warning('[Slides:assets] fetch failed %s: %s', src, e)
            url_map[src] = src          # keep remote; renderer/exporter retry
            return src
        ext = os.path.splitext(src.split('?')[0])[1].lower()
        if ext not in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'):
            ext = '.jpg'
        os.makedirs(media_dir, exist_ok=True)
        name = f'remote_{downloaded:02d}{ext}'
        with open(os.path.join(media_dir, name), 'wb') as f:
            f.write(data)
        url_map[src] = f'media/{name}'
        downloaded += 1
        return f'media/{name}'

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
        for page in deck.pages:
            with open(os.path.join(deck_dir, page.path), 'w',
                      encoding='utf-8') as f:
                yaml.safe_dump(page.raw | {
                    'pageType': page.page_type,
                    'background': page.background,
                    'elements': page.elements,
                }, f, allow_unicode=True, sort_keys=False)
    logger.info('[Slides:assets] %d remote image(s) localised', downloaded)
    return {'downloaded': downloaded, 'pages': len(deck.pages)}


def _write_manifest(deck_dir: str, ctx: dict) -> None:
    """Write deck.pptd from the outline + design artifacts (idempotent)."""
    import yaml
    outline = ctx['artifacts']['outline']
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
    with open(os.path.join(deck_dir, 'deck.pptd'), 'w', encoding='utf-8') as f:
        yaml.safe_dump(manifest, f, allow_unicode=True, sort_keys=False)


# ── Stage: render ─────────────────────────────────────────

def _run_render(ctx: dict) -> dict:
    from lib.slides.pptd import parse_deck
    from lib.slides.render_png import render_previews
    deck = parse_deck(os.path.join(ctx['deck_dir'], 'deck.pptd'))
    manifest = render_previews(deck, os.path.join(ctx['deck_dir'], 'preview'),
                               scale=2.0)
    return {'previews': [p['png'] for p in manifest['pages']],
            'failed': manifest['failed']}


# ── Stage: visual_qa ──────────────────────────────────────

def _run_visual_qa(ctx: dict) -> dict:
    """VLM checklist per page + ONE author repair round for actionable
    findings. Fully degradable: no vision model / no browser → skipped."""
    from lib.design_sys import visual_qa as vqa
    from lib.design_sys.themes import get_theme
    avail, reason = vqa.visual_qa_available()
    if not avail:
        logger.info('[Slides:qa] skipped: %s', reason)
        return {'ran': False, 'reason': reason}
    from lib.slides.author import author_page
    from lib.slides.pptd import parse_deck
    from lib.slides.render_png import render_page_png
    outline = ctx['artifacts']['outline']
    design = ctx['artifacts']['design']
    theme = get_theme(design['theme_id'])
    deck = parse_deck(os.path.join(ctx['deck_dir'], 'deck.pptd'))
    previews = (ctx['artifacts'].get('render') or {}).get('previews') or []
    repaired = 0
    clean = 0
    for i, png in enumerate(previews):
        if ctx.get('abort_event') is not None and ctx['abort_event'].is_set():
            raise InterruptedError('aborted during visual QA')
        res = vqa.qa_frame(png, theme=theme, label=f'page-{i + 1:02d}',
                           subject='幻灯片页面',
                           model=ctx.get('qa_model') or '')
        if not res.get('ok'):
            continue
        actionable = [f for f in res.get('findings') or []
                      if f.get('severity') in ('blocker', 'major')]
        if not actionable:
            clean += 1
            continue
        brief = outline['pages'][i]
        fix = author_page(deck, brief, i, len(deck.pages), theme=theme,
                          image_urls=ctx.get('image_urls') or [],
                          lang=ctx.get('lang', 'zh'),
                          model=ctx.get('model') or None, max_rounds=2,
                          extra_findings=[vqa.findings_text(actionable)])
        if fix['mode'] != 'authored':
            continue
        # Never let a repair make a page WORSE than a valid one: re-validate
        # (author_page already does) and re-render the preview.
        page_rel = deck.pages[i].path
        with open(os.path.join(ctx['deck_dir'], page_rel), 'w',
                  encoding='utf-8') as f:
            f.write(fix['yaml'])
        try:
            deck = parse_deck(os.path.join(ctx['deck_dir'], 'deck.pptd'))
            render_page_png(deck, i, png, scale=2.0)
            repaired += 1
        except Exception as e:
            logger.warning('[Slides:qa] page %d re-render failed: %s',
                           i + 1, e)
    logger.info('[Slides:qa] %d clean, %d repaired', clean, repaired)
    return {'ran': True, 'clean': clean, 'repaired': repaired}


# ── Stage: export ─────────────────────────────────────────

def _run_export(ctx: dict) -> dict:
    from lib.slides.pptd import parse_deck
    from lib.slides.export_pptx import export_pptx
    deck = parse_deck(os.path.join(ctx['deck_dir'], 'deck.pptd'))
    out_path = os.path.join(ctx['deck_dir'], f'{_safe_name(deck.title)}.pptx')
    summary = export_pptx(deck, out_path,
                          transition=ctx.get('transition') or 'fade')
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

def slides_recipe_stages() -> list:
    """Fresh Stage objects on EVERY call.

    Module-level ``Stage('render', _run_render, …)`` constants froze their
    function references at import time, silently defeating the documented
    monkeypatch seams (``_llm_chat`` / ``_run_render``): the unit tests patch
    ``recipe._run_render`` yet the REAL chromium render still ran — green on
    dev machines (browser present), red on CI (no chromium, 2026-08-06).
    Building the graph here resolves each seam at call time, after any patch.
    """
    return [Stage('outline', _run_outline, gate=_gate_outline, retry=1),
            Stage('design', _run_design),
            Stage('author', _run_author, gate=_gate_author),
            Stage('assets', _run_assets),
            Stage('render', _run_render, retry=1),
            Stage('visual_qa', _run_visual_qa),
            Stage('export', _run_export, gate=_gate_export, retry=1)]


def build_deck_from_topic(topic: str, workdir: str, *, lang: str = 'zh',
                          style: str = '', size=(1280, 720),
                          max_pages: int = _DEFAULT_PAGES,
                          model: str | None = None,
                          image_urls: list | None = None,
                          abort_event=None, emit=None) -> dict:
    """Run the full stage graph; returns the export artifact (+ friends).

    Checkpointed at ``<workdir>/pipeline_state.json`` — a crash resumes at
    the first unfinished stage.
    """
    max_pages = max(_MIN_PAGES, min(int(max_pages or _DEFAULT_PAGES),
                                    _MAX_PAGES))
    deck_dir = os.path.join(workdir, 'deck')
    os.makedirs(deck_dir, exist_ok=True)
    ctx = {'topic': topic, 'workdir': workdir, 'deck_dir': deck_dir,
           'lang': lang, 'style': style, 'size': tuple(size),
           'max_pages': max_pages, 'model': model,
           'image_urls': list(image_urls or []),
           'abort_event': abort_event, 'emit': emit}
    state_path = os.path.join(workdir, 'pipeline_state.json')
    artifacts = run_stages(
        slides_recipe_stages(), ctx, state_path=state_path, emit=emit,
        abort_check=(lambda: bool(abort_event is not None
                                  and abort_event.is_set())))
    export = artifacts['export']
    author = artifacts.get('author') or {}
    qa = artifacts.get('visual_qa') or {}
    render = artifacts.get('render') or {}
    outline = artifacts['outline']
    return {
        'pptx_path': export['pptx_path'],
        'title': outline['title'],
        'scenario': outline['scenario'],
        'theme_id': artifacts['design']['theme_id'],
        'pages': author.get('total', len(outline['pages'])),
        'authored_pages': author.get('authored', 0),
        'previews': render.get('previews') or [],
        'qa': qa,
        'bytes': export.get('bytes', 0),
        'deck_dir': deck_dir,
    }
