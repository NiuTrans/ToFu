# HOT_PATH
"""Write-approval gating — metadata enrichers + the ``_handle_approval`` gate.

Registry pattern: each tool type that needs approval has a dedicated
enricher function that augments the base ``approval_meta`` dict.  The
``_handle_approval`` gate emits the ``write_approval_request`` event and
blocks waiting for the user's decision — it NEVER executes the tool.
"""

from __future__ import annotations

import json
import hmac
import math
import time
import uuid
from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.approval import request_write_approval
from lib.tasks_pkg.executor import _finalize_tool_round
from lib.tasks_pkg.manager import append_event
from lib.tools.registry import build_tool_result_meta
from lib.tool_results.tool_rejection import stamp_tool_rejection

logger = get_logger(__name__)
_TOOL_APPROVAL_RECEIPT_MAX = 64
_TOOL_APPROVAL_RECEIPT_TTL_SECONDS = 300


_TOOL_APPROVAL_RECEIPT_AUTHORITIES = (
            'human', 'auto_write_mode',
            # Delegated adjudicator (docs/contracts/TOOL_EXECUTION_POLICY.md): the
            # bounded adjudicator sub-agent's decision is the receipt
            # authority.
            'agent_adjudicator',
            # Git workspace guard (docs/modules/project_integration.md): the
            # executing agent's own deliberation is the receipt authority.
            'agent_git_guard_ack', 'agent_git_guard_solo', 'git_safe_write')

def mint_approval_receipt(task: dict, fn_name: str, tc_id: str,
                          fn_args: dict, *, authority: str) -> bool:
    """Bind an already-authorized gateway decision to one executable call."""
    if not tc_id or authority not in _TOOL_APPROVAL_RECEIPT_AUTHORITIES:
        return False
    from lib.tasks_pkg.tool_dispatch._flags import _call_id_signature
    receipts = task.setdefault('_tool_approval_receipts', {})
    while len(receipts) >= _TOOL_APPROVAL_RECEIPT_MAX:
        receipts.pop(next(iter(receipts)), None)
    receipts[tc_id] = {
        'signature': _call_id_signature(fn_name, fn_args),
        'minted_at': time.time(), 'authority': authority,
    }
    return True


def consume_approval_receipt(
    task: dict[str, Any],
    fn_name: str,
    tc_id: str,
    fn_args: dict[str, Any],
) -> bool:
    """Consume one approval bound to the exact tool name, call id, and args."""
    receipts = task.get('_tool_approval_receipts')
    if not isinstance(receipts, dict):
        return False
    received = receipts.pop(str(tc_id or ''), None)
    if not isinstance(received, dict):
        return False
    signature = received.get('signature')
    try:
        age = time.time() - float(received.get('minted_at'))
    except (TypeError, ValueError):
        return False
    if (not isinstance(signature, str) or not math.isfinite(age) or age < 0
            or age > _TOOL_APPROVAL_RECEIPT_TTL_SECONDS):
        return False
    from lib.tasks_pkg.tool_dispatch._flags import _call_id_signature
    expected = _call_id_signature(fn_name, fn_args)
    return hmac.compare_digest(signature, expected)


def approval_receipt_authority(task, fn_name, tc_id, fn_args):
    """Inspect an exact live receipt without consuming a handler's authority.

    The normal pipeline still checks ownership, current write policy, Git
    safety and hooks. This only prevents a second attended request for an
    already approved identical call; expiry/argument changes fail closed.
    """
    receipts = task.get('_tool_approval_receipts')
    received = receipts.get(str(tc_id or '')) if isinstance(receipts, dict) else None
    if not isinstance(received, dict):
        return None
    try:
        age = time.time() - float(received.get('minted_at'))
    except (TypeError, ValueError):
        return None
    signature = received.get('signature')
    if (not isinstance(signature, str) or not math.isfinite(age) or age < 0
            or age > _TOOL_APPROVAL_RECEIPT_TTL_SECONDS):
        return None
    from lib.tasks_pkg.tool_dispatch._flags import _call_id_signature
    if not hmac.compare_digest(signature, _call_id_signature(fn_name, fn_args)):
        return None
    authority = received.get('authority')
    return authority if authority in _TOOL_APPROVAL_RECEIPT_AUTHORITIES else None


# ── Approval metadata enrichers ────────────────────────────────────────
# Registry pattern: each tool type that needs approval has a dedicated
# function that enriches the base ``approval_meta`` dict.
#
# ⚠️ THE CONTRACT IS "THE DIALOG RENDERS THE RISK", NOT "AN ENRICHER EXISTS".
# The frontend (``_renderPendingApprovalBlock`` in static/js/ui/tool_rounds.js)
# only draws a detail block for shapes it recognises. Historically those were
# four hardcoded family shapes (batch editSummaries / search+replace /
# command / contentPreview), so an enricher that wrote some OTHER key produced
# a dialog with NO detail at all — the user saw a bare tool name and approved
# blind. That measurably happened to 8 of the first 15 enrichers, including
# ``browser_execute_js``, whose own docstring promised the JS body was
# "surfaced verbatim": it wrote ``search`` but not ``replace``, so the
# search+replace branch never fired and the code was invisible.
#
# Use :func:`_risk` for anything that is not genuinely a diff/command/content
# preview. It fills ``riskFields``, the generic list the renderer draws for
# any tool, so a new write tool needs ZERO frontend changes.


def enrich_approval_metadata(approval_meta, fn_name, fn_args):
    """Unknown/third-party write tools still disclose their exact arguments.

    Specialized renderers remain authoritative. The generic riskFields shape
    is already rendered by the browser and avoids a new UI/core registry for
    every independently installed tool.
    """
    enricher = _APPROVAL_META_ENRICHERS.get(fn_name)
    if enricher is not None:
        enricher(approval_meta, fn_args)
    else:
        _risk(approval_meta, ("Arguments", json.dumps(fn_args, ensure_ascii=False, indent=2, allow_nan=False)))


def _risk(approval_meta, *fields, note=''):
    """Declare which arguments carry this tool's risk.

    Args:
        approval_meta: the dict to enrich (mutated in place).
        *fields: ``(label, value)`` pairs, most important first. Empty /
            ``None`` values are dropped so the dialog never shows a blank row
            (an omitted optional arg is not a risk worth naming).
        note: optional one-line explanation of what approving permits.

    Bounding is left to the renderer, which truncates per field — the value is
    stored whole so a future consumer (audit log, headless client) is not
    handed a pre-truncated string.
    """
    rows = []
    for label, value in fields:
        if value is None:
            continue
        text = value if isinstance(value, str) else str(value)
        if not text.strip():
            continue
        rows.append({'label': label, 'value': text})
    if rows:
        approval_meta['riskFields'] = rows
    if note:
        approval_meta['description'] = note


def _approval_meta_run_command(approval_meta, fn_args):
    """Enrich approval metadata for ``run_command``."""
    approval_meta['command'] = fn_args.get('command', '')
    approval_meta['description'] = fn_args.get('description', '')
    approval_meta['path'] = fn_args.get('working_dir', '') or ''


def _approval_meta_paper_import_arxiv(approval_meta, fn_args):
    """Show the exact arXiv identity and what approving creates."""
    arxiv = str(fn_args.get('arxiv') or '')
    approval_meta['path'] = arxiv
    _risk(
        approval_meta,
        ('arXiv paper', arxiv),
        note=(
            'Download the paper\'s TeX source from arxiv.org and extract it '
            'into a fresh arxiv-<id>/ directory inside the workspace. Existing '
            'files are never overwritten.'),
    )


def _approval_meta_paper_cite_insert(approval_meta, fn_args):
    """Show the exact PID and what approving writes."""
    identifier = str(fn_args.get('doi') or fn_args.get('openalex_id')
                     or fn_args.get('arxiv') or '')
    approval_meta['path'] = str(fn_args.get('cite_file') or 'refs.bib')
    _risk(
        approval_meta,
        ('Work identifier', identifier),
        ('Cite key', str(fn_args.get('key') or '(auto-proposed)')),
        ('Anchor file', str(fn_args.get('tex_file') or '(paper main file)')),
        note=(
            'Fetch the work\'s real BibTeX from Crossref/OpenAlex, append it '
            'to the project .bib (deduplicated), and insert \\cite{key} at '
            'the anchor in the .tex file. Both edits are journaled and '
            'undoable.'),
    )


def _approval_meta_request_skill_install(approval_meta, fn_args):
    """Show the exact curated package and the model's stated need."""
    catalog_id = str(fn_args.get('catalog_id') or '')
    scope = str(fn_args.get('scope') or 'global')
    overwrite = bool(fn_args.get('overwrite', False))
    try:
        from lib.skills.catalog import get_catalog_entry
        entry = get_catalog_entry(catalog_id) or {}
    except Exception as exc:
        logger.debug('[Approval] skill catalog lookup failed: %s', exc)
        entry = {}
    approval_meta['path'] = catalog_id
    _risk(
        approval_meta,
        ('Catalog skill', catalog_id),
        ('Package name', entry.get('name')),
        ('Publisher', entry.get('author')),
        ('Pinned revision', entry.get('source_revision')),
        ('Verified content SHA-256', entry.get('content_sha256')),
        ('Install scope', scope),
        ('Replace same-id package', 'yes' if overwrite else 'no'),
        ('Why this task needs it', fn_args.get('reason')),
        note=(
            'Download and install a verified instruction package. Bundled '
            'scripts will be retained but not executed.'),
    )


def _approval_meta_write_file(approval_meta, fn_args):
    """Enrich approval metadata for ``write_file``."""
    content = fn_args.get('content', '')
    approval_meta['contentPreview'] = content[:500] + ('…' if len(content) > 500 else '')
    approval_meta['contentLines'] = content.count('\n') + 1
    approval_meta['contentChars'] = len(content)


def _approval_meta_apply_diff(approval_meta, fn_args):
    """Enrich approval metadata for ``apply_diff`` (single edit)."""
    search_text = fn_args.get('search', '')
    replace_text = fn_args.get('replace', '')
    approval_meta['search'] = search_text[:2000] + ('…' if len(search_text) > 2000 else '')
    approval_meta['replace'] = replace_text[:2000] + ('…' if len(replace_text) > 2000 else '')
    approval_meta['searchLines'] = search_text.count('\n') + 1
    approval_meta['searchChars'] = len(search_text)
    approval_meta['replaceLines'] = replace_text.count('\n') + 1
    approval_meta['replaceChars'] = len(replace_text)
    if fn_args.get('replace_all'):
        approval_meta['replaceAll'] = True


def _approval_meta_apply_diffs(approval_meta, fn_args):
    """Enrich approval metadata for ``apply_diffs`` (batch)."""
    edits = fn_args.get('edits') or []
    paths = list(dict.fromkeys(
        e.get('path', '?') for e in edits if isinstance(e, dict)
    ))
    approval_meta['path'] = (
        ', '.join(paths[:5])
        + (f' +{len(paths)-5} more' if len(paths) > 5 else '')
    )
    approval_meta['editCount'] = len(edits)
    approval_meta['batchMode'] = True
    approval_meta['description'] = f'Batch: {len(edits)} edits across {len(paths)} file(s)'
    edit_summaries = []
    for e in edits[:20]:
        if not isinstance(e, dict):
            continue
        s_text = e.get('search', '')
        r_text = e.get('replace', '')
        edit_summaries.append({
            'path': e.get('path', '?'),
            'description': e.get('description', ''),
            'search': s_text[:500] + ('…' if len(s_text) > 500 else ''),
            'replace': r_text[:500] + ('…' if len(r_text) > 500 else ''),
            'searchLines': s_text.count('\n') + 1,
            'replaceLines': r_text.count('\n') + 1,
        })
    approval_meta['editSummaries'] = edit_summaries


def _approval_meta_insert_content(approval_meta, fn_args):
    """Enrich approval metadata for ``insert_content`` (single insertion)."""
    anchor_text = fn_args.get('anchor', '')
    content_text = fn_args.get('content', '')
    pos = fn_args.get('position', 'after')
    approval_meta['search'] = anchor_text[:2000] + ('…' if len(anchor_text) > 2000 else '')
    approval_meta['replace'] = content_text[:2000] + ('…' if len(content_text) > 2000 else '')
    approval_meta['searchLines'] = anchor_text.count('\n') + 1
    approval_meta['searchChars'] = len(anchor_text)
    approval_meta['replaceLines'] = content_text.count('\n') + 1
    approval_meta['replaceChars'] = len(content_text)
    approval_meta['description'] = approval_meta.get('description', '') or f'Insert {pos} anchor'


def _approval_meta_insert_contents(approval_meta, fn_args):
    """Enrich approval metadata for ``insert_contents`` (batch)."""
    edits = fn_args.get('edits') or []
    paths = list(dict.fromkeys(
        e.get('path', '?') for e in edits if isinstance(e, dict)
    ))
    approval_meta['path'] = (
        ', '.join(paths[:5])
        + (f' +{len(paths)-5} more' if len(paths) > 5 else '')
    )
    approval_meta['editCount'] = len(edits)
    approval_meta['batchMode'] = True
    approval_meta['description'] = f'Batch: {len(edits)} insertions across {len(paths)} file(s)'
    edit_summaries = []
    for e in edits[:20]:
        if not isinstance(e, dict):
            continue
        anchor_text = e.get('anchor', '')
        content_text = e.get('content', '')
        pos = e.get('position', 'after')
        edit_summaries.append({
            'path': e.get('path', '?'),
            'description': e.get('description', f'Insert {pos} anchor'),
            'search': anchor_text[:500] + ('…' if len(anchor_text) > 500 else ''),
            'replace': content_text[:500] + ('…' if len(content_text) > 500 else ''),
            'searchLines': anchor_text.count('\n') + 1,
            'replaceLines': content_text.count('\n') + 1,
        })
    approval_meta['editSummaries'] = edit_summaries


def _approval_meta_edit_file(approval_meta, fn_args):
    """Show mixed edit operations without hiding insert direction."""
    edits = fn_args.get('edits') or []
    paths = list(dict.fromkeys(
        e.get('path', '?') for e in edits if isinstance(e, dict)
    ))
    approval_meta['path'] = (
        ', '.join(paths[:5])
        + (f' +{len(paths)-5} more' if len(paths) > 5 else '')
    )
    approval_meta['editCount'] = len(edits)
    approval_meta['batchMode'] = True
    approval_meta['description'] = (
        fn_args.get('description')
        or f'{len(edits)} edits across {len(paths)} file(s)'
    )
    summaries = []
    for edit in edits[:20]:
        if not isinstance(edit, dict):
            continue
        anchor = edit.get('anchor', '')
        content = edit.get('content', '')
        operation = edit.get('operation', '?')
        summaries.append({
            'path': edit.get('path', '?'),
            'operation': operation,
            'description': edit.get('description') or operation.replace('_', ' '),
            'search': anchor[:500] + ('…' if len(anchor) > 500 else ''),
            'replace': content[:500] + ('…' if len(content) > 500 else ''),
            'searchLines': anchor.count('\n') + 1,
            'replaceLines': content.count('\n') + 1,
        })
    approval_meta['editSummaries'] = summaries


def _approval_meta_browser_execute_js(approval_meta, fn_args):
    """Enrich approval metadata for ``browser_execute_js``.

    The JS body IS the risk. This previously wrote only ``search``, so the
    renderer's ``search`` + ``replace`` branch never fired and the code was
    invisible despite the promise in this docstring — the exact failure the
    ``riskFields`` shape exists to prevent.
    """
    code = fn_args.get('code', '') or ''
    approval_meta['codeChars'] = len(code)
    approval_meta['codeLines'] = code.count('\n') + 1
    tab = fn_args.get('tab_id', '') or fn_args.get('tabId', '')
    if tab:
        approval_meta['path'] = f'tab {tab}'
    _risk(
        approval_meta,
        ('JavaScript to run in your page', code),
        ('Tab', tab or None),
        note='Execute JavaScript in the browser page',
    )


def _approval_meta_browser_devtools(approval_meta, fn_args):
    """Show the exact DevTools action and executable input being approved."""
    action = str(fn_args.get('action') or 'console_read')
    expression = str(fn_args.get('expression') or '')
    source_url = str(
        fn_args.get('source_url') or fn_args.get('sourceUrl') or '')
    line = fn_args.get('line_number', fn_args.get('lineNumber'))
    column = fn_args.get('column_number', fn_args.get('columnNumber'))
    location = None
    if source_url or line is not None:
        location = f'{source_url or "script"}:{line if line is not None else 0}'
        if column is not None:
            location += f':{column}'
    tab = fn_args.get('tab_id', fn_args.get('tabId'))
    approval_meta['description'] = f'DevTools: {action}'
    if tab is not None:
        approval_meta['path'] = f'tab {tab}'
    _risk(
        approval_meta,
        ('DevTools action', action),
        ('Expression', expression or None),
        ('Script location', location),
        ('Breakpoint condition', fn_args.get('condition')),
        ('Breakpoint', fn_args.get('breakpoint_id')
         or fn_args.get('breakpointId')),
        ('Call frame', fn_args.get('call_frame_id')
         or fn_args.get('callFrameId')),
        ('Tab', tab),
        note='Control JavaScript execution in the browser page',
    )


def _approval_meta_browser_navigate(approval_meta, fn_args):
    """Enrich approval metadata for ``browser_navigate`` (destination URL)."""
    url = fn_args.get('url', '') or ''
    approval_meta['path'] = url
    approval_meta['url'] = url
    _risk(approval_meta, ('Destination URL', url),
          note='Navigate the browser to this URL')


def _approval_meta_browser_research_page(approval_meta, fn_args):
    """Describe the bounded temporary-tab traversal used for deep research.

    ``browser_research_page`` is exempt from interactive approval because it is
    read-only, but it deliberately shares the serial browser-mutation lane with
    navigation tools.  Keeping complete metadata here preserves the registry
    invariant and makes the operation intelligible if policy later requires an
    approval prompt.
    """
    url = fn_args.get('url', '') or ''
    max_scrolls = fn_args.get('max_scrolls', fn_args.get('maxScrolls', 5))
    max_pages = fn_args.get('max_pages', fn_args.get('maxPages', 3))
    pagination = fn_args.get('pagination', 'auto') or 'auto'
    approval_meta['path'] = url
    approval_meta['url'] = url
    approval_meta['maxScrolls'] = max_scrolls
    approval_meta['maxPages'] = max_pages
    approval_meta['pagination'] = pagination
    _risk(
        approval_meta,
        ('Research URL', url),
        ('Traversal bounds',
         f'{max_pages} page(s), {max_scrolls} scroll(s), pagination={pagination}'),
        note='Read this URL in a bounded temporary browser tab',
    )


def _approval_meta_browser_site(approval_meta, fn_args):
    """Describe native reads in the serial browser lane without exposing secrets."""
    from lib.browser.network_evidence import redact_value

    site = str(fn_args.get('site') or '')[:64]
    operation = str(fn_args.get('operation') or '')[:64]
    params = fn_args.get('params')
    preview = json.dumps(redact_value(params), ensure_ascii=False) if isinstance(params, dict) else ''
    if len(preview) > 4000:
        preview = preview[:4000] + '… (parameter preview truncated)'
    approval_meta['path'] = site
    _risk(
        approval_meta,
        ('Native site', site or 'Discover permitted sites'),
        ('Read operation', operation or 'Inspect available operations'),
        ('Parameters', preview),
        note='Use a declared read-only capability in the selected browser; site policy still applies',
    )


def _approval_meta_browser_fill_form(approval_meta, fn_args):
    """Enrich approval metadata for ``browser_fill_form``.

    Field VALUES may be personal data the model inferred; show the field names
    and a bounded preview of each value so the user can spot a bad autofill.
    """
    fields = fn_args.get('fields') or fn_args.get('form_data') or {}
    pairs = []
    if isinstance(fields, dict):
        items = list(fields.items())
    elif isinstance(fields, list):
        items = [(f.get('selector', '?'), f.get('value', ''))
                 for f in fields if isinstance(f, dict)]
    else:
        items = []
    for k, v in items[:20]:
        v_str = str(v)
        pairs.append(f'{k} = {v_str[:120]}' + ('…' if len(v_str) > 120 else ''))
    approval_meta['fieldCount'] = len(items)
    _risk(
        approval_meta,
        ('Fields to fill', '\n'.join(pairs)),
        note=f'Fill {len(items)} form field(s) in the browser',
    )


def _approval_meta_schedule_create(approval_meta, fn_args):
    """Enrich approval metadata for ``schedule_create``.

    A cron job outlives the turn and can run shell/python repeatedly, so both
    the CADENCE and the PAYLOAD must be visible before approval.
    """
    cmd = fn_args.get('command', '') or ''
    approval_meta['schedule'] = fn_args.get('schedule', '')
    approval_meta['taskType'] = fn_args.get('task_type', 'command')
    approval_meta['path'] = fn_args.get('name', '')
    _risk(
        approval_meta,
        ('Payload that will run', cmd),
        ('Schedule (cron)', approval_meta['schedule']),
        ('Task type', approval_meta['taskType']),
        ('Name', fn_args.get('name') or None),
        note=(f"Persist a recurring {approval_meta['taskType']} job on "
              f"schedule '{approval_meta['schedule']}' (outlives this turn)"),
    )


def _approval_meta_schedule_manage(approval_meta, fn_args):
    """Enrich approval metadata for ``schedule_manage`` (action + target)."""
    action = fn_args.get('action', '') or ''
    task_id = fn_args.get('task_id', '') or ''
    approval_meta['action'] = action
    approval_meta['path'] = task_id
    _risk(approval_meta,
          ('Action', action or 'manage'),
          ('Scheduled task id', task_id),
          note=f"{action or 'manage'} a scheduled task")


def _approval_meta_timer_create(approval_meta, fn_args):
    """Enrich approval metadata for ``timer_create``.

    Surfaces the predicate AND the continuation — the continuation is what
    eventually runs unattended, so it is the part worth reading.
    """
    cont = fn_args.get('continuation_message', '') or ''
    check = fn_args.get('check_command', '') or fn_args.get('condition_command', '') or ''
    approval_meta['search'] = check[:1000] + ('…' if len(check) > 1000 else '')
    approval_meta['replace'] = cont[:1000] + ('…' if len(cont) > 1000 else '')
    approval_meta['pollInterval'] = fn_args.get('poll_interval', '')
    approval_meta['description'] = 'Create a polling watcher (runs unattended)'


def _approval_meta_timer_manage(approval_meta, fn_args):
    """Enrich approval metadata for ``timer_manage`` (action + target)."""
    action = fn_args.get('action', '') or ''
    approval_meta['action'] = action
    approval_meta['path'] = fn_args.get('timer_id', '') or ''
    _risk(approval_meta,
          ('Action', action or 'manage'),
          ('Timer id', approval_meta['path']),
          note=f"{action or 'manage'} a polling watcher")


# ══════════════════════════════════════════════════════════
#  browser interaction — the SELECTOR/TARGET is the risk
#
#  These drive the user's real, logged-in browser session. A click on
#  ``#confirm-purchase`` is indistinguishable from a click on ``#cancel``
#  unless the dialog names the target, so the selector is always field #1.
# ══════════════════════════════════════════════════════════

def _approval_meta_browser_click(approval_meta, fn_args):
    """``browser_click`` — the element about to be activated."""
    sel = fn_args.get('selector', '') or ''
    approval_meta['path'] = sel
    _risk(
        approval_meta,
        ('Element to click', sel),
        ('Tab', fn_args.get('tab_id') or None),
        ('Right-click', 'true' if fn_args.get('right_click') else None),
        note='Click an element in your browser page',
    )


def _approval_meta_browser_type(approval_meta, fn_args):
    """``browser_type`` — the VALUE typed into the user's page is the risk."""
    target = fn_args.get('text') or fn_args.get('selector', '') or ''
    approval_meta['path'] = target
    _risk(
        approval_meta,
        ('Text to type', fn_args.get('value') or None),
        ('Into field', target or None),
        ('Tab', fn_args.get('tab_id') or None),
        note='Type text into a field in your browser page',
    )


def _approval_meta_browser_press_key(approval_meta, fn_args):
    """``browser_press_key`` — keystrokes can submit forms / fire shortcuts."""
    keys = fn_args.get('keys', '') or ''
    _risk(
        approval_meta,
        ('Keys to send', keys),
        ('Focus element', fn_args.get('selector') or None),
        ('Tab', fn_args.get('tab_id') or None),
        note='Send synthetic keystrokes to your browser page',
    )


def _approval_meta_browser_menu_click(approval_meta, fn_args):
    """``browser_menu_click`` — which menu item gets activated."""
    item = fn_args.get('item_text', '') or ''
    target = fn_args.get('target_text') or fn_args.get('target_selector', '') or ''
    approval_meta['path'] = item
    _risk(
        approval_meta,
        ('Menu item to activate', item),
        ('Menu opened on', target or None),
        ('Via', fn_args.get('via') or None),
        ('Submenu item', fn_args.get('submenu_text') or None),
        ('Tab', fn_args.get('tab_id') or None),
        note='Open a menu and activate an item in your browser',
    )


def _approval_meta_browser_check(approval_meta, fn_args):
    """``browser_check`` — which control gets (un)checked."""
    target = fn_args.get('text') or fn_args.get('selector', '') or ''
    approval_meta['path'] = target
    _risk(
        approval_meta,
        ('Control to set', target),
        ('New state', 'checked' if fn_args.get('checked', True) else 'unchecked'),
        ('Tab', fn_args.get('tab_id') or None),
        note='Set a radio/checkbox/switch in your browser page',
    )


def _approval_meta_browser_select_option(approval_meta, fn_args):
    """``browser_select_option`` — which option gets chosen in a dropdown."""
    target = fn_args.get('text') or fn_args.get('selector', '') or ''
    approval_meta['path'] = fn_args.get('option', '') or target
    _risk(
        approval_meta,
        ('Option to choose', fn_args.get('option') or None),
        ('In select', target or None),
        ('Tab', fn_args.get('tab_id') or None),
        note='Choose an option in a dropdown in your browser page',
    )


def _approval_meta_browser_close_tab(approval_meta, fn_args):
    """``browser_close_tab`` — which tab(s) get closed.

    Closing a tab can discard unsaved work in the user's own session, so the
    id list is named even though it looks innocuous.
    """
    ids = fn_args.get('tab_ids')
    single = fn_args.get('tab_id')
    if isinstance(ids, (list, tuple)) and ids:
        target = ', '.join(str(i) for i in ids)
    else:
        target = str(single) if single is not None else ''
    _risk(
        approval_meta,
        ('Tab(s) to close', target or 'active tab'),
        note='Close browser tab(s) — unsaved page state is lost',
    )


# ══════════════════════════════════════════════════════════
#  desktop — runs on the USER'S OWN MACHINE, outside the workspace
#
#  Nothing here is confined to a project root, so the path / command is the
#  whole risk. ``desktop_move_file`` is deliberately absent: it is in the
#  desktop spec's ``write_tools`` but NOT in ``provides``, so the model
#  cannot call it and it can never reach this dialog.
# ══════════════════════════════════════════════════════════

def _approval_meta_desktop_run_command(approval_meta, fn_args):
    """``desktop_run_command`` — a shell command on the user's machine."""
    cmd = fn_args.get('command', '') or ''
    approval_meta['path'] = fn_args.get('cwd', '') or ''
    _risk(
        approval_meta,
        ('Command to run on YOUR machine', cmd),
        ('Working directory', fn_args.get('cwd') or None),
        ('Timeout', fn_args.get('timeout') or None),
        note='Run a shell command on your local machine (not the server)',
    )


def _approval_meta_desktop_write_file(approval_meta, fn_args):
    """``desktop_write_file`` — target path + a preview of the new bytes."""
    path = fn_args.get('path', '') or ''
    content = fn_args.get('content', '') or ''
    approval_meta['path'] = path
    approval_meta['contentChars'] = len(content)
    _risk(
        approval_meta,
        ('File to write on YOUR machine', path),
        ('New content', content),
        ('Create parent dirs', 'true' if fn_args.get('createDirs') else None),
        note=f'Overwrite a local file ({len(content):,} chars)',
    )


def _approval_meta_desktop_open_file(approval_meta, fn_args):
    """``desktop_open_file`` — hands a path to the OS default handler."""
    path = fn_args.get('path', '') or ''
    approval_meta['path'] = path
    _risk(
        approval_meta,
        ('File to open on YOUR machine', path),
        note='Open a local file with its default application',
    )


def _approval_meta_desktop_open_app(approval_meta, fn_args):
    """``desktop_open_app`` — launches a local application with arguments."""
    app = fn_args.get('app', '') or ''
    args = fn_args.get('args')
    if isinstance(args, (list, tuple)):
        arg_text = ' '.join(str(a) for a in args)
    else:
        arg_text = '' if args is None else str(args)
    approval_meta['path'] = app
    _risk(
        approval_meta,
        ('Application to launch on YOUR machine', app),
        ('Arguments', arg_text or None),
        note='Launch a local application',
    )


# ══════════════════════════════════════════════════════════
#  memory CRUD — irreversible edits to the user's stored notes
#
#  The TARGET ID is the risk: nothing in the dialog otherwise tells the user
#  WHICH note is about to be rewritten or deleted. ``merge_memories`` is the
#  sharpest — it deletes N notes and writes 1.
# ══════════════════════════════════════════════════════════

def _approval_meta_record_working_fact(approval_meta, fn_args):
    """Show the exact claim, revision and evidence before a fact mutation."""
    _risk(approval_meta,
          ('Fact', fn_args.get('id') or 'New fact'),
          ('Revision', str(fn_args.get('revision', ''))),
          ('Kind', fn_args.get('kind')),
          ('Claim', fn_args.get('text')),
          ('Sources', json.dumps(fn_args.get('sources', []), ensure_ascii=False)),
          note='Persist a sourced working claim; this does not verify an inference.')


def _approval_meta_create_memory(approval_meta, fn_args):
    """``create_memory`` — new note; body + scope are what persist."""
    name = fn_args.get('name', '') or ''
    body = fn_args.get('body', '') or ''
    approval_meta['path'] = name
    approval_meta['contentChars'] = len(body)
    _risk(
        approval_meta,
        ('Memory name', name),
        ('Description', fn_args.get('description') or None),
        ('Body', body),
        ('Scope', fn_args.get('scope') or None),
        note='Save a new memory (persists across sessions)',
    )


def _approval_meta_update_memory(approval_meta, fn_args):
    """``update_memory`` — rewrites an existing note in place."""
    mid = fn_args.get('memory_id', '') or ''
    body = fn_args.get('body') or ''
    approval_meta['path'] = mid
    _risk(
        approval_meta,
        ('Memory to overwrite', mid),
        ('New description', fn_args.get('description') or None),
        ('New name', fn_args.get('name') or None),
        ('New body', body or None),
        note='Overwrite an existing memory (previous content is replaced)',
    )


def _approval_meta_delete_memory(approval_meta, fn_args):
    """``delete_memory`` — irreversible removal of one note."""
    mid = fn_args.get('memory_id', '') or ''
    approval_meta['path'] = mid
    _risk(
        approval_meta,
        ('Memory to DELETE', mid),
        note='Permanently delete a memory',
    )


def _approval_meta_merge_memories(approval_meta, fn_args):
    """``merge_memories`` — deletes N notes and writes 1 replacement.

    The count and the exact id list are both surfaced: "merge" reads as
    additive, but the sources are destroyed.
    """
    ids = fn_args.get('memory_ids')
    id_list = [str(i) for i in ids] if isinstance(ids, (list, tuple)) else []
    body = fn_args.get('body', '') or ''
    approval_meta['path'] = fn_args.get('name', '') or ''
    approval_meta['mergeSourceCount'] = len(id_list)
    _risk(
        approval_meta,
        (f'{len(id_list)} memory(ies) to DELETE', '\n'.join(id_list)),
        ('Replacement name', fn_args.get('name') or None),
        ('Replacement body', body),
        ('Scope', fn_args.get('scope') or None),
        note=(f'Delete {len(id_list)} memory(ies) and replace them with one '
              f'merged note'),
    )


# ══════════════════════════════════════════════════════════
#  motion_video — long, expensive renders that WRITE FILES
#
#  Risk is twofold: the output path is overwritten, and the job can burn
#  minutes of CPU / TTS quota. Both go in the dialog.
# ══════════════════════════════════════════════════════════

def _approval_meta_motion_video_render(approval_meta, fn_args):
    """``motion_video_render`` — renders one scene to an MP4 (≈3.5x realtime)."""
    out = fn_args.get('output', '') or ''
    approval_meta['path'] = out
    _risk(
        approval_meta,
        ('Output MP4 (overwritten)', out),
        ('Scene project dir', fn_args.get('project_dir') or None),
        ('Quality', fn_args.get('quality') or None),
        ('fps', fn_args.get('fps') or None),
        note='Render a scene to MP4 (headless Chrome; minutes of CPU)',
    )


def _approval_meta_motion_video_concat(approval_meta, fn_args):
    """``motion_video_concat`` — assembles scene MP4s into the final file."""
    out = fn_args.get('output', '') or ''
    inputs = fn_args.get('inputs')
    in_list = [str(i) for i in inputs] if isinstance(inputs, (list, tuple)) else []
    approval_meta['path'] = out
    _risk(
        approval_meta,
        ('Output MP4 (overwritten)', out),
        (f'{len(in_list)} input scene(s)', '\n'.join(in_list)),
        note='Concatenate scene MP4s into the final video',
    )


def _approval_meta_motion_video_mux(approval_meta, fn_args):
    """``motion_video_mux`` — muxes video + narration into the deliverable."""
    out = fn_args.get('output', '') or ''
    approval_meta['path'] = out
    _risk(
        approval_meta,
        ('Output MP4 (overwritten)', out),
        ('Video track', fn_args.get('video') or None),
        ('Audio track', fn_args.get('audio') or None),
        note='Mux video with the narration track',
    )


def _approval_meta_motion_video_narrate(approval_meta, fn_args):
    """``motion_video_narrate`` — synthesizes TTS WAVs (spends TTS quota)."""
    out_dir = fn_args.get('out_dir', '') or ''
    approval_meta['path'] = out_dir
    _risk(
        approval_meta,
        ('Output directory for narration WAVs', out_dir),
        ('Storyboard', fn_args.get('scenes_path') or None),
        ('Voice', fn_args.get('voice') or None),
        ('Speed', fn_args.get('speed') or None),
        note='Synthesize per-scene TTS narration (spends TTS quota)',
    )


def _approval_meta_local_serve_deploy(approval_meta, fn_args):
    """``local_serve_deploy`` — installs packages + spawns a long-lived server.

    The MODEL PATH and the ENGINE are the risk: approval permits a network
    install into data/local_serve/ and a managed server binding 127.0.0.1.
    """
    path = str(fn_args.get('model_path') or '')
    approval_meta['path'] = path
    _risk(
        approval_meta,
        ('Model path to deploy', path),
        ('Engine override', fn_args.get('engine') or None),
        note=('Download & install an inference engine into an isolated venv '
              '(bounded 20 GiB budget), start it as a managed local server, '
              'and register it as a selectable model provider'),
    )


def _approval_meta_local_serve_stop(approval_meta, fn_args):
    """``local_serve_stop`` — kills a running server others may be using."""
    iid = str(fn_args.get('instance_id') or '')
    approval_meta['path'] = iid
    _risk(
        approval_meta,
        ('Deployment to stop', iid),
        note=('Stop the managed local server (frees GPU/RAM; conversations '
              'using this model lose it until redeployed)'),
    )


def _approval_meta_local_serve_remove(approval_meta, fn_args):
    """``local_serve_remove`` — unregisters the provider, forgets the row."""
    iid = str(fn_args.get('instance_id') or '')
    approval_meta['path'] = iid
    _risk(
        approval_meta,
        ('Deployment to REMOVE', iid),
        note=('Stop the server, unregister its provider (the model vanishes '
              'from the picker), and delete the deployment record. Engine '
              'venv and model files are kept'),
    )


#: The adjustable knobs of ``update_search_settings`` (schema order). Every
#: one is optional and any subset may change in one call, so the dialog must
#: name each knob being touched — a bare "update settings" row is the blind
#: approval this enricher exists to prevent.
_SEARCH_SETTINGS_KNOBS = (
    'profile', 'overrides', 'fetch_top_n', 'fetch_timeout',
    'max_chars_search', 'max_chars_direct', 'max_chars_pdf',
    'max_download_mb', 'llm_content_filter',
    'block_domain', 'unblock_domain',
)


def _approval_meta_update_search_settings(approval_meta, fn_args):
    """``update_search_settings`` — mutates SERVER-WIDE search/fetch config.

    Changes are global, persist across restarts, and hit every conversation,
    so each knob being changed is listed with its new value. A no-arg call
    is a pure READ (the model inspecting current values before proposing a
    change) — say so instead of rendering an empty table.
    """
    changes = [
        (f'Setting: {k}', str(fn_args[k]))
        for k in _SEARCH_SETTINGS_KNOBS
        if fn_args.get(k) is not None
    ]
    if not changes:
        _risk(
            approval_meta,
            ('Change set', 'none — read-only inspection of current values'),
            note='Read the current search/fetch pipeline settings (no change)',
        )
        return
    approval_meta['path'] = ', '.join(k for k, _v in changes)
    _risk(
        approval_meta,
        *changes,
        note=('Change SERVER-WIDE search/fetch settings — applies globally, '
              'persists across restarts, affects every conversation'),
    )


def _approval_meta_call_mcp_write_tool(approval_meta, fn_args):
    """Show the exact remote MCP mutator and its arguments before approval.

    The progressive wrapper deliberately hides a potentially huge MCP catalog
    behind one stable tool name.  Without this enricher that indirection also
    hid the *actual* remote operation from the approval dialog: every write
    looked like the same opaque ``call_mcp_write_tool`` action.
    """
    name = str(fn_args.get('name') or '')
    arguments = fn_args.get('arguments')
    try:
        arguments_text = json.dumps(
            arguments if isinstance(arguments, dict) else {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ': '),
        )
    except (TypeError, ValueError) as exc:
        logger.debug('[Approval] MCP arguments are not JSON serializable: %s',
                     exc)
        arguments_text = str(arguments)
    approval_meta['path'] = name
    _risk(
        approval_meta,
        ('Remote MCP write tool', name),
        ('Arguments', arguments_text),
        note='Invoke a state-changing tool on an external MCP server',
    )


def _approval_meta_evolution(approval_meta, fn_args):
    """Expose actual goal mutation arguments in the existing approval surface."""
    fields = ('program_id', 'action', 'step', 'steps', 'source_seq', 'through_seq', 'decision', 'content',
              'references', 'title', 'url', 'note', 'material_id', 'name',
              'description', 'knowledge', 'examples', 'code', 'tests',
              'queries', 'reason')
    rows = [
        (field, fn_args[field])
        for field in fields
        if field in fn_args
    ]
    _risk(
        approval_meta,
        *rows,
        note='Current owner-bound evolution goal; no automatic capability activation',
    )


def _approval_meta_evolution_artifact(approval_meta, fn_args):
    """Show the selected snapshots and optional final reply in the existing dialog."""
    paths = fn_args.get('paths')
    if not isinstance(paths, list):
        paths = [fn_args.get('path')]
    selected = [path[:512] for path in paths[:16] if isinstance(path, str)]
    delivery = fn_args.get('delivery')
    has_delivery = isinstance(delivery, str) and bool(delivery.strip())
    _risk(
        approval_meta,
        ('Project-relative artifact paths', '\n'.join(selected)),
        ('Requested final delivery', 'yes' if has_delivery else 'no'),
        ('Delivery reply preview', delivery[:2000] if has_delivery else None),
        ('Delivery reply characters', len(delivery) if has_delivery else None),
        note='Freeze owner-bound artifact snapshots. A requested final reply '
             'still requires verified execution; independent review decides acceptance.',
    )


def _approval_meta_kill_background(approval_meta, fn_args):
    """Show exactly which background process tree is being killed."""
    _risk(
        approval_meta,
        ('command_id', fn_args.get('command_id')),
        note='The process tree receives SIGTERM then SIGKILL.',
    )


def _approval_meta_kill(approval_meta, fn_args):
    """Unified kill: show which background handle is being stopped."""
    handle = fn_args.get('id') or fn_args.get('command_id')
    ids = fn_args.get('ids')
    _risk(
        approval_meta,
        ('id', handle or (', '.join(str(i) for i in ids) if ids else None)),
        note='Background commands receive SIGTERM then SIGKILL; running '
             'swarm agents are aborted.',
    )


def _approval_meta_manage_agents(approval_meta, fn_args):
    """Show the identity, action and delegated objective before team changes."""
    action = str(fn_args.get('action') or '')
    _risk(
        approval_meta,
        ('Action', action),
        ('Scope', fn_args.get('scope') or 'current project/conversation'),
        ('Agent ID', fn_args.get('agent_id')),
        ('Source child ID', fn_args.get('source_agent_id')),
        ('Name', fn_args.get('name')),
        ('Role', fn_args.get('role')),
        ('Objective', fn_args.get('objective')),
        ('Abandon active run', 'yes' if fn_args.get('abandon') is True else None),
        note=(
            'This action changes a reusable agent record or launches a child. '
            'The child uses the current task permissions.'),
    )


def _approval_meta_coordinator_work(approval_meta, fn_args):
    """Show the delegated scope or exact reviewed checkpoint before approval."""
    from lib.project_dispatch.repository import CONTRACT
    defaults = CONTRACT['delegationDefaults']
    _risk(approval_meta,
        ('Worker', fn_args.get('title')), ('Objective', fn_args.get('brief')),
        ('Token allowance', fn_args.get('max_tokens', defaults['workerTokens']) if 'brief' in fn_args else None),
        ('Model requests', fn_args.get('max_model_requests', defaults['workerModelRequests']) if 'brief' in fn_args else None),
        ('Assignment', fn_args.get('assignment_id')), ('Result revision', fn_args.get('result_revision')),
        ('Decision', fn_args.get('decision') or fn_args.get('outcome')), ('Report', fn_args.get('report_turn_id')), ('Evidence', fn_args.get('note')),
        ('Checkpoint to integrate', fn_args.get('checkpoint_sha')),
        note='Independent project work under the current request; publication uses existing verification gates.')


# Module-level dispatch table — maps tool name → approval meta enricher.
# Only tools that need special approval metadata are listed; tools not in
# this dict get the base metadata only (path + description).
_APPROVAL_META_ENRICHERS = {
    'cancel_project_work': _approval_meta_coordinator_work,
    'finish_project_request': _approval_meta_coordinator_work,
    'delegate_project_work': _approval_meta_coordinator_work,
    'review_project_work': _approval_meta_coordinator_work,
    'manage_agents': _approval_meta_manage_agents,
    'evolution_artifact': _approval_meta_evolution_artifact,
    'evolution_coordinate': _approval_meta_evolution,
    'evolution_material': _approval_meta_evolution,
    'evolution_teach': _approval_meta_evolution,
    'evolution_reindex': _approval_meta_evolution,
    'evolution_explore': _approval_meta_evolution,
    'kill_background_command': _approval_meta_kill_background,
    'kill':               _approval_meta_kill,
    'run_command':      _approval_meta_run_command,
    'request_skill_install': _approval_meta_request_skill_install,
    'write_file':       _approval_meta_write_file,
    'edit_file':        _approval_meta_edit_file,
    'apply_diff':       _approval_meta_apply_diff,
    'apply_diffs':      _approval_meta_apply_diffs,
    'insert_content':   _approval_meta_insert_content,
    'insert_contents':  _approval_meta_insert_contents,
    # Newly write-partitioned families. Without an enricher the prompt renders
    # a bare tool name and the user approves blind — false confidence, worse
    # than not prompting at all.
    'browser_execute_js':     _approval_meta_browser_execute_js,
    'browser_devtools':       _approval_meta_browser_devtools,
    'browser_navigate':       _approval_meta_browser_navigate,
    'browser_research_page':  _approval_meta_browser_research_page,
    'browser_site':           _approval_meta_browser_site,
    'browser_fill_form':      _approval_meta_browser_fill_form,
    'schedule_create':        _approval_meta_schedule_create,
    'schedule_manage':        _approval_meta_schedule_manage,
    'timer_create':           _approval_meta_timer_create,
    'timer_manage':           _approval_meta_timer_manage,
    # ── browser interaction: the selector/target is the risk ──
    'browser_click':            _approval_meta_browser_click,
    'browser_close_tab':        _approval_meta_browser_close_tab,
    'browser_type':             _approval_meta_browser_type,
    'browser_press_key':        _approval_meta_browser_press_key,
    'browser_check':            _approval_meta_browser_check,
    'browser_select_option':    _approval_meta_browser_select_option,
    'browser_menu_click':       _approval_meta_browser_menu_click,
    # ── desktop: runs on the user's own machine ──
    'desktop_run_command':      _approval_meta_desktop_run_command,
    'desktop_write_file':       _approval_meta_desktop_write_file,
    'desktop_open_file':        _approval_meta_desktop_open_file,
    'desktop_open_app':         _approval_meta_desktop_open_app,
    # ── memory CRUD: irreversible edits to stored notes ──
    'record_working_fact':      _approval_meta_record_working_fact,
    'create_memory':            _approval_meta_create_memory,
    'update_memory':            _approval_meta_update_memory,
    'delete_memory':            _approval_meta_delete_memory,
    'merge_memories':           _approval_meta_merge_memories,
    # ── motion_video: overwrites output files, burns CPU/TTS quota ──
    'motion_video_render':      _approval_meta_motion_video_render,
    'motion_video_concat':      _approval_meta_motion_video_concat,
    'motion_video_mux':         _approval_meta_motion_video_mux,
    'motion_video_narrate':     _approval_meta_motion_video_narrate,
    # ── search settings: server-wide config mutation ──
    'update_search_settings':   _approval_meta_update_search_settings,
    # ── local_serve: installs engines / spawns & kills managed servers ──
    'local_serve_deploy':         _approval_meta_local_serve_deploy,
    'local_serve_stop':           _approval_meta_local_serve_stop,
    'local_serve_remove':         _approval_meta_local_serve_remove,
    # ── progressive MCP: remote target + payload are otherwise hidden ──
    'call_mcp_write_tool':       _approval_meta_call_mcp_write_tool,
    # ── paper: network download + new workspace directory ──
    'paper_import_arxiv':        _approval_meta_paper_import_arxiv,
    'paper_cite_insert':         _approval_meta_paper_cite_insert,
}


def _handle_approval(
    task: dict[str, Any],
    fn_name: str,
    fn_args: dict[str, Any],
    rn: int,
    round_entry: dict[str, Any],
    project_path: str | None,
    round_num: int,
    model: str,
    cfg: dict[str, Any] | None = None,
    mint_receipt: bool = False,
    file_recovery=None,
) -> tuple[bool, str | None]:
    """Gate a write operation on manual user approval (no execution).

    Emits a ``write_approval_request`` event and blocks waiting for the user
    response. This function ONLY decides — it never executes the tool. On
    approval the caller lets the tool fall through to the normal serial
    write-tool dispatch, so a single execution path serves project writes,
    run_command, memory, MCP, and custom write tools.

    Uses the :data:`_APPROVAL_META_ENRICHERS` dispatch table to build
    tool-specific approval metadata.

    Returns
    -------
    tuple[bool, str | None]
        ``(approved, reject_content)``. When approved, ``(True, None)`` and
        ``round_entry`` is left ``pending_approval`` for the executor to
        finalize. When rejected, ``(False, message)`` and the round has
        already been finalized with a ``rejected`` badge.
    """
    from lib.tasks_pkg.invocation import is_child_invocation
    if is_child_invocation(task):
        # No human-interaction port was delegated. Never wait on an invisible
        # approval or treat unattended execution as permission to auto-approve.
        tool_content = (
            f'`{fn_name}` requires human approval unavailable to this invocation. '
            'Report the blocked operation to the parent without executing it.')
        stamp_tool_rejection(
            round_entry, {'kind': 'approval_unavailable', 'tool': fn_name},
            reason=tool_content, retryable=False)
        meta = build_tool_result_meta(fn_name, fn_args, tool_content)
        meta.update(badge='rejected', writeOk=False)
        _finalize_tool_round(task, rn, round_entry, [meta])
        return False, tool_content

    # This seam is reached only after the pipeline's current admission and
    # child/unattended/Git policy. Keep the exact receipt for handler consume.
    if file_recovery is None and approval_receipt_authority(
            task, fn_name, round_entry.get('toolCallId'), fn_args):
        return True, None
    tid = task['id'][:8]
    approval_id = f'{task["id"]}_{uuid.uuid4().hex[:8]}'
    approval_meta = {
        'approvalId': approval_id,
        'toolName': fn_name,
        'path': fn_args.get('path', ''),
        'description': fn_args.get('description', ''),
    }

    enrich_approval_metadata(approval_meta, fn_name, fn_args)
    if file_recovery is not None:
        explanations = {
            'missing_evidence': 'The saved publication receipt is missing.',
            'evidence_conflict': 'Saved evidence does not match the current file or project.',
            'unsupported': 'This operation has no supported publication proof.',
        }
        unresolved = '\n\n'.join(
            f"Earlier write {index}: {explanations.get(reason, 'Publication could not be established.')}\n"
            f"Operation: {operation}\nProducing {'invocation' if kind == 'invocation' else 'Turn'}: {producer}"
            for index, (_, kind, producer, operation, _, reason) in enumerate(file_recovery.unknown, 1))
        _risk(approval_meta,
            ('Unresolved earlier writes', unresolved),
            ('Requested new write', json.dumps(fn_args, ensure_ascii=False, indent=2)),
            note='Earlier writes may already have taken effect. Approving permits only this new write, '
                 'which may duplicate or replace earlier work; it does not resolve or replay the earlier operation.')

    round_entry['status'] = 'pending_approval'
    round_entry['approvalId'] = approval_id
    round_entry['approvalMeta'] = approval_meta

    def publish_request():
        append_event(task, build_event(
            EventType.WRITE_APPROVAL_REQUEST,
            roundNum=rn,
            toolCallId=round_entry.get('toolCallId', ''),
            approvalId=approval_id,
            meta=approval_meta,
        ))
    logger.debug(
        '[Task %s] Waiting for write approval: tool=%s path=%s round=%d model=%s',
        tid, fn_name, fn_args.get('path', ''), round_num, model,
    )

    from lib.tasks_pkg.manager import task_user_id
    approved = request_write_approval(
        approval_id,
        timeout=120,
        owner_user_id=task_user_id(task),
        task=task,
        on_registered=publish_request,
    )

    if task.get('aborted'):
        # The pipeline owns aborted settlement; never mint a grant or a
        # rejection for a task stopped while the approval dialog was open.
        return False, 'Task aborted by user.'

    if not approved:
        tool_content = f'⚠️ User rejected this {fn_name} operation on {fn_args.get("path", "")}.'
        stamp_tool_rejection(
            round_entry,
            {'kind': 'approval_denied', 'tool': fn_name},
            reason=tool_content, retryable=True,
        )
        meta = build_tool_result_meta(fn_name, fn_args, tool_content)
        meta['badge'] = 'rejected'
        meta['writeOk'] = False
        _finalize_tool_round(task, rn, round_entry, [meta])
        return False, tool_content

    # Approved — do NOT execute here. The caller lets the item fall through to
    # the normal serial write-tool dispatch (_execute_tool_one), so a single
    # execution path serves project writes, run_command, memory, MCP, and custom
    # write tools uniformly. round_entry stays 'pending_approval' until that
    # execution finalizes it.
    logger.info('[Task %s] Write approved: tool=%s — dispatching via normal path',
                tid, fn_name)
    # Browser approval is a durable exact-domain grant. Subsequent page writes
    # for the same user + client/profile run directly; another domain or a
    # revoked grant asks again. The generic execution layer independently
    # enforces the grant, so unattended tasks cannot bypass this by skipping
    # the UI gate.
    if fn_name.startswith('browser_'):
        try:
            from lib.browser.access import browser_tool_access
            browser_tool_access(
                fn_name, fn_args,
                owner_user_id=str(task.get('_userId') or ''),
                client_id=str((cfg or {}).get('browserClientId') or ''),
                grant_on_success=True)
        except Exception as exc:
            logger.warning('[Task %s] browser domain grant could not be saved: %s',
                           tid, exc)
    if mint_receipt:
        tc_id = str(round_entry.get('toolCallId') or '')
        if not mint_approval_receipt(
                task, fn_name, tc_id, fn_args, authority='human'):
            logger.error('[Task %s] approved %s has no tool call id',
                         tid, fn_name)
            return False, 'Approval could not be bound to this tool call.'
        if file_recovery is not None:
            task['_tool_approval_receipts'][tc_id]['fileRecoveryEvidence'] = file_recovery.fingerprint
    return True, None
