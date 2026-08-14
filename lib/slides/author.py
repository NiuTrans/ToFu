"""lib/slides/author.py — the per-page authoring loop.

One bounded LLM exchange per page: the page brief (from the outline stage)
+ the binding theme + the scenario bible + the PPTD cheatsheet → one
``.page`` YAML. The zero-LLM validator is the inner loop: findings go back
to the model for a repair round (up to ``max_rounds`` total attempts).

Never-fail-the-deck (same discipline as motion_video's scene author): a page
that still fails validation after the rounds degrades to a clean minimal
title+body page built from its brief, so one bad page cannot kill the deck.
"""

from __future__ import annotations

import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['author_page', 'fallback_page', 'PAGE_STYLES_DOC']

_MAX_ROUNDS = 3
_MAX_TOKENS = 6000

#: Theme textStyles every page may reference ($title/$body/$caption/$bignum).
PAGE_STYLES_DOC = """\
- $title    页标题(判断句/问句,大字号,结构色)
- $body     正文(可读字号,墨色)
- $caption  辅助/来源/页脚(小字号,辅助色,可加字距)
- $bignum   巨型数字(强调色,特大字号)
"""


def _llm(messages, *, max_tokens: int, model: str | None = None):
    from lib.llm_dispatch.api import dispatch_chat
    return dispatch_chat(messages, max_tokens=max_tokens, temperature=0.35,
                         prefer_model=model, strict_model=bool(model),
                         log_prefix='[Slides:author]')


def _validate_page_text(deck, page_path: str, text: str) -> list:
    """Validate ONE page's YAML in the deck's context. Findings, zero-LLM."""
    import yaml
    from lib.slides.pptd import Deck, Page, validate_deck
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        logger.debug('[Slides] page YAML parse failed (author will see the '
                     'finding): %s', e)
        return [f'YAML 解析失败: {e}']
    if not isinstance(data, dict):
        return ['页面必须是 YAML mapping']
    if not isinstance(data.get('elements'), list) or not data['elements']:
        return ['页面需要非空 elements 数组']
    page = Page(path=page_path,
                page_type=str(data.get('pageType') or 'content'),
                background=data.get('background')
                           or {'type': 'solid', 'color': '#FFFFFF'},
                elements=data['elements'], raw=data)
    trial = Deck(title=deck.title, size=deck.size, theme=deck.theme,
                 pages=[page], root=deck.root)
    return validate_deck(trial)


def _extract_yaml(content: str) -> str:
    text = (content or '').strip()
    m = re.search(r'```(?:yaml|yml)?\s*\n(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Tolerate leading commentary: the page starts at the first top-level key.
    m = re.search(r'^(pageType|background|elements|notes):', text, re.MULTILINE)
    return text[m.start():].strip() if m else text


def _build_prompt(deck, brief: dict, page_index: int, total: int,
                  theme_block: str, bible_excerpt: str, cheatsheet: str,
                  image_urls: list, lang: str) -> str:
    purpose = brief.get('purpose') or ''
    key = brief.get('key_message') or ''
    layout = brief.get('layout_hint') or ''
    notes = brief.get('content_notes') or ''
    ptype = brief.get('pageType') or 'content'
    from lib.slides._creative_plan import page_packet
    packet = page_packet(brief, page_index, total, deck_title=deck.title)
    images_block = ''
    if image_urls:
        images_block = (
            '\n## 可用图片(只允许引用以下 URL;不允许编造其他图片地址)\n'
            + '\n'.join(f'- {u}' for u in image_urls[:12]) + '\n')
    assigned = [str(p) for p in (brief.get('resolved_assets') or []) if p]
    if assigned:
        images_block = (
            '\n## 本页已生成并验证的主视觉(必须实际放入页面)\n'
            + '\n'.join(f'- {p}' for p in assigned)
            + '\n这些路径是 deck 内的真实文件。至少一个 image 元素必须引用'
              '本页指定路径;生成后不用等于没有素材。用裁切/蒙版/留白把它'
              '做成构图主体,不要缩成卡片角落的装饰。\n')
    source_cards = [s for s in (brief.get('sources') or [])
                    if isinstance(s, dict) and s.get('url')]
    sources_block = ''
    if source_cards:
        heading = (
            '\n## 本页事实来源(必须保持事实与数字一致,并用 $caption '
            '在页脚标注来源域名)\n' if lang == 'zh' else
            '\n## Sources for this page (keep facts/numbers exact and cite '
            'the source domains in a $caption footer)\n')
        sources_block = heading + '\n'.join(
            f'- [{s.get("id")}] {s.get("point")} — {s.get("url")}'
            for s in source_cards) + '\n'
    if lang == 'zh':
        grounding_rules = (
            '## 素材—文案关联纪律(强制)\n'
            '- 每句贴在图片上或紧邻图片的判断,都必须由当前裁切区域中真实可见的物体/部位支持。\n'
            '- 若使用引线、箭头或圆点标注,逐条确认端点落在语义对应部位；'
            '禁止指着窗户写地板、指着座椅写滑轨。\n'
            '- 无法从图片明确确认具体部位时,不要猜也不要画引线,改用图片外 caption。'
            '标注组使用 anno-line-N / anno-dot-N / anno-text-N 对应命名。\n')
        return (
            f'你是顶级演示设计师,正在为《{deck.title}》设计第 '
            f'{page_index + 1}/{total} 页(pageType: {ptype})。\n\n'
            f'## 本页任务\n- 读者任务: {purpose}\n- 核心信息: {key}\n'
            f'- 版式提示: {layout}\n- 内容素材: {notes}\n\n'
            f'{packet}\n{theme_block}\n\n'
            f'## 主题文字样式\n{PAGE_STYLES_DOC}\n'
            f'{images_block}\n'
            f'{sources_block}'
            f'{grounding_rules}\n'
            f'## 设计圣经(本场景纪律,必须遵守)\n{bible_excerpt}\n\n'
            f'## PPTD 格式(只允许这个子集)\n{cheatsheet}\n\n'
            f'页面尺寸 {deck.width}×{deck.height} px。只输出本页的 YAML '
            f'(pageType/background/elements),不要代码围栏外的任何解释。')
    grounding_rules = (
        '## Asset-to-copy grounding (binding)\n'
        '- Every claim on or beside an image must be supported by a visible '
        'object/feature in the current crop.\n'
        '- Trace every callout line/arrow/dot to its endpoint; the endpoint '
        'must land on the feature named by the label.\n'
        '- If the exact feature cannot be verified, omit the callout and use '
        'an outside caption. Name groups anno-line-N / anno-dot-N / '
        'anno-text-N.\n')
    return (
        f'You are a world-class presentation designer authoring page '
        f'{page_index + 1}/{total} (pageType: {ptype}) of "{deck.title}".\n\n'
        f'## This page\n- reader task: {purpose}\n- key message: {key}\n'
        f'- layout hint: {layout}\n- material: {notes}\n\n'
        f'{packet}\n{theme_block}\n\n'
        f'## Theme text styles\n{PAGE_STYLES_DOC}\n'
        f'{images_block}\n'
        f'{sources_block}'
        f'{grounding_rules}\n'
        f'## Design bible (binding)\n{bible_excerpt}\n\n'
        f'## PPTD format (this subset only)\n{cheatsheet}\n\n'
        f'Page geometry {deck.width}×{deck.height} px. Output ONLY this '
        f'page\'s YAML (pageType/background/elements), no commentary.')


def author_page(deck, brief: dict, page_index: int, total: int, *,
                theme=None, image_urls: list | None = None, lang: str = 'zh',
                model: str | None = None, max_rounds: int = _MAX_ROUNDS,
                extra_findings: list | None = None,
                seed_yaml: str | None = None) -> dict:
    """Author one page. Returns ``{'ok', 'yaml', 'mode', 'rounds',
    'findings'}`` where mode is 'authored' or 'fallback' (never raises)."""
    from lib.design_sys.themes import design_bible_text, theme_prompt_block

    if theme is None:
        from lib.design_sys.themes import default_theme_id, get_theme
        theme = get_theme(default_theme_id('tech-engineering'))
    theme_block = theme_prompt_block(theme)
    bible = design_bible_text(theme.scenario, limit=3500)
    cheatsheet = _read_cheatsheet(deck)
    prompt = _build_prompt(deck, brief, page_index, total, theme_block,
                           bible, cheatsheet, image_urls or [], lang)
    if extra_findings:
        prompt += ('\n\n## 视觉评审发现的问题(本轮必须修复)\n'
                   + '\n'.join(f'- {f}' for f in extra_findings[:8]))
    if seed_yaml:
        prompt += (
            '\n\n## 当前页面源码(在此基础上做最小修复)\n'
            f'```yaml\n{seed_yaml}\n```\n'
            '保留当前构图、文案、素材和无关元素；只调整问题元素的 bounds、'
            '字号、行高或文案换行。allowOverlap 只允许用于确实有意叠放的装饰'
            '字符，不得用来掩盖正文碰撞或溢出。输出完整页面 YAML。')

    messages = [{'role': 'user', 'content': prompt}]
    findings: list = []
    for rnd in range(1, max_rounds + 1):
        try:
            content, usage = _llm(messages, max_tokens=_MAX_TOKENS,
                                  model=model)
        except Exception as e:
            logger.warning('[Slides] page %d author dispatch failed: %s',
                           page_index + 1, e)
            break
        yaml_text = _extract_yaml(content or '')
        findings = _validate_page_text(deck, f'pages/{page_index + 1:02d}.page',
                                       yaml_text)
        required_assets = [str(path) for path in
                           (brief.get('resolved_assets') or []) if path]
        missing_assets = [path for path in required_assets
                          if path not in yaml_text]
        if not findings and missing_assets:
            findings.append(
                '页面没有引用已经生成的本页主视觉: '
                + ', '.join(missing_assets)
                + '。必须用 image 元素实际放入页面,不能生成后丢弃。')
        if not findings:
            logger.info('[Slides] page %d authored in %d round(s)',
                        page_index + 1, rnd)
            return {'ok': True, 'yaml': yaml_text, 'mode': 'authored',
                    'rounds': rnd, 'findings': []}
        logger.info('[Slides] page %d round %d: %d finding(s), e.g. %.100s',
                    page_index + 1, rnd, len(findings), findings[0])
        messages = [
            {'role': 'user', 'content': prompt},
            {'role': 'assistant', 'content': content},
            {'role': 'user', 'content': (
                '校验发现以下问题,请修复后重新输出完整页面 YAML(只输出 '
                'YAML):\n' + '\n'.join(f'- {f}' for f in findings[:10]))},
        ]
    logger.warning('[Slides] page %d degraded to fallback after %d rounds',
                   page_index + 1, max_rounds)
    if seed_yaml:
        return {'ok': False, 'yaml': seed_yaml, 'mode': 'unchanged',
                'rounds': max_rounds, 'findings': findings}
    return {'ok': True, 'yaml': fallback_page(deck, brief, theme=theme),
            'mode': 'fallback', 'rounds': max_rounds, 'findings': findings}


def edit_page(deck_dir: str, page_index: int, instruction: str, *,
              lang: str = 'zh', model: str | None = None,
              max_rounds: int = 2) -> dict:
    """Chat-driven single-page edit: instruction → re-authored page → fresh
    preview + re-exported PPTX.

    The page's CURRENT yaml is the base the model edits (never a blank
    rewrite), so an instruction like「把标题改成 X」keeps everything else.
    Returns ``{'ok', 'mode', 'pptx_path', 'preview', 'detail'}``; never
    raises — a failed edit leaves the on-disk page untouched.
    """
    from lib.slides.pptd import parse_deck
    from lib.slides.render_png import render_page_png

    deck = parse_deck(os.path.join(deck_dir, 'deck.pptd'))
    if page_index < 0 or page_index >= len(deck.pages):
        return {'ok': False,
                'detail': f'page {page_index + 1} out of range '
                          f'(deck has {len(deck.pages)} pages)'}
    page = deck.pages[page_index]
    page_abs = os.path.join(deck_dir, page.path)
    try:
        with open(page_abs, encoding='utf-8') as f:
            current_yaml = f.read()
    except OSError as e:
        logger.warning('[Slides] edit_page cannot read %s: %s', page_abs, e)
        return {'ok': False, 'detail': f'cannot read page file: {e}'}

    from lib.design_sys.themes import (design_bible_text, get_theme,
                                       theme_prompt_block)
    # Reconstruct the theme from the deck's own tokens (the deck is the
    # source of truth after a restart; the registry theme only feeds the
    # bible/prohibition text).
    theme = None
    from lib.design_sys.themes import THEMES
    colors = (deck.theme or {}).get('colors') or {}
    for t in THEMES:
        if t.colors.get('primary') == colors.get('primary'):
            theme = t
            break
    if theme is None:
        from lib.design_sys.themes import classify_scenario, default_theme_id
        theme = get_theme(default_theme_id(classify_scenario(deck.title)))
    theme_block = theme_prompt_block(theme)
    bible = design_bible_text(theme.scenario, limit=2500)
    cheatsheet = _read_cheatsheet(deck)

    if lang == 'zh':
        prompt = (
            f'你是顶级演示设计师。这是《{deck.title}》第 {page_index + 1} 页'
            f'的当前 PPTD 源文件:\n```yaml\n{current_yaml}\n```\n\n'
            f'## 用户的修改指令(必须完成)\n{instruction}\n\n'
            f'要求:在现有页面基础上修改,保持其余元素不动;输出修改后的'
            f'完整页面 YAML(只输出 YAML)。\n\n'
            f'{theme_block}\n\n## 设计纪律\n{bible}\n\n'
            f'## PPTD 格式子集\n{cheatsheet}\n'
            f'页面尺寸 {deck.width}×{deck.height} px。')
    else:
        prompt = (
            f'You are a world-class presentation designer. Current PPTD '
            f'source of page {page_index + 1} of "{deck.title}":\n'
            f'```yaml\n{current_yaml}\n```\n\n'
            f'## Edit instruction (must do)\n{instruction}\n\n'
            f'Modify the existing page; keep everything else intact. Output '
            f'the COMPLETE updated page YAML only.\n\n'
            f'{theme_block}\n\n## Design discipline\n{bible}\n\n'
            f'## PPTD subset\n{cheatsheet}\n'
            f'Page geometry {deck.width}×{deck.height} px.')

    messages = [{'role': 'user', 'content': prompt}]
    findings: list = []
    for rnd in range(1, max_rounds + 1):
        try:
            content, _usage = _llm(messages, max_tokens=_MAX_TOKENS,
                                   model=model)
        except Exception as e:
            logger.warning('[Slides] page %d edit dispatch failed: %s',
                           page_index + 1, e)
            return {'ok': False, 'detail': f'LLM dispatch failed: {e}'}
        yaml_text = _extract_yaml(content or '')
        findings = _validate_page_text(deck, page.path, yaml_text)
        if not findings:
            from lib.json_store import write_text_atomic
            write_text_atomic(page_abs, yaml_text)
            deck = parse_deck(os.path.join(deck_dir, 'deck.pptd'))
            preview = os.path.join(deck_dir, 'preview', 'pages',
                                   f'{page_index + 1:02d}.png')
            try:
                render_page_png(deck, page_index, preview, scale=2.0)
            except Exception as e:
                logger.warning('[Slides] page %d preview re-render failed: '
                               '%s', page_index + 1, e)
            from lib.slides.export_pptx import export_pptx
            import glob as _glob
            existing = _glob.glob(os.path.join(deck_dir, '*.pptx'))
            out_path = existing[0] if existing else os.path.join(
                deck_dir, 'deck.pptx')
            export_pptx(deck, out_path)
            logger.info('[Slides] page %d edited in %d round(s) → %s',
                        page_index + 1, rnd, out_path)
            return {'ok': True, 'mode': 'edited', 'rounds': rnd,
                    'pptx_path': out_path, 'preview': preview}
        messages = [
            {'role': 'user', 'content': prompt},
            {'role': 'assistant', 'content': content},
            {'role': 'user', 'content': (
                '校验发现以下问题,请修复后重新输出完整页面 YAML(只输出 '
                'YAML):\n' + '\n'.join(f'- {f}' for f in findings[:10]))},
        ]
    return {'ok': False,
            'detail': 'edit failed validation after '
                      f'{max_rounds} round(s): {findings[:2]}',
            'findings': findings}


def _read_cheatsheet(deck) -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'guide', 'PPTD_CHEATSHEET.md')
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        logger.warning('[Slides] cheatsheet unreadable: %s', e)
        return ''
    return (text.replace('{W}', str(deck.width))
                .replace('{H}', str(deck.height)))


def fallback_page(deck, brief: dict, *, theme=None) -> str:
    """The zero-LLM floor: title + body on the theme ground. Always valid.
    Every color goes through the $token system, so the page follows whatever
    theme the deck carries without this function naming one hex value."""
    title = str(brief.get('key_message') or brief.get('purpose')
                or deck.title)[:60]
    body = str(brief.get('content_notes') or '')[:400] or title
    import html as _h
    t, b = _h.escape(title), _h.escape(body)
    return f'''pageType: {brief.get("pageType") or "content"}
background: {{type: solid, color: "$bg"}}
elements:
  - elementId: title
    elementType: text
    bounds: [72, 72, {deck.width - 144}, 120]
    content:
      style: "$title"
      align: [left, middle]
      text: |
        <p>{t}</p>
  - elementId: rule
    elementType: shape
    bounds: [72, 210, 64, 6]
    shapeName: rect
    fill: {{type: solid, color: "$accent"}}
  - elementId: body
    elementType: text
    bounds: [72, 250, {deck.width - 144}, {deck.height - 330}]
    content:
      style: "$body"
      align: [left, top]
      lineHeight: 1.6
      text: |
        <p>{b}</p>
'''
