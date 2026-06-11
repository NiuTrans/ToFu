"""routes/api_v1/tasks.py — Generic task lifecycle endpoints.

Replaces the per-feature ``/api/paper/poll``, ``/api/translate/poll``, etc.
for headless callers (legacy paths remain). One uniform shape:

  GET  /api/v1/tasks                 — list (filter by kind/status)
  GET  /api/v1/tasks/{id}            — full state snapshot
  GET  /api/v1/tasks/{id}/events     — long-poll cursor replay
  GET  /api/v1/tasks/{id}/stream     — SSE event replay
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

from flask import Blueprint, Response, request

from lib.api_response import api_not_found, api_ok
from lib.log import audit_log, get_logger
from lib.openapi import api_meta

from .auth import current_auth, require_scope

logger = get_logger(__name__)

api_v1_tasks_bp = Blueprint('api_v1_tasks', __name__)


def _registries() -> dict:
    """Return ``{kind: runtime}`` for every TaskRuntime we know about.

    Imported lazily to avoid a circular import on package init.
    """
    out = {}
    try:
        from lib.tasks_pkg.manager import _chat_runtime
        out['chat'] = _chat_runtime
    except Exception as e:
        logger.debug('[api_v1.tasks] chat runtime unavailable: %s', e)
    for mod_path, attr in (
        ('routes.paper', '_report_runtime'),
        ('routes.paper', '_translate_runtime'),
        ('routes.translate', '_translate_runtime'),
        ('routes.api_v1.agents', '_search_runtime'),
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


def _public_task(task: dict) -> dict:
    """Copy a task dict, dropping internal handles (locks, events_lock, etc.)."""
    SKIP = {'events_lock', 'abort_event', 'content_lock'}
    out = {}
    for k, v in task.items():
        if k in SKIP:
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
            with rt._lock:  # type: ignore[attr-defined]
                tasks = list(rt._tasks.values())  # type: ignore[attr-defined]
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
                'created_at': t.get('created_at'),
                'finished_at': t.get('finished_at'),
                'meta': t.get('meta') or {},
            })
    items.sort(key=lambda x: x.get('created_at') or 0, reverse=True)
    return api_ok({'tasks': items[:limit], 'total': len(items)})


def _find_task(task_id: str):
    for rt in _registries().values():
        t = rt.get(task_id)
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
    rt, task = _find_task(task_id)
    if task is None:
        return api_not_found('Task not found')
    return api_ok(_public_task(task))


@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>/events', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Cursor-based event replay (long-poll)',
          tags=['tasks'], scope='tasks',
          parameters=[{'name': 'cursor', 'in': 'query',
                        'schema': {'type': 'integer', 'default': 0}}])
def task_events(task_id):
    try:
        cursor = max(0, int(request.args.get('cursor') or 0))
    except (ValueError, TypeError) as _e_audit:
        logger.debug('[tasks] task_events caught %s: %s', type(_e_audit).__name__, _e_audit)
        cursor = 0
    rt, task = _find_task(task_id)
    if task is None:
        return api_not_found('Task not found')
    return api_ok(rt.poll(task_id, cursor=cursor))


@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>/stream', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Server-Sent Events stream of task events',
          tags=['tasks'], scope='tasks',
          parameters=[{'name': 'cursor', 'in': 'query',
                        'schema': {'type': 'integer', 'default': 0}}],
          responses={
              '200': {'description': 'SSE',
                      'content': {'text/event-stream': {
                          'schema': {'type': 'string'}}}},
              '404': {'description': 'Not Found'},
          })
def task_stream(task_id):
    try:
        cursor = max(0, int(request.args.get('cursor') or 0))
    except (ValueError, TypeError) as _e_audit:
        logger.debug('[tasks] task_stream caught %s: %s', type(_e_audit).__name__, _e_audit)
        cursor = 0
    rt, task = _find_task(task_id)
    if task is None:
        return api_not_found('Task not found')

    def gen():
        nonlocal cursor
        last_heartbeat = time.time()
        while True:
            with task['events_lock']:
                new_events = list(task['events'][cursor:])
                cursor = len(task['events'])
            for ev in new_events:
                yield f'id: {ev.get("seq", "")}\n'
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

    return Response(gen(), mimetype='text/event-stream', headers={
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-cache, no-transform',
        'X-Accel-Buffering': 'no',
        'Connection': 'keep-alive',
    })


@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>/abort', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Abort a task', tags=['tasks'], scope='tasks')
def task_abort(task_id):
    rt, task = _find_task(task_id)
    if task is None:
        return api_not_found('Task not found')
    if task.get('status') in ('done', 'error', 'aborted'):
        return api_ok(taskId=task_id, status=task['status'],
                       note='already finished')
    rt.abort(task_id)
    if 'aborted' in task:
        task['aborted'] = True
    audit_log('api_task_abort', task_id=task_id, kind=task.get('kind'),
              key_id=(current_auth().key_id if current_auth() else ''))
    return api_ok(taskId=task_id, status='aborting')


@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>', methods=['DELETE'])
@require_scope('admin')
@api_meta(summary='Drop a task from the registry (admin)',
          tags=['tasks'], scope='admin')
def task_delete(task_id):
    rt, task = _find_task(task_id)
    if task is None:
        return api_not_found('Task not found')
    try:
        with rt._lock:  # type: ignore[attr-defined]
            rt._tasks.pop(task_id, None)  # type: ignore[attr-defined]
    except Exception as e:
        logger.warning('[api_v1.tasks] delete %s failed: %s', task_id, e)
    audit_log('api_task_delete', task_id=task_id,
              key_id=(current_auth().key_id if current_auth() else ''))
    return api_ok(taskId=task_id, status='deleted')


__all__ = ['api_v1_tasks_bp']
