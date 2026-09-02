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

Cost posture (owner 拍板 lineage): pages bounded (3..20, default 12), one
bounded author loop per page, one QA repair round per page. A page that
fails degrades to a clean fallback page — the DECK never fails because one
page did.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil

from lib.log import get_logger
from lib.production.research import (
    RESEARCH_RESUME_TTL_S,
    evidence_checkpoint_version,
)
from lib.production.stages import Stage, run_stages

logger = get_logger(__name__)

__all__ = ['build_deck_from_topic', 'slides_recipe_stages']

_DEFAULT_PAGES = 12
_MIN_PAGES = 3
_MAX_PAGES = 20
_RESEARCH_RESUME_TTL_S = RESEARCH_RESUME_TTL_S
_RESEARCH_CHECKPOINT_VERSION = evidence_checkpoint_version(freshness='month')


# ── Seams (monkeypatchable) ───────────────────────────────

def _llm_chat(messages, **kwargs):
    from lib.llm_dispatch.api import dispatch_chat
    return dispatch_chat(messages, **kwargs)


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
            '由 PPTD 原生元素绘制(asset_mode=code),避免图生文字和虚构数据。\n')
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
        'elements (asset_mode=code).')


def _run_outline(ctx: dict) -> dict:
    topic = ctx['topic']
    lang = ctx.get('lang', 'zh')
    gate_feedback = list(ctx.pop('_outline_gate_feedback', []) or [])
    from lib.design_sys.themes import SCENARIOS, classify_scenario
    scenarios_doc = ', '.join(f'{sid}({m["label"]})'
                              for sid, m in SCENARIOS.items())
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
    research_doc = signal_doc + format_research_cards(research)
    prompt = _build_outline_prompt(
        topic, lang=lang, max_pages=ctx.get('max_pages', _DEFAULT_PAGES),
        scenarios_doc=scenarios_doc, style_hint=ctx.get('style') or '',
        research_doc=research_doc,
        research_as_of=str(research.get('as_of') or ''))
    content, usage = _llm_chat([{'role': 'user', 'content': prompt}],
                               max_tokens=4096, temperature=0.4,
                               prefer_model=ctx.get('model') or None,
                               strict_model=bool(ctx.get('model')),
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
           'source_cards': cards,
           'research_as_of': str(research.get('as_of') or ''),
           'usage': usage if isinstance(usage, dict) else {}}
    from lib.slides._creative_plan import normalise_deck_plan
    normalise_deck_plan(out)
    cards_by_id = {card['id']: card for card in cards}
    for page in out['pages']:
        source_ids = re.findall(r'\bS\d+\b',
                                str(page.get('content_notes') or ''))
        page['sources'] = [cards_by_id[sid] for sid in source_ids
                           if sid in cards_by_id][:4]
    logger.info('[Slides:outline] %r → %d pages, scenario=%s',
                out['title'][:50], len(out['pages']), scenario)
    return out


def _gate_outline(ctx: dict, artifact: dict) -> list:
    pages = artifact.get('pages') or []
    errors = []
    if len(pages) < _MIN_PAGES:
        return [f'outline too thin ({len(pages)} pages)']
    for i, p in enumerate(pages):
        if not (p.get('key_message') or p.get('purpose')):
            return [f'outline page {i + 1} has neither key_message nor purpose']
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
    if errors:
        ctx['_outline_gate_feedback'] = list(errors)
    else:
        ctx.pop('_outline_gate_feedback', None)
    return errors


# ── Stage: design (zero-LLM) ──────────────────────────────

def _run_design(ctx: dict) -> dict:
    import lib.design_sys.fonts as _fonts
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
    image_urls, input_findings = _materialise_input_images(
        ctx.get('image_urls') or [], deck_dir)
    asset_preflight = {'by_page': {}, 'records': [], 'findings': []}
    try:
        from lib.slides._asset_preflight import prepare_deck_assets
        asset_preflight = prepare_deck_assets(outline, deck_dir)
    except Exception as e:
        logger.warning('[Slides:author] asset preflight crashed: %s', e,
                       exc_info=True)
        asset_preflight['findings'] = [f'asset preflight crashed: {e}']
    asset_preflight['findings'] = (list(asset_preflight.get('findings') or [])
                                   + input_findings)
    emit = ctx.get('emit')
    for i, brief in enumerate(briefs):
        if ctx.get('abort_event') is not None and ctx['abort_event'].is_set():
            raise InterruptedError('aborted during authoring')
        res = author_page(deck, brief, i, total, theme=theme,
                          image_urls=(list(asset_preflight['by_page'].get(i, []))
                                      + image_urls),
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
    return {'page_files': page_files, 'authored': authored, 'total': total,
            'assets_by_page': asset_preflight['by_page'],
            'input_images': image_urls,
            'asset_findings': asset_preflight['findings']}


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
    for index, raw in enumerate(values or []):
        src = str(raw or '').strip()
        if not src:
            continue
        if src.startswith(('http://', 'https://')):
            if src not in out:
                out.append(src)
            continue

        # Already a valid, self-contained deck path needs no copy.
        joined = os.path.realpath(os.path.join(root, src))
        if (not os.path.isabs(src) and joined.startswith(root + os.sep)
                and os.path.isfile(joined)):
            rel = os.path.relpath(joined, root).replace(os.sep, '/')
            if rel not in out:
                out.append(rel)
            continue

        local = os.path.realpath(src)
        if not os.path.isfile(local):
            findings.append(f'caller image does not exist and was omitted: {src}')
            continue
        ext = os.path.splitext(local)[1].lower()
        if ext not in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'):
            findings.append(f'caller image has unsupported type and was omitted: {src}')
            continue
        try:
            with open(local, 'rb') as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()[:12]
            rel = f'media/input_{index + 1:02d}_{digest}{ext}'
            dest = os.path.join(root, *rel.split('/'))
            os.makedirs(media_dir, exist_ok=True)
            if not os.path.isfile(dest):
                from lib.json_store import atomic_output_path
                with atomic_output_path(dest) as tmp:
                    shutil.copy2(local, tmp)
            if rel not in out:
                out.append(rel)
        except OSError as e:
            findings.append(f'caller image could not be copied and was omitted: '
                            f'{src} ({e})')
    return out, findings


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
    outline = ctx['artifacts']['outline']
    design = ctx['artifacts']['design']
    author_artifact = ctx['artifacts'].get('author') or {}
    assets_by_page = author_artifact.get('assets_by_page') or {}
    theme = get_theme(design['theme_id'])
    by_page: dict = {}
    for finding in initial.get('findings') or []:
        by_page.setdefault(int(finding['page']) - 1, []).append(finding)

    originals = {}
    candidates = set()
    for index, findings in sorted(by_page.items()):
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
            extra_findings=[findings_text(findings)])
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
    for index in accepted:
        preview = os.path.join(ctx['deck_dir'], 'preview', 'pages',
                               f'{index + 1:02d}.png')
        try:
            render_page_png(deck, index, preview, scale=2.0)
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


# ── Stage: visual_qa ──────────────────────────────────────

def _run_visual_qa(ctx: dict) -> dict:
    """VLM checklist per page + ONE author repair round for actionable
    findings. Fully degradable: no vision model / no browser → skipped."""
    import lib.design_sys.visual_qa as vqa
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
    author_artifact = ctx['artifacts'].get('author') or {}
    assets_by_page = author_artifact.get('assets_by_page') or {}
    deck_findings = []
    try:
        from lib.design_sys.contact_sheet import build_contact_sheet
        contact = os.path.join(ctx['deck_dir'], '.tofu-qa',
                               'deck-contact.png')
        build_contact_sheet(previews, contact, label_prefix='Page')
        deck_qa = vqa.qa_frame(
            contact, theme=theme, label='deck-coherence',
            subject='整套幻灯片接触表（按页码从左到右、从上到下）',
            model=ctx.get('qa_model') or '')
        if deck_qa.get('ok'):
            deck_findings = [f for f in deck_qa.get('findings') or []
                             if f.get('severity') in ('blocker', 'major')]
    except Exception as e:
        logger.warning('[Slides:qa] deck contact-sheet QA failed: %s', e)
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
        page_tokens = (f'Page {i + 1}', f'page {i + 1}',
                       f'第{i + 1}页', f'页面 {i + 1}')
        actionable += [f for f in deck_findings
                       if any(token in str(f.get('element') or '')
                              for token in page_tokens)]
        if not actionable:
            clean += 1
            continue
        brief = outline['pages'][i]
        fix = author_page(deck, brief, i, len(deck.pages), theme=theme,
                          image_urls=(list(assets_by_page.get(i, []) or
                                             assets_by_page.get(str(i), []))
                                      + list(author_artifact.get('input_images')
                                             or [])),
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
    return {"ran": True, "clean": clean, "repaired": repaired,
            "coherence_findings": len(deck_findings),
            "coherence": deck_findings}


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
    return [Stage('research', _run_research,
                  resume_ttl_s=_RESEARCH_RESUME_TTL_S,
                  checkpoint_version=_RESEARCH_CHECKPOINT_VERSION),
            Stage('outline', _run_outline, gate=_gate_outline, retry=1),
            Stage('design', _run_design),
            Stage('author', _run_author, gate=_gate_author),
            Stage('assets', _run_assets),
            Stage('render', _run_render, retry=1),
            Stage('layout_qa', _run_layout_qa),
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
    # Chromium receives a file:// URL during preview rendering.  A relative
    # workdir turns into the invalid ``file://relative/path`` form (two slashes
    # mean the first component is parsed as a host), so make the production
    # root absolute once at the public boundary.
    workdir = os.path.abspath(workdir)
    max_pages = max(_MIN_PAGES, min(int(max_pages or _DEFAULT_PAGES),
                                    _MAX_PAGES))
    deck_dir = os.path.join(workdir, 'deck')
    os.makedirs(deck_dir, exist_ok=True)
    ctx = {'topic': topic, 'workdir': workdir, 'deck_dir': deck_dir,
           'lang': lang, 'style': style, 'size': tuple(size),
           'max_pages': max_pages, 'model': model,
           'qa_model': model or '',
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
    layout_qa = artifacts.get('layout_qa') or {}
    render = artifacts.get('render') or {}
    outline = artifacts['outline']
    return {
        'pptx_path': export['pptx_path'],
        'title': outline['title'],
        'scenario': outline['scenario'],
        'theme_id': artifacts['design']['theme_id'],
        'pages': author.get('total', len(outline['pages'])),
        'authored_pages': author.get('authored', 0),
        'generated_assets': sum(
            len(v) for v in (author.get('assets_by_page') or {}).values()),
        'asset_findings': author.get('asset_findings') or [],
        'previews': render.get('previews') or [],
        'qa': qa,
        'layout_qa': layout_qa,
        'bytes': export.get('bytes', 0),
        'deck_dir': deck_dir,
    }
