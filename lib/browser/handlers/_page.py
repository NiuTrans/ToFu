"""lib/browser/handlers/_page.py — Page-level analysis handlers.

Handlers for summarizing a page and extracting app/framework state.
Each takes fn_args (dict) and returns a string result for the LLM,
communicating with the browser extension via send_browser_command().
"""

import json

from lib.browser.display import update_tab_title
from lib.log import get_logger

logger = get_logger(__name__)


def _facade():
    """Return the package facade so collaborators resolve at call time."""
    import lib.browser.handlers as _pkg
    return _pkg


def send_browser_command(*args, **kwargs):
    """Facade-resolving proxy for lib.browser.queue.send_browser_command."""
    return _facade().send_browser_command(*args, **kwargs)


def _handle_summarize_page(fn_args):
    tab_id = fn_args.get('tabId')
    if tab_id is None:
        return 'Error: tabId is required.'
    result, error = send_browser_command('summarize_page', {'tabId': int(tab_id)}, timeout=15)
    if error:
        return f'Error summarizing page: {error}'
    if isinstance(result, dict):
        sum_title = result.get('title', 'Untitled')
        sum_url = result.get('url', '')
        if (sum_title and sum_title != 'Untitled') or sum_url:
            update_tab_title(tab_id, sum_title if sum_title != 'Untitled' else None, url=sum_url)
        lines = [f"Page Summary: {sum_title}"]
        lines.append(f"   URL: {result.get('url', '')}")
        lines.append(f"   Framework: {result.get('framework', 'Unknown')}")
        lines.append(f"   Canvas: {result.get('canvasCount', 0)}, SVG: {result.get('svgCount', 0)}, DOM elements: {result.get('domElementCount', 0):,}")

        buttons = result.get('mainButtons', [])
        if buttons:
            lines.append(f"\n   Buttons ({len(buttons)}):")
            for b in buttons[:10]:
                lines.append(f"      - {b.get('text', '(no text)')} -> {b.get('selector', '')}")

        links = result.get('mainLinks', [])
        if links:
            lines.append(f"\n   Links ({len(links)}):")
            for lnk in links[:10]:
                lines.append(f"      - {lnk.get('text', '(no text)')} -> {lnk.get('href', '')[:80]}")

        forms = result.get('forms', [])
        if forms:
            lines.append(f"\n   Forms ({len(forms)}):")
            for frm in forms:
                lines.append(f"      - {frm.get('method', 'GET').upper()} {frm.get('action', '')} ({frm.get('inputCount', 0)} inputs)")

        tables = result.get('tables', [])
        if tables:
            lines.append(f"\n   Tables ({len(tables)}):")
            for tbl in tables:
                lines.append(f"      - {tbl.get('rows', 0)} rows x {tbl.get('cols', 0)} cols")

        if result.get('hasModal'):
            lines.append("\n   Modal/Dialog detected on page")

        if result.get('canvasCount', 0) > 0:
            lines.append("\n   TIP: This page uses Canvas rendering. For interaction, use browser_screenshot to see the layout, then browser_execute_js to access app data or simulate clicks.")

        return '\n'.join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_get_app_state(fn_args):
    tab_id = fn_args.get('tabId')
    if tab_id is None:
        return 'Error: tabId is required.'
    params = {'tabId': int(tab_id)}
    if fn_args.get('depth'):
        params['depth'] = fn_args['depth']
    result, error = send_browser_command('get_app_state', params, timeout=20)
    if error:
        return f'Error getting app state: {error}'
    if isinstance(result, dict):
        lines = [f"App State (Framework: {result.get('framework', 'Unknown')})"]

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
            lines.append(f"\n   Chart Library: {result['chartLib']}")
            chart_data = result.get('chartData')
            if chart_data:
                if chart_data.get('nodes'):
                    lines.append(f"      Nodes: {len(chart_data['nodes'])}")
                    for n in chart_data['nodes'][:5]:
                        lines.append(f"         - {n.get('id', '?')}: {n.get('label', '')}")
                if chart_data.get('edges'):
                    lines.append(f"      Edges: {len(chart_data['edges'])}")

        global_vars = result.get('globalVars', {})
        if global_vars:
            lines.append(f"\n   Global variables found: {', '.join(global_vars.keys())}")
            for k, v in list(global_vars.items())[:5]:
                v_display = json.dumps(v, ensure_ascii=False)[:200] if isinstance(v, (dict, list)) else str(v)[:200]
                lines.append(f"      {k} = {v_display}")

        if result.get('vueError'):
            lines.append(f"\n   Vue extraction error: {result['vueError']}")
        if result.get('chartError'):
            lines.append(f"\n   Chart extraction error: {result['chartError']}")

        return '\n'.join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)
