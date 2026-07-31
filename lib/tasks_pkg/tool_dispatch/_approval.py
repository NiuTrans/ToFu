# HOT_PATH
"""Write-approval gating — metadata enrichers + the ``_handle_approval`` gate.

Registry pattern: each tool type that needs approval has a dedicated
enricher function that augments the base ``approval_meta`` dict.  The
``_handle_approval`` gate emits the ``write_approval_request`` event and
blocks waiting for the user's decision — it NEVER executes the tool.
"""

from __future__ import annotations

import uuid
from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.approval import request_write_approval
from lib.tasks_pkg.executor import _finalize_tool_round
from lib.tasks_pkg.manager import append_event
from lib.tools import build_project_tool_meta

logger = get_logger(__name__)


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


def _approval_meta_create_project(approval_meta, fn_args):
    """Enrich approval metadata for ``create_project``.

    Registering a workspace root widens where every later write may land, and
    ``overwrite=true`` accepts a NON-EMPTY existing dir — both belong in the
    dialog.
    """
    target_path = fn_args.get('path', '')
    root_name = fn_args.get('name', '')
    overwrite = bool(fn_args.get('overwrite', False))
    approval_meta['path'] = target_path
    approval_meta['rootName'] = root_name
    approval_meta['overwrite'] = overwrite
    _risk(
        approval_meta,
        ('Workspace root path', target_path),
        ('Root name', root_name),
        ('overwrite (accept non-empty dir)', 'true' if overwrite else None),
        note=('Register a new workspace root — later writes may target it'
              + (' (overwrite=true)' if overwrite else '')),
    )


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


def _approval_meta_browser_navigate(approval_meta, fn_args):
    """Enrich approval metadata for ``browser_navigate`` (destination URL)."""
    url = fn_args.get('url', '') or ''
    approval_meta['path'] = url
    approval_meta['url'] = url
    _risk(approval_meta, ('Destination URL', url),
          note='Navigate the browser to this URL')


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


def _approval_meta_charter_commit(approval_meta, fn_args):
    """Enrich approval metadata for ``project_charter_commit``.

    The committed text becomes shared intent injected into EVERY sibling
    conversation of the project, so it gets the widest blast radius in this
    family — show the full decision text.
    """
    decision = fn_args.get('decision', '') or ''
    approval_meta['decisionChars'] = len(decision)
    _risk(
        approval_meta,
        ('Decision text (every sibling conversation reads it)', decision),
        note='Commit a project-wide charter decision',
    )


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


def _approval_meta_browser_hover_and_click(approval_meta, fn_args):
    """``browser_hover_and_click`` — hover target THEN click target.

    Two selectors, and the CLICK one is what commits the action; both are
    shown because a wrong hover target silently changes which menu opens.
    """
    click_sel = fn_args.get('click_selector', '') or ''
    approval_meta['path'] = click_sel
    _risk(
        approval_meta,
        ('Element to click', click_sel),
        ('Hover first', fn_args.get('hover_selector') or None),
        ('Tab', fn_args.get('tab_id') or None),
        note='Hover then click an element in your browser page',
    )


def _approval_meta_browser_right_click_menu(approval_meta, fn_args):
    """``browser_right_click_menu`` — target + which context-menu item."""
    item = fn_args.get('menu_item_text', '') or ''
    target = fn_args.get('target_selector', '') or ''
    approval_meta['path'] = target
    _risk(
        approval_meta,
        ('Context-menu item to activate', item),
        ('Target element', target),
        ('Submenu item', fn_args.get('submenu_item_text') or None),
        ('Tab', fn_args.get('tab_id') or None),
        note='Open a context menu and activate an item',
    )


def _approval_meta_browser_keyboard(approval_meta, fn_args):
    """``browser_keyboard`` — the keystrokes being injected.

    Keys can submit a form (Enter) or trigger a browser/OS shortcut, so the
    literal key sequence is the risk.
    """
    keys = fn_args.get('keys', '') or ''
    _risk(
        approval_meta,
        ('Keys to send', keys),
        ('Focus element', fn_args.get('selector') or None),
        ('Tab', fn_args.get('tab_id') or None),
        note='Send synthetic keystrokes to your browser page',
    )


def _approval_meta_browser_create_tab(approval_meta, fn_args):
    """``browser_create_tab`` — the URL that will be opened."""
    url = fn_args.get('url', '') or ''
    approval_meta['path'] = url
    approval_meta['url'] = url
    _risk(
        approval_meta,
        ('URL to open in a new tab', url),
        ('Focus the new tab', 'true' if fn_args.get('active') else None),
        note='Open a new browser tab',
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


# Module-level dispatch table — maps tool name → approval meta enricher.
# Only tools that need special approval metadata are listed; tools not in
# this dict get the base metadata only (path + description).
_APPROVAL_META_ENRICHERS = {
    'run_command':      _approval_meta_run_command,
    'write_file':       _approval_meta_write_file,
    'apply_diff':       _approval_meta_apply_diff,
    'apply_diffs':      _approval_meta_apply_diffs,
    'insert_content':   _approval_meta_insert_content,
    'insert_contents':  _approval_meta_insert_contents,
    'create_project':   _approval_meta_create_project,
    # Newly write-partitioned families. Without an enricher the prompt renders
    # a bare tool name and the user approves blind — false confidence, worse
    # than not prompting at all.
    'browser_execute_js':     _approval_meta_browser_execute_js,
    'browser_navigate':       _approval_meta_browser_navigate,
    'browser_fill_form':      _approval_meta_browser_fill_form,
    'schedule_create':        _approval_meta_schedule_create,
    'schedule_manage':        _approval_meta_schedule_manage,
    'timer_create':           _approval_meta_timer_create,
    'timer_manage':           _approval_meta_timer_manage,
    'project_charter_commit': _approval_meta_charter_commit,
    # ── browser interaction: the selector/target is the risk ──
    'browser_click':            _approval_meta_browser_click,
    'browser_hover_and_click':  _approval_meta_browser_hover_and_click,
    'browser_right_click_menu': _approval_meta_browser_right_click_menu,
    'browser_keyboard':         _approval_meta_browser_keyboard,
    'browser_create_tab':       _approval_meta_browser_create_tab,
    'browser_close_tab':        _approval_meta_browser_close_tab,
    # ── desktop: runs on the user's own machine ──
    'desktop_run_command':      _approval_meta_desktop_run_command,
    'desktop_write_file':       _approval_meta_desktop_write_file,
    'desktop_open_file':        _approval_meta_desktop_open_file,
    'desktop_open_app':         _approval_meta_desktop_open_app,
    # ── memory CRUD: irreversible edits to stored notes ──
    'create_memory':            _approval_meta_create_memory,
    'update_memory':            _approval_meta_update_memory,
    'delete_memory':            _approval_meta_delete_memory,
    'merge_memories':           _approval_meta_merge_memories,
    # ── motion_video: overwrites output files, burns CPU/TTS quota ──
    'motion_video_render':      _approval_meta_motion_video_render,
    'motion_video_concat':      _approval_meta_motion_video_concat,
    'motion_video_mux':         _approval_meta_motion_video_mux,
    'motion_video_narrate':     _approval_meta_motion_video_narrate,
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
    tid = task['id'][:8]
    approval_id = f'{task["id"]}_{uuid.uuid4().hex[:8]}'
    approval_meta = {
        'approvalId': approval_id,
        'toolName': fn_name,
        'path': fn_args.get('path', ''),
        'description': fn_args.get('description', ''),
    }

    # Dispatch to tool-specific enricher (if one exists)
    enricher = _APPROVAL_META_ENRICHERS.get(fn_name)
    if enricher is not None:
        enricher(approval_meta, fn_args)

    round_entry['status'] = 'pending_approval'
    round_entry['approvalId'] = approval_id
    round_entry['approvalMeta'] = approval_meta
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

    approved = request_write_approval(approval_id, timeout=120)

    if not approved:
        tool_content = f'⚠️ User rejected this {fn_name} operation on {fn_args.get("path", "")}.'
        meta = build_project_tool_meta(fn_name, fn_args, tool_content)
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
    return True, None
