"""Unified page-reading handler and its internal representations.

Handlers for summarizing a page and extracting app/framework state.
Every bridge call uses an explicit request-scoped runtime.
"""

import json

from lib.log import get_logger

logger = get_logger(__name__)


def _summarize_page(fn_args, runtime):
    tab_id = fn_args.get('tabId')
    if tab_id is None:
        return 'Error: tabId is required.'
    result, error = runtime.send(
        'summarize_page', {'tabId': int(tab_id)}, timeout=15)
    if error:
        return f'Error summarizing page: {error}'
    if isinstance(result, dict):
        sum_title = result.get('title', 'Untitled')
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


#: Below this extracted-text length the auto mode considers a page "sparse"
#: (Canvas/SVG/SPA-rendered) and attaches the structural summary so the model
#: is never asked to diagnose the rendering technology itself.
_AUTO_SPARSE_CHARS = 400


def _handle_read_page(fn_args, runtime):
    """browser_read_page — the ONE perception entry (v2, ).

    Merges read_tab / summarize_page / get_interactive_elements /
    get_app_state. mode='auto' reads the text optimistically and only pays
    for a structural summary when the text proves sparse — the canvas/SPA
    routing the old descriptions taught the model to do by hand.
    """
    from lib.browser._resolve import resolve_work_tab
    from ._interact import _read_elements
    from ._tabs import (
        _extract_best_text, _read_tab, _render_read_result,
        _result_url_allowed,
    )

    tab_id = resolve_work_tab(
        fn_args, route_key=runtime.route_key, send=runtime.send)
    if tab_id is None:
        return ('Error: no tab to read. Pass tab_id, or call '
                'browser_list_tabs / browser_navigate first.')
    mode = str(fn_args.get('mode') or 'auto').lower()
    if mode == 'text':
        return _read_tab({
            'tabId': tab_id,
            'selector': fn_args.get('selector'),
            'maxChars': fn_args.get('maxChars', 50000),
        }, runtime)
    if mode == 'data':
        try:
            from lib.browser.protocol import (
                BrowserCapability, BrowserUpgradeRequired,
                require_capabilities,
            )
            require_capabilities(
                runtime.client_id, [BrowserCapability.NETWORK_BODY])
        except BrowserUpgradeRequired as exc:
            return ('Error: browser extension upgrade required for captured API '
                    f'data; missing capabilities: {", ".join(exc.missing)}')
        result, error = runtime.send('read_tab', {
            'tabId': int(tab_id), 'maxChars': fn_args.get('maxChars', 30_000),
        }, timeout=30)
        if error:
            return f'Error reading captured API data from tab {tab_id}: {error}'
        if not _result_url_allowed(result, runtime):
            return 'Error: browser read result was denied by domain policy'
        from lib.browser.network_evidence import render_network_evidence
        evidence = render_network_evidence(
            result, owner_user_id=runtime.owner_user_id,
            max_chars=fn_args.get('maxChars', 30_000))
        return evidence or (
            'No business-data API response was captured for this page. '
            'Navigate or reload it once with the current browser extension, '
            'then retry mode="data".')
    if mode == 'elements':
        return _read_elements({
            'tabId': tab_id,
            'viewport': fn_args.get('viewport', False),
            'maxElements': fn_args.get('maxElements', 200),
        }, runtime)
    if mode == 'app_state':
        return _read_app_state(
            {'tabId': tab_id, 'depth': fn_args.get('depth')}, runtime)
    if mode != 'auto':
        return (f"Error: unknown mode '{mode}' — use auto (default), text, "
                f"data, elements, or app_state.")
    # ── auto: optimistic text read; diagnose only on sparsity ──
    result, error = runtime.send('read_tab', {
        'tabId': int(tab_id), 'selector': fn_args.get('selector'),
        'maxChars': fn_args.get('maxChars', 30000),
    }, timeout=30)
    if error:
        return f'Error reading tab {tab_id}: {error}'
    if not _result_url_allowed(result, runtime):
        return 'Error: browser read result was denied by domain policy'
    if isinstance(result, dict) and not result.get('error') and not result.get('elements'):
        text, _method = _extract_best_text(result)
        from lib.browser.network_evidence import render_network_evidence
        max_chars = fn_args.get('maxChars', 30_000)
        network_text = render_network_evidence(
            result, owner_user_id=runtime.owner_user_id,
            max_chars=max_chars)
        if len((text or '').strip()) >= _AUTO_SPARSE_CHARS or network_text:
            return _render_read_result(
                result, tab_id, network_text=network_text,
                max_chars=max_chars)
        # Sparse: the page is likely Canvas/SVG/SPA — attach the structural
        # summary (framework, forms, canvas count) so the model gets the
        # diagnosis instead of having to make it.
        summary = _summarize_page({'tabId': tab_id}, runtime)
        body = _render_read_result(result, tab_id)
        return (
            f'Text extraction is sparse ({len((text or "").strip())} chars) — '
            f'the page is likely Canvas/SVG/SPA-rendered.\n'
            f'Next steps: browser_research_page(url="{result.get("url", "")}") '
            f'for automatic network/state/scroll extraction, browser_screenshot '
            f'to SEE the layout, or mode="app_state" for framework/chart data.\n\n'
            f'--- Structural summary ---\n{summary}\n\n'
            f'--- Extracted text (sparse) ---\n{body}'
        )
    return _render_read_result(result, tab_id)


def _read_app_state(fn_args, runtime):
    tab_id = fn_args.get('tabId')
    if tab_id is None:
        return 'Error: tabId is required.'
    params = {'tabId': int(tab_id)}
    if fn_args.get('depth'):
        params['depth'] = fn_args['depth']
    result, error = runtime.send('get_app_state', params, timeout=20)
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
