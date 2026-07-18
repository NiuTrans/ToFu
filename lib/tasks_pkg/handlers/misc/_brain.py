# HOT_PATH
"""Cross-conversation "brain" misc handlers: past-conversation reference,
project charter, project board, live peer status / messaging / intervention.

``append_event`` is resolved THROUGH the package facade
(``lib.tasks_pkg.handlers.misc``) at call time so a test patching
``lib.tasks_pkg.handlers.misc.append_event`` steers ``_make_intervention_approval_fn``
(and therefore ``_handle_peer_tool``) exactly as before the package split.
"""

from __future__ import annotations

from lib.conv_ref import execute_conv_ref_tool
from lib.log import get_logger
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.handlers._adapter import simple_call
from lib.tools import (
    BOARD_TOOL_NAMES,
    CHARTER_TOOL_NAMES,
    CONV_REF_TOOL_NAMES,
    PEER_TOOL_NAMES,
)

logger = get_logger(__name__)


def _append_event(task, ev):
    """Route append_event through the package facade so monkeypatching
    ``lib.tasks_pkg.handlers.misc.append_event`` remains effective."""
    from lib.tasks_pkg.handlers import misc as _facade
    return _facade.append_event(task, ev)


@tool_registry.tool_set(CONV_REF_TOOL_NAMES, category='conversations',
                        description='List and retrieve past conversations')
def _handle_conv_ref_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    current_conv_id = task.get('convId')

    def _run(_fn_name, _fn_args):
        return execute_conv_ref_tool(
            _fn_name, _fn_args,
            current_conv_id=current_conv_id,
            project_path=project_path,
        )

    detail = fn_args.get('keyword', 'all') if fn_name == 'list_conversations' else fn_args.get('conversation_id', '?')[:8]

    def _post_build(meta, _tool_content, _fn_args):
        """Attach a STRUCTURED conversation digest for get_conversation so the
        frontend renders a clean human-view card instead of dumping the raw
        ``═══`` / ``── User Message #`` transcript as Markdown. Read off the DB
        (never re-parsed from the prose result); the verbatim transcript stays
        available via the row's "model view" affordance. Skipped for raw-mode
        reads (the debugging JSON dump is meant to be shown verbatim)."""
        if fn_name != 'get_conversation' or _fn_args.get('raw'):
            return
        conv_id = (_fn_args.get('conversation_id') or '').strip()
        if not conv_id:
            return
        try:
            from lib.conv_ref import build_conversation_digest
            digest = build_conversation_digest(conv_id, current_conv_id=current_conv_id)
        except Exception as e:
            logger.debug('[ConvRef] digest build failed for %s: %s', conv_id, e)
            return
        if digest:
            meta['convDigest'] = digest

    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_run,
        source='Conversations', module_tag='ConvRef',
        title=f'{fn_name}: {detail}',
        post_build=_post_build,
    )


@tool_registry.tool_set(CHARTER_TOOL_NAMES, category='conversations',
                        description='Read / propose / commit project charter decisions (north star)')
def _handle_charter_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    current_conv_id = task.get('convId', '')

    def _run(_fn_name, _fn_args):
        from lib.conversations.project_charter import execute_charter_tool
        return execute_charter_tool(
            _fn_name, _fn_args,
            current_conv_id=current_conv_id,
            project_path=project_path if project_enabled else '')

    verb = {'project_charter_read': 'read',
            'project_charter_propose': 'propose',
            'project_charter_commit': 'commit'}.get(fn_name, 'charter')
    # Structured enrichment (rendered off engine/args data, NOT re-parsed prose):
    # a propose carries the proposal text + a pending-human-review marker so the
    # frontend can render a distinct "awaiting review" affordance.
    _extra = None
    if fn_name == 'project_charter_propose':
        _extra = {'charterProposal': {
            'proposal': (fn_args.get('proposal') or '').strip(),
            'title': (fn_args.get('title') or '').strip(),
            'pending': True,
        }}
    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_run,
        source='Charter', module_tag='Charter', badge=verb, extra=_extra,
    )


@tool_registry.tool_set(BOARD_TOOL_NAMES, category='conversations',
                        description='Read / post / claim / complete / block project board epics')
def _handle_board_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    current_conv_id = task.get('convId', '')

    def _run(_fn_name, _fn_args):
        from lib.conversations.project_board import execute_board_tool
        return execute_board_tool(
            _fn_name, _fn_args,
            current_conv_id=current_conv_id,
            project_path=project_path if project_enabled else '')

    _verb = fn_name.replace('project_board_', '', 1)

    def _post_build(meta, _tool_content, _fn_args):
        """Attach a STRUCTURED board snapshot (read) or transition (mutation),
        read off the engine — never re-parsed from the prose result."""
        if not project_enabled or not project_path:
            return
        try:
            from lib.conversations.project_board import read_board
            board = read_board(project_path)
        except Exception as e:
            logger.debug('[Board] post_build read failed: %s', e)
            return
        if fn_name == 'project_board_read':
            # Compact mini-kanban: counts + lane epic titles (structured).
            lanes = {'open': [], 'claimed': [], 'done': []}
            for tk in board.get('tasks', []):
                lanes.setdefault(tk.get('status', 'open'), []).append({
                    'id': tk.get('id', ''), 'title': tk.get('title', ''),
                    'owner': tk.get('owner_conv_id', ''),
                    'dispatched': bool(tk.get('dispatched')),
                })
            meta['boardSnapshot'] = {
                'open': board.get('open', 0), 'claimed': board.get('claimed', 0),
                'done': board.get('done', 0),
                'lanes': lanes,
            }
        else:
            # Mutation → an explicit transition (verb + target epic + status).
            tid = (_fn_args.get('task_id') or '').strip()
            title = ''
            status = ''
            for tk in board.get('tasks', []):
                if tk.get('id') == tid:
                    title = tk.get('title', '')
                    status = tk.get('status', '')
                    break
            meta['boardTransition'] = {
                'verb': _verb, 'taskId': tid, 'title': title, 'status': status,
            }

    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_run,
        source='Board', module_tag='Board', badge=_verb,
        post_build=_post_build,
    )


def _make_intervention_approval_fn(task, rn, tc_id, round_entry):
    """Build the human-approval callback for a coercive peer hard-abort.

    Returns ``approval_fn(prompt) -> approver | None`` that routes the request
    through the SAME human-guidance seam ``ask_human`` uses: it emits a
    ``human_guidance_request`` choice event (Approve / Deny) the UI already
    renders + resolves, then BLOCKS on ``request_human_guidance`` until the
    human decides (or the task aborts). Grant → returns the approver identity
    (the resolving user, or 'human'); deny/abort → returns None.

    Under AUTOPILOT a coercive kill of another conversation is NEVER
    auto-authorized (the VU may freely answer questions, but must not silently
    green-light stopping a sibling) → returns None (advisory fallback).
    """
    from lib.ids import short_id

    def _approval_fn(prompt: str):
        from lib.tasks_pkg.autopilot import is_autopilot_enabled
        if is_autopilot_enabled(task):
            logger.info('[Peer] hard-abort auto-DENIED under autopilot task=%s',
                        task.get('id', '?')[:8])
            return None
        guidance_id = short_id('hg_', 12)
        options = [{'label': 'Approve abort', 'value': 'approve'},
                   {'label': 'Deny', 'value': 'deny'}]
        round_entry['status'] = 'awaiting_human'
        round_entry['guidanceId'] = guidance_id
        round_entry['guidanceQuestion'] = prompt
        round_entry['guidanceType'] = 'choice'
        round_entry['guidanceOptions'] = options
        _append_event(task, {
            'type': 'human_guidance_request',
            'roundNum': rn,
            'toolCallId': tc_id,
            'guidanceId': guidance_id,
            'question': prompt,
            'responseType': 'choice',
            'options': options,
            'intervention': True,
        })
        from lib.tasks_pkg.human_guidance import request_human_guidance
        resp = request_human_guidance(guidance_id, task=task)
        if resp is None:
            return None  # task aborted while waiting
        rl = str(resp).strip().lower()
        approved = ('approve' in rl or rl in ('yes', 'ok', 'y', 'approved')) \
            and not rl.startswith('deny') and rl not in ('no', 'n')
        if not approved:
            return None
        # Stamp the approver identity for the audit_log('intervention', …).
        who = str(resp).strip()
        return who if who and 'approve' not in who.lower() else 'human'

    return _approval_fn


@tool_registry.tool_set(PEER_TOOL_NAMES, category='conversations',
                        description='Live peer status / peer messaging / advisory intervention')
def _handle_peer_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    current_conv_id = task.get('convId', '')

    # Only project_intervene(hard_abort=True) needs the human-approval seam;
    # build it lazily so status/message paths carry no approval overhead.
    approval_fn = None
    if fn_name == 'project_intervene' and bool(fn_args.get('hard_abort')):
        approval_fn = _make_intervention_approval_fn(task, rn, tc_id, round_entry)

    def _run(_fn_name, _fn_args):
        from lib.conversations.project_peer import execute_peer_tool
        return execute_peer_tool(
            _fn_name, _fn_args,
            current_conv_id=current_conv_id,
            project_path=project_path if project_enabled else '',
            config=cfg, approval_fn=approval_fn)

    _verb = {'project_peer_status': 'status',
             'project_feed_read': 'feed',
             'project_message': 'message',
             'project_intervene': 'intervene'}.get(fn_name, 'peer')

    def _post_build(meta, _tool_content, _fn_args):
        """Attach STRUCTURED meta (read off the engine, never re-parsed prose):
        the live peer list for ``project_peer_status``, the recent activity
        events for ``project_feed_read``, and a delivery descriptor for
        ``project_message`` / ``project_intervene``."""
        if not project_enabled or not project_path:
            return
        # ── project_peer_status → live peer cards ──
        if fn_name == 'project_peer_status':
            try:
                from lib.conversations.project_peer import build_peer_status
                status = build_peer_status(project_path, current_conv_id)
            except Exception as e:
                logger.debug('[Peer] post_build status failed: %s', e)
                return
            target = (_fn_args.get('conv_id') or '').strip()
            peers = status.get('peers', [])
            if target:
                peers = [p for p in peers if (p.get('convId', '') or '').startswith(target)]
            meta['peerStatus'] = {
                'count': len(peers),
                'peers': [{
                    'convId': p.get('convId', ''),
                    'agentId': p.get('agentId', ''),
                    'title': p.get('title', ''),
                    'statusLabel': p.get('statusLabel', ''),
                    'round': p.get('round', 0),
                    'currentFile': p.get('currentFile', ''),
                    'claimedEpic': p.get('claimedEpic', ''),
                } for p in peers],
            }
            return
        # ── project_feed_read → chronological activity events ──
        if fn_name == 'project_feed_read':
            try:
                limit = int(_fn_args.get('limit') or 25)
            except (TypeError, ValueError) as e:
                logger.debug('[Peer] feed_read limit=%r not an int (%s) — '
                             'using default 25', _fn_args.get('limit'), e)
                limit = 25
            limit = max(1, min(limit, 60))
            try:
                from lib.conversations.project_feed import read_project_feed
                feed = read_project_feed(project_path, limit=limit)
            except Exception as e:
                logger.debug('[Peer] post_build feed failed: %s', e)
                return
            events = feed.get('events', []) or []
            # Backfill a human-readable title for events whose stored title is
            # empty (task-lifecycle started/completed/aborted are emitted with
            # no title) so the card never shows a bare `conv <id>`. Same
            # DB-backed resolver build_peer_status uses (real title, else a
            # snippet of the opening user turn — never an id).
            need = list({ev.get('conv_id') for ev in events
                         if not (ev.get('title') or '').strip() and ev.get('conv_id')})
            titles = {}
            if need:
                try:
                    from lib.conversations.project_peer import _titles_by_conv
                    titles = _titles_by_conv(need)
                except Exception as e:
                    logger.debug('[Peer] feed title backfill failed: %s', e)
            meta['feedActivity'] = {
                'count': len(events),
                'events': [{
                    'kind': ev.get('kind', 'note'),
                    'title': (ev.get('title') or '').strip()
                    or titles.get(ev.get('conv_id'), ''),
                    'convId': ev.get('conv_id', ''),
                    # Forward the FULL summary — the feed row caps its DISPLAY
                    # summary at _SUMMARY_MAX_CHARS but preserves the untruncated
                    # text in payload['summary_full']; showing the capped value
                    # cut sentences off mid-word.
                    'summary': (ev.get('payload') or {}).get('summary_full')
                    or ev.get('summary', ''),
                    'ts': ev.get('ts', 0),
                    'mine': bool(ev.get('conv_id') and ev.get('conv_id') == current_conv_id),
                } for ev in events],
            }
            return
        # ── project_message / project_intervene → delivery descriptor ──
        if fn_name in ('project_message', 'project_intervene'):
            to = (_fn_args.get('to_conv_id') or '').strip()
            text = (_fn_args.get('text') or _fn_args.get('message') or '').strip()
            content = _tool_content if isinstance(_tool_content, str) else str(_tool_content)
            low = content.lower()
            # Classify the outcome off the well-known result-string phrasing.
            if low.startswith('error') or 'was denied' in low or 'requires explicit human' in low:
                outcome = 'failed'
            elif 'not sent' in low or 'rate limit' in low:
                outcome = 'rate_limited'
            elif 'denied' in low:
                outcome = 'denied'
            else:
                outcome = 'delivered'
            hard = bool(_fn_args.get('hard_abort')) if fn_name == 'project_intervene' else False
            meta['peerDelivery'] = {
                'tool': fn_name,
                'toConv': to,
                'text': text,
                'hardAbort': hard,
                'outcome': outcome,
            }
            return

    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_run,
        source='Peer', module_tag='Peer', badge=_verb,
        post_build=_post_build,
    )
