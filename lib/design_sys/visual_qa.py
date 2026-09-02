"""lib/design_sys/visual_qa.py — multimodal visual QA for produced frames/pages.

The missing half of every gate stack we had (docs/modules/ingest_media.md
§3.3): the existing gates are all PROGRAMMATIC — contract, contrast, overflow,
fill. None of them can see that a frame is ugly. This module puts a
vision-capable model on the rendered pixels with a designer's checklist, and
returns STRUCTURED findings a repair loop can act on.

Checklist adapted from open-kimi-ppt-skill's SKILL.md step4 (MIT) plus two
additions the theme system makes checkable: palette/type consistency against
the binding theme, and the anti-AI-slop prohibitions.

Degradation discipline (the whole point of the return shape):

  * no playwright / no Chromium        → ``skipped`` (infrastructure, never
                                         a defect charged to the scene);
  * no vision-capable model slot       → ``skipped``;
  * VLM call fails / unparseable reply → ``ok=False`` with ``reason`` — the
                                         caller decides; it must NOT fail a
                                         film/deck over a QA outage.

Nothing here retries. The owning capability decides what findings mean
(repair round, advisory note, quality-axis entry).
"""

from __future__ import annotations

import base64
import json
import os
import re

from lib.log import get_logger
from lib.production.contracts import normalise_findings

logger = get_logger(__name__)

__all__ = ['QA_CHECKLIST', 'visual_qa_available', 'screenshot_composition',
           'qa_frame', 'findings_text']

#: The designer's checklist, one row per item. ``id`` is stable for telemetry.
QA_CHECKLIST: tuple = (
    ('distortion', '图片/图形是否变形(拉伸、压缩、模糊、像素化)'),
    ('occlusion-key', '文字是否压在关键画面上(人脸、产品主体、Logo、图表数据区)'),
    ('out-of-bounds', '元素是否超出页面/画框边界'),
    ('contrast', '文字与背景、相邻色块之间对比是否足够(可读性)'),
    ('typography', '排版是否统一(对齐轴、间距、字号层级、页边距)'),
    ('overflow', '文字是否溢出或疑似被截断(文本过长、行距过密、字号过大)'),
    ('occlusion-layer', '内容是否被上层元素遮挡'),
    ('theme-fidelity', '是否忠于绑定主题(配色/字族/单一强调色)——出现主题外'
                       '的颜色体系即判违例'),
    ('temporal-staging', '若图像是从左到右的动画时序接触表:开场/中段/收束是否'
                         '都可读,是否存在空白开场、跳变、碰撞或未完成的收束'),
    ('semantic-consistency', '数字、标签、图表和状态在各时刻是否与叙事'
                            '一致;动画中途不得出现错误数据或互相矛盾的标签'),
    ('deck-coherence', '若图像是整套页面接触表:视觉身份、网格、页边距、'
                       '字体层级和强调色是否连贯,同时仍有清晰节奏变化'),
    ('layout-repetition', '若是整套页面:是否连续重复同一种卡片/分栏模板,'
                         '或每页都像换文案不换构图'),
    ('asset-relevance', '图片/插画是否真正支撑该页判断,而不是泛化装饰、'
                       '错误主体、虚构数据或与文字无关的素材'),
    ('annotation-grounding', '逐条沿引线/箭头/圆点从标注文案追到端点:端点'
                             '是否真的落在能证明该文案的可见物体或部位上;'
                             '不得指着窗户写地板、指着座椅写滑轨'),
    ('ai-slop', '是否有 AI 味套路(卡片墙、蓝紫渐变、玻璃拟态、辉光描边、'
                '2x2 矩阵摆拍、无意义装饰)'),
)

_QA_PROMPT_ZH = """你是一名苛刻的视觉设计评审。下面是一张{subject}的渲染图{theme_line}。
请逐项核查清单,只报告**真实可见**的问题(不要臆测,不要报清单外项目)。
若页面含引线、箭头、圆点或贴图标注,必须逐条从标注文字沿线检查到端点,
确认端点实际落到的物体/部位与文案一致；不要只看文本框和图片是否相邻。
例如端点落在窗户却写“纯平地板”,必须报告 annotation-grounding:

{checklist}

输出严格 JSON(不要代码围栏、不要解释):
{{"findings": [{{"check": "清单项id", "element": "出问题的元素/区域",
"issue": "问题描述", "severity": "blocker|major|minor",
"fix": "具体修法(改什么属性/挪到哪/换成什么)"}}]}}
没有问题时输出 {{"findings": []}}。severity 口径:blocker=不可交付(出界/不可读/压关键画面),
major=明显拉低品质,minor=可打磨。"""


def visual_qa_available() -> tuple:
    """``(available, reason)`` — infrastructure + model preflight.

    Split from the QA call so a caller can decide ONCE per job whether the QA
    stage exists at all, without paying a browser boot per scene to find out.
    """
    try:
        import playwright.sync_api  # noqa: F401
    except Exception as e:
        logger.debug('[VisualQA] playwright unavailable: %s', e)
        return False, f'playwright unavailable: {e}'
    if not _vision_model():
        return False, 'no vision-capable model slot in the dispatcher'
    return True, ''


def _vision_model() -> str:
    """First dispatcher slot advertising the vision capability, or ''."""
    try:
        from lib.llm_dispatch.factory import get_dispatcher
        dispatcher = get_dispatcher()
        for slot in getattr(dispatcher, 'slots', []) or []:
            try:
                if 'vision' in (getattr(slot, 'capabilities', None) or ()):
                    return getattr(slot, 'model', '') or ''
            except Exception as e:
                logger.debug('[VisualQA] slot capability probe failed: %s', e)
                continue
    except Exception as e:
        logger.debug('[VisualQA] dispatcher probe failed: %s', e)
    return ''


def screenshot_composition(scene_dir: str, out_path: str, *,
                           width: int = 0, height: int = 0,
                           settle_ms: int = 500,
                           timeout_ms: int = 20000) -> str:
    """Screenshot a composition's ``index.html`` at its SETTLED end state.

    Seeks every registered GSAP timeline to completion first — QA judges the
    frame the viewer actually reads, not the half-entered one. Returns
    ``out_path``; raises on failure (the caller's try/except maps that to a
    skip — an unbootable browser is infrastructure).
    """
    from playwright.sync_api import sync_playwright
    try:
        import chromium_env
        chromium_env.ensure_chromium_env(os.environ)
    except Exception as e:
        logger.debug('[VisualQA] chromium_env shim unavailable: %s', e)

    index = os.path.abspath(os.path.join(scene_dir, 'index.html'))
    if not os.path.isfile(index):
        raise FileNotFoundError(f'no composition at {index}')
    with open(index, encoding='utf-8') as fh:
        head = fh.read(4096)
    if not width or not height:
        mw = re.search(r'data-width="(\d+)"', head)
        mh = re.search(r'data-height="(\d+)"', head)
        width = width or (int(mw.group(1)) if mw else 1080)
        height = height or (int(mh.group(1)) if mh else 1440)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={'width': width,
                                              'height': height})
            page.goto('file://' + index, wait_until='load',
                      timeout=timeout_ms)
            page.wait_for_timeout(350)
            page.evaluate(
                '() => { const t = window.__timelines || {};'
                ' for (const k in t) { try { t[k].progress(1).pause(); }'
                ' catch (e) {} } }')
            page.wait_for_timeout(settle_ms)
            page.screenshot(path=out_path)
        finally:
            browser.close()
    return out_path


def qa_frame(image_path: str, *, theme=None, label: str = '',
             subject: str = '视频帧', model: str = '',
             max_tokens: int = 1500) -> dict:
    """Run the checklist against one rendered frame/page image.

    Returns ``{'ok', 'skipped', 'reason', 'findings', 'has_blocker',
    'summary'}``; NEVER raises. ``findings`` items are
    ``{'check', 'element', 'issue', 'severity', 'fix'}``.
    """
    out = {'ok': False, 'skipped': False, 'reason': '', 'findings': [],
           'has_blocker': False, 'summary': ''}
    if not os.path.isfile(image_path):
        out['skipped'] = True
        out['reason'] = f'frame image missing: {image_path}'
        return out

    model = model or _vision_model()
    if not model:
        out['skipped'] = True
        out['reason'] = 'no vision-capable model slot'
        return out
    try:
        from lib.model_info._capabilities import model_supports_vision
        if not model_supports_vision(model):
            out['skipped'] = True
            out['reason'] = f'{model} has no vision capability'
            return out
    except Exception as e:
        logger.debug('[VisualQA] vision probe failed for %s: %s', model, e)

    theme_line = ''
    if theme is not None:
        c = theme.colors
        theme_line = (f',绑定主题为「{theme.label}」(背景{c["bg"]} 墨色'
                      f'{c["ink"]} 结构色{c["primary"]} 强调色{c["accent"]})')
    checklist = '\n'.join(f'{i}. [{cid}] {text}'
                          for i, (cid, text) in enumerate(QA_CHECKLIST, 1))
    prompt = _QA_PROMPT_ZH.format(subject=subject, theme_line=theme_line,
                                  checklist=checklist)

    try:
        with open(image_path, 'rb') as fh:
            data_uri = ('data:image/png;base64,'
                        + base64.b64encode(fh.read()).decode('ascii'))
    except OSError as e:
        logger.debug('[VisualQA] frame unreadable %s: %s', image_path, e)
        out['skipped'] = True
        out['reason'] = f'frame unreadable: {e}'
        return out

    try:
        from lib.llm_dispatch.api import dispatch_chat
        content, _usage = dispatch_chat(
            [{'role': 'user', 'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': data_uri}},
            ]}],
            max_tokens=max_tokens, temperature=0.1, prefer_model=model,
            strict_model=True,
            log_prefix=f'[VisualQA:{label}]')
    except Exception as e:
        out['reason'] = f'VLM dispatch failed: {e}'
        logger.warning('[VisualQA] %s QA dispatch failed: %s', label, e)
        return out

    findings = _parse_findings(content or '')
    if findings is None:
        out['reason'] = 'unparseable QA reply'
        logger.warning('[VisualQA] %s reply not parseable: %.200s',
                       label, content)
        return out
    out['ok'] = True
    out['findings'] = findings
    out['has_blocker'] = any(f.get('severity') == 'blocker' for f in findings)
    out['summary'] = f'{len(findings)} finding(s)'
    logger.info('[VisualQA] %s: %d finding(s) (blocker=%s)',
                label, len(findings), out['has_blocker'])
    return out


_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)


def _parse_findings(content: str) -> list | None:
    """Parse the VLM reply into findings; ``None`` = unparseable."""
    m = _JSON_RE.search(content or '')
    if not m:
        return None
    try:
        raw = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        logger.debug('[VisualQA] QA reply JSON parse failed: %s', e)
        return None
    items = raw.get('findings')
    if not isinstance(items, list):
        return None
    valid_checks = {cid for cid, _ in QA_CHECKLIST}
    return normalise_findings(items, valid_checks=valid_checks)


def findings_text(findings: list, *, limit: int = 6) -> str:
    """Render findings as the bullet list a repair prompt consumes."""
    lines = []
    for f in findings[:limit]:
        sev = f.get('severity', 'minor')
        lines.append(f'- [{sev}] {f.get("issue", "")}'
                     + (f' 修法: {f["fix"]}' if f.get('fix') else ''))
    return '\n'.join(lines)
