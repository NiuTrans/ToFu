# HOT_PATH
"""Miscellaneous tool handlers: ask_human, scheduler, desktop, swarm, conv_ref."""

from __future__ import annotations

import os

from lib.conv_ref import execute_conv_ref_tool
from lib.desktop_tools import DESKTOP_TOOL_NAMES
from lib.log import get_logger
from lib.scheduler import SCHEDULER_TOOL_NAMES, execute_scheduler_tool
from lib.swarm.tools import SWARM_TOOL_NAMES
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round, tool_registry
from lib.tasks_pkg.handlers._adapter import simple_call
from lib.tasks_pkg.manager import append_event
from lib.tools import (
    BOARD_TOOL_NAMES,
    CHARTER_TOOL_NAMES,
    CONV_REF_TOOL_NAMES,
    PEER_TOOL_NAMES,
)

logger = get_logger(__name__)


# ── Shared constant: application root ──
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@tool_registry.handler('ask_human', category='human_guidance',
                       description='Ask the user a question and wait for their response')
def _handle_ask_human(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    """Handle ask_human tool — block indefinitely until user responds."""
    import uuid as _uuid

    from lib.tasks_pkg.human_guidance import request_human_guidance

    question = fn_args.get('question', '')
    response_type = fn_args.get('response_type', 'free_text')
    options = fn_args.get('options', [])
    # ★ Defensive: models sometimes return options as a JSON string, a dict,
    #   or omit it entirely. Normalize to a list of dicts so the frontend's
    #   options.map(…) call can never crash on a string/object.
    if isinstance(options, str):
        try:
            import json as _json
            options = _json.loads(options)
        except (ValueError, TypeError) as _e:
            logger.warning('[Executor] ask_human: options arrived as a '
                           'non-JSON string, coercing to []: %s', _e)
            options = []
    if not isinstance(options, list):
        logger.warning('[Executor] ask_human: options not a list '
                       '(type=%s), coercing to []',
                       type(options).__name__)
        options = []
    # Normalise each option to a dict so the frontend receives a
    # uniform shape even if the model emitted bare strings.
    _norm_opts = []
    for _o in options:
        if isinstance(_o, dict):
            _norm_opts.append(_o)
        elif isinstance(_o, str):
            _norm_opts.append({'label': _o})
        else:
            logger.debug('[Executor] ask_human: dropping non-dict/str '
                         'option of type=%s', type(_o).__name__)
    options = _norm_opts

    if not question:
        logger.warning('[Executor] ask_human called with empty question, task=%s',
                       task.get('id', '?')[:8])
        tool_content = 'Error: question parameter is required.'
        meta = _build_simple_meta(
            fn_name, tool_content, source='HumanGuidance',
            title='Missing question', snippet='No question provided',
            badge='error',
        )
        _finalize_tool_round(task, rn, round_entry, [meta])
        return tc_id, tool_content, False

    guidance_id = f'hg_{_uuid.uuid4().hex[:12]}'
    logger.info('[Executor] ask_human: question=%.200s, type=%s, '
                'options=%d, guidance_id=%s, task=%s',
                question, response_type, len(options), guidance_id,
                task.get('id', '?')[:8])

    round_entry['status'] = 'awaiting_human'
    round_entry['guidanceId'] = guidance_id
    round_entry['guidanceQuestion'] = question
    round_entry['guidanceType'] = response_type
    round_entry['guidanceOptions'] = options
    append_event(task, {
        'type': 'human_guidance_request',
        'roundNum': rn,
        'toolCallId': tc_id,
        'guidanceId': guidance_id,
        'question': question,
        'responseType': response_type,
        'options': options,
    })

    # ── Autopilot: simulated user answers the question instead of blocking ──
    # When autopilot is on, we don't want to wait for a real human; the VU
    # answers using the conversation context.  We still emit the synthetic
    # request → response pair so the frontend renders the autopilot bubble
    # in place of a normal ask_human prompt.
    from lib.tasks_pkg.autopilot import is_autopilot_enabled, run_virtual_user
    if is_autopilot_enabled(task):
        logger.info('[Executor] ask_human → routed to Autopilot VU '
                    '(no human wait): guidance_id=%s, task=%s',
                    guidance_id, task.get('id', '?')[:8])
        # Build a minimal augmented messages list so the VU sees the
        # exact question being asked, not just the last assistant turn.
        _vu_task = task
        try:
            _orig_msgs = list(task.get('messages') or [])
            _augmented = list(_orig_msgs)
            _augmented.append({'role': 'assistant', 'content': question})
            _vu_task = dict(task)
            _vu_task['messages'] = _augmented
        except Exception as _e:
            logger.debug('[Executor] ask_human VU augmentation failed '
                         '(falling back to raw history): %s', _e)
        vu_reply = run_virtual_user(_vu_task)
        if vu_reply is None:
            user_response = None  # task aborted or VU stopped
        else:
            user_response = vu_reply or '(no further input)'
            # Resolve so the SSE event consumer (frontend) sees a synthetic
            # response, matching the live human-guidance event shape.
            from lib.tasks_pkg.human_guidance import resolve_human_guidance
            resolve_human_guidance(guidance_id, user_response)
            append_event(task, {
                'type': 'human_guidance_response',
                'roundNum': rn,
                'guidanceId': guidance_id,
                'response': user_response,
                'isVirtualUser': True,
            })
    elif not task.get('_attended'):
        # No interactive session can answer this. Only the three routes/chat.py
        # entry points stamp task['_attended']=True; every headless/autonomous
        # surface (/api/v1/agent/run, scheduler, compat OpenAI/Anthropic,
        # swarm sub-agents) omits it — and autopilot was already handled above.
        # Blocking here would wedge the task on the 120s abort-poll with nobody
        # to resolve it (mirrors the attendance-aware write-approval gate in
        # tool_dispatch.py). Return a clear sentinel so the model proceeds on
        # its best assumption instead.
        logger.warning('[Executor] ask_human in an unattended execution mode '
                       '(no interactive session) — returning sentinel instead '
                       'of blocking: guidance_id=%s, task=%s',
                       guidance_id, task.get('id', '?')[:8])
        user_response = None
        tool_content = ('Cannot ask the user in this execution mode — no '
                        'interactive session is available to answer. Proceed '
                        'with your best assumption or use the tools available '
                        'to you to find the answer yourself.')
        # Mark the round resolved (not awaiting_human) so no UI/consumer waits.
        round_entry['status'] = 'unanswerable'
        meta = _build_simple_meta(
            fn_name, tool_content, source='HumanGuidance',
            title=question[:2000],
            snippet='No interactive session — proceeding without an answer',
            badge='unanswerable',
            extra={
                'guidanceId': guidance_id,
                'question': question,
                'responseType': response_type,
                'unanswerable': True,
            },
        )
        _finalize_tool_round(task, rn, round_entry, [meta])
        return tc_id, tool_content, False
    else:
        logger.info('[Executor] ask_human blocking indefinitely for user '
                    'response: guidance_id=%s, task=%s',
                    guidance_id, task.get('id', '?')[:8])
        user_response = request_human_guidance(guidance_id, task=task)

    if task.get('aborted') or user_response is None:
        tool_content = '[Task was aborted while waiting for human guidance]'
        logger.warning('[Executor] ask_human aborted/cancelled: '
                       'guidance_id=%s, task=%s, aborted=%s',
                       guidance_id, task.get('id', '?')[:8],
                       task.get('aborted', False))
    else:
        tool_content = f'Human response: {user_response}'
        logger.info('[Executor] ask_human received response: '
                    'guidance_id=%s, response_len=%d, task=%s',
                    guidance_id, len(user_response), task.get('id', '?')[:8])

    # ★ No title/snippet truncation: the user specifically flagged that
    #   "incomplete displays are not allowed" — the original 80-char title
    #   and 120-char snippet caps were producing cut-off question text
    #   ending mid-word (e.g. "…exercise t"). Pass the full strings; the
    #   frontend renderer already word-wraps. We only need a soft upper
    #   bound so a pathological 100 KB prompt doesn't bloat every SSE
    #   event — cap at 2000 chars with an ellipsis for safety, which is
    #   well above any legitimate ask_human question.
    _FULL_LIMIT = 2000
    def _clip(s):
        if not s:
            return s
        return s if len(s) <= _FULL_LIMIT else s[:_FULL_LIMIT - 1] + '…'
    meta = _build_simple_meta(
        fn_name, tool_content, source='HumanGuidance',
        title=_clip(question),
        snippet=_clip(user_response or 'No response'),
        badge='answered' if user_response else 'aborted',
        extra={
            'guidanceId': guidance_id,
            'question': question,
            'responseType': response_type,
            'userResponse': user_response,
        },
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


# ═══ Adapter-based handlers (simple_call does log→time→exec→meta→finalize) ═══

@tool_registry.tool_set(SCHEDULER_TOOL_NAMES, category='scheduler',
                        description='Schedule reminders and recurring tasks')
def _handle_scheduler_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    fn_args['_source_conv_id'] = task.get('convId', '')
    fn_args['_source_task_id'] = task.get('id', '')
    fn_args['_tool_call_id'] = tc_id
    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=execute_scheduler_tool,
        source='Scheduler', module_tag='Scheduler',
    )


def _run_desktop(fn_name, fn_args):
    """Desktop tool executor — wraps send_desktop_command + format_desktop_result."""
    from lib.desktop import format_desktop_result, send_desktop_command
    cmd_type = fn_name.replace('desktop_', '', 1)
    result, error = send_desktop_command(cmd_type, fn_args, timeout=30)
    if error:
        return f'Desktop Agent Error: {error}'
    return format_desktop_result(cmd_type, result)


@tool_registry.tool_set(DESKTOP_TOOL_NAMES, category='desktop',
                        description='Interact with the desktop agent')
def _handle_desktop_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_run_desktop,
        source='Desktop Agent', module_tag='Desktop',
    )


# Swarm tool badge labels (text only — the frontend renders the SVG icon, so
# no emoji prefix here per CLAUDE.md §3.4; an emoji would duplicate the icon).
_SWARM_BADGE_VERB = {
    'spawn_agents':     'spawn',
    'await_agents':     'await',
    'get_agent_result': 'result',
    'store_artifact':   'store',
    'read_artifact':    'read',
    'list_artifacts':   'list',
}


@tool_registry.handler('todo_write', category='task',
                       description='Maintain the structured task checklist')
def _handle_todo_write(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    """Persist the model's checklist onto ``task['_todos']`` (survives
    compaction — it's on the task dict, not in ``messages``) and feed the
    continuation enforcer.  The list REPLACES the prior state each call.

    Note: NOT a state-changing tool (no file mutation) — so it correctly does
    NOT reset the zero-deliverable streak; the two guards stay orthogonal.
    """
    from lib.tools.todo import apply_todo_write

    todos, tool_content = apply_todo_write(fn_args)
    task['_todos'] = todos

    total = len(todos)
    done = sum(1 for t in todos if t.get('status') == 'completed')
    in_prog = sum(1 for t in todos if t.get('status') == 'in_progress')
    logger.info('[Executor] todo_write: %d item(s) — %d done, %d in_progress '
                '(task=%s)', total, done, in_prog, task.get('id', '?')[:8])

    badge = f'{done}/{total}' if total else 'cleared'
    meta = _build_simple_meta(
        fn_name, tool_content, source='Checklist',
        title=f'Checklist · {done}/{total} done' if total else 'Checklist cleared',
        snippet=tool_content[:200],
        badge=badge,
        # Structured payload so the frontend renders a live progress panel
        # off engine data, not by re-parsing the result prose.
        extra={'todos': todos},
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


@tool_registry.tool_set(SWARM_TOOL_NAMES, category='swarm',
                        description='Spawn and manage parallel sub-agents (async)')
def _handle_swarm_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    # Custom executor closes over task/cfg/all_tools to preserve the full swarm API
    def _run_swarm(_fn_name, _fn_args):
        from lib.swarm.integration import execute_swarm_tool
        return execute_swarm_tool(
            _fn_name, _fn_args, task,
            on_event=lambda ev: append_event(task, ev),
            project_path=project_path,
            project_enabled=project_enabled,
            model=cfg.get('model'),
            thinking_enabled=cfg.get('thinking_enabled', False),
            search_mode=cfg.get('search_mode', 'multi'),
            cfg=cfg,
            all_tools=all_tools or [],
        )

    verb = _SWARM_BADGE_VERB.get(fn_name, 'swarm')
    badge = verb
    if fn_name == 'spawn_agents':
        num_agents = len(fn_args.get('agents', []))
        badge = f'{num_agents} agents'
    elif fn_name == 'await_agents':
        ids = fn_args.get('ids') or []
        badge = f'await {len(ids) or "all"}'
    elif fn_name == 'get_agent_result':
        badge = f'{(fn_args.get("agent_id") or "?")[:8]}'

    post_build = _build_await_post_build() if fn_name == 'await_agents' else None

    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_run_swarm,
        source='Swarm', badge=badge, module_tag='Swarm',
        post_build=post_build,
    )


def _build_await_post_build():
    """Return a post_build hook that rewrites the await_agents result badge
    from its JSON payload so the UI shows the real outcome.

    Without this every await row gets a generic ``await all`` badge that
    looks identical whether the wait completed cleanly or hit the hard-cap
    timeout. The hook surfaces ``done N/M`` plus a ``timed out`` marker so the
    user has full visibility of partial completions.

    No emoji prefix — the frontend renders the SVG icon and colors the badge
    via ``meta.awaitTimedOut`` (amber) per CLAUDE.md §3.4.
    """
    import json as _json

    def _post_build(meta, tool_content, _fn_args):
        try:
            data = _json.loads(tool_content) if isinstance(tool_content, str) else tool_content
        except (ValueError, TypeError) as e:
            logger.debug('[Swarm] await_agents result not JSON, keeping default badge: %s', e)
            return
        if not isinstance(data, dict):
            return
        if data.get('status') == 'error':
            meta['badge'] = 'no swarm'
            return
        completed = data.get('completed') or []
        still_running = data.get('still_running') or []
        n_done = len(completed)
        n_total = n_done + len(still_running)
        timed_out = bool(data.get('timed_out'))
        if timed_out:
            # Amber warning badge (via awaitTimedOut) — partial result, wait cut short.
            meta['badge'] = f'timed out · {n_done}/{n_total} done'
            meta['awaitTimedOut'] = True
        elif n_total:
            meta['badge'] = f'{n_done}/{n_total} done'
        else:
            # Nothing was waited on (all already finished, or swarm idle).
            meta['badge'] = 'done'
        if still_running:
            meta['awaitStillRunning'] = still_running

    return _post_build


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
    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_run,
        source='Conversations', module_tag='ConvRef',
        title=f'{fn_name}: {detail}',
    )


@tool_registry.tool_set(CHARTER_TOOL_NAMES, category='conversations',
                        description='Read / propose to the project charter (north star)')
def _handle_charter_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    current_conv_id = task.get('convId', '')

    def _run(_fn_name, _fn_args):
        from lib.conversations.project_charter import execute_charter_tool
        return execute_charter_tool(
            _fn_name, _fn_args,
            current_conv_id=current_conv_id,
            project_path=project_path if project_enabled else '')

    verb = 'read' if fn_name == 'project_charter_read' else 'propose'
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

    # Path-lease tools (project_claim_path/project_release_path) are also routed
    # here (they're in BOARD_TOOL_NAMES), but they carry no epic task_id and are
    # NOT a board-epic mutation — a stripped 'project_board_' prefix would leave
    # the ugly full name as the badge. Map them to a lease verb instead.
    _LEASE_VERB = {'project_claim_path': 'hold', 'project_release_path': 'release'}
    _is_lease_tool = fn_name in _LEASE_VERB
    _verb = _LEASE_VERB.get(fn_name) or fn_name.replace('project_board_', '', 1)

    def _post_build(meta, _tool_content, _fn_args):
        """Attach a STRUCTURED board snapshot (read) or transition (mutation),
        read off the engine — never re-parsed from the prose result."""
        if not project_enabled or not project_path:
            return
        # A lease op has no epic to describe (no task_id → the epic lookup below
        # yields empty title/status, and boardTransition would then SUPPRESS the
        # accurate prose body in _structuredConvMetaBody). The outcome string
        # from execute_board_tool already conveys held / advisory-refusal /
        # released, so let it render as the Markdown body — attach no transition.
        if _is_lease_tool:
            return
        try:
            from lib.conversations.project_board import read_board
            board = read_board(project_path)
        except Exception as e:
            logger.debug('[Board] post_build read failed: %s', e)
            return
        if fn_name == 'project_board_read':
            # Compact mini-kanban: counts + lane epic titles (structured).
            lanes = {'open': [], 'claimed': [], 'deferred': [], 'done': []}
            for tk in board.get('tasks', []):
                lanes.setdefault(tk.get('status', 'open'), []).append({
                    'id': tk.get('id', ''), 'title': tk.get('title', ''),
                    'owner': tk.get('owner_conv_id', ''),
                    'dispatched': bool(tk.get('dispatched')),
                })
            meta['boardSnapshot'] = {
                'open': board.get('open', 0), 'claimed': board.get('claimed', 0),
                'deferred': board.get('deferred', 0), 'done': board.get('done', 0),
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
    import uuid as _uuid

    def _approval_fn(prompt: str):
        from lib.tasks_pkg.autopilot import is_autopilot_enabled
        if is_autopilot_enabled(task):
            logger.info('[Peer] hard-abort auto-DENIED under autopilot task=%s',
                        task.get('id', '?')[:8])
            return None
        guidance_id = f'hg_{_uuid.uuid4().hex[:12]}'
        options = [{'label': 'Approve abort', 'value': 'approve'},
                   {'label': 'Deny', 'value': 'deny'}]
        round_entry['status'] = 'awaiting_human'
        round_entry['guidanceId'] = guidance_id
        round_entry['guidanceQuestion'] = prompt
        round_entry['guidanceType'] = 'choice'
        round_entry['guidanceOptions'] = options
        append_event(task, {
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



