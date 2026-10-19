"""routes/api_v1/tasks.py — Generic task lifecycle endpoints.

Replaces the per-feature ``/api/paper/poll``, ``/api/translate/poll``, etc.
for headless callers (legacy paths remain). One uniform shape:

  GET  /api/v1/tasks                 — list (filter by kind/status)
  GET  /api/v1/tasks/{id}            — current state + replay cursor summary
  GET  /api/v1/tasks/{id}/events     — bounded long-poll cursor replay
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

import asyncio
import json
import time
from contextlib import nullcontext

import re
from urllib.parse import unquote

from quart import Blueprint, request

from lib.api_response import (
    api_bad_request, api_conflict, api_internal_error, api_not_found, api_ok,
    api_service_unavailable,
    sse_response,
)
from lib.research.action_budget import action_logical_budget_schema
from lib.production.work_unit import project_work_unit
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.request_parser import optional_bool, parse_body, require_str
from lib.task_replay.task_replay import (
    TASK_REPLAY_TERMINAL_EVENT_TYPES,
    TASK_REPLAY_TERMINAL_STATUSES,
    project_bounded_replay_payload,
    task_event_base_cursor,
)
from lib.tools.tool_env import (
    MAX_CUSTOM_TOOL_CALL_ID_CHARS,
    MAX_CUSTOM_TOOL_RESULT_CHARS,
    resolve_client_tool_result,
)
from routes.task_http import task_replay_cursor, task_replay_parameters

from .auth import current_auth, request_user_id, require_scope

logger = get_logger(__name__)

api_v1_tasks_bp = Blueprint('api_v1_tasks', __name__)
_TASK_REPLAY_PARAMETERS = task_replay_parameters()
_TASK_VIEW_PARAMETER = {
    'name': 'view', 'in': 'query', 'required': False,
    'description': 'current uses live state with cold fallback; durable reads only the owner-scoped committed chat task.',
    'schema': {'type': 'string', 'enum': ['current', 'durable'], 'default': 'current'},
}


def _task_read_view():
    values = request.args.getlist('view')
    if len(values) > 1 or (values and values[0] not in {'current', 'durable'}):
        return False, api_bad_request('view must be current or durable', field='view')
    return values == ['durable'], None


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
        ('lib.paper.builds.runtime', '_paper_build_runtime'),
        ('lib.longform.runtime', '_longform_runtime'),
        ('lib.research.runtime', '_research_runtime'),
        ('lib.research.action_runtime', '_research_action_runtime'),
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
         ('lang', 'n_ideas', 'seed_arxiv_ids', 'harvest_n', 'model')),
        ('lib.research.action', 'start_research_action', 'research-action',
         'direction', ('action', 'lang', 'expected_revision',
                       'confirm_external_writes', 'model', 'logical_budget')),
        ('lib.longform.api', 'start_report_job', 'longform-report', 'topic',
         ('lang', 'depth')),
        ('lib.slides.api', 'start_slides_job', 'slides-deck', 'topic',
         ('lang', 'style', 'max_pages', 'size', 'model', 'creative_mode')),
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
    summary='Start a registered production job',
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
                     'description': (
                         "Registered kind, including 'research', "
                         "'research-action', and 'longform-report'.")},
            'direction': {'type': 'string',
                          'description': (
                              'research/research-action: the direction to mine '
                              'or advance')},
            'topic': {'type': 'string',
                      'description': 'longform-report: the report topic'},
            'lang': {'type': 'string'},
            'action': {
                'type': 'string',
                'description': (
                    "research-action: 'experiment', 'analyze', 'manuscript', "
                    "'compile', or 'publish'")},
            'expected_revision': {
                'type': 'integer', 'minimum': 0,
                'description': 'research-action workspace CAS revision'},
            'logical_budget': action_logical_budget_schema(),
            'confirm_external_writes': {
                'type': 'boolean',
                'description': 'Explicit authority for write-class actions'},
            'model': {'type': 'string'},
            'creative_mode': {
                'type': 'string', 'enum': ['director', 'standard'],
                'description': (
                    'slides-deck: director candidate-and-critic planning '
                    '(default) or standard single-plan A/B control')},
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
    except (TypeError, ValueError) as e:
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
        if k == 'config' and isinstance(v, dict):
            # The config bag is the same dict the tool builder uses as
            # ``ctx.cfg``, so it accumulates process-private entries such as
            # ``_mcpToolSearchTextByName`` (a MappingProxyType cache view).
            # The leading-underscore convention applies here too: those
            # values are both an authority leak and not JSON-serializable —
            # one reached Quart's provider and 500'd GET /api/v1/tasks/{id}.
            out[k] = {ck: cv for ck, cv in v.items()
                      if not str(ck).startswith('_')}
            continue
        if k == 'messages':
            # Don't dump the full prompt back; clients already have it.
            out['msg_count'] = len(v) if isinstance(v, list) else 0
            continue
        if k == 'events':
            # Event history has its own cursor replay endpoint. Embedding the
            # same retained window here made a status refresh duplicate tens
            # of megabytes for long tasks. Publish only enough producer-owned
            # cursor metadata for a client to attach safely.
            lock = task.get('events_lock')
            with (lock if lock is not None else nullcontext()):
                retained = v if isinstance(v, (list, tuple)) else []
                base_cursor = task_event_base_cursor(task, retained)
                out['event_replay'] = {
                    'retained_count': len(retained),
                    'base_cursor': base_cursor,
                    'next_cursor': base_cursor + len(retained),
                }
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
              {'name': 'direction', 'in': 'query', 'schema': {'type': 'string'},
               'description': 'Exact task metadata direction; filtered before the result limit.'},
              {'name': 'lang', 'in': 'query', 'schema': {'type': 'string'},
               'description': 'Exact task metadata language; filtered before the result limit.'},
              {'name': 'limit', 'in': 'query',
               'schema': {'type': 'integer', 'default': 50, 'maximum': 500}},
          ])
def list_tasks():
    owner_user_id = int(request_user_id())
    kind = request.args.get('kind') or ''
    status = request.args.get('status') or ''
    direction = request.args.get('direction')
    lang = request.args.get('lang')
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
            metadata = t.get('meta') or {}
            if direction is not None and metadata.get('direction') != direction:
                continue
            if lang is not None and metadata.get('lang') != lang:
                continue
            item = {
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
            }
            unit = project_work_unit(t, events=[], compact=True)
            if unit is not None:
                item['workUnit'] = unit
            items.append(item)
    items.sort(key=lambda x: x.get('created_at') or 0, reverse=True)
    return api_ok({'tasks': items[:limit], 'total': len(items)})


def _find_task(task_id: str, *, user_id: int):
    foreign_present = False
    for rt in _registries().values():
        t = rt.get_owned(task_id, user_id=user_id)
        if t is not None:
            return rt, t, False
        try:
            foreign_present = foreign_present or rt.get(task_id) is not None
        except Exception as e:
            logger.debug(
                '[api_v1.tasks] live presence probe failed task=%s: %s',
                task_id[:8], e)
    return None, None, foreign_present


def _task_accessible(task_id: str, *, user_id: int) -> bool:
    """Owner check for the read-only Request Inspector endpoints.

    A live-registry miss is NOT "unknown task": after a server restart or a
    registry TTL/capacity eviction the in-memory dict is gone while the
    durable event log (6h TTL) and the ``task_results`` row still exist.
    404ing there made the drawer report every finished run as "records
    cleaned up" when they were not. Fall back to the persisted row's
    ``user_id`` (swarm-agent ids ``parent#agent:<id>`` check their parent's
    row); only a truly unknown or foreign task 404s.
    """
    _runtime, task, foreign_present = _find_task(
        task_id, user_id=user_id)
    if task is not None:
        return True
    if foreign_present:
        return False
    try:
        from lib.tasks_pkg.durable_chat_replay import (
            persisted_chat_task_owner_matches,
        )
        return persisted_chat_task_owner_matches(
            task_id, user_id=user_id)
    except Exception as e:
        logger.debug('[api_v1.tasks] persisted owner check failed for '
                     'task=%s: %s', task_id[:8], e)
        return False


def _request_inspector_task_accessible(task_id: str, *, user_id: int) -> bool:
    """Authorize a normal task or a persisted inspector child, fail closed."""
    parent_task_id, marker, _agent_id = task_id.partition('#agent:')
    if not marker:
        return _task_accessible(task_id, user_id=user_id)
    if not _task_accessible(parent_task_id, user_id=user_id):
        return False
    from lib.tasks_pkg.request_inspector import (
        inspector_child_belongs_to_parent,
    )
    return inspector_child_belongs_to_parent(task_id, parent_task_id)


def _inspector_turn_query(raw_turn: str) -> str:
    """Undo one proxy-added escape layer on generated Inspector turn tokens.

    Some reverse proxies encode the client's ``%3A`` once more. Quart then
    yields ``request%3A<uuid>`` instead of the persisted ``request:<uuid>``.
    Decode only this minted shape, so arbitrary caller-supplied turn names
    retain their exact-match authority.
    """
    if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}%3[Aa][0-9a-f]{32}', raw_turn):
        return unquote(raw_turn)
    return raw_turn


def _inspector_task_path(raw_task_id: str) -> str:
    """Restore one proxy escape layer on a minted child evidence stream ID."""
    match = re.fullmatch(
        r'([A-Za-z0-9_-]{1,128})%23agent%3[Aa]([A-Za-z0-9_-]{1,128})',
        raw_task_id,
    )
    return f'{match[1]}#agent:{match[2]}' if match else raw_task_id


@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Get task state', tags=['tasks'], scope='tasks',
          parameters=[_TASK_VIEW_PARAMETER],
          responses={
              '200': {'description': 'OK', 'content': {'application/json': {
                  'schema': {'$ref': '#/components/schemas/TaskState'}}}},
              '404': {'description': 'Not Found'},
          })
def get_task(task_id):
    durable_only, invalid_view = _task_read_view()
    if invalid_view is not None:
        return invalid_view
    owner_user_id = int(request_user_id())
    rt, task, foreign_present = _find_task(
        task_id, user_id=owner_user_id)
    read_source = 'live'
    if durable_only:
        rt, task = None, None
    if task is None:
        if foreign_present:
            return api_not_found('Task not found')
        try:
            from lib.tasks_pkg.durable_chat_replay import (
                load_durable_chat_task,
            )
            snapshot = load_durable_chat_task(task_id, user_id=owner_user_id)
            from lib.research.action_terminal import load_public_action_task
            retained_action = (load_public_action_task(task_id, user_id=owner_user_id)
                               if snapshot is None and not durable_only else None)
        except Exception as e:
            logger.error(
                '[api_v1.tasks] durable state failed task=%s: %s',
                task_id[:8], e, exc_info=True)
            return api_internal_error('internal_error')
        if snapshot is None and retained_action is None:
            return api_not_found('Task not found')
        task = retained_action if retained_action is not None else snapshot.public_task()
        read_source = 'durable'
    payload = _public_task(task)
    payload['readSource'] = read_source
    if rt is not None and getattr(rt, 'kind', '') != 'chat':
        unit = project_work_unit(task)
        if unit is not None:
            payload['workUnit'] = unit
    return api_ok(payload)


@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>/events', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Cursor-based event replay (long-poll)',
          description='Returns a bounded, lossless page. Continue with '
                      'next_cursor until caught_up=true; terminal result '
                      'fields are emitted only on the caught-up page.',
          tags=['tasks'], scope='tasks',
          parameters=[*_TASK_REPLAY_PARAMETERS, _TASK_VIEW_PARAMETER])
def task_events(task_id):
    durable_only, invalid_view = _task_read_view()
    if invalid_view is not None:
        return invalid_view
    cursor = task_replay_cursor(request.args)
    owner_user_id = int(request_user_id())
    rt, task, foreign_present = _find_task(
        task_id, user_id=owner_user_id)
    if durable_only:
        rt, task = None, None
    if task is None:
        if foreign_present:
            return api_not_found('Task not found')
        try:
            from lib.tasks_pkg.durable_chat_replay import (
                load_durable_chat_replay,
            )
            snapshot = load_durable_chat_replay(
                task_id, user_id=owner_user_id)
            if snapshot is None:
                return api_not_found('Task not found')
            _snapshot, payload = snapshot.bounded_replay_payload(cursor)
        except Exception as e:
            logger.error(
                '[api_v1.tasks] durable replay failed task=%s: %s',
                task_id[:8], e, exc_info=True)
            return api_internal_error('internal_error')
        return api_ok(payload)
    return api_ok(project_bounded_replay_payload(
        rt.poll(task_id, cursor=cursor)))


@api_v1_tasks_bp.route('/api/v1/tasks/by-conv/<conv_id>', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Request Inspector: task rows for a conversation',
          tags=['tasks'], scope='tasks',
          parameters=[
              {'name': 'limit', 'in': 'query',
               'schema': {'type': 'integer', 'default': 30, 'maximum': 100}},
              {'name': 'before', 'in': 'query',
               'schema': {'type': 'integer'},
               'description': 'Exclusive createdAt-ms cursor paging OLDER '
                              'persisted rows (live rows are first-page '
                              'only). From the previous page\'s oldest row.'},
          ])
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
        limit = max(1, min(int(request.args.get('limit') or 30), 100))
    except (ValueError, TypeError):
        limit = 30
    before_raw = request.args.get('before')
    before = None
    if before_raw:
        try:
            before = max(0, int(before_raw))
        except (ValueError, TypeError):
            return api_bad_request('before must be an integer ms cursor')
    try:
        return api_ok(list_conv_tasks(
            conv_id, user_id=owner_user_id, limit=limit, before=before))
    except Exception as e:
        logger.error('[api_v1.tasks] by-conv failed for conv=%s: %s',
                     conv_id[:8], e, exc_info=True)
        return api_internal_error('internal_error')


@api_v1_tasks_bp.route('/api/v1/tasks/conv-wire/<conv_id>', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Request Inspector: flat wire feed for a conversation',
          tags=['tasks'], scope='tasks')
def tasks_conv_wire(conv_id):
    """The flat debug feed: every message exactly once, turn/agent/rebuild
    section markers, and the turn nav model (2026-09-22 drawer — no more
    task list / round list / single-round detail)."""
    from lib.conversations.repository import get_conversation
    from lib.tasks_pkg.request_inspector import get_conv_inspector_wire
    owner_user_id = int(request_user_id())
    if get_conversation(
            conv_id, user_id=owner_user_id,
            include_messages=False) is None:
        return api_not_found('Conversation not found')
    try:
        return api_ok(get_conv_inspector_wire(conv_id, user_id=owner_user_id))
    except Exception as e:
        logger.error('[api_v1.tasks] conv-wire failed for conv=%s: %s',
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
    owner_user_id = int(request_user_id())
    if not _request_inspector_task_accessible(
            task_id, user_id=owner_user_id):
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
    """Return live event-derived or permanent attempt-owned timing evidence."""
    from lib.tasks_pkg.turn_trace import (
        fold_task_trace,
        merge_client_trace_evidence,
        read_persisted_task_trace,
    )
    owner_user_id = int(request_user_id())
    durable = read_persisted_task_trace(task_id, user_id=owner_user_id)
    if not _task_accessible(task_id, user_id=owner_user_id) and durable is None:
        return api_not_found('Task not found')
    try:
        # A terminal attempt snapshot is the permanent authority. In particular,
        # prefer it once the 6-hour streaming tier starts disappearing while
        # 30-day structural rows still make a raw fold look superficially
        # non-empty.
        if durable is not None and durable.get('running') is False \
                and isinstance(durable.get('summary'), dict):
            return api_ok(durable)
        live = fold_task_trace(task_id)
        if durable is not None:
            live = merge_client_trace_evidence(
                live, durable, task_id=task_id)
        if live.get('eventsAvailable'):
            return api_ok(live)
        return api_ok(durable or live)
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
    from lib.tasks_pkg.request_inspector import (
        RequestInspectorReadError, get_request_payload,
    )
    task_id = _inspector_task_path(task_id)
    owner_user_id = int(request_user_id())
    if not _request_inspector_task_accessible(
            task_id, user_id=owner_user_id):
        return api_not_found('Task not found')
    kind = request.args.get('kind', 'request')
    turn = _inspector_turn_query(request.args.get('turn', ''))
    # fold=1: server computes the shared prefix vs the previous round so the
    # drawer downloads ONE payload per click instead of two.
    fold = str(request.args.get('fold') or '').lower() in ('1', 'true', 'yes')
    # Log the FULL stream id: the '#agent:' suffix is what distinguishes a
    # swarm / goal-mode child stream from its parent, and truncating to [:8]
    # would hide exactly the part a payload miss diagnosis needs.
    logger.info('[ri] payload fetch task=%s round=%s kind=%s turn=%s',
                task_id, round_num, kind, turn or '-')
    try:
        payload = get_request_payload(
            task_id, round_num, turn=turn, kind=kind, user_id=owner_user_id,
            fold=fold)
    except RequestInspectorReadError:
        return api_service_unavailable('Round evidence temporarily unavailable')
    except Exception as e:
        logger.error('[api_v1.tasks] request payload failed for task=%s '
                     'round=%s: %s', task_id[:8], round_num, e, exc_info=True)
        return api_internal_error('internal_error')
    if payload is None:
        logger.warning('[ri] payload MISS task=%s round=%s kind=%s — '
                       'no matching snapshot', task_id, round_num, kind)
        return api_not_found('Round snapshot not found')
    logger.info('[ri] payload hit task=%s round=%s kind=%s msgs=%d',
                task_id, round_num, kind,
                len(payload.get('messages') or ()))
    return api_ok(payload)


@api_v1_tasks_bp.route(
    '/api/v1/tasks/<task_id>/raw-archives/<archive_id>/<part>',
    methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Request Inspector: bounded raw provider archive chunk',
          tags=['tasks'], scope='tasks')
def task_raw_archive_chunk(task_id, archive_id, part):
    """Lazily hydrate one bounded request/response body chunk."""
    task_id = _inspector_task_path(task_id)
    owner_user_id = int(request_user_id())
    if not _request_inspector_task_accessible(
            task_id, user_id=owner_user_id):
        return api_not_found('Task not found')
    if part not in {'request', 'response'}:
        return api_bad_request('part must be request or response')
    try:
        offset = max(0, int(request.args.get('offset') or 0))
        limit = max(1, min(1024 * 1024,
                           int(request.args.get('limit') or 256 * 1024)))
    except (TypeError, ValueError, OverflowError):
        return api_bad_request('offset and limit must be integers')
    try:
        from lib.tasks_pkg.request_inspector import get_raw_archive_chunk

        chunk = get_raw_archive_chunk(
            task_id, archive_id, part, user_id=owner_user_id,
            offset=offset, limit=limit)
    except Exception as exc:
        logger.error('[api_v1.tasks] raw archive read failed task=%s: %s',
                     task_id[:8], exc, exc_info=True)
        return api_internal_error('internal_error')
    if chunk is None:
        return api_not_found('Raw archive not found')
    return api_ok(chunk)


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
    owner_user_id = int(request_user_id())
    rt, task, foreign_present = _find_task(
        task_id, user_id=owner_user_id)
    durable_snapshot = None
    if task is None:
        if foreign_present:
            return api_not_found('Task not found')
        try:
            from lib.tasks_pkg.durable_chat_replay import (
                load_durable_chat_replay,
            )
            durable_snapshot = load_durable_chat_replay(
                task_id, user_id=owner_user_id)
        except Exception as e:
            logger.error(
                '[api_v1.tasks] durable stream lookup failed task=%s: %s',
                task_id[:8], e, exc_info=True)
            return api_internal_error('internal_error')
        if durable_snapshot is None:
            return api_not_found('Task not found')

    async def gen():
        nonlocal cursor, durable_snapshot
        from lib.task_replay.task_replay import task_memory_replay_page
        last_heartbeat = time.time()
        while True:
            if task is not None:
                page = task_memory_replay_page(task, cursor)
                new_events = page.events
                cursor = page.next_cursor
                frames = page.frames
                terminal = task.get('status') in TASK_REPLAY_TERMINAL_STATUSES
                terminal_payload = {
                    'type': 'done',
                    'status': str(task.get('status') or ''),
                }
            else:
                durable_snapshot, payload = (
                    await asyncio.to_thread(
                        durable_snapshot.bounded_replay_payload, cursor))
                new_events = payload['events']
                cursor = int(payload['next_cursor'])
                frames = [
                    (int(event['seq']), event) for event in new_events
                ]
                terminal = bool(
                    payload.get('done') and payload.get('caught_up'))
                terminal_payload = {
                    'type': 'done',
                    'status': str(payload.get('status') or ''),
                }
                for field in (
                    'content', 'thinking', 'error', 'finishedAt',
                ):
                    if field in payload:
                        terminal_payload[field] = payload[field]
                finish_reason = durable_snapshot.metadata.get('finishReason')
                if finish_reason:
                    terminal_payload['finishReason'] = finish_reason

            for event_id, ev in frames:
                yield f'id: {event_id}\n'
                yield f'data: {json.dumps(ev, ensure_ascii=False)}\n\n'
                if ev.get('type') in TASK_REPLAY_TERMINAL_EVENT_TYPES:
                    return
            if terminal and not new_events:
                yield 'data: ' + json.dumps(
                    terminal_payload, ensure_ascii=False) + '\n\n'
                return
            now = time.time()
            if now - last_heartbeat > 15:
                yield ': heartbeat\n\n'
                last_heartbeat = now
            if task is not None:
                await asyncio.sleep(0.05)
                continue

            # A cold non-terminal row can belong to a worker in another
            # process. Refresh storage at a bounded cadence; terminal rows are
            # immutable and simply drain their already-captured event window.
            if not durable_snapshot.terminal:
                await asyncio.sleep(0.5)
                from lib.tasks_pkg.durable_chat_replay import (
                    load_durable_chat_replay,
                )
                refreshed = await asyncio.to_thread(
                    load_durable_chat_replay,
                    task_id, user_id=owner_user_id)
                if refreshed is None:
                    return
                durable_snapshot = refreshed

    return sse_response(gen())


@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>/abort', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Abort a task', tags=['tasks'], scope='tasks')
def task_abort(task_id):
    owner_user_id = int(request_user_id())
    rt, task, _foreign_present = _find_task(
        task_id, user_id=owner_user_id)
    if task is None:
        # A registry miss is not "no such task": the worker can be LIVE while
        # its registry row is gone (the 2026-08-01 evaporation family), and
        # behind N replicas this request may simply have landed on the wrong
        # process. Plant the durable signal the worker's abort poll consumes
        # (mirrors routes/chat_poll_abort.py). Chat tasks use the
        # task_results tombstone; other TaskRuntime kinds use the
        # runtime_state_store flag (distributed mode only, so personal-mode
        # 404 semantics are unchanged).
        from lib.agent_core.task_abort_signal import plant_durable_abort
        from lib.tasks_pkg.manager import plant_abort_tombstone
        if plant_abort_tombstone(
                task_id, source='api_v1_task_abort', user_id=owner_user_id):
            return api_ok(taskId=task_id, status='abort_signaled',
                          note='task absent from registry; abort tombstone '
                               'planted — the live worker consumes it at its '
                               'next abort poll')
        if plant_durable_abort(
                task_id, user_id=owner_user_id,
                source='api_v1_task_abort'):
            return api_ok(taskId=task_id, status='abort_signaled',
                          note='task absent from this replica; durable abort '
                               'signal planted — the owning replica delivers '
                               'it at its next watch sweep')
        return api_not_found('Task not found')
    if task.get('status') in TASK_REPLAY_TERMINAL_STATUSES:
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
                       'call_id': {
                           'type': 'string',
                           'maxLength': MAX_CUSTOM_TOOL_CALL_ID_CHARS,
                       },
                       'content': {
                           'type': 'string',
                           'maxLength': MAX_CUSTOM_TOOL_RESULT_CHARS,
                       },
                       'is_error': {'type': 'boolean'}}}}}})
def task_tool_result(task_id):
    owner_user_id = int(request_user_id())
    _, task, _foreign_present = _find_task(
        task_id, user_id=owner_user_id)
    if task is None:
        return api_not_found('Task not found')
    body = parse_body(exact=('call_id', 'content', 'is_error',))
    try:
        call_id = require_str(
            body, 'call_id', max_len=MAX_CUSTOM_TOOL_CALL_ID_CHARS)
        content = require_str(
            body, 'content', allow_empty=True,
            max_len=MAX_CUSTOM_TOOL_RESULT_CHARS)
    except ValueError as e:
        return api_bad_request(str(e))
    is_error = optional_bool(body, 'is_error', default=False)
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
    rt, task, _foreign_present = _find_task(
        task_id, user_id=owner_user_id)
    if task is None:
        return api_not_found('Task not found')
    if task.get('status') in ('pending', 'running'):
        rt.abort_owned(task_id, user_id=owner_user_id)
        return api_conflict(
            'Task is still active; cancellation was requested. Delete it '
            'after it reaches a terminal state.',
            error_code='task_active',
        )
    if (task.get('_executionTerminalizing')
            or task.get('_finalize_started_at')
            or task.get('_terminalPersistencePending')):
        return api_conflict(
            'Task terminal settlement is still being published or persisted. '
            'Retry deletion after settlement completes.',
            error_code='task_settling',
        )
    if not rt.remove_owned(task_id, user_id=owner_user_id):
        return api_not_found('Task not found')
    audit_log('api_task_delete', task_id=task_id,
              key_id=(current_auth().key_id if current_auth() else ''))
    return api_ok(taskId=task_id, status='deleted')


__all__ = ['api_v1_tasks_bp']  # task_tool_result registered on the same bp
