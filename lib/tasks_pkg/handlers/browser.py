# HOT_PATH
"""Browser tool handler and badge dispatch registry."""

from __future__ import annotations

import json
import re

from lib.browser.advanced import ADVANCED_BROWSER_TOOL_NAMES
from lib.log import get_logger
from lib.tasks_pkg.executor import _finalize_tool_round, tool_registry
from lib.tasks_pkg.manager import append_event
from lib.tools.browser import BROWSER_TOOL_NAMES, PAGE_PREVIEW_TOOL_NAMES
from lib.tools.image_gen import IMAGE_GEN_TOOL_NAMES
from lib.tools.result_envelope import tool_result_error

from lib.tools.result_envelope import ToolResultEnvelopeV2

logger = get_logger(__name__)

# One bounded reconnect window before enqueue, shared by roots and children.
# It covers the bridge's ordinary long poll plus a transport retry; it never
# retries an already-issued page action.
BROWSER_CONNECTION_WAIT_SECONDS = 15.0


# ── Browser tool badge registry ─────────────────────────────────────────

def _badge_list_tabs(meta, fn_name, display_text, chars, is_screenshot):
    if tool_result_error(display_text) is not None:
        meta['badge'] = 'error'
        return
    m = re.search(r'\((\d+) total\)', display_text[:200])
    meta['badge'] = f'{m.group(1)} tabs' if m else 'tabs'
    meta['snippet'] = display_text[:150].replace('\n', ' ')

def _badge_read_tab(meta, fn_name, display_text, chars, is_screenshot):
    meta['badge'] = (
        'error' if tool_result_error(display_text) is not None
        else f'{chars:,} chars')

def _badge_ok_or_error(label_ok, label_fail='error'):
    """Factory for simple ok/fail badge handlers.

    Badges are plain text (no emoji) per CLAUDE.md §3.4 — the frontend
    renders the per-tool SVG icon and colors the badge by ok/fail class.
    """
    def _handler(meta, fn_name, display_text, chars, is_screenshot):
        if tool_result_error(display_text) is not None:
            meta['badge'] = label_fail
            return
        # Failure envelopes seen across the browser handlers:
        #   'Error …'                       (bridge/validation errors)
        #   '<fn_name> failed: …'           (advanced tools, dispatch.py)
        #   '<fn_name> error: …'            (advanced tools, dispatch.py)
        #   'Click failed: …' / 'No clear match …' / 'Timeout: …' (v2 actions)
        # Success envelopes ('Clicked …', 'Typed …', '… succeeded (N steps)',
        # arbitrary execute_js JSON) never START with those words, so the
        # anchor on the head keeps arbitrary result text from false-failing.
        head = display_text[:80]
        ok = not re.match(
            r'(?i)\s*(error\b|no clear match\b|timeout\b|[\w .]*\bfailed\b|[\w .]*\berror\b)',
            head)
        meta['badge'] = label_ok if ok else label_fail
    return _handler

def _badge_screenshot(meta, fn_name, display_text, chars, is_screenshot):
    meta['badge'] = 'captured' if is_screenshot else 'failed'

def _badge_devtools(meta, fn_name, display_text, chars, is_screenshot):
    if (tool_result_error(display_text) is not None
            or re.match(r'(?i)\s*error\b', display_text[:80])):
        meta['badge'] = 'error'
        return
    match = re.match(r'DevTools ([a-z_]+)', display_text[:100])
    meta['badge'] = match.group(1).replace('_', ' ') if match else 'devtools'

def _badge_regex_count(pattern, unit, fallback='done'):
    """Factory for badges that extract a count via regex."""
    def _handler(meta, fn_name, display_text, chars, is_screenshot):
        if tool_result_error(display_text) is not None:
            meta['badge'] = 'error'
            return
        m = re.search(pattern, display_text[:200])
        meta['badge'] = f'{m.group(1)} {unit}'.strip() if m else fallback
    return _handler

_BROWSER_BADGE_DISPATCH = {

    'browser_site':                     _badge_ok_or_error('site', 'error'),
    'browser_list_tabs':                _badge_list_tabs,
    'browser_read_page':                _badge_read_tab,
    'browser_research_page':            _badge_read_tab,
    'browser_devtools':                 _badge_devtools,
    'browser_execute_js':               _badge_ok_or_error('ok', 'error'),
    'browser_screenshot':               _badge_screenshot,
    'browser_click':                    _badge_ok_or_error('clicked', 'failed'),
    'browser_scroll':                   _badge_ok_or_error('scrolled', 'error'),
    'browser_type':                     _badge_ok_or_error('typed', 'failed'),
    'browser_press_key':                _badge_ok_or_error('sent', 'failed'),
    'browser_check':                    _badge_ok_or_error('set', 'failed'),
    'browser_select_option':            _badge_ok_or_error('selected', 'failed'),
    'browser_menu_click':               _badge_ok_or_error('clicked', 'failed'),
    'browser_fill_form':                _badge_ok_or_error('filled', 'failed'),
    'browser_get_cookies':              _badge_regex_count(r'(\d+) cookies?', 'cookies'),
    'browser_get_history':              _badge_regex_count(r'(\d+) results?', 'results'),
    'browser_close_tab':                _badge_ok_or_error('closed', 'failed'),
    'browser_navigate':                 _badge_ok_or_error('done', 'failed'),
    'browser_preview_page':             _badge_screenshot,
}


@tool_registry.tool_set(BROWSER_TOOL_NAMES | ADVANCED_BROWSER_TOOL_NAMES | PAGE_PREVIEW_TOOL_NAMES, category='browser',
                        description='Execute a browser automation tool')
def _handle_browser_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    from lib.browser.dispatch import execute_browser_tool
    browser_client_id = cfg.get('browserClientId') or None

    display_metadata = {} if fn_name == 'browser_execute_js' else None
    if fn_name in PAGE_PREVIEW_TOOL_NAMES or fn_name == 'browser_fill_form':
        # browser_preview_page renders server-side and resolves project-
        # relative HTML paths; browser_fill_form's type='file' fields resolve
        # project-relative upload paths through the same workspace seam.
        # Handlers on the BROWSER_HANDLERS table receive fn_args only, so the
        # workspace root travels as an internal (underscore-prefixed) arg.
        fn_args = dict(fn_args or {})
        fn_args['_projectPath'] = project_path
    tool_content = execute_browser_tool(
        fn_name, fn_args, client_id=browser_client_id,
        owner_user_id=str(task.get('_userId') or ''),
        work_key=str(cfg.get('browserWorkKey') or ''),
        connection_wait=BROWSER_CONNECTION_WAIT_SECONDS,
        cancellation=task.get('abort_event'),
        **({'display_metadata': display_metadata} if display_metadata is not None else {}))
    typed_error = tool_result_error(tool_content)

    is_screenshot = isinstance(tool_content, dict) and tool_content.get('__screenshot__')
    if is_screenshot:
        display_text = (f'Page preview rendered ({tool_content.get("format", "jpeg")})'
                        if fn_name in PAGE_PREVIEW_TOOL_NAMES
                        else f'Screenshot captured ({tool_content.get("format", "png")})')
    elif isinstance(tool_content, ToolResultEnvelopeV2):
        display_text = tool_content.to_model_text()
    else:
        display_text = tool_content if isinstance(tool_content, str) else json.dumps(tool_content, ensure_ascii=False)

    # Text-bearing read_page modes receive the LLM content filter. Structured
    # elements/app_state responses must remain machine-readable.
    is_read_tab = (fn_name == 'browser_read_page'
                   and (fn_args or {}).get('mode', 'auto') in ('auto', 'text')
                   and isinstance(tool_content, str)
                   and typed_error is None
                   and not tool_content.startswith('Error'))
    if is_read_tab and not display_text.startswith('Browser content:') and len(display_text) > 1500:
        tab_url = ''
        for line in display_text.split('\n', 5):
            if line.startswith('URL: '):
                tab_url = line[5:].strip()
                break
        user_question = task.get('lastUserQuery', '')
        try:
            from lib.search.search_runtime import ensure_search_runtime
            ensure_search_runtime()
            from tofu_search.fetch.content_filter import IRRELEVANT_SENTINEL, filter_web_content
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
    if display_metadata and display_metadata.get('tabTitle'):
        meta['tabTitle'] = display_metadata['tabTitle']
    # ── Screenshot: render the captured image inline in the timeline ──
    # The extension returns a full ``data:<mime>;base64,...`` URL; expose it
    # via ``imageDataUris`` (the same field read_files/inspect_image use) so
    # the frontend draws an <img> instead of a useless "N chars" row.
    if is_screenshot and tool_content.get('dataUrl'):
        fmt = tool_content.get('format', 'png')
        meta['imageDataUris'] = [{
            'uri': tool_content['dataUrl'],
            'format': fmt,
            'filename': f'screenshot.{fmt}',
        }]
    badge_fn = _BROWSER_BADGE_DISPATCH.get(fn_name)
    if badge_fn is not None:
        badge_fn(meta, fn_name, display_text, chars, is_screenshot)
    _finalize_tool_round(
        task, rn, round_entry, [meta],
        status='error' if typed_error is not None else 'done')
    return tc_id, tool_content, is_read_tab


# ═══ Image generation handler (extracted to executor_image.py) ═══════

from lib.tasks_pkg.executor_image import register_image_gen_handler as _reg_image

_reg_image(tool_registry, IMAGE_GEN_TOOL_NAMES, _finalize_tool_round, append_event)
del _reg_image
