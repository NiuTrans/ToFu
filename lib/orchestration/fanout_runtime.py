"""Dynamic Flow expansion on the existing dependency-ready Swarm scheduler.

Expansion is validated and published before any child is submitted. Leaves
use the ordinary role runtime, so budgets, tools, routing, evidence and typed
outcomes are identical to static roles. Existing asynchronous Swarm tools keep
their independent invocation lifetime; this control explicitly joins its DAG.
"""
from __future__ import annotations

import json
import threading
import uuid

from lib.orchestration.graph import FlowExecutionError, FlowEventDeliveryError
from lib.orchestration.events import event_preview
from lib.orchestration._role_specs import _validate_role_params
from lib.swarm.planner import resolve_execution_order
from lib.swarm.registry import resolve_spawnable_role
from lib.swarm.types import SubTaskSpec, SubAgentResult


def expansion_specs(text: str, *, maximum: int | None = None) -> list[SubTaskSpec]:
    if len(text.encode('utf-8')) > 64 * 1024:
        raise FlowExecutionError('fanout expansion exceeds 64 KiB')
    try:
        rows = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise FlowExecutionError('fanout requires a JSON task array') from exc
    if not isinstance(rows, list) or not rows:
        raise FlowExecutionError('fanout requires a nonempty task array')
    specs = []
    for row in rows:
        if not isinstance(row, dict) or set(row) - {
                'id', 'role', 'objective', 'context', 'depends_on', 'model_override', 'tools'}:
            raise FlowExecutionError('invalid fanout task fields')
        identity = row.get('id')
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 128:
            raise FlowExecutionError('fanout task requires a bounded id')
        role = row.get('role', 'general')
        objective = row.get('objective')
        context = row.get('context', '')
        deps = row.get('depends_on', [])
        if (not isinstance(role, str) or not role or len(role) > 128
                or not isinstance(objective, str) or not objective.strip()
                or not isinstance(context, str) or len(context) > 4000
                or not isinstance(deps, list)
                or any(not isinstance(dep, str) for dep in deps)):
            raise FlowExecutionError('invalid fanout task identity, context or dependencies')
        if resolve_spawnable_role(role) != role:
            raise FlowExecutionError('fanout requires a registered delegatable role')
        params = {key: row[key] for key in ('objective', 'model_override', 'tools') if key in row}
        errors = []
        _validate_role_params(role, identity, params, errors, [])
        if errors:
            raise FlowExecutionError('; '.join(map(str, errors)))
        specs.append(SubTaskSpec(id=identity, role=role, objective=objective,
                                context=context, depends_on=deps,
                                model_override=row.get('model_override') or '',
                                tools=row.get('tools'), max_retries=0))
    identities = {spec.id for spec in specs}
    objectives = {spec.objective.strip().lower() for spec in specs}
    if len(objectives) != len(specs):
        raise FlowExecutionError('fanout tasks must have distinct objectives')
    if len(identities) != len(specs) or any(
            dep not in identities for spec in specs for dep in spec.depends_on):
        raise FlowExecutionError('fanout contains duplicate ids or unknown dependencies')
    try:
        resolve_execution_order(specs)
    except ValueError as exc:
        raise FlowExecutionError(str(exc)) from exc
    return specs


def run_fanout(node: dict, context: str, *, role_runtime, emit, abort_check,
               max_parallel: int, iteration: int, execution_scope: str = '') -> str:
    from lib.swarm.scheduler import StreamingScheduler
    params = node.get('params') or {}
    raw = params.get('specs_json')
    if execution_scope:
        from lib.orchestration.execution_journal import fingerprint
        suffix = fingerprint(execution_scope)[:32]
    else:
        suffix = uuid.uuid4().hex
    scope = f'{node["id"]}:{suffix}'
    if not raw:
        planner = {'id': f'{scope}:planner', 'type': 'role', 'role': 'planner', 'params': {
            'objective': str(params.get('objective') or 'Decompose this work') +
                '\nReturn ONLY a JSON array of tasks with id, role, objective, context, '
                'depends_on. Dependencies reference task ids. '
                'Do not execute tasks. All tools and models remain bounded by inherited authority.',
            'tools': [],
        }}
        planned = role_runtime.run_result(planner, context, iteration=iteration)
        if planned.status != 'completed':
            raise FlowExecutionError('fanout planning did not complete')
        raw = planned.output
    specs = expansion_specs(raw)
    emit({'type': 'fanout_expanded', 'node_id': node['id'], 'execution_id': scope,
          'specs': [spec.to_dict() for spec in specs]})
    failures = []
    statuses = {}
    status_lock = threading.Lock()
    failed_publication = threading.Event()
    nodes = {spec.id: {'id': f'{scope}:{spec.id}', 'type': 'role', 'role': spec.role,
                      'params': {'objective': spec.objective,
                                 **({'model_override': spec.model_override} if spec.model_override else {}),
                                 **({'tools': spec.tools} if spec.tools is not None else {})}} for spec in specs}

    class RoleAgent:
        def __init__(self, spec):
            self.spec = spec
            self.messages = []

        def run(self):
            with status_lock:
                failed_dependencies = [dep for dep in self.spec.depends_on
                                       if statuses.get(dep) != 'completed']
            if failed_dependencies:
                with status_lock:
                    statuses[self.spec.id] = 'failed'
                return SubAgentResult(status='failed', error_message=
                    'Required dependencies did not complete: ' + ', '.join(failed_dependencies))
            dependency_context = '\n'.join(str(message.get('content') or '') for message in self.messages)
            try:
                result = role_runtime.run_result(
                    {**nodes[self.spec.id], '_runtimeAbortCheck':
                     lambda: failed_publication.is_set() or abort_check()}, '\n\n'.join(filter(None, (
                        context, self.spec.context, dependency_context))), iteration=iteration)
            except FlowEventDeliveryError as exc:
                failures.append(exc)
                failed_publication.set()
                raise
            with status_lock:
                statuses[self.spec.id] = result.status
            return SubAgentResult(status=result.status, final_answer=result.output,
                                  error_message=result.error)

    scheduler = StreamingScheduler(agent_factory=RoleAgent,
        max_parallel=max_parallel, default_retries=0,
        abort_check=lambda: failed_publication.is_set() or abort_check())
    try:
        admitted = scheduler.add_specs(specs)
        if len(admitted) != len(specs):
            raise FlowExecutionError('fanout tasks must have distinct objectives')
        results = scheduler.run_until_idle()
        if failures:
            raise failures[0]
        if len(results) != len(specs) or any(result.status != 'completed' for _, result in results):
            raise FlowExecutionError('fanout did not complete every dependency')
        outputs = {spec.id: result.final_answer for spec, result in results}
        output = '\n\n'.join(f'[{spec.id}]\n{outputs[spec.id]}' for spec in specs)
        emit({'type': 'fanout_complete', 'node_id': node['id'], 'execution_id': scope,
              'output': event_preview(output), 'status': 'completed'})
        return '\n\n'.join(filter(None, (context, output)))
    finally:
        failed_publication.set()
        scheduler.shutdown(require_quiescence=True)
