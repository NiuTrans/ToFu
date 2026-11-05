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


def _handle_read_page(fn_args, runtime):
    """browser_read_page — the ONE perception entry (v2, ).

    Merges read_tab / summarize_page / get_interactive_elements /
    get_app_state; mode='media' runs the fixed inventory projection owned by
    ``lib.browser.media``. mode='auto' reads the text optimistically, consults
    the page's embedded data islands (``lib.browser.data_islands``) only when
    the rendered page supplied nothing, and only then pays for a structural
    summary — the canvas/SPA routing the old descriptions taught the model to
    do by hand. mode='data' reads captured API data and the islands.
    """
    from lib.browser._resolve import resolve_work_tab
    from ._interact import _read_elements
    from ._tabs import (
        _render_read_result,
        _result_url_allowed,
    )

    from lib.browser.content_contract import ReadTarget
    try:
        target = ReadTarget.from_value(fn_args.get('target'))
    except ValueError as exc:
        return f'Error: {exc}'
    tab_id = resolve_work_tab(
        fn_args, route_key=runtime.route_key, send=runtime.send)
    if tab_id is None:
        return ('Error: no tab to read. Pass tab_id, or call '
                'browser_list_tabs / browser_navigate first.')
    mode = str(fn_args.get('mode') or 'auto').lower()
    if mode == 'data':
        from lib.browser.protocol import (
            BrowserCapability, BrowserUpgradeRequired, require_capabilities,
        )
        api_note = ''
        try:
            require_capabilities(
                runtime.client_id, [BrowserCapability.NETWORK_BODY])
        except BrowserUpgradeRequired as exc:
            # Islands need no capture capability; the read continues without
            # captured bodies and says so.
            api_note = ('Captured API data unavailable: browser extension upgrade '
                        'required for captured API data; missing capabilities: '
                        f'{", ".join(exc.missing)}.')
        result, error = runtime.send('read_tab', {
            'tabId': int(tab_id), 'maxChars': 80_000,
        }, timeout=30)
        if error:
            return f'Error reading page data from tab {tab_id}: {error}'
        if not _result_url_allowed(result, runtime):
            return 'Error: browser read result was denied by domain policy'
        if api_note:
            result = {key: value for key, value in result.items()
                      if key not in ('network', 'networkResponses')}
        from lib.browser.content import collect_content, render_content
        from lib.browser.data_islands import render_islands_report, with_islands
        islands = _read_islands(tab_id, result, runtime)
        content = collect_content(with_islands(result, islands),
                                  owner_user_id=runtime.owner_user_id,
                                  target=target, representation='data')
        return '\n\n'.join(part for part in (
            render_content(content), api_note,
            render_islands_report(islands, content, target=target)) if part)
    if mode == 'elements':
        return _read_elements({
            'tabId': tab_id,
            'viewport': fn_args.get('viewport', False),
            'maxElements': fn_args.get('maxElements', 200),
        }, runtime)
    if mode == 'app_state':
        return _read_app_state(
            {'tabId': tab_id, 'depth': fn_args.get('depth')}, runtime)
    if mode == 'media':
        from lib.browser.media import (
            accept_media_inventory,
            media_inventory_script,
            render_media_inventory,
        )
        result, error = runtime.send('execute_js', {
            'tabId': int(tab_id), 'code': media_inventory_script(),
        })
        if error:
            return f'Error reading media from tab {tab_id}: {error}'
        if isinstance(result, dict) and result.get('__error'):
            return (f'Error reading media from tab {tab_id}: the inventory '
                    'script failed on this page.')
        if not _result_url_allowed(result, runtime):
            return 'Error: browser read result was denied by domain policy'
        return render_media_inventory(
            accept_media_inventory(result, owner_user_id=runtime.owner_user_id),
            tab_id=tab_id)
    if mode not in ('auto', 'text'):
        return (f"Error: unknown mode '{mode}' — use auto (default), text, "
                f"data, elements, app_state, or media.")
    # ── auto: optimistic text read; diagnose only when nothing was accepted ──
    result, error = runtime.send('read_tab', {
        'tabId': int(tab_id), 'selector': fn_args.get('selector'),
        'maxChars': 80_000,
    }, timeout=30)
    if error:
        return f'Error reading tab {tab_id}: {error}'
    if not _result_url_allowed(result, runtime):
        return 'Error: browser read result was denied by domain policy'
    if isinstance(result, dict) and not result.get('error') and not result.get('elements'):
        from lib.browser.content import collect_content, render_content
        if mode == 'text':
            # Explicit DOM mode uses the same acceptance core, without API/state enrichment.
            result = {key: value for key, value in result.items()
                      if key not in ('network', 'networkResponses', 'initialStatePayloads')}
        content = collect_content(result, owner_user_id=runtime.owner_user_id,
                                  target=target)
        islands = None
        if (mode == 'auto' and content['status'] == 'unavailable'
                and not content['coverage']['stop_reason'].startswith('http_error_')):
            # Structural trigger: embedded data is consulted only when the
            # rendered page supplied nothing, so it never replaces DOM content.
            from lib.browser.data_islands import with_islands
            islands = _read_islands(tab_id, result, runtime)
            if islands.payloads:
                enriched = collect_content(
                    with_islands(result, islands),
                    owner_user_id=runtime.owner_user_id, target=target)
                if enriched['status'] != 'unavailable':
                    content = enriched
        if (content['status'] in ('ok', 'partial', 'empty', 'blocked')
                or content['coverage']['stop_reason'].startswith('http_error_')):
            # The accepted page URL names this read on its timeline row,
            # which the answer's Sources footer cites as an opened page.
            if content['status'] in ('ok', 'partial') and runtime.display_metadata is not None:
                runtime.display_metadata['url'] = content['url']
            return _render_with_evidence(content)
        body = render_content(content)
        islands_report = ''
        if islands is not None and islands.available and islands.found:
            from lib.browser.data_islands import render_islands_report
            islands_report = '\n\n' + render_islands_report(
                islands, content, target=target)
        # The same acceptance owner, untargeted, measures the page's own
        # readable text, so a target that selected nothing is never
        # misdiagnosed as a Canvas/SPA page.
        page_text = ''
        if target != ReadTarget():
            page_text = collect_content(
                result, owner_user_id=runtime.owner_user_id)['content']
        # The receipt leads, so the task layer's page-text filter never
        # rewrites this diagnosis.
        if page_text:
            if target.query:
                return (
                    body + '\n\nNo content matched the requested target query '
                    f'{json.dumps(target.query, ensure_ascii=False)}: the page '
                    f'has {len(page_text)} chars of accepted text, but the query '
                    'does not occur verbatim (case-insensitive) in that text or '
                    'in any record.\n'
                    'Next step: retry browser_read_page without target.query, '
                    'or with a shorter or different keyword.' + islands_report)
            return (
                body + '\n\nNo content matched the requested target kind '
                f'"{target.kind}": the page has {len(page_text)} chars of '
                'accepted text, but none of it is content of that kind.\n'
                'Next step: retry browser_read_page with target.kind="auto", '
                'or without target.' + islands_report)
        # Sparse: the page is likely Canvas/SVG/SPA — attach the structural
        # summary (framework, forms, canvas count) so the model gets the
        # diagnosis instead of having to make it.
        summary = _summarize_page({'tabId': tab_id}, runtime)
        diagnostic = (
            f'Text extraction is sparse ({len(page_text)} chars of page text '
            f'accepted) — the page is likely Canvas/SVG/SPA-rendered.\n'
            f'Next steps: browser_research_page(url="{result.get("url", "")}") '
            f'for automatic network/state/scroll extraction, browser_screenshot '
            f'to SEE the layout, or mode="app_state" for framework/chart data.\n\n'
            f'--- Structural summary ---\n{summary}\n\n'
            f'--- Extracted text (sparse) ---\n{body}'
        )
        return diagnostic + islands_report
    return _render_read_result(result, tab_id)


def _read_islands(tab_id, page, runtime):
    """Run the fixed island reader on the tab just read; a failure only
    loses the islands, never the rest of the read."""
    from lib.browser.data_islands import IslandRead, data_islands_script, parse_islands
    from ._tabs import _result_url_allowed

    page_url = str(page.get('finalUrl') or page.get('url') or '')
    result, error = runtime.send('execute_js', {
        'tabId': int(tab_id), 'code': data_islands_script(),
    })
    if error:
        return IslandRead.unavailable(page_url, f'reader command failed: {str(error)[:200]}')
    if not isinstance(result, dict) or result.get('__error') or not result.get('url'):
        return IslandRead.unavailable(page_url, 'the reader script returned no page result')
    if not _result_url_allowed(result, runtime):
        return IslandRead.unavailable(page_url, 'the reader result was denied by domain policy')
    return parse_islands(result, page_url=page_url)


def _render_with_evidence(content):
    """Pass accepted content and canonical evidence intact to common L0."""
    from lib.browser.content import render_content
    body = render_content(content)
    evidence_json = getattr(content, 'evidence_json', '')
    if not evidence_json:
        return body
    from lib.tools.result_value import (CapturedToolResult, ResultPart,
                                        TrustedResultText)
    return TrustedResultText(body, projection_normalized=True,
                             source_omitted=getattr(content, 'source_omitted', False),
                             canonical=CapturedToolResult(
        (ResultPart('text', evidence_json),),
        source_coverage=(
            'partial'
            if (content.get('coverage') or {}).get('truncated') else 'complete'),
        retention_coverage='complete'))


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
