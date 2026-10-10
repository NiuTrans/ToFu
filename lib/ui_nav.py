"""UI-jump directory: render contracts/ui_nav_targets_v1.json for the model.

Single responsibility: load the generated contract (the frontend registries
frontend/src/features/settings/setting-jump.ts and features/ui-nav.ts are the
authority; scripts/gen_ui_nav_targets.mjs emits the contract) and render a
compact, token-bounded system-context block telling the model which
``setting:<key>`` / ``page:<key>`` link targets exist. The assistant writes
ordinary markdown links with those keys; the browser renders them as buttons
that open the settings modal at the exact control or launch a sub-page.

No state, no mutation, stdlib only (safe for cross-process contract modules).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from lib.log import get_logger

logger = get_logger(__name__)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1] / 'contracts' / 'ui_nav_targets_v1.json'
)
_DESC_MAX_CHARS = 60


@lru_cache(maxsize=1)
def _contract() -> dict:
    return json.loads(_CONTRACT_PATH.read_text(encoding='utf-8'))


def _name(target: dict) -> str:
    name = target.get('name') or {}
    return str(name.get('zh') or name.get('en') or '').strip()


def build_ui_nav_directory() -> str:
    """Render the directory block; '' when the contract is missing/invalid.

    Kept deliberately compact (the composer budget is 1,400 tokens): tab
    targets on one wrapped line, control/page targets one line each with
    descriptions truncated. Keys are the only load-bearing tokens — names
    and descriptions just help the model pick the right key.
    """
    try:
        doc = _contract()
    except Exception as exc:  # missing/corrupt contract: degrade to no block
        logger.debug('[UiNav] contract unavailable, skipping nav block: %s', exc)
        return ''
    targets = doc.get('targets')
    if not isinstance(targets, list) or not targets:
        return ''

    tabs: list[str] = []
    controls: list[str] = []
    pages: list[str] = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        key = str(target.get('key') or '').strip()
        name = _name(target)
        if not key or not name:
            continue
        scheme = target.get('scheme')
        if scheme == 'page':
            line = f'- `{key}` {name}'
            flag = str(target.get('requiresFlag') or '').strip()
            if flag:
                line += f'（需先开启功能开关 {flag}）'
            pages.append(line)
        elif scheme == 'setting' and target.get('tabLevel'):
            tabs.append(f'`{key}` {name}')
        elif scheme == 'setting':
            desc = str((target.get('description') or {}).get('zh') or '').strip()
            if len(desc) > _DESC_MAX_CHARS:
                desc = desc[: _DESC_MAX_CHARS - 1] + '…'
            line = f'- `{key}` {name}'
            if desc:
                line += f' — {desc}'
            controls.append(line)

    if not (tabs or controls or pages):
        return ''
    sections = [
        '## 界面导航目录',
        '',
        '当用户询问某个功能在哪里设置、或想打开某个页面时，在回答中附上跳转链接'
        '（前端会渲染成可点击按钮，点击直达对应界面并高亮目标）：',
        '- 设置页签/设置项：`[按钮文字](setting:<key>)`',
        '- 子页面：`[按钮文字](page:<key>)`',
        '只能使用下面列出的 key，不要编造；一次回复最多给出一两个最相关的链接。',
    ]
    if tabs:
        sections.append('')
        sections.append('设置页签（setting:）：' + ' · '.join(tabs))
    if controls:
        sections.append('')
        sections.append('设置项（setting:）：')
        sections.extend(controls)
    if pages:
        sections.append('')
        sections.append('子页面（page:）：')
        sections.extend(pages)
    return '\n'.join(sections)


__all__ = ['build_ui_nav_directory']
