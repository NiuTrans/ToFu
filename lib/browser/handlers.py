"""lib/browser/handlers.py — Tool handler functions for each browser tool.

Each handler takes fn_args (dict) and returns a string result for the LLM.
They communicate with the browser extension via send_browser_command().
"""

import json

from lib.browser.display import update_tab_title
from lib.browser.queue import send_browser_command
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    '_handle_list_tabs', '_handle_read_tab', '_handle_execute_js',
    '_handle_screenshot', '_handle_get_cookies', '_handle_get_history',
    '_handle_create_tab', '_handle_close_tab', '_handle_navigate',
    '_handle_get_interactive_elements', '_handle_click', '_handle_keyboard',
    '_handle_hover', '_handle_wait', '_handle_summarize_page',
    '_handle_get_app_state',
]


def _handle_list_tabs(fn_args):
    result, error = send_browser_command('list_tabs', timeout=15)
    if error:
        return f'❌ Error listing tabs: {error}'
    if isinstance(result, list):
        lines = [f'Open tabs ({len(result)} total):\n']
        for t in result:
            active_mark = ' ★ (active)' if t.get('active') else ''
            url = t.get('url', '')
            title = t.get('title', '(no title)')
            # Cache tab ID → title for display strings
            update_tab_title(t.get('id'), title)
            lines.append(f'  Tab {t["id"]}: {title}{active_mark}')
            lines.append(f'    URL: {url}')
        return '\n'.join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_read_tab(fn_args):
    tab_id = fn_args.get('tabId')
    if tab_id is None:
        return '❌ Error: tabId is required. Use browser_list_tabs first to get tab IDs.'
    result, error = send_browser_command('read_tab', {
        'tabId': int(tab_id),
        'selector': fn_args.get('selector'),
        'maxChars': fn_args.get('maxChars', 50000),
    }, timeout=30)
    if error:
        return f'❌ Error reading tab {tab_id}: {error}'
    if isinstance(result, dict):
        if result.get('error'):
            return f'❌ {result["error"]}'
        title = result.get('title', '')
        url = result.get('url', '')
        # Cache tab ID → title for display strings
        if title:
            update_tab_title(tab_id, title)
        if result.get('elements'):
            elements = result['elements']
            lines = [f'Tab: {title}', f'URL: {url}',
                     f'Found {result.get("count", len(elements))} element(s):\n']
            for i, el in enumerate(elements):
                text = el.get('text', '').strip()
                if text:
                    lines.append(f'[{i+1}] <{el.get("tag", "?")}> {text[:2000]}')
            return '\n'.join(lines)
        else:
            # ── Prefer server-side extraction from HTML (same pipeline as fetch) ──
            raw_html = result.get('html', '')
            text = None
            extract_method = 'innerText'
            if raw_html and len(raw_html) > 200:
                try:
                    from lib.fetch.html_extract import extract_html_text
                    text = extract_html_text(raw_html, 80000, url=url)
                    if text and len(text) > 50:
                        extract_method = 'html→extract'
                    else:
                        text = None
                except Exception as e:
                    logger.warning('read_tab HTML extraction failed, falling back to innerText: %s', e)
            if not text:
                text = result.get('text', '')
            truncated = result.get('truncated', False)
            header = f'Tab: {title}\nURL: {url}\nContent ({len(text):,} chars, {extract_method}'
            if truncated and extract_method == 'innerText':
                header += f', truncated from {result.get("textLength", "?"):,}'
            header += '):\n\n'
            return header + text
    return str(result)


def _handle_execute_js(fn_args):
    tab_id = fn_args.get('tabId')
    code = fn_args.get('code', '')
    if tab_id is None:
        return '❌ Error: tabId is required.'
    if not code:
        return '❌ Error: code is required.'
    result, error = send_browser_command('execute_js', {
        'tabId': int(tab_id),
        'code': code,
    }, timeout=30)
    if error:
        return f'❌ Error executing JS in tab {tab_id}: {error}'
    if result is None:
        return '✅ Executed successfully (no return value)'
    if isinstance(result, dict) and result.get('__error'):
        return f'❌ JS Error: {result.get("message", "unknown error")}'
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False, indent=2)
    return str(result)


def _handle_screenshot(fn_args):
    params = {}
    if fn_args.get('tabId') is not None:
        params['tabId'] = int(fn_args['tabId'])
    if fn_args.get('format'):
        params['format'] = fn_args['format']
    result, error = send_browser_command('screenshot_tab', params, timeout=15)
    if error:
        return f'❌ Error taking screenshot: {error}'
    if isinstance(result, dict) and result.get('dataUrl'):
        data_url = result['dataUrl']
        fmt = result.get('format', 'png')

        original_size = len(data_url)

        # Apply compression for large images
        compressed_url = data_url
        compression_applied = False
        max_size = 500 * 1024  # 500KB threshold

        if original_size > max_size:
            try:
                import base64
                import io

                from PIL import Image

                # Decode base64
                b64_data = data_url.split(',', 1)[1] if ',' in data_url else data_url
                img_data = base64.b64decode(b64_data)
                img = Image.open(io.BytesIO(img_data))

                # Resize if too tall (max 3000px height)
                width, height = img.size
                max_height = 3000
                if height > max_height:
                    scale = max_height / height
                    width = int(width * scale)
                    height = max_height
                    img = img.resize((width, height), Image.LANCZOS)
                    compression_applied = True

                # Convert to JPEG for smaller size (quality=70)
                output = io.BytesIO()
                img = img.convert('RGB')  # Remove alpha for JPEG
                img.save(output, format='JPEG', quality=70, optimize=True)
                output.seek(0)

                compressed_b64 = base64.b64encode(output.read()).decode('ascii')
                compressed_url = f'data:image/jpeg;base64,{compressed_b64}'
                fmt = 'jpeg'
                compression_applied = True

            except Exception as e:
                # Fall back to original if compression fails
                logger.warning("Screenshot compression failed, using original: %s", e, exc_info=True)

        # Return structured result with metadata
        return {
            '__screenshot__': True,
            'dataUrl': compressed_url,
            'format': fmt,
            'originalSize': original_size,
            'compressedSize': len(compressed_url),
            'compressionApplied': compression_applied,
        }
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_get_cookies(fn_args):
    params = {}
    if fn_args.get('url'): params['url'] = fn_args['url']
    if fn_args.get('domain'): params['domain'] = fn_args['domain']
    if fn_args.get('name'): params['name'] = fn_args['name']
    result, error = send_browser_command('get_cookies', params, timeout=10)
    if error:
        return f'❌ Error getting cookies: {error}'
    if isinstance(result, list):
        lines = [f'🍪 Cookies ({len(result)} found):\n']
        for c in result:
            lines.append(f'  {c.get("name", "?")} = {str(c.get("value", ""))[:100]}')
            lines.append(f'    domain={c.get("domain", "")} path={c.get("path", "")} secure={c.get("secure", "")}')
        return '\n'.join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_get_history(fn_args):
    params = {
        'query': fn_args.get('query', ''),
        'maxResults': fn_args.get('maxResults', 100),
    }
    result, error = send_browser_command('get_history', params, timeout=10)
    if error:
        return f'❌ Error getting history: {error}'
    if isinstance(result, list):
        lines = [f'📜 History ({len(result)} entries):\n']
        for h in result:
            lines.append(f'  {h.get("title", "(no title)")}')
            lines.append(f'    URL: {h.get("url", "")}')
            lines.append(f'    Visits: {h.get("visitCount", 0)}, Last: {h.get("lastVisitTime", "")}')
        return '\n'.join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_create_tab(fn_args):
    url = fn_args.get('url', 'about:blank')
    params = {'url': url}
    if fn_args.get('active') is not None:
        params['active'] = fn_args['active']
    result, error = send_browser_command('create_tab', params, timeout=10)
    if error:
        return f'❌ Error creating tab: {error}'
    if isinstance(result, dict):
        return f'✅ Created new tab #{result.get("id", "?")} → {url}'
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_close_tab(fn_args):
    params = {}
    if fn_args.get('tabId') is not None:
        params['tabId'] = int(fn_args['tabId'])
    if fn_args.get('tabIds'):
        params['tabIds'] = [int(t) for t in fn_args['tabIds']]
    result, error = send_browser_command('close_tab', params, timeout=10)
    if error:
        return f'❌ Error closing tab(s): {error}'
    if isinstance(result, dict) and result.get('closed'):
        return f'✅ Closed tab(s): {result["closed"]}'
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_navigate(fn_args):
    tab_id = fn_args.get('tabId')
    url = fn_args.get('url')
    if tab_id is None:
        return '❌ Error: tabId is required.'
    if not url:
        return '❌ Error: url is required.'
    params = {
        'tabId': int(tab_id),
        'url': url,
        'waitForLoad': fn_args.get('waitForLoad', False),
    }
    result, error = send_browser_command('navigate', params, timeout=35)
    if error:
        return f'❌ Error navigating tab {tab_id}: {error}'
    if isinstance(result, dict):
        # Cache tab title from navigation result
        nav_title = result.get('title', '')
        if nav_title:
            update_tab_title(result.get('id', tab_id), nav_title)
        return f'✅ Navigated tab #{result.get("id", tab_id)} → {result.get("url", url)} (status: {result.get("status", "?")})'
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_get_interactive_elements(fn_args):
    tab_id = fn_args.get('tabId')
    if tab_id is None:
        return '❌ Error: tabId is required. Use browser_list_tabs first.'
    params = {
        'tabId': int(tab_id),
        'maxElements': fn_args.get('maxElements', 200),
        'viewport': fn_args.get('viewport', False),
    }
    result, error = send_browser_command('get_interactive_elements', params, timeout=15)
    if error:
        return f'❌ Error getting elements from tab {tab_id}: {error}'
    if isinstance(result, dict):
        elements = result.get('elements', [])
        title = result.get('title', '')
        url = result.get('url', '')
        # Cache tab ID → title for display strings
        if title:
            update_tab_title(tab_id, title)
        total = result.get('total', len(elements))
        lines = [f'Tab: {title}', f'URL: {url}',
                 f'Interactive elements ({len(elements)} shown, {total} total):\n']
        for i, el in enumerate(elements):
            tag = el.get('tag', '?')
            text = el.get('text', '')
            selector = el.get('selector', '')
            role = el.get('role', '')
            extra_parts = []
            if role: extra_parts.append(f'role={role}')
            if el.get('href'): extra_parts.append(f'href={el["href"][:80]}')
            if el.get('type'): extra_parts.append(f'type={el["type"]}')
            if el.get('ariaLabel'): extra_parts.append(f'aria-label="{el["ariaLabel"]}"')
            if el.get('title'): extra_parts.append(f'title="{el["title"]}"')
            if el.get('placeholder'): extra_parts.append(f'placeholder="{el["placeholder"]}"')
            if el.get('disabled'): extra_parts.append('DISABLED')
            extra = f' ({", ".join(extra_parts)})' if extra_parts else ''
            display_text = f' "{text[:60]}"' if text else ''
            lines.append(f'  [{i+1}] <{tag}>{display_text}{extra}')
            lines.append(f'       selector: {selector}')
        return '\n'.join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_click(fn_args):
    tab_id = fn_args.get('tabId')
    selector = fn_args.get('selector', '')
    if tab_id is None:
        return '❌ Error: tabId is required.'
    if not selector:
        return '❌ Error: selector is required. Use browser_get_interactive_elements to discover selectors.'
    params = {
        'tabId': int(tab_id),
        'selector': selector,
        'rightClick': fn_args.get('rightClick', False),
        'scrollTo': fn_args.get('scrollTo', True),
    }
    result, error = send_browser_command('click_element', params, timeout=15)
    if error:
        return f'❌ Error clicking element in tab {tab_id}: {error}'
    if isinstance(result, dict):
        if not result.get('clicked'):
            return f'❌ Click failed: {result.get("error", "unknown error")}'
        click_type = 'Right-clicked' if result.get('rightClick') else 'Clicked'
        tag = result.get('tag', '?')
        text = result.get('text', '')
        text_display = f' "{text[:60]}"' if text else ''
        return f'✅ {click_type} <{tag}>{text_display} (selector: {selector})'
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_keyboard(fn_args):
    tab_id = fn_args.get('tabId')
    keys = fn_args.get('keys', '')
    if tab_id is None:
        return '❌ Error: tabId is required.'
    if not keys:
        return '❌ Error: keys is required.'
    params = {
        'tabId': int(tab_id),
        'keys': keys,
    }
    if fn_args.get('selector'):
        params['selector'] = fn_args['selector']
    result, error = send_browser_command('keyboard_input', params, timeout=10)
    if error:
        return f'❌ Error sending keyboard input in tab {tab_id}: {error}'
    if isinstance(result, dict):
        if result.get('success'):
            target = result.get('target', '')
            target_display = f' on <{target}>' if target else ''
            return f'✅ Sent keys "{keys}"{target_display}'
        return f'❌ Keyboard input failed: {result.get("error", "unknown error")}'
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_hover(fn_args):
    tab_id = fn_args.get('tabId')
    selector = fn_args.get('selector', '')
    if tab_id is None:
        return '❌ Error: tabId is required.'
    if not selector:
        return '❌ Error: selector is required.'
    params = {
        'tabId': int(tab_id),
        'selector': selector,
    }
    result, error = send_browser_command('hover_element', params, timeout=10)
    if error:
        return f'❌ Error hovering element in tab {tab_id}: {error}'
    if isinstance(result, dict):
        if result.get('hovered') or result.get('success'):
            tag = result.get('tag', '?')
            text = result.get('text', '')
            text_display = f' "{text[:60]}"' if text else ''
            return f'✅ Hovered <{tag}>{text_display} (selector: {selector})'
        return f'❌ Hover failed: {result.get("error", "unknown error")}'
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_wait(fn_args):
    tab_id = fn_args.get('tabId')
    if tab_id is None:
        return '❌ Error: tabId is required.'
    params = {'tabId': int(tab_id)}
    selector = fn_args.get('selector')
    wait_time = fn_args.get('time')
    if selector:
        params['selector'] = selector
        params['condition'] = fn_args.get('condition', 'present')
        params['timeout'] = fn_args.get('timeout', 5000)
        result, error = send_browser_command('wait_for_element', params, timeout=max(15, (params['timeout'] / 1000) + 5))
    elif wait_time:
        # Simple time-based wait: just sleep on server side
        import time
        wait_seconds = min(float(wait_time), 30)  # Cap at 30 seconds
        time.sleep(wait_seconds)
        return f'✅ Waited {wait_seconds}s'
    else:
        return '❌ Error: either "selector" or "time" parameter is required.'
    if error:
        return f'❌ Error waiting for element in tab {tab_id}: {error}'
    if isinstance(result, dict):
        if result.get('found') or result.get('success'):
            return f'✅ Element found: {selector} (condition: {params.get("condition", "present")})'
        return f'⏰ Timeout: element "{selector}" not found within {params["timeout"]}ms'
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_summarize_page(fn_args):
    tab_id = fn_args.get('tabId')
    if tab_id is None:
        return '❌ Error: tabId is required.'
    result, error = send_browser_command('summarize_page', {'tabId': int(tab_id)}, timeout=15)
    if error:
        return f'❌ Error summarizing page: {error}'
    if isinstance(result, dict):
        sum_title = result.get('title', 'Untitled')
        if sum_title and sum_title != 'Untitled':
            update_tab_title(tab_id, sum_title)
        lines = [f"📄 Page Summary: {sum_title}"]
        lines.append(f"   URL: {result.get('url', '')}")
        lines.append(f"   Framework: {result.get('framework', 'Unknown')}")
        lines.append(f"   Canvas: {result.get('canvasCount', 0)}, SVG: {result.get('svgCount', 0)}, DOM elements: {result.get('domElementCount', 0):,}")

        buttons = result.get('mainButtons', [])
        if buttons:
            lines.append(f"\n   🔘 Buttons ({len(buttons)}):")
            for b in buttons[:10]:
                lines.append(f"      • {b.get('text', '(no text)')} → {b.get('selector', '')}")

        links = result.get('mainLinks', [])
        if links:
            lines.append(f"\n   🔗 Links ({len(links)}):")
            for lnk in links[:10]:
                lines.append(f"      • {lnk.get('text', '(no text)')} → {lnk.get('href', '')[:80]}")

        forms = result.get('forms', [])
        if forms:
            lines.append(f"\n   📝 Forms ({len(forms)}):")
            for frm in forms:
                lines.append(f"      • {frm.get('method', 'GET').upper()} {frm.get('action', '')} ({frm.get('inputCount', 0)} inputs)")

        tables = result.get('tables', [])
        if tables:
            lines.append(f"\n   📊 Tables ({len(tables)}):")
            for tbl in tables:
                lines.append(f"      • {tbl.get('rows', 0)} rows × {tbl.get('cols', 0)} cols")

        if result.get('hasModal'):
            lines.append("\n   ⚠️ Modal/Dialog detected on page")

        if result.get('canvasCount', 0) > 0:
            lines.append("\n   💡 TIP: This page uses Canvas rendering. For interaction, use browser_screenshot to see the layout, then browser_execute_js to access app data or simulate clicks.")

        return '\n'.join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_get_app_state(fn_args):
    tab_id = fn_args.get('tabId')
    if tab_id is None:
        return '❌ Error: tabId is required.'
    params = {'tabId': int(tab_id)}
    if fn_args.get('depth'):
        params['depth'] = fn_args['depth']
    result, error = send_browser_command('get_app_state', params, timeout=20)
    if error:
        return f'❌ Error getting app state: {error}'
    if isinstance(result, dict):
        lines = [f"🔧 App State (Framework: {result.get('framework', 'Unknown')})"]

        if result.get('vueInstance'):
            vue = result['vueInstance']
            lines.append("\n   Vue detected:")
            lines.append(f"      Router: {'Yes' if vue.get('hasRouter') else 'No'}")
            lines.append(f"      Store: {'Yes' if vue.get('hasStore') else 'No'}")
            comp_tree = vue.get('componentTree', [])
            if comp_tree:
                lines.append("      Component tree:")
                for c in comp_tree[:10]:
                    lines.append(f"         - {c.get('name', 'Anonymous')} {'(has children)' if c.get('hasChildren') else ''}")

        if result.get('chartLib'):
            lines.append(f"\n   📊 Chart Library: {result['chartLib']}")
            chart_data = result.get('chartData')
            if chart_data:
                if chart_data.get('nodes'):
                    lines.append(f"      Nodes: {len(chart_data['nodes'])}")
                    for n in chart_data['nodes'][:5]:
                        lines.append(f"         • {n.get('id', '?')}: {n.get('label', '')}")
                if chart_data.get('edges'):
                    lines.append(f"      Edges: {len(chart_data['edges'])}")

        global_vars = result.get('globalVars', {})
        if global_vars:
            lines.append(f"\n   🌍 Global variables found: {', '.join(global_vars.keys())}")
            for k, v in list(global_vars.items())[:5]:
                v_display = json.dumps(v, ensure_ascii=False)[:200] if isinstance(v, (dict, list)) else str(v)[:200]
                lines.append(f"      {k} = {v_display}")

        if result.get('vueError'):
            lines.append(f"\n   ⚠️ Vue extraction error: {result['vueError']}")
        if result.get('chartError'):
            lines.append(f"\n   ⚠️ Chart extraction error: {result['chartError']}")

        return '\n'.join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)
