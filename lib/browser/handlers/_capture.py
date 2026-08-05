"""lib/browser/handlers/_capture.py — Data capture + scripting handlers.

Handlers for taking screenshots, executing JS, and reading cookies/history.
Each takes fn_args (dict) and returns a result for the LLM, communicating
with the browser extension via send_browser_command().
"""

import json

from lib.log import get_logger

logger = get_logger(__name__)


def _facade():
    """Return the package facade so collaborators resolve at call time."""
    import lib.browser.handlers as _pkg
    return _pkg


def send_browser_command(*args, **kwargs):
    """Facade-resolving proxy for lib.browser.queue.send_browser_command."""
    return _facade().send_browser_command(*args, **kwargs)


def _handle_execute_js(fn_args):
    code = fn_args.get('code', '')
    if not code:
        return 'Error: code is required.'
    # v2: tab_id optional — defaults to the working tab (pt_869e5648403e4745)
    from lib.browser._resolve import resolve_work_tab
    tab_id = resolve_work_tab(fn_args, send_browser_command)
    if tab_id is None:
        return ('Error: no tab to run JS in. Pass tab_id, or call '
                'browser_list_tabs / browser_navigate first.')
    result, error = send_browser_command('execute_js', {
        'tabId': int(tab_id),
        'code': code,
    }, timeout=30)
    if error:
        return f'Error executing JS in tab {tab_id}: {error}'
    if result is None:
        return 'Executed successfully (no return value)'
    if isinstance(result, dict) and result.get('__error'):
        return f'JS Error: {result.get("message", "unknown error")}'
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False, indent=2)
    return str(result)


def _handle_screenshot(fn_args):
    params = {}
    if fn_args.get('tabId') is not None:
        params['tabId'] = int(fn_args['tabId'])
    if fn_args.get('format'):
        params['format'] = fn_args['format']
    # fullPage defaults to True on the extension side; pass through only if
    # the caller explicitly opts out so older extensions keep working.
    if fn_args.get('fullPage') is False:
        params['fullPage'] = False
    # Full-page captures can take longer (lazy-load triggering + CDP attach).
    # The extension allows 55s for ANY screenshot_tab (COMMAND_TIMEOUT_OVERRIDES
    # in background.js), so the server-side wait must not give up first — a 15s
    # viewport budget made the server report a timeout while the extension was
    # still legitimately attaching CDP / walking its fallback chain. Keep both
    # values safely under the extension's 55s cap.
    full_page_requested = fn_args.get('fullPage', True) is not False
    timeout = 60 if full_page_requested else 30
    result, error = send_browser_command('screenshot_tab', params, timeout=timeout)
    if error:
        return f'Error taking screenshot: {error}'
    if isinstance(result, dict) and result.get('dataUrl'):
        data_url = result['dataUrl']
        fmt = result.get('format', 'png')
        is_full_page = bool(result.get('fullPage'))
        fallback_reason = result.get('fallbackReason')
        if fallback_reason:
            logger.warning('[Screenshot] full-page CDP capture failed, used viewport fallback: %s', fallback_reason)

        original_size = len(data_url)

        # Apply compression for large images
        compressed_url = data_url
        compression_applied = False
        max_size = 500 * 1024  # 500KB threshold
        # Full-page screenshots can legitimately be very tall; allow more
        # vertical resolution before downsampling so the LLM can still read
        # text in long documents/pages.
        max_height = 12000 if is_full_page else 3000

        if original_size > max_size:
            try:
                import base64
                import io

                from PIL import Image

                # Decode base64
                b64_data = data_url.split(',', 1)[1] if ',' in data_url else data_url
                img_data = base64.b64decode(b64_data)
                img = Image.open(io.BytesIO(img_data))

                # Resize if too tall
                width, height = img.size
                if height > max_height:
                    scale = max_height / height
                    width = int(width * scale)
                    height = max_height
                    img = img.resize((width, height), Image.LANCZOS)
                    compression_applied = True

                # Convert to JPEG for smaller size (quality=70). optimize=True
                # adds a second Huffman-optimization pass that ~doubles encode
                # time on large full-page captures for a few % size gain — not
                # worth it on the hot screenshot path.
                output = io.BytesIO()
                img = img.convert('RGB')  # Remove alpha for JPEG
                img.save(output, format='JPEG', quality=70)
                output.seek(0)

                compressed_b64 = base64.b64encode(output.read()).decode('ascii')
                compressed_url = f'data:image/jpeg;base64,{compressed_b64}'
                fmt = 'jpeg'
                compression_applied = True

            except Exception as e:
                # Fall back to original if compression fails
                logger.warning("Screenshot compression failed, using original: %s", e, exc_info=True)

        # Return structured result with metadata
        out = {
            '__screenshot__': True,
            'dataUrl': compressed_url,
            'format': fmt,
            'originalSize': original_size,
            'compressedSize': len(compressed_url),
            'compressionApplied': compression_applied,
            'fullPage': is_full_page,
        }
        if result.get('width') is not None:
            out['width'] = result['width']
        if result.get('height') is not None:
            out['height'] = result['height']
        if result.get('contentHeight') is not None:
            out['contentHeight'] = result['contentHeight']
        if result.get('truncatedHeight'):
            out['truncatedHeight'] = True
        if fallback_reason:
            out['fallbackReason'] = fallback_reason
        return out
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_get_cookies(fn_args):
    params = {}
    if fn_args.get('url'): params['url'] = fn_args['url']
    if fn_args.get('domain'): params['domain'] = fn_args['domain']
    if fn_args.get('name'): params['name'] = fn_args['name']
    result, error = send_browser_command('get_cookies', params, timeout=10)
    if error:
        return f'Error getting cookies: {error}'
    if isinstance(result, list):
        lines = [f'Cookies ({len(result)} found):\n']
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
        return f'Error getting history: {error}'
    if isinstance(result, list):
        lines = [f'History ({len(result)} entries):\n']
        for h in result:
            lines.append(f'  {h.get("title", "(no title)")}')
            lines.append(f'    URL: {h.get("url", "")}')
            lines.append(f'    Visits: {h.get("visitCount", 0)}, Last: {h.get("lastVisitTime", "")}')
        return '\n'.join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)
