# HOT_PATH
"""Browser tool handler and badge dispatch registry."""

from __future__ import annotations

import json
import re

from lib.browser.advanced import ADVANCED_BROWSER_TOOL_NAMES
from lib.log import get_logger
from lib.tasks_pkg.executor import _finalize_tool_round, tool_registry
from lib.tasks_pkg.manager import append_event
from lib.tools import BROWSER_TOOL_NAMES, IMAGE_GEN_TOOL_NAMES

logger = get_logger(__name__)


# ── Browser tool badge registry ─────────────────────────────────────────

def _badge_list_tabs(meta, fn_name, display_text, chars, is_screenshot):
    m = re.search(r'\((\d+) total\)', display_text[:200])
    meta['badge'] = f'{m.group(1)} tabs' if m else 'tabs'
    meta['snippet'] = display_text[:150].replace('\n', ' ')

def _badge_read_tab(meta, fn_name, display_text, chars, is_screenshot):
    meta['badge'] = f'{chars:,} chars'

def _badge_ok_or_error(icon_ok, icon_fail='❌ error'):
    """Factory for simple ok/fail badge handlers."""
    def _handler(meta, fn_name, display_text, chars, is_screenshot):
        ok = not display_text.startswith('❌')
        meta['badge'] = icon_ok if ok else icon_fail
    return _handler

def _badge_screenshot(meta, fn_name, display_text, chars, is_screenshot):
    meta['badge'] = '📸 captured' if is_screenshot else '❌ failed'

def _badge_regex_count(pattern, icon, unit, fallback='done'):
    """Factory for badges that extract a count via regex."""
    def _handler(meta, fn_name, display_text, chars, is_screenshot):
        m = re.search(pattern, display_text[:200])
        meta['badge'] = f'{icon} {m.group(1)} {unit}' if m else f'{icon} {fallback}' if icon else fallback
    return _handler

_BROWSER_BADGE_DISPATCH = {
    'browser_list_tabs':                _badge_list_tabs,
    'browser_read_tab':                 _badge_read_tab,
    'browser_execute_js':               _badge_ok_or_error('✅ ok', '❌ error'),
    'browser_screenshot':               _badge_screenshot,
    'browser_get_interactive_elements': _badge_regex_count(r'(\d+) shown', '🔍', 'elements'),
    'browser_click':                    _badge_ok_or_error('🖱️ clicked', '❌ failed'),
    'browser_get_cookies':              _badge_regex_count(r'(\d+) cookies?', '🍪', ''),
    'browser_get_history':              _badge_regex_count(r'(\d+) results?', '', 'results', fallback='done'),
    'browser_create_tab':               _badge_ok_or_error('➕ opened', '❌ failed'),
    'browser_close_tab':                _badge_ok_or_error('✖ closed', '❌ failed'),
    'browser_navigate':                 _badge_ok_or_error('🔗 done', '❌ failed'),
}


@tool_registry.tool_set(BROWSER_TOOL_NAMES | ADVANCED_BROWSER_TOOL_NAMES, category='browser',
                        description='Execute a browser automation tool')
def _handle_browser_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    from lib.browser import execute_browser_tool
    browser_client_id = cfg.get('browserClientId') or None
    tool_content = execute_browser_tool(fn_name, fn_args, client_id=browser_client_id)

    is_screenshot = isinstance(tool_content, dict) and tool_content.get('__screenshot__')
    if is_screenshot:
        display_text = f'📸 Screenshot captured ({tool_content.get("format", "png")})'
    else:
        display_text = tool_content if isinstance(tool_content, str) else json.dumps(tool_content, ensure_ascii=False)

    # browser_read_tab: apply LLM content filter
    is_read_tab = (fn_name == 'browser_read_tab'
                   and isinstance(tool_content, str)
                   and not tool_content.startswith('❌'))
    if is_read_tab and len(display_text) > 1500:
        tab_url = ''
        for line in display_text.split('\n', 5):
            if line.startswith('URL: '):
                tab_url = line[5:].strip()
                break
        user_question = task.get('lastUserQuery', '')
        try:
            from lib.fetch.content_filter import IRRELEVANT_SENTINEL, filter_web_content
            raw_chars = len(display_text)
            filtered = filter_web_content(
                display_text, url=tab_url,
                query='', user_question=user_question,
            )
            if filtered == IRRELEVANT_SENTINEL:
                logger.info('[Browser:read_tab] LLM deemed page irrelevant: %s', tab_url[:100])
            elif filtered and filtered != display_text:
                tool_content = filtered
                display_text = filtered
                logger.info('[Browser:read_tab] LLM-filtered %d → %d chars, url=%s',
                            raw_chars, len(filtered), tab_url[:80])
        except Exception as e:
            logger.warning('[Browser:read_tab] Content filter failed, using raw text: %s', e, exc_info=True)

    chars = len(display_text)
    meta = {
        'title': fn_name, 'source': 'Browser', 'fetched': True,
        'fetchedChars': chars, 'url': '',
        'snippet': display_text[:120].replace('\n', ' '),
        'badge': f'{chars} chars',
    }
    badge_fn = _BROWSER_BADGE_DISPATCH.get(fn_name)
    if badge_fn is not None:
        badge_fn(meta, fn_name, display_text, chars, is_screenshot)
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, is_read_tab


# ═══ Image generation handler (extracted to executor_image.py) ═══════

from lib.tasks_pkg.executor_image import register_image_gen_handler as _reg_image

_reg_image(tool_registry, IMAGE_GEN_TOOL_NAMES, _finalize_tool_round, append_event)
del _reg_image
