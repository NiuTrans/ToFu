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

Nothing here performs a semantic QA retry. The transport may try at most two
provider slots under the finite production 429 budget; the owning capability
decides what findings mean (repair round, advisory note, quality-axis entry).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re

from lib.log import get_logger
from lib.production.contracts import normalise_findings

logger = get_logger(__name__)

__all__ = ['QA_CHECKLIST', 'visual_qa_available', 'screenshot_composition',
           'resolve_visual_qa_model', 'qa_frame_input_sha256', 'qa_frame',
           'load_visual_qa_cache', 'cached_visual_qa_result',
           'remember_visual_qa_result', 'findings_text']

_MAX_QA_IMAGE_BYTES = 16 * 1024 * 1024
_MAX_QA_OUTPUT_TOKENS = 4096
_QA_INPUT_VERSION = 'visual-qa-input-v1'

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


def resolve_visual_qa_model(preferred: str = '') -> str:
    """Resolve one model once so cache identity and dispatch cannot drift."""
    return str(preferred or '').strip() or _vision_model()


def _qa_prompt(theme, subject: str) -> str:
    theme_line = ''
    if theme is not None:
        c = theme.colors
        theme_line = (f',绑定主题为「{theme.label}」(背景{c["bg"]} 墨色'
                      f'{c["ink"]} 结构色{c["primary"]} 强调色{c["accent"]})')
    checklist = '\n'.join(f'{i}. [{cid}] {text}'
                          for i, (cid, text) in enumerate(QA_CHECKLIST, 1))
    return _QA_PROMPT_ZH.format(subject=subject, theme_line=theme_line,
                                checklist=checklist)


def _qa_output_tokens(max_tokens) -> int:
    try:
        return max(128, min(_MAX_QA_OUTPUT_TOKENS, int(max_tokens)))
    except (TypeError, ValueError, OverflowError):
        return 1500


def _qa_input_sha256(prompt: str, model: str, output_tokens: int,
                     raw_image: bytes) -> str:
    payload = json.dumps({
        'version': _QA_INPUT_VERSION,
        'prompt': prompt,
        'model': model,
        'max_tokens': output_tokens,
        'temperature': 0.1,
        'strict_model': True,
        'image_bytes': len(raw_image),
        'image_sha256': hashlib.sha256(raw_image).hexdigest(),
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def qa_frame_input_sha256(image_path: str, *, theme=None,
                          subject: str = '视频帧', model: str = '',
                          max_tokens: int = 1500) -> str:
    """Hash every semantic VLM input using the same builders as dispatch."""
    resolved_model = resolve_visual_qa_model(model)
    if not resolved_model:
        raise ValueError('no vision-capable model slot')
    with open(image_path, 'rb') as source:
        raw_image = source.read(_MAX_QA_IMAGE_BYTES + 1)
        file_size = os.fstat(source.fileno()).st_size
    if (not 0 < file_size <= _MAX_QA_IMAGE_BYTES
            or len(raw_image) != file_size):
        raise ValueError('frame image is empty, oversized, or changed during read')
    return _qa_input_sha256(
        _qa_prompt(theme, subject), resolved_model,
        _qa_output_tokens(max_tokens), raw_image)


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
             max_tokens: int = 1500, abort_check=None,
             max_429_attempts: int | None = None,
             owner_user_id: int | None = None,
             provider_pin_id: str = '') -> dict:
    """Run the checklist against one rendered frame/page image.

    Returns ``{'ok', 'skipped', 'reason', 'findings', 'has_blocker',
    'summary'}``; NEVER raises. ``findings`` items are
    ``{'check', 'element', 'issue', 'severity', 'fix'}``.
    """
    out = {'ok': False, 'skipped': False, 'reason': '', 'findings': [],
           'has_blocker': False, 'summary': ''}
    if abort_check is not None and abort_check():
        out['skipped'] = True
        out['reason'] = 'aborted before visual QA'
        return out
    if not os.path.isfile(image_path):
        out['skipped'] = True
        out['reason'] = f'frame image missing: {image_path}'
        return out

    model = resolve_visual_qa_model(model)
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

    prompt = _qa_prompt(theme, subject)

    try:
        image_bytes = os.path.getsize(image_path)
    except OSError as e:
        out['skipped'] = True
        out['reason'] = f'frame stat failed: {e}'
        return out
    if image_bytes > _MAX_QA_IMAGE_BYTES:
        out['skipped'] = True
        out['reason'] = (f'frame image is {image_bytes} bytes; visual QA limit '
                         f'is {_MAX_QA_IMAGE_BYTES}')
        return out
    try:
        with open(image_path, 'rb') as fh:
            raw_image = fh.read(_MAX_QA_IMAGE_BYTES + 1)
    except OSError as e:
        logger.debug('[VisualQA] frame unreadable %s: %s', image_path, e)
        out['skipped'] = True
        out['reason'] = f'frame unreadable: {e}'
        return out
    if len(raw_image) > _MAX_QA_IMAGE_BYTES:
        out['skipped'] = True
        out['reason'] = 'frame grew beyond the visual QA byte limit'
        return out
    if not raw_image:
        out['skipped'] = True
        out['reason'] = 'frame image is empty'
        return out
    output_tokens = _qa_output_tokens(max_tokens)
    out['input_sha256'] = _qa_input_sha256(
        prompt, model, output_tokens, raw_image)
    data_uri = ('data:image/png;base64,'
                + base64.b64encode(raw_image).decode('ascii'))

    try:
        from lib.llm_dispatch.api import dispatch_chat
        from lib.llm_dispatch.provider_pin import provider_pin
        from lib.production.llm_policy import production_llm_dispatch_kwargs
        with provider_pin(provider_pin_id):
            content, _usage = dispatch_chat(
                [{'role': 'user', 'content': [
                    {'type': 'text', 'text': prompt},
                    {'type': 'image_url', 'image_url': {'url': data_uri}},
                ]}],
                max_tokens=output_tokens, temperature=0.1, prefer_model=model,
                strict_model=True, owner_user_id=owner_user_id,
                **production_llm_dispatch_kwargs(
                    abort_check=abort_check,
                    max_429_attempts=max_429_attempts),
                log_prefix=f'[VisualQA:{label}]')
    except Exception as e:
        if abort_check is not None and abort_check():
            out['skipped'] = True
            out['reason'] = 'aborted during visual QA'
            return out
        out['reason'] = f'VLM dispatch failed: {e}'
        logger.warning('[VisualQA] %s QA dispatch failed: %s', label, e)
        return out
    if abort_check is not None and abort_check():
        out['skipped'] = True
        out['reason'] = 'aborted after visual QA'
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


def load_visual_qa_cache(path: str, *, version: str, max_entries: int,
                         max_bytes: int) -> dict:
    """Load one bounded capability-owned cache of validated QA results."""
    entry_limit = max(1, int(max_entries))
    byte_limit = max(1, int(max_bytes))
    try:
        with open(path, 'rb') as source:
            data = source.read(byte_limit + 1)
        if len(data) > byte_limit:
            raise ValueError('visual QA cache exceeds byte limit')
        parsed = json.loads(data.decode('utf-8'))
    except FileNotFoundError:
        parsed = {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        logger.warning('[VisualQA] cache ignored at %s: %s', path, exc)
        parsed = {}
    if (not isinstance(parsed, dict)
            or parsed.get('version') != version
            or not isinstance(parsed.get('entries'), dict)):
        return {'version': version, 'entries': {}}
    entries = {}
    for key, row in parsed['entries'].items():
        if len(entries) >= entry_limit:
            break
        if isinstance(key, str) and isinstance(row, dict):
            entries[key] = row
    return {'version': version, 'entries': entries}


def _visual_qa_result_payload(result: dict, *, max_findings: int) -> dict | None:
    if not isinstance(result, dict) or result.get('ok') is not True:
        return None
    valid_checks = {check for check, _text in QA_CHECKLIST}
    findings = normalise_findings(
        result.get('findings'), valid_checks=valid_checks
    )[:max(1, int(max_findings))]
    return {
        'ok': True,
        'skipped': False,
        'reason': '',
        'findings': findings,
        'has_blocker': any(item.get('severity') == 'blocker'
                           for item in findings),
        'summary': f'{len(findings)} finding(s)',
    }


def _visual_qa_result_sha256(result: dict) -> str:
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def cached_visual_qa_result(row: dict | None, input_sha256: str, *,
                            max_findings: int = 64) -> dict | None:
    """Return a hash-verified successful result for one exact VLM input."""
    if (not isinstance(row, dict)
            or row.get('input_sha256') != input_sha256
            or not re.fullmatch(r'[0-9a-f]{64}',
                                str(row.get('result_sha256') or ''))):
        return None
    result = _visual_qa_result_payload(
        row.get('result'), max_findings=max_findings)
    if (result is None
            or _visual_qa_result_sha256(result) != row['result_sha256']):
        return None
    return {**result, 'reused': True, 'input_sha256': input_sha256}


def remember_visual_qa_result(cache: dict, path: str, key: str,
                              input_sha256: str, result: dict, *,
                              max_entries: int,
                              max_bytes: int,
                              max_findings: int = 64) -> bool:
    """Atomically retain one successful exact-input result under a hard cap."""
    payload = _visual_qa_result_payload(
        result, max_findings=max_findings)
    if payload is None or result.get('input_sha256') != input_sha256:
        return False
    entries = cache.get('entries')
    if not isinstance(entries, dict):
        entries = {}
        cache['entries'] = entries
    entry_limit = max(1, int(max_entries))
    while key not in entries and len(entries) >= entry_limit:
        entries.pop(next(iter(entries)))
    entries[key] = {
        'input_sha256': input_sha256,
        'result': payload,
        'result_sha256': _visual_qa_result_sha256(payload),
    }
    byte_limit = max(1, int(max_bytes))

    def _encoded() -> str:
        return json.dumps(cache, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'))

    encoded = _encoded()
    while len(encoded.encode('utf-8')) > byte_limit:
        victim = next((entry_key for entry_key in entries if entry_key != key),
                      None)
        if victim is None:
            entries.pop(key, None)
            logger.warning('[VisualQA] one cache result exceeds %d bytes; '
                           'not retained at %s', byte_limit, path)
            return False
        entries.pop(victim, None)
        encoded = _encoded()
    from lib.json_store import write_text_atomic
    try:
        write_text_atomic(path, encoded)
    except OSError as exc:
        logger.warning('[VisualQA] cache write failed at %s: %s', path, exc)
        return False
    return True


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
