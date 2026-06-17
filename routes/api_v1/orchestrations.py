"""routes/api_v1/orchestrations.py — Orchestration definition CRUD.

Stores user-authored orchestration graphs (from the frontend
Orchestration Studio) as a flat JSON array at
``data/config/orchestrations.json`` via :mod:`lib.json_store`
(atomic + locked). Each entry carries the declarative
``tofu.orchestration/v1`` definition validated by
:mod:`lib.orchestration`.

Routes:
  GET    /api/v1/orchestrations                 — list all (metadata + def)
  GET    /api/v1/orchestrations/{id}            — fetch one
  POST   /api/v1/orchestrations                 — create
  PUT    /api/v1/orchestrations/{id}            — replace definition
  DELETE /api/v1/orchestrations/{id}            — remove
  POST   /api/v1/orchestrations/validate        — validate without saving
  POST   /api/v1/orchestrations/layout          — tidy node positions (pure)
  POST   /api/v1/orchestrations/compose         — LLM author/edit from NL
  POST   /api/v1/orchestrations/plan            — dry-run execution preview
  POST   /api/v1/orchestrations/run             — execute (background task)
  GET    /api/v1/orchestrations/run/poll/{id}   — poll a run's events
  POST   /api/v1/orchestrations/run/abort/{id}  — abort a run
  POST   /api/v1/orchestrations/run/human-approve — resolve an approval gate
  POST   /api/v1/orchestrations/run/human-input   — resolve an input gate
"""

from __future__ import annotations

import secrets
import time

from flask import Blueprint, jsonify

from lib.api_response import api_bad_request, api_not_found, api_ok
from lib.config_dir import config_path as _config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import get_logger
from lib.openapi import api_meta
from lib.orchestration import layout_definition, validate_definition
from lib.request_parser import optional_str, parse_body
from lib.task_runtime import TaskRuntime

from .auth import current_auth, require_auth
from .._task_routes import register_task_routes

logger = get_logger(__name__)

api_v1_orchestrations_bp = Blueprint('api_v1_orchestrations', __name__)

_ORCH_PATH = _config_path('orchestrations.json')

#: Background runtime for flow executions. Events stream to the
#: ``orchestration`` push channel; the frontend polls /run/poll/<id>.
orchestration_run_runtime = TaskRuntime('orchestration-run', ttl=3600,
                                        push_channel='orchestration')


def _read_all() -> list[dict]:
    data = read_json(_ORCH_PATH, default=[])
    return data if isinstance(data, list) else []


def _new_id() -> str:
    return 'orch_' + hex(int(time.time() * 1000))[2:] + secrets.token_hex(2)


_NODE_SCHEMA = {
    'type': 'object',
    'properties': {
        'id': {'type': 'string'},
        'type': {'type': 'string', 'enum': ['role', 'control']},
        'role': {'type': 'string'},
        'kind': {'type': 'string'},
        'name': {'type': 'string'},
        'pos': {'type': 'object'},
        'params': {'type': 'object'},
    },
    'required': ['id', 'type'],
}

_DEF_SCHEMA = {
    'type': 'object',
    'properties': {
        'schema': {'type': 'string'},
        'name': {'type': 'string'},
        'nodes': {'type': 'array', 'items': _NODE_SCHEMA},
        'edges': {'type': 'array', 'items': {'type': 'object'}},
    },
    'required': ['name', 'nodes', 'edges'],
}


@api_v1_orchestrations_bp.route('/api/v1/orchestrations', methods=['GET'])
@require_auth
@api_meta(
    summary='List orchestration definitions',
    description='Returns all stored orchestration definitions with metadata.',
    tags=['orchestrations'],
)
def list_orchestrations():
    return jsonify(_read_all())


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/<orch_id>', methods=['GET'])
@require_auth
@api_meta(summary='Get one orchestration definition', tags=['orchestrations'])
def get_orchestration(orch_id):
    for entry in _read_all():
        if entry.get('id') == orch_id:
            return jsonify(entry)
    return api_not_found('Orchestration not found')


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/validate', methods=['POST'])
@require_auth
@api_meta(
    summary='Validate a definition without saving',
    description='Runs lib.orchestration.validate_definition and returns '
                '{ok, errors, warnings}.',
    tags=['orchestrations'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': _DEF_SCHEMA}}},
)
def validate_orchestration():
    body = parse_body()
    verdict = validate_definition(body)
    return jsonify(verdict)


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/compose', methods=['POST'])
@require_auth
@api_meta(
    summary='Compose / edit a definition from natural language',
    description='LLM turns a NL requirement (+ optional current graph + '
                'chat history) into a validated, auto-laid-out definition. '
                'Returns {ok, reply, definition, validation}.',
    tags=['orchestrations'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'type': 'object', 'required': ['requirement'], 'properties': {
            'requirement': {'type': 'string'},
            'current': {'type': 'object'},
            'history': {'type': 'array', 'items': {'type': 'object'}}}}}}},
)
def compose_orchestration():
    from lib.orchestration_composer import compose

    body = parse_body()
    requirement = optional_str(body, 'requirement', default='', max_len=4000).strip()
    if not requirement:
        return api_bad_request('requirement is required', field='requirement')
    current = body.get('current') if isinstance(body.get('current'), dict) else None
    history = body.get('history') if isinstance(body.get('history'), list) else None

    result = compose(requirement, current=current, history=history)
    logger.info('[Orchestrations] compose ok=%s nodes=%s',
                result.get('ok'),
                len((result.get('definition') or {}).get('nodes') or []))
    return jsonify(result)


@api_v1_orchestrations_bp.route('/api/v1/orchestrations', methods=['POST'])
@require_auth
@api_meta(
    summary='Create an orchestration definition',
    tags=['orchestrations'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': _DEF_SCHEMA}}},
)
def create_orchestration():
    body = parse_body()
    verdict = validate_definition(body)
    if not verdict['ok']:
        return api_bad_request('Invalid orchestration definition',
                               errors=verdict['errors'],
                               warnings=verdict['warnings'])

    new_entry: dict = {}

    def _mutate(entries):
        if not isinstance(entries, list):
            entries = []
        now = int(time.time() * 1000)
        new_entry.update({
            'id': _new_id(),
            'name': body.get('name'),
            'definition': body,
            'createdAt': now,
            'updatedAt': now,
        })
        entries.append(new_entry)
        return entries

    update_json_atomic(_ORCH_PATH, _mutate, default=[])
    logger.info('[Orchestrations] created id=%s name=%r',
                new_entry['id'], new_entry.get('name'))
    resp = dict(new_entry)
    resp['warnings'] = verdict['warnings']
    return jsonify(resp), 201


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/<orch_id>', methods=['PUT'])
@require_auth
@api_meta(
    summary='Replace an orchestration definition',
    tags=['orchestrations'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': _DEF_SCHEMA}}},
)
def update_orchestration(orch_id):
    body = parse_body()
    verdict = validate_definition(body)
    if not verdict['ok']:
        return api_bad_request('Invalid orchestration definition',
                               errors=verdict['errors'],
                               warnings=verdict['warnings'])

    found: list[dict] = []

    def _mutate(entries):
        if not isinstance(entries, list):
            entries = []
        for e in entries:
            if e.get('id') == orch_id:
                e['name'] = body.get('name')
                e['definition'] = body
                e['updatedAt'] = int(time.time() * 1000)
                found.append(e)
                break
        return entries

    update_json_atomic(_ORCH_PATH, _mutate, default=[])
    if not found:
        return api_not_found('Orchestration not found')
    logger.info('[Orchestrations] updated id=%s name=%r',
                orch_id, found[0].get('name'))
    resp = dict(found[0])
    resp['warnings'] = verdict['warnings']
    return jsonify(resp)


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/<orch_id>', methods=['DELETE'])
@require_auth
@api_meta(summary='Delete an orchestration definition', tags=['orchestrations'])
def delete_orchestration(orch_id):
    deleted = {'flag': False}

    def _mutate(entries):
        if not isinstance(entries, list):
            return []
        kept = [e for e in entries if e.get('id') != orch_id]
        deleted['flag'] = len(kept) != len(entries)
        return kept

    update_json_atomic(_ORCH_PATH, _mutate, default=[])
    if not deleted['flag']:
        return api_not_found('Orchestration not found')
    logger.info('[Orchestrations] deleted id=%s', orch_id)
    return api_ok()


def _resolve_definition(body: dict) -> dict | None:
    """Get a definition from an inline 'definition' or a stored 'id'."""
    defn = body.get('definition')
    if isinstance(defn, dict):
        return defn
    oid = body.get('id')
    if isinstance(oid, str) and oid:
        for entry in _read_all():
            if entry.get('id') == oid:
                return entry.get('definition')
    return None


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/builtin/<name>', methods=['GET'])
@require_auth
@api_meta(
    summary='Get a built-in canonical flow definition',
    description='Returns a server-authored reference flow (e.g. the canonical '
                'endpoint loop) as a tofu.orchestration/v1 definition. The '
                'backend is the single source of truth for these shapes.',
    tags=['orchestrations'],
)
def builtin_orchestration(name):
    from lib.orchestration import (
        build_autopilot_definition, build_endpoint_definition,
    )

    builders = {
        'endpoint': build_endpoint_definition,
        'autopilot': build_autopilot_definition,
    }
    builder = builders.get(name)
    if builder is None:
        return api_not_found(f'Unknown built-in flow {name!r}')
    return jsonify({'ok': True, 'definition': builder()})


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/role-schema', methods=['GET'])
@require_auth
@api_meta(
    summary='Get the per-role structured-param schema',
    description='Returns the FieldSpec lists the studio inspector renders for '
                'each role (the structured "what to do" inputs that fold into '
                'the delegation brief). Field labels are i18n KEYS — the '
                'frontend resolves wording via t(). Query ?role=<name> returns '
                'just that role\'s schema (generic fallback for unknown roles); '
                'omit it to get the full {roles: {...}, generic: [...], kinds} '
                'map. The backend owns structure; this is the single source so '
                'the inspector never mirrors a constant.',
    tags=['orchestrations'],
)
def role_schema_orchestration():
    from flask import request

    from lib.orchestration import (
        ROLE_PARAM_SCHEMA, VALID_PARAM_KINDS, role_param_schema,
    )

    role = (request.args.get('role') or '').strip()
    if role:
        return jsonify({'ok': True, 'role': role,
                        'fields': role_param_schema(role)})
    return jsonify({
        'ok': True,
        'roles': {r: spec for r, spec in ROLE_PARAM_SCHEMA.items()},
        'generic': role_param_schema('__generic__'),
        'kinds': sorted(VALID_PARAM_KINDS),
    })


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/layout', methods=['POST'])
@require_auth
@api_meta(
    summary='Auto-layout a definition (tidy node positions)',
    description='Runs lib.orchestration.layout_definition — BFS layering + '
                'barycenter crossing-minimization — and returns the same '
                'definition with every node\'s pos recomputed into clean '
                'top-down lanes. Pure: no agents run, nothing is stored. '
                'Accepts an inline "definition" or a stored "id".',
    tags=['orchestrations'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'type': 'object', 'properties': {
            'definition': _DEF_SCHEMA, 'id': {'type': 'string'}}}}}},
)
def layout_orchestration():
    body = parse_body()
    defn = _resolve_definition(body)
    if not isinstance(defn, dict):
        return api_bad_request('definition or id is required')
    layout_definition(defn)
    return jsonify({'ok': True, 'definition': defn})


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/plan', methods=['POST'])
@require_auth
@api_meta(
    summary='Dry-run a definition (no agents run)',
    description='Returns the ordered execution steps a run would take, '
                'without invoking any LLM/agent. {ok, steps, error}.',
    tags=['orchestrations'],
)
def plan_orchestration():
    from lib.orchestration_engine import compile_plan

    body = parse_body()
    defn = _resolve_definition(body)
    if not isinstance(defn, dict):
        return api_bad_request('definition or id is required')
    return jsonify(compile_plan(defn))


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/run', methods=['POST'])
@require_auth
@api_meta(
    summary='Execute an orchestration (background task)',
    description='Validates then runs the flow on a background task. Returns '
                '{task_id}; poll /run/poll/<task_id> for streamed events and '
                'the final result. Pass an inline "definition" or a stored "id", '
                'plus an optional "input" string (the user request).',
    tags=['orchestrations'],
)
def run_orchestration():
    from lib.orchestration_engine import FlowExecutor, FlowExecutionError

    body = parse_body()
    defn = _resolve_definition(body)
    if not isinstance(defn, dict):
        return api_bad_request('definition or id is required')
    verdict = validate_definition(defn)
    if not verdict['ok']:
        return api_bad_request('Invalid orchestration definition',
                               errors=verdict['errors'])
    user_input = optional_str(body, 'input', default='', max_len=8000)

    task = orchestration_run_runtime.create(meta={'name': defn.get('name')})
    tid = task['id']

    def _worker():
        def _on_event(ev):
            orchestration_run_runtime.append_event(tid, ev)
        try:
            executor = FlowExecutor(
                defn,
                on_event=_on_event,
                abort_check=task['abort_event'].is_set,
            )
            result = executor.run(initial_context=user_input)
            orchestration_run_runtime.finish(tid, result=result)
        except FlowExecutionError as e:
            orchestration_run_runtime.finish(tid, error=str(e),
                                             error_context='orchestration:structural')

    orchestration_run_runtime.spawn(tid, _worker)
    logger.info('[Orchestrations] run START task=%s name=%r',
                tid, defn.get('name'))
    return jsonify({'ok': True, 'task_id': tid})


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/run/human-approve',
                                methods=['POST'])
@require_auth
@api_meta(
    summary='Resolve a human approval gate in a running flow',
    description='Unblocks a flow paused on a human node with mode=approve. '
                'Body: {requestId, approved}. Reuses the chat write-approval '
                'primitive (lib.tasks_pkg.resolve_write_approval).',
    tags=['orchestrations'],
)
def orchestration_human_approve():
    from lib.tasks_pkg import resolve_write_approval

    body = parse_body()
    req_id = optional_str(body, 'requestId', default='').strip()
    if not req_id:
        return api_bad_request('requestId is required', field='requestId')
    approved = bool(body.get('approved'))
    if not resolve_write_approval(req_id, approved):
        return api_not_found('Approval request not found or expired')
    logger.info('[Orchestrations] human approve req=%s approved=%s',
                req_id, approved)
    return api_ok({'requestId': req_id, 'approved': approved})


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/run/human-input',
                                methods=['POST'])
@require_auth
@api_meta(
    summary='Resolve a human input gate in a running flow',
    description='Unblocks a flow paused on a human node with mode=input. '
                'Body: {requestId, response}. Reuses the chat ask-human '
                'primitive (lib.tasks_pkg.resolve_human_guidance).',
    tags=['orchestrations'],
)
def orchestration_human_input():
    from lib.tasks_pkg import resolve_human_guidance

    body = parse_body()
    req_id = optional_str(body, 'requestId', default='').strip()
    if not req_id:
        return api_bad_request('requestId is required', field='requestId')
    response_text = optional_str(body, 'response', default='', max_len=8000)
    if not resolve_human_guidance(req_id, response_text):
        return api_not_found('Input request not found or expired')
    logger.info('[Orchestrations] human input req=%s len=%d',
                req_id, len(response_text))
    return api_ok({'requestId': req_id})


# ═══════════════════════════════════════════════════════════════════
#  Durable run instances (Task Mode) — see docs/proposals/TASK_MODE.md
#
#  The /run endpoint above is the ephemeral chat-inline path (TaskRuntime
#  only, TTL-purged). /tasks is its durable sibling: a run instance pins a
#  definition snapshot in the DB and mirrors every engine event to
#  orchestration_run_events so the run is reopenable after a reload/restart.
# ═══════════════════════════════════════════════════════════════════

def _created_by() -> str:
    ctx = current_auth()
    return getattr(ctx, 'key_id', '') if ctx else ''


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/tasks', methods=['POST'])
@require_auth
@api_meta(
    summary='Create a durable orchestration run instance (Task Mode)',
    description='Validates then runs the flow as a DURABLE, reopenable run. '
                'Unlike /run, the definition snapshot + every event are '
                'persisted to the DB. Pass an inline "definition" or a stored '
                '"id", plus an optional "input". Returns {ok, run_id}; poll '
                '/tasks/<run_id>/events for streamed + replayable events.',
    tags=['orchestrations'],
)
def create_run_task():
    from lib.orchestration_engine import FlowExecutor, FlowExecutionError
    from lib import orchestration_runs as runs

    body = parse_body()
    defn = _resolve_definition(body)
    if not isinstance(defn, dict):
        return api_bad_request('definition or id is required')
    verdict = validate_definition(defn)
    if not verdict['ok']:
        return api_bad_request('Invalid orchestration definition',
                               errors=verdict['errors'])
    user_input = optional_str(body, 'input', default='', max_len=8000)
    orch_id = optional_str(body, 'id', default='')

    run_id = runs.new_run_id()
    runs.create_run(run_id, definition=defn, input_text=user_input,
                    orch_id=orch_id, name=defn.get('name') or '',
                    created_by=_created_by())

    task = orchestration_run_runtime.create(
        task_id=run_id, meta={'name': defn.get('name'), 'run_id': run_id})
    tid = task['id']

    def _worker():
        # Dual sink: the in-memory runtime (live push + poll) AND the durable
        # event log (cross-restart replay). append_event returns the seq the
        # runtime assigned, which is the PK we persist under.
        def _on_event(ev):
            seq = orchestration_run_runtime.append_event(tid, ev)
            if seq is not None:
                runs.append_event(run_id, seq, ev)
        try:
            executor = FlowExecutor(
                defn,
                on_event=_on_event,
                abort_check=task['abort_event'].is_set,
            )
            result = executor.run(initial_context=user_input)
            orchestration_run_runtime.finish(tid, result=result)
            status = (result or {}).get('status') or 'done'
            runs.update_status(
                run_id,
                'done' if status == 'completed' else 'error',
                final=(result or {}).get('final') or '',
                error=None if status == 'completed' else ((result or {}).get('error') or 'failed'))
        except FlowExecutionError as e:
            orchestration_run_runtime.finish(tid, error=str(e),
                                             error_context='orchestration:structural')
            runs.update_status(run_id, 'error', error=str(e))

    orchestration_run_runtime.spawn(tid, _worker)
    logger.info('[Orchestrations] task run START run=%s name=%r',
                run_id, defn.get('name'))
    return jsonify({'ok': True, 'run_id': run_id}), 201


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/tasks', methods=['GET'])
@require_auth
@api_meta(
    summary='List durable run instances',
    description='Returns run headers (newest first), without the definition '
                'blob. Optional query filters: ?status= and ?orch_id=.',
    tags=['orchestrations'],
)
def list_run_tasks():
    from flask import request
    from lib import orchestration_runs as runs

    status = (request.args.get('status') or '').strip()
    orch_id = (request.args.get('orch_id') or '').strip()
    return jsonify({'ok': True,
                    'runs': runs.list_runs(status=status, orch_id=orch_id)})


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/tasks/<run_id>',
                                methods=['GET'])
@require_auth
@api_meta(summary='Fetch one durable run instance (header + definition)',
          tags=['orchestrations'])
def get_run_task(run_id):
    from lib import orchestration_runs as runs

    run = runs.get_run(run_id)
    if run is None:
        return api_not_found('Run not found')
    return jsonify({'ok': True, 'run': run})


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/tasks/<run_id>/events',
                                methods=['GET'])
@require_auth
@api_meta(
    summary='Durable cursor replay of a run\'s events',
    description='Returns persisted events with seq >= cursor. Survives reload '
                'and server restart (unlike the in-memory /run/poll). '
                '{ok, events, next_cursor, status, done}.',
    tags=['orchestrations'],
)
def get_run_task_events(run_id):
    from flask import request
    from lib import orchestration_runs as runs

    run = runs.get_run(run_id)
    if run is None:
        return api_not_found('Run not found')
    try:
        cursor = int(request.args.get('cursor') or 0)
    except (ValueError, TypeError):
        cursor = 0
    events = runs.get_events(run_id, cursor)
    next_cursor = (events[-1]['seq'] + 1) if events else cursor
    status = run['status']
    return jsonify({
        'ok': True,
        'events': events,
        'next_cursor': next_cursor,
        'status': status,
        'done': status in ('done', 'error', 'aborted'),
    })


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/tasks/<run_id>/abort',
                                methods=['POST'])
@require_auth
@api_meta(summary='Abort a running durable run instance', tags=['orchestrations'])
def abort_run_task(run_id):
    from lib import orchestration_runs as runs

    if runs.get_run(run_id) is None:
        return api_not_found('Run not found')
    orchestration_run_runtime.abort(run_id)
    runs.update_status(run_id, 'aborted')
    logger.info('[Orchestrations] task run ABORT run=%s', run_id)
    return api_ok({'run_id': run_id})


@api_v1_orchestrations_bp.route('/api/v1/orchestrations/tasks/<run_id>',
                                methods=['DELETE'])
@require_auth
@api_meta(summary='Delete a durable run instance and its events',
          tags=['orchestrations'])
def delete_run_task(run_id):
    from lib import orchestration_runs as runs

    if not runs.delete_run(run_id):
        return api_not_found('Run not found')
    logger.info('[Orchestrations] task run DELETE run=%s', run_id)
    return api_ok()


register_task_routes(
    api_v1_orchestrations_bp, orchestration_run_runtime,
    url_prefix='/api/v1/orchestrations/run',
)


__all__ = ['api_v1_orchestrations_bp', 'orchestration_run_runtime']
