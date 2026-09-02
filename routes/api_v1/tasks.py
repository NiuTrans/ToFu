"""routes/api_v1/tasks.py — Generic task lifecycle endpoints.

Replaces the per-feature ``/api/paper/poll``, ``/api/translate/poll``, etc.
for headless callers (legacy paths remain). One uniform shape:

  GET  /api/v1/tasks                 — list (filter by kind/status)
  GET  /api/v1/tasks/{id}            — full state snapshot
  GET  /api/v1/tasks/{id}/events     — long-poll cursor replay
  GET  /api/v1/tasks/{id}/stream     — SSE event replay
  POST /api/v1/tasks/start           — START a production job (kind-dispatched)
  POST /api/v1/tasks/{id}/abort      — graceful stop
  DELETE /api/v1/tasks/{id}          — drop from registry (admin only)

Kinds discovered at runtime from the registered ``TaskRuntime`` instances
in the project. The chat runtime ('chat') is special-cased because its
task dicts carry extra fields the orchestrator depends on; everything
else uses the standardised TaskRuntime shape directly.
"""

from __future__ import annotations

import json
import time

from quart import Blueprint, request

from lib.api_response import (
    api_bad_request, api_internal_error, api_not_found, api_ok, sse_response,
)
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.request_parser import optional_bool, parse_body, require_str
from routes.task_http import task_replay_cursor, task_replay_parameters

from .auth import current_auth, request_user_id, require_scope

logger = get_logger(__name__)

api_v1_tasks_bp = Blueprint('api_v1_tasks', __name__)
_TASK_REPLAY_PARAMETERS = task_replay_parameters()


def _registries() -> dict:
    """Return ``{kind: runtime}`` for every TaskRuntime we know about.

    Imported lazily to avoid a circular import on package init. The key is
    always the runtime's OWN ``.kind``, never a literal here, so renaming a
    kind can't desync it from what ``/api/v1/tasks?kind=…`` filters on.

    An entry whose module fails to import is SKIPPED (logged at debug), so a
    missing/optional capability degrades to "absent" rather than taking down
    the generic endpoints for every other runtime.
    """
    out = {}
    try:
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        out[chat_task_runtime.kind] = chat_task_runtime
    except Exception as e:
        logger.debug('[api_v1.tasks] chat runtime unavailable: %s', e)
    for mod_path, attr in (
        ('routes.paper', '_report_runtime'),
        ('routes.paper', '_translate_runtime'),
        ('routes.paper', '_qa_runtime'),
        ('routes.paper', '_recommend_runtime'),
        ('routes.paper', '_deepen_runtime'),
        ('routes.translate', '_translate_runtime'),
        ('routes.api_v1.agents', '_search_runtime'),
        ('routes.api_v1.orchestrations', 'orchestration_run_runtime'),
        # Production-substrate capabilities. Both are ordinary TaskRuntime
        # instances with the standard task shape, but were absent from this
        # list — so /api/v1/tasks could not see a motion job at all, and
        # podcast had to hand-write its own poll route
        # (docs/modules/production.md).
        ('lib.motion_video.runtime', '_motion_runtime'),
        ('lib.paper.podcast_runtime', '_podcast_runtime'),
        ('lib.longform.runtime', '_longform_runtime'),
        ('lib.research.runtime', '_research_runtime'),
        ('lib.slides.runtime', '_slides_runtime'),
    ):
        try:
            mod = __import__(mod_path, fromlist=[attr])
            rt = getattr(mod, attr, None)
            if rt is not None:
                out[rt.kind] = rt
        except Exception as e:
            logger.debug('[api_v1.tasks] %s.%s unavailable: %s',
                         mod_path, attr, e)
    # Plugin task runtimes (e.g. trading-sim) via the tofu.task_runtimes
    # entry-point group — no core file names an optional feature.
    try:
        from routes.plugin_registry import discover_task_runtime_plugins
        for rt in discover_task_runtime_plugins():
            if rt is not None:
                out[rt.kind] = rt
    except Exception as e:
        logger.debug('[api_v1.tasks] plugin task-runtime discovery failed: %s', e)
    return out


#: Capability-agnostic START registry: ``kind -> {start, input, params}``.
#:
#: Poll / abort / stream have always been generic (``_registries()`` above),
#: but START had no HTTP verb at all — ``produce_research`` and
#: ``produce_report`` existed ONLY as LLM tools. That makes them unreachable
#: from a UI control for two independent reasons:
#:
#:   1. it depends on the model CHOOSING to call the tool, and
#:   2. ``lib/tools/registry/_build.py::_build_produce`` is SEARCH-GATED, so
#:      all three produce tools vanish when a user turns web search off —
#:      a button whose availability tracks an unrelated setting.
#:
#: So this table is deliberately NOT "research + a special case": each entry
#: declares its own primary ``input`` field name and the extra ``params`` it
#: accepts, and the route below reads them from the table. Adding the next
#: capability is one dict entry, not a new route (charter: an iteration's
#: extensibility is measured by how much LESS the next capability has to
#: write).
#:
#: The keys MUST match the ``kind`` its TaskRuntime registers in
#: ``_registries()`` — otherwise a client could start a job and never be able
#: to poll it. ``test_kinds_are_startable_and_pollable_under_the_same_name``
#: pins that.
_STARTERS: dict = {}


def _starters() -> dict:
    """Return ``{kind: {start, input, params}}`` for every startable capability.

    Resolved lazily (and memoised) for the same reason ``_registries()`` is: a
    capability whose module fails to import degrades to "absent" instead of
    taking the whole endpoint down.
    """
    if _STARTERS:
        return _STARTERS
    for mod_path, attr, kind, field, params in (
        ('lib.research.api', 'produce_research', 'research', 'direction',
         ('lang', 'n_ideas', 'seed_arxiv_ids')),
        ('lib.longform.api', 'start_report_job', 'longform-report', 'topic',
         ('lang', 'depth')),
        ('lib.slides.api', 'start_slides_job', 'slides-deck', 'topic',
         ('lang', 'style', 'max_pages', 'size', 'model')),
    ):
        try:
            mod = __import__(mod_path, fromlist=[attr])
            fn = getattr(mod, attr, None)
            if callable(fn):
                _STARTERS[kind] = {'start': fn, 'input': field, 'params': params}
        except Exception as e:
            logger.debug('[api_v1.tasks] starter %s.%s unavailable: %s',
                         mod_path, attr, e)
    return _STARTERS


@api_v1_tasks_bp.route('/api/v1/tasks/start', methods=['POST'])
@require_scope('tasks')
@api_meta(
    summary='Start a production job (research / long-form report)',
    description=(
        'Launch a "one sentence → finished product" job WITHOUT going through '
        'the LLM tool path. Poll it with GET /api/v1/tasks/{id}, watch it with '
        '/events or /stream, stop it with /abort. Returns the task id; a '
        'duplicate request joins the in-flight job instead of regenerating '
        '(deduped=true).'),
    tags=['tasks'], scope='tasks',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'type': 'object', 'required': ['kind'], 'properties': {
            'kind': {'type': 'string',
                     'description': "'research' | 'longform-report'"},
            'direction': {'type': 'string',
                          'description': "research: the direction to mine"},
            'topic': {'type': 'string',
                      'description': 'longform-report: the report topic'},
            'lang': {'type': 'string'},
            'conv_id': {'type': 'string'},
        }}}}})
def start_task():
    """Start a production job by kind. Capability-agnostic by construction."""
    body = parse_body()
    try:
        kind = require_str(body, 'kind')
    except ValueError as e:
        return api_bad_request(str(e))

    spec = _starters().get(kind)
    if spec is None:
        return api_bad_request(
            f'unknown task kind {kind!r} — startable kinds: '
            f'{sorted(_starters()) or "(none available)"}')

    field = spec['input']
    value = body.get(field)
    if not (isinstance(value, str) and value.strip()):
        return api_bad_request(
            f'{field!r} is required and must be a non-empty string for '
            f'kind={kind!r}')

    kwargs = {}
    for p in spec['params']:
        if body.get(p) is not None:
            kwargs[p] = body[p]
    conv_id = body.get('conv_id')
    if isinstance(conv_id, str) and conv_id:
        kwargs['conv_id'] = conv_id
    kwargs['user_id'] = int(request_user_id())

    try:
        res = spec['start'](value.strip(), **kwargs) or {}
    except TypeError as e:
        # A caller-supplied param the capability does not accept — a client
        # bug, not a server fault, so report it as such with the real reason.
        logger.warning('[api_v1.tasks] start %s rejected params %s: %s',
                       kind, sorted(kwargs), e)
        return api_bad_request(f'invalid parameters for kind={kind!r}: {e}')
    except Exception as e:
        logger.error('[api_v1.tasks] start %s failed: %s', kind, e, exc_info=True)
        return api_internal_error('internal_error')

    task_id = res.get('task_id') or ''
    audit_log('api_task_start', kind=kind, task_id=task_id,
              deduped=bool(res.get('deduped')),
              key_id=(current_auth().key_id if current_auth() else ''))
    logger.info('[api_v1.tasks] started %s task=%s deduped=%s',
                kind, task_id, bool(res.get('deduped')))
    return api_ok(taskId=task_id, kind=kind, deduped=bool(res.get('deduped')))


def _public_task(task: dict) -> dict:
    """Copy a task dict, dropping private runtime state and sync handles.

    ``artifact_quality`` (the product-quality axis — see
    lib/agent_core/task_runtime.py) MUST survive this copy. A degraded job
    keeps ``status='done'`` by design, so ``artifact_quality`` is the ONLY
    thing on this response that distinguishes "delivered a good artifact"
    from "delivered a valid artifact out of a broken pipeline". Adding it to
    SKIP, or filtering the response down to a hand-listed field set, silently
    re-hides the R3 total-wipe class of bug.
    """
    SKIP = {'events_lock', 'abort_event', 'content_lock'}
    out = {}
    for k, v in task.items():
        # Leading-underscore fields are process-private by repository
        # convention. They include locks, callback carriers, dispatch state,
        # credentials and bounded sets such as ``_budgetWarnings``. Exposing
        # them was both an authority leak and a reliability bug: the first set
        # reached Quart's JSON provider and made GET /api/v1/tasks/{id} return
        # 500. Public metadata has explicit non-private mirrors on the task.
        if k in SKIP or k.startswith('_'):
            continue
        if k == 'messages':
            # Don't dump the full prompt back; clients already have it.
            out['msg_count'] = len(v) if isinstance(v, list) else 0
            continue
        out[k] = v
    return out


@api_v1_tasks_bp.route('/api/v1/tasks', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='List tasks', tags=['tasks'], scope='tasks',
          parameters=[
              {'name': 'kind', 'in': 'query',
               'schema': {'type': 'string'},
               'description': "Filter by kind (chat, paper-report, translate, …)"},
              {'name': 'status', 'in': 'query',
               'schema': {'type': 'string'}},
              {'name': 'limit', 'in': 'query',
               'schema': {'type': 'integer', 'default': 50, 'maximum': 500}},
          ])
def list_tasks():
    owner_user_id = int(request_user_id())
    kind = request.args.get('kind') or ''
    status = request.args.get('status') or ''
    try:
        limit = max(1, min(int(request.args.get('limit') or 50), 500))
    except (ValueError, TypeError) as _e_audit:
        logger.debug('[tasks] list_tasks caught %s: %s', type(_e_audit).__name__, _e_audit)
        limit = 50

    items = []
    for k, rt in _registries().items():
        if kind and k != kind:
            continue
        try:
            tasks = rt.snapshot_owned(user_id=owner_user_id)
        except Exception as e:
            logger.debug('[api_v1.tasks] snapshot %s failed: %s', k, e)
            continue
        for t in tasks:
            if status and t.get('status') != status:
                continue
            items.append({
                'id': t.get('id'),
                'kind': t.get('kind') or k,
                'status': t.get('status'),
                # Product-quality axis. This surface hand-lists its fields, so
                # unlike _public_task it must name the field explicitly or a
                # degraded job is indistinguishable from a clean one in the
                # list view (status is 'done' for both, by design).
                'artifact_quality': t.get('artifact_quality'),
                'created_at': t.get('created_at'),
                'finished_at': t.get('finished_at'),
                'meta': t.get('meta') or {},
            })
    items.sort(key=lambda x: x.get('created_at') or 0, reverse=True)
    return api_ok({'tasks': items[:limit], 'total': len(items)})


def _find_task(task_id: str, *, user_id: int):
    for rt in _registries().values():
        t = rt.get_owned(task_id, user_id=user_id)
        if t is not None:
            return rt, t
    return None, None


@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Get task state', tags=['tasks'], scope='tasks',
          responses={
              '200': {'description': 'OK', 'content': {'application/json': {
                  'schema': {'$ref': '#/components/schemas/TaskState'}}}},
              '404': {'description': 'Not Found'},
          })
def get_task(task_id):
    rt, task = _find_task(task_id, user_id=int(request_user_id()))
    if task is None:
        return api_not_found('Task not found')
    return api_ok(_public_task(task))


@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>/events', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Cursor-based event replay (long-poll)',
          tags=['tasks'], scope='tasks',
          parameters=_TASK_REPLAY_PARAMETERS)
def task_events(task_id):
    cursor = task_replay_cursor(request.args)
    rt, task = _find_task(task_id, user_id=int(request_user_id()))
    if task is None:
        return api_not_found('Task not found')
    return api_ok(rt.poll(task_id, cursor=cursor))


@api_v1_tasks_bp.route('/api/v1/tasks/by-conv/<conv_id>', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Request Inspector: task rows for a conversation',
          tags=['tasks'], scope='tasks')
def tasks_by_conv(conv_id):
    """Task rows for the Request Inspector drawer (live registry +
    task_results + exact kind-counted snapshot tallies)."""
    from lib.conversations.repository import get_conversation
    from lib.tasks_pkg.request_inspector import list_conv_tasks
    owner_user_id = int(request_user_id())
    if get_conversation(
            conv_id, user_id=owner_user_id,
            include_messages=False) is None:
        return api_not_found('Conversation not found')
    try:
        return api_ok(list_conv_tasks(conv_id, user_id=owner_user_id))
    except Exception as e:
        logger.error('[api_v1.tasks] by-conv failed for conv=%s: %s',
                     conv_id[:8], e, exc_info=True)
        return api_internal_error('internal_error')


@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>/requests', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Request Inspector: metadata-only request rows',
          tags=['tasks'], scope='tasks')
def task_requests(task_id):
    """Fold the task's persisted event log into request/attempt/state rows.

    METADATA-ONLY (design doc §3.3, frozen): request rows never carry the
    message payload — fetch it per round via ``/requests/<round_num>``.
    Returns 200 with ``eventsAvailable:false`` for expired (>6h) or
    unknown tasks so the UI can show an honest empty state."""
    from lib.tasks_pkg.request_inspector import fold_request_log
    if _find_task(task_id, user_id=int(request_user_id()))[1] is None:
        return api_not_found('Task not found')
    try:
        return api_ok(fold_request_log(task_id))
    except Exception as e:
        logger.error('[api_v1.tasks] requests fold failed for task=%s: %s',
                     task_id[:8], e, exc_info=True)
        return api_internal_error('internal_error')


@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>/trace', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Turn Trace: per-task timing span fold',
          tags=['tasks'], scope='tasks')
def task_trace(task_id):
    """Fold the task's persisted event log into the timing span tree
    (turn → round → llm/tool/wait/compaction + explicit gaps + the
    declared-budget over-run list). Contract: docs/TURN_TRACE_CONTRACT.md.
    Returns 200 with ``eventsAvailable:false`` for expired (>6h) or
    unknown tasks so the UI can show an honest empty state."""
    from lib.tasks_pkg.turn_trace import fold_task_trace
    if _find_task(task_id, user_id=int(request_user_id()))[1] is None:
        return api_not_found('Task not found')
    try:
        return api_ok(fold_task_trace(task_id))
    except Exception as e:
        logger.error('[api_v1.tasks] trace fold failed for task=%s: %s',
                     task_id[:8], e, exc_info=True)
        return api_internal_error('internal_error')


@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>/requests/<round_num>',
                       methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Request Inspector: full payload for one round',
          tags=['tasks'], scope='tasks')
def task_request_payload(task_id, round_num):
    """On-demand full payload (messages + tools + params) for one snapshot
    round. ``?kind=state`` serves the post-tool / final / fallback mirrors
    (same roundNum axis — design §3.1); default is the pre-request snapshot.
    404 when the round has no matching snapshot (expired, wrong kind, or
    unknown)."""
    from lib.tasks_pkg.request_inspector import get_request_payload
    if _find_task(task_id, user_id=int(request_user_id()))[1] is None:
        return api_not_found('Task not found')
    try:
        payload = get_request_payload(
            task_id, round_num, turn=request.args.get('turn', ''),
            kind=request.args.get('kind', 'request'))
    except Exception as e:
        logger.error('[api_v1.tasks] request payload failed for task=%s '
                     'round=%s: %s', task_id[:8], round_num, e, exc_info=True)
        return api_internal_error('internal_error')
    if payload is None:
        return api_not_found('Round snapshot not found')
    return api_ok(payload)


@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>/stream', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Server-Sent Events stream of task events',
          tags=['tasks'], scope='tasks',
          parameters=_TASK_REPLAY_PARAMETERS,
          responses={
              '200': {'description': 'SSE',
                      'content': {'text/event-stream': {
                          'schema': {'type': 'string'}}}},
              '404': {'description': 'Not Found'},
          })
def task_stream(task_id):
    cursor = task_replay_cursor(request.args)
    rt, task = _find_task(task_id, user_id=int(request_user_id()))
    if task is None:
        return api_not_found('Task not found')

    def gen():
        nonlocal cursor
        from lib.task_replay import task_memory_replay_page
        last_heartbeat = time.time()
        while True:
            page = task_memory_replay_page(task, cursor)
            new_events = page.events
            cursor = page.next_cursor
            for event_id, ev in page.frames:
                yield f'id: {event_id}\n'
                yield f'data: {json.dumps(ev, ensure_ascii=False)}\n\n'
                if ev.get('type') in ('done', 'error', 'aborted'):
                    return
            if task.get('status') in ('done', 'error', 'aborted') and not new_events:
                yield 'data: {"type":"done","status":"' + str(task['status']) + '"}\n\n'
                return
            now = time.time()
            if now - last_heartbeat > 15:
                yield ': heartbeat\n\n'
                last_heartbeat = now
            time.sleep(0.05)

    return sse_response(gen())


@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>/abort', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Abort a task', tags=['tasks'], scope='tasks')
def task_abort(task_id):
    owner_user_id = int(request_user_id())
    rt, task = _find_task(task_id, user_id=owner_user_id)
    if task is None:
        return api_not_found('Task not found')
    if task.get('status') in ('done', 'error', 'aborted'):
        return api_ok(taskId=task_id, status=task['status'],
                       note='already finished')
    rt.abort_owned(task_id, user_id=owner_user_id)
    if 'aborted' in task:
        task['aborted'] = True
    audit_log('api_task_abort', task_id=task_id, kind=task.get('kind'),
              key_id=(current_auth().key_id if current_auth() else ''))
    return api_ok(taskId=task_id, status='aborting')


@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>/tool_result', methods=['POST'])
@require_scope('tasks')
@api_meta(
    summary='Return a client-executed custom tool result',
    description=(
        'Client-handoff backend for per-request custom tools (execution.mode='
        '"client"). When the agent calls such a tool the task emits a '
        '`custom_tool_call` event `{callId, toolName, arguments}` and blocks; '
        'the client executes the tool and POSTs the result here to unblock it. '
        'See docs/CUSTOM_TOOLS.md.'),
    tags=['tasks'], scope='tasks',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'type': 'object', 'required': ['call_id', 'content'],
                   'properties': {
                       'call_id': {'type': 'string'},
                       'content': {'type': 'string'},
                       'is_error': {'type': 'boolean'}}}}}})
def task_tool_result(task_id):
    owner_user_id = int(request_user_id())
    _, task = _find_task(task_id, user_id=owner_user_id)
    if task is None:
        return api_not_found('Task not found')
    body = parse_body()
    try:
        call_id = require_str(body, 'call_id')
        content = require_str(body, 'content', allow_empty=True)
    except ValueError as e:
        return api_bad_request(str(e))
    is_error = optional_bool(body, 'is_error', default=False)
    from lib.tools.tool_env import resolve_client_tool_result
    ok = resolve_client_tool_result(
        call_id,
        content,
        task_id=task_id,
        user_id=owner_user_id,
        is_error=is_error,
    )
    if not ok:
        return api_not_found(
            f'No pending custom tool call {call_id!r} (expired, already '
            'resolved, or unknown).')
    audit_log('api_task_tool_result', task_id=task_id, call_id=call_id,
              is_error=is_error,
              key_id=(current_auth().key_id if current_auth() else ''))
    return api_ok(taskId=task_id, callId=call_id, resolved=True)


@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>', methods=['DELETE'])
@require_scope('admin')
@api_meta(summary='Drop a task from the registry (admin)',
          tags=['tasks'], scope='admin')
def task_delete(task_id):
    owner_user_id = int(request_user_id())
    rt, task = _find_task(task_id, user_id=owner_user_id)
    if task is None:
        return api_not_found('Task not found')
    if not rt.remove_owned(task_id, user_id=owner_user_id):
        return api_not_found('Task not found')
    audit_log('api_task_delete', task_id=task_id,
              key_id=(current_auth().key_id if current_auth() else ''))
    return api_ok(taskId=task_id, status='deleted')


__all__ = ['api_v1_tasks_bp']  # task_tool_result registered on the same bp
