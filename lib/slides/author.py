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
                         prefer_model=model, log_prefix='[Slides:author]')


def _validate_page_text(deck, page_path: str, text: str) -> list:
    """Validate ONE page's YAML in the deck's context. Findings, zero-LLM."""
    import yaml
    from lib.slides.pptd import Deck, Page, validate_deck
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
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
    images_block = ''
    if image_urls:
        images_block = (
            '\n## 可用图片(只允许引用以下 URL;不允许编造其他图片地址)\n'
            + '\n'.join(f'- {u}' for u in image_urls[:12]) + '\n')
    if lang == 'zh':
        return (
            f'你是顶级演示设计师,正在为《{deck.title}》设计第 '
            f'{page_index + 1}/{total} 页(pageType: {ptype})。\n\n'
            f'## 本页任务\n- 读者任务: {purpose}\n- 核心信息: {key}\n'
            f'- 版式提示: {layout}\n- 内容素材: {notes}\n\n'
            f'{theme_block}\n\n'
            f'## 主题文字样式\n{PAGE_STYLES_DOC}\n'
            f'{images_block}\n'
            f'## 设计圣经(本场景纪律,必须遵守)\n{bible_excerpt}\n\n'
            f'## PPTD 格式(只允许这个子集)\n{cheatsheet}\n\n'
            f'页面尺寸 {deck.width}×{deck.height} px。只输出本页的 YAML '
            f'(pageType/background/elements),不要代码围栏外的任何解释。')
    return (
        f'You are a world-class presentation designer authoring page '
        f'{page_index + 1}/{total} (pageType: {ptype}) of "{deck.title}".\n\n'
        f'## This page\n- reader task: {purpose}\n- key message: {key}\n'
        f'- layout hint: {layout}\n- material: {notes}\n\n'
        f'{theme_block}\n\n'
        f'## Theme text styles\n{PAGE_STYLES_DOC}\n'
        f'{images_block}\n'
        f'## Design bible (binding)\n{bible_excerpt}\n\n'
        f'## PPTD format (this subset only)\n{cheatsheet}\n\n'
        f'Page geometry {deck.width}×{deck.height} px. Output ONLY this '
        f'page\'s YAML (pageType/background/elements), no commentary.')


def author_page(deck, brief: dict, page_index: int, total: int, *,
                theme=None, image_urls: list | None = None, lang: str = 'zh',
                model: str | None = None, max_rounds: int = _MAX_ROUNDS,
                extra_findings: list | None = None) -> dict:
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
    return {'ok': True, 'yaml': fallback_page(deck, brief, theme=theme),
            'mode': 'fallback', 'rounds': max_rounds, 'findings': findings}


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
