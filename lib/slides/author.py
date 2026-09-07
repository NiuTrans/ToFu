"""lib/slides/author.py — the per-page authoring loop.

One bounded LLM exchange per page: the page brief (from the outline stage)
+ the binding theme + the scenario bible + the PPTD cheatsheet → one
``.page`` YAML. The zero-LLM validator is the inner loop: findings go back
to the model for a repair round (up to ``max_rounds`` total attempts).

An individual call still returns a deterministic fallback so the batch can
finish and retry only failed pages. The recipe's author gate owns the product
decision: a deck with any fallback page is retried once and is never published
as designer-quality output while fallback pages remain.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['AuthorPromptContext', 'author_page', 'author_page_input_sha256',
           'fallback_page', 'PAGE_STYLES_DOC',
           'prepare_author_prompt_context']

_MAX_ROUNDS = 3
_MAX_TOKENS = 6000
_AUTHOR_INPUT_VERSION = 'slide-page-author-v3'

_INTERNAL_COPY_RE = re.compile(
    r'(?:章节|本章|过渡|转场|承上启下)(?:引导|过渡|转场)?页'
    r'|引出(?:后续|下一页|下文|时间线|章节)'
    r'|(?:用于|意在)(?:引出|过渡|承接)'
    r'|(?:placeholder|section\s+(?:divider|transition)|transition\s+page|'
    r'introduc(?:e|ing)\s+the\s+next|\bTODO\b|\bTBD\b|占位|待补充)',
    re.IGNORECASE,
)
_SOURCE_MARKER_RE = re.compile(r'\[S\d{1,3}\]', re.IGNORECASE)
_NUMBER_RE = re.compile(r'(?<![A-Za-z0-9_])\d+(?:\.\d+)?')

#: Theme textStyles every page may reference ($title/$body/$caption/$bignum).
PAGE_STYLES_DOC = """\
- $title    页标题(判断句/问句,大字号,结构色)
- $body     正文(可读字号,墨色)
- $caption  辅助/来源/页脚(小字号,辅助色,可加字距)
- $bignum   巨型数字(强调色,特大字号)
"""


@dataclass(frozen=True)
class AuthorPromptContext:
    """Immutable deck-level prompt material shared by page workers."""

    theme: object
    theme_block: str
    bible_excerpt: str
    cheatsheet: str


def _llm(messages, *, max_tokens: int, model: str | None = None,
         abort_check=None, max_429_attempts: int | None = None,
         owner_user_id: int | None = None, provider_pin_id: str = ''):
    from lib.llm_dispatch.api import dispatch_chat
    from lib.llm_dispatch.provider_pin import provider_pin
    from lib.production.llm_policy import production_llm_dispatch_kwargs
    with provider_pin(provider_pin_id):
        return dispatch_chat(
            messages, max_tokens=max_tokens, temperature=0.35,
            prefer_model=model, strict_model=bool(model),
            owner_user_id=owner_user_id,
            **production_llm_dispatch_kwargs(
                abort_check=abort_check,
                max_429_attempts=max_429_attempts),
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
    if not isinstance(data.get('elements') or [], list):
        return ['页面 elements 必须是数组']
    try:
        from lib.slides.components import expand_page_components
        elements = expand_page_components(data)
    except ValueError as exc:
        return [f'语义组件无效: {exc}']
    if not elements:
        return ['页面需要非空 elements 或 components 数组']
    page = Page(path=page_path,
                page_type=str(data.get('pageType') or 'content'),
                background=data.get('background')
                           or {'type': 'solid', 'color': '#FFFFFF'},
                elements=elements, raw=data)
    trial = Deck(title=deck.title, size=deck.size, theme=deck.theme,
                 pages=[page], root=deck.root)
    findings = validate_deck(trial)
    visible_text = '\n'.join(_visible_text_values(data))
    leaked = _INTERNAL_COPY_RE.search(visible_text)
    if leaked:
        findings.append(
            '页面正文泄漏了内部策划/版式指令: '
            f'{leaked.group(0)!r}。请改成读者可见的事实、判断或章节标题。')
    if _SOURCE_MARKER_RE.search(visible_text):
        findings.append(
            '页面正文仍含 [S1] 这类内部来源编号；请改为页脚来源域名，'
            '不要把研究阶段标记展示给读者。')
    return findings


def _visible_text_values(data) -> list[str]:
    """Collect reader-visible strings without inspecting layout numbers."""
    values: list[str] = []

    def visit(value, *, text_context: bool = False) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, text_context=(text_context or key == 'text'))
        elif isinstance(value, list):
            for item in value:
                visit(item, text_context=text_context)
        elif text_context and isinstance(value, (str, int, float)):
            values.append(str(value))

    for element in data.get('elements') or []:
        if not isinstance(element, dict):
            continue
        content = element.get('content')
        if isinstance(content, dict) and content.get('text') is not None:
            values.append(str(content.get('text')))
        elif isinstance(content, str):
            values.append(content)
        if element.get('elementType') == 'table':
            visit(element.get('rows'), text_context=True)
        if element.get('elementType') == 'chart':
            visit((element.get('data') or {}).get('categories'),
                  text_context=True)
            for series in (element.get('data') or {}).get('series') or []:
                if isinstance(series, dict) and series.get('name') is not None:
                    values.append(str(series['name']))
    return values


def _brief_fidelity_findings(brief: dict, text: str, *, page_index: int,
                             total: int) -> list[str]:
    """Reject invented chart numbers that have no page-brief evidence."""
    import yaml
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []

    evidence_parts = [
        str(brief.get('key_message') or ''),
        str(brief.get('content_notes') or ''),
    ]
    for source in brief.get('sources') or []:
        if isinstance(source, dict):
            evidence_parts.extend((str(source.get('title') or ''),
                                   str(source.get('point') or '')))
    allowed = {float(token) for token in _NUMBER_RE.findall(
        '\n'.join(evidence_parts))}
    # Small integers are structural labels (page numbers, steps, ranks), not
    # quantitative claims. Larger chart values need exact page evidence.
    allowed.update(float(value) for value in range(0, max(20, total) + 1))
    allowed.update((float(page_index + 1), float(total)))

    unsupported: list[str] = []
    for element in data.get('elements') or []:
        if (not isinstance(element, dict)
                or element.get('elementType') != 'chart'):
            continue
        for series in (element.get('data') or {}).get('series') or []:
            if not isinstance(series, dict):
                continue
            for value in series.get('values') or []:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                if float(value) not in allowed:
                    unsupported.append(str(value))
    if not unsupported:
        return []
    return [
        '图表包含本页事实素材中不存在的数值: '
        + ', '.join(list(dict.fromkeys(unsupported))[:8])
        + '。禁止为了“看起来像图表”而编造归一化分数；没有精确数据时，'
          '改用定性对比、流程图或结构图。'
    ]


def _extract_yaml(content: str) -> str:
    text = (content or '').strip()
    m = re.search(r'```(?:yaml|yml)?\s*\n(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Tolerate leading commentary: the page starts at the first top-level key.
    m = re.search(
        r'^(pageType|background|elements|components|notes):', text, re.MULTILINE)
    return text[m.start():].strip() if m else text


def _shared_author_prompt_prefix(deck, total: int, theme_block: str,
                                 bible_excerpt: str, cheatsheet: str,
                                 lang: str) -> str:
    """Return the immutable deck/design contract shared by every page."""
    if lang == 'zh':
        grounding_rules = (
            '## 素材—文案关联纪律(强制)\n'
            '- 每句贴在图片上或紧邻图片的判断,都必须由当前裁切区域中真实可见的物体/部位支持。\n'
            '- 若使用引线、箭头或圆点标注,逐条确认端点落在语义对应部位；'
            '禁止指着窗户写地板、指着座椅写滑轨。\n'
            '- 无法从图片明确确认具体部位时,不要猜也不要画引线,改用图片外 caption。'
            '标注组使用 anno-line-N / anno-dot-N / anno-text-N 对应命名。\n')
        return (
            f'你是顶级演示设计师,正在为《{deck.title}》设计一套 {total} 页的演示。\n\n'
            f'{theme_block}\n\n'
            f'## 主题文字样式\n{PAGE_STYLES_DOC}\n'
            f'{grounding_rules}\n'
            f'## 设计圣经(本场景纪律,必须遵守)\n{bible_excerpt}\n\n'
            f'## PPTD 格式(只允许这个子集)\n{cheatsheet}\n\n'
            f'页面尺寸 {deck.width}×{deck.height} px。')
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
        f'You are a world-class presentation designer authoring a {total}-page '
        f'deck titled "{deck.title}".\n\n'
        f'{theme_block}\n\n'
        f'## Theme text styles\n{PAGE_STYLES_DOC}\n'
        f'{grounding_rules}\n'
        f'## Design bible (binding)\n{bible_excerpt}\n\n'
        f'## PPTD format (this subset only)\n{cheatsheet}\n\n'
        f'Page geometry {deck.width}×{deck.height} px.')


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
    stable_prefix = _shared_author_prompt_prefix(
        deck, total, theme_block, bible_excerpt, cheatsheet, lang)
    if lang == 'zh':
        return (
            f'{stable_prefix}\n\n'
            f'## 本页任务\n- 页码: {page_index + 1}/{total}\n'
            f'- pageType: {ptype}\n- 读者任务: {purpose}\n- 核心信息: {key}\n'
            f'- 版式提示: {layout}\n- 内容素材: {notes}\n\n'
            f'{packet}\n'
            f'{images_block}\n'
            f'{sources_block}'
            '读者任务、版式提示、内容素材是内部输入，禁止把“章节引导页”'
            '“引出下一页”“过渡页”等策划语原样放进页面。禁止编造来源中'
            '不存在的数字或评测分数；没有精确数据时使用定性对比或流程图。\n'
            '只输出本页的 YAML '
            f'(pageType/background/elements/components),不要代码围栏外的任何解释。')
    return (
        f'{stable_prefix}\n\n'
        f'## This page\n- page: {page_index + 1}/{total}\n'
        f'- pageType: {ptype}\n- reader task: {purpose}\n- key message: {key}\n'
        f'- layout hint: {layout}\n- material: {notes}\n\n'
        f'{packet}\n'
        f'{images_block}\n'
        f'{sources_block}'
        'Reader task, layout hint, and material are internal inputs; never '
        'render phrases such as "section divider", "transition page", or '
        '"introduce the next page". Never invent benchmark numbers; use a '
        'qualitative comparison or diagram when exact sourced data is absent.\n'
        'Output ONLY this '
        f'page\'s YAML (pageType/background/elements/components), no commentary.')


def prepare_author_prompt_context(deck, theme=None) -> AuthorPromptContext:
    """Resolve and read deck-level prompt material exactly once per batch."""
    from lib.design_sys.themes import design_bible_text, theme_prompt_block

    if theme is None:
        from lib.design_sys.themes import default_theme_id, get_theme
        theme = get_theme(default_theme_id('tech-engineering'))
    return AuthorPromptContext(
        theme=theme,
        theme_block=theme_prompt_block(theme),
        bible_excerpt=design_bible_text(theme.scenario, limit=3500),
        cheatsheet=_read_cheatsheet(deck),
    )


def _author_prompt(deck, brief: dict, page_index: int, total: int, *,
                   theme=None, image_urls: list | None = None,
                   lang: str = 'zh', extra_findings: list | None = None,
                   seed_yaml: str | None = None,
                   prompt_context: AuthorPromptContext | None = None):
    """Return ``(resolved_theme, exact_prompt)`` for authoring/checkpointing."""
    context = prompt_context or prepare_author_prompt_context(deck, theme)
    if theme is not None and context.theme != theme:
        raise ValueError('prompt_context theme does not match page theme')
    theme = context.theme
    prompt = _build_prompt(
        deck, brief, page_index, total, context.theme_block,
        context.bible_excerpt, context.cheatsheet, image_urls or [], lang)
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
    return theme, prompt


def author_page_input_sha256(
        deck, brief: dict, page_index: int, total: int, *, theme=None,
        image_urls: list | None = None, lang: str = 'zh',
        model: str | None = None, max_rounds: int = _MAX_ROUNDS,
        extra_findings: list | None = None,
        seed_yaml: str | None = None,
        prompt_context: AuthorPromptContext | None = None) -> str:
    """Hash every prompt/policy input that can change one authored page."""
    _resolved_theme, prompt = _author_prompt(
        deck, brief, page_index, total, theme=theme,
        image_urls=image_urls, lang=lang, extra_findings=extra_findings,
        seed_yaml=seed_yaml, prompt_context=prompt_context)
    payload = json.dumps({
        'version': _AUTHOR_INPUT_VERSION,
        'prompt': prompt,
        'model': str(model or ''),
        'max_rounds': int(max_rounds),
        'max_tokens': _MAX_TOKENS,
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def author_page(deck, brief: dict, page_index: int, total: int, *,
                theme=None, image_urls: list | None = None, lang: str = 'zh',
                model: str | None = None, max_rounds: int = _MAX_ROUNDS,
                extra_findings: list | None = None,
                seed_yaml: str | None = None, abort_check=None,
                max_429_attempts: int | None = None,
                owner_user_id: int | None = None,
                provider_pin_id: str = '',
                prompt_context: AuthorPromptContext | None = None) -> dict:
    """Author one page. Returns ``{'ok', 'yaml', 'mode', 'rounds',
    'findings'}`` where mode is authored/fallback/unchanged/aborted."""
    theme, prompt = _author_prompt(
        deck, brief, page_index, total, theme=theme,
        image_urls=image_urls, lang=lang, extra_findings=extra_findings,
        seed_yaml=seed_yaml, prompt_context=prompt_context)

    messages = [{'role': 'user', 'content': prompt}]
    findings: list = []
    for rnd in range(1, max_rounds + 1):
        if abort_check is not None and abort_check():
            return {'ok': False, 'yaml': seed_yaml or '', 'mode': 'aborted',
                    'rounds': rnd - 1, 'findings': ['aborted']}
        try:
            content, usage = _llm(messages, max_tokens=_MAX_TOKENS,
                                  model=model, abort_check=abort_check,
                                  max_429_attempts=max_429_attempts,
                                  owner_user_id=owner_user_id,
                                  provider_pin_id=provider_pin_id)
        except Exception as e:
            if abort_check is not None and abort_check():
                return {'ok': False, 'yaml': seed_yaml or '',
                        'mode': 'aborted', 'rounds': rnd - 1,
                        'findings': ['aborted']}
            logger.warning('[Slides] page %d author dispatch failed: %s',
                           page_index + 1, e)
            findings = [f'页面作者调用失败: {type(e).__name__}: {e}']
            break
        if abort_check is not None and abort_check():
            return {'ok': False, 'yaml': seed_yaml or '', 'mode': 'aborted',
                    'rounds': rnd, 'findings': ['aborted']}
        yaml_text = _extract_yaml(content or '')
        findings = _validate_page_text(deck, f'pages/{page_index + 1:02d}.page',
                                       yaml_text)
        if not findings:
            findings.extend(_brief_fidelity_findings(
                brief, yaml_text, page_index=page_index, total=total))
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
              max_rounds: int = 2, owner_user_id: int | None = None,
              tenant_id: str | None = None) -> dict:
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

    from lib.production.owner_routing import owner_chat_route
    with owner_chat_route(
            owner_user_id, tenant_id=tenant_id, prefer_model=model or '',
            owner_tag=(f'slides-edit:{owner_user_id}'
                       if owner_user_id is not None else 'slides-edit')) as route:
        messages = [{'role': 'user', 'content': prompt}]
        findings: list = []
        for rnd in range(1, max_rounds + 1):
            try:
                content, _usage = _llm(
                    messages, max_tokens=_MAX_TOKENS, model=model,
                    owner_user_id=owner_user_id,
                    provider_pin_id=route.pin_id)
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
    """Deterministic editorial floor used between author retry attempts.

    Internal transition prose and research markers are removed. The recipe
    will not publish while any fallback remains, but keeping a valid page on
    disk lets the next bounded attempt retry only the failed pages.
    """
    import yaml

    def clean(value, limit: int) -> str:
        text = _SOURCE_MARKER_RE.sub('', str(value or ''))
        text = re.sub(r'\s+', ' ', text).strip(' ；;。')
        if _INTERNAL_COPY_RE.search(text):
            return ''
        if len(text) <= limit:
            return text
        clipped = text[:limit].rsplit(' ', 1)[0] or text[:limit]
        return clipped.rstrip('，,；;：:') + '…'

    page_type = str(brief.get('pageType') or 'content')
    title = clean(brief.get('key_message') or brief.get('purpose')
                  or deck.title, 72) or clean(deck.title, 72)
    body = clean(brief.get('content_notes'), 420)
    if not body:
        body = clean(brief.get('purpose'), 220)
    if body == title:
        body = ''

    elements = [{
        'elementId': 'accent-rail', 'elementType': 'shape',
        'bounds': [0, 0, 18, deck.height], 'shapeName': 'rect',
        'fill': {'type': 'solid', 'color': '$accent'},
    }, {
        'elementId': 'page-kind', 'elementType': 'text',
        'bounds': [72, 54, deck.width - 144, 30],
        'content': {'style': '$body', 'fontSize': 12,
                    'color': '$muted', 'align': ['left', 'middle'],
                    'letterSpacing': 3,
                    'text': page_type.replace('_', ' ').upper()},
    }]
    if page_type == 'chapter':
        elements.extend([{
            'elementId': 'chapter-panel', 'elementType': 'shape',
            'bounds': [0, 0, 360, deck.height], 'shapeName': 'rect',
            'fill': {'type': 'solid', 'color': '$primary'},
        }, {
            'elementId': 'chapter-mark', 'elementType': 'shape',
            'bounds': [420, 210, 72, 6], 'shapeName': 'rect',
            'fill': {'type': 'solid', 'color': '$accent'},
        }, {
            'elementId': 'title', 'elementType': 'text',
            'bounds': [420, 240, deck.width - 500, 220],
            'content': {'style': '$title', 'fontSize': 42,
                        'lineHeight': 1.25, 'align': ['left', 'middle'],
                        'text': title},
        }])
        if body:
            elements.append({
                'elementId': 'body', 'elementType': 'text',
                'bounds': [420, 480, deck.width - 500, 100],
                'content': {'style': '$body', 'color': '$muted',
                            'lineHeight': 1.5, 'align': ['left', 'top'],
                            'text': body},
            })
    else:
        elements.extend([{
            'elementId': 'title', 'elementType': 'text',
            'bounds': [72, 112, deck.width - 180, 150],
            'content': {'style': '$title', 'fontSize': 38,
                        'lineHeight': 1.25, 'align': ['left', 'middle'],
                        'text': title},
        }, {
            'elementId': 'rule', 'elementType': 'shape',
            'bounds': [72, 282, 72, 6], 'shapeName': 'rect',
            'fill': {'type': 'solid', 'color': '$accent'},
        }, {
            'elementId': 'body-panel', 'elementType': 'shape',
            'bounds': [72, 326, deck.width - 144, deck.height - 398],
            'shapeName': 'roundRect',
            'fill': {'type': 'solid', 'color': '$hairline'},
        }])
        if body:
            elements.append({
                'elementId': 'body', 'elementType': 'text',
                'bounds': [112, 362, deck.width - 224, deck.height - 470],
                'content': {'style': '$body', 'fontSize': 20,
                            'lineHeight': 1.55, 'align': ['left', 'top'],
                            'text': body},
            })
    return yaml.safe_dump({
        'pageType': page_type,
        'background': {'type': 'solid', 'color': '$bg'},
        'elements': elements,
    }, allow_unicode=True, sort_keys=False)
