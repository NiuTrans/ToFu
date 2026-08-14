"""Canonical data registry for orchestration HTTP operations."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from lib.orchestration.compose_request_contract import (
    compose_request_contract,
)
from lib.orchestration.definition_selection_contract import (
    definition_selection_contract,
)
from lib.orchestration.http_endpoint_model import OrchestrationHttpEndpoint
from lib.orchestration.human_gate_request_contract import (
    human_gate_request_contract,
)
from lib.task_replay import task_replay_request_contract


_COMPOSE = compose_request_contract()
_SELECTION = definition_selection_contract()
_HUMAN_GATE = human_gate_request_contract()
_REPLAY = task_replay_request_contract()


def _endpoint(
    route: str,
    method: str,
    response_contract: str,
    **kwargs,
) -> OrchestrationHttpEndpoint:
    return OrchestrationHttpEndpoint(
        route=route,
        method=method,
        response_contract=response_contract,
        **kwargs,
    )


def _path(field: str, index: int = 0) -> tuple[tuple[str, int], ...]:
    return ((field, index),)


ORCHESTRATION_HTTP_ENDPOINTS: Mapping[
    str, OrchestrationHttpEndpoint,
] = MappingProxyType({
    'definition-list': _endpoint(
        '/api/v1/orchestrations', 'GET', 'definition-list'),
    'definition-read': _endpoint(
        '/api/v1/orchestrations/<orch_id>', 'GET', 'definition-read',
        path_args=_path('orch_id')),
    'definition-create': _endpoint(
        '/api/v1/orchestrations', 'POST', 'definition-save',
        body_arg=0, write_operation='create'),
    'definition-update': _endpoint(
        '/api/v1/orchestrations/<orch_id>', 'PUT', 'definition-save',
        path_args=_path('orch_id'),
        body_arg=1, write_operation='replace',
        write_version_arg=2, write_contract_arg=3),
    'definition-delete': _endpoint(
        '/api/v1/orchestrations/<orch_id>', 'DELETE', 'definition-delete',
        path_args=_path('orch_id'),
        write_operation='delete',
        write_version_arg=1, write_contract_arg=2),
    'validation': _endpoint(
        '/api/v1/orchestrations/validate', 'POST', 'validation',
        body_arg=0, request_options_arg=1),
    'compose': _endpoint(
        '/api/v1/orchestrations/compose', 'POST', 'compose',
        body_args=(
            (_COMPOSE['requirementField'], 0),
            (_COMPOSE['currentField'], 1),
            (_COMPOSE['historyField'], 2),
        )),
    'builtin': _endpoint(
        '/api/v1/orchestrations/builtin/<name>', 'GET', 'builtin',
        path_args=_path('name')),
    'layout': _endpoint(
        '/api/v1/orchestrations/layout', 'POST', 'layout',
        body_args=(
            (_SELECTION['inlineField'], 0),
            (_SELECTION['storedIdField'], 1),
        )),
    'authoring-contract': _endpoint(
        '/api/v1/orchestrations/authoring-contract', 'GET',
        'authoring-contract'),
    'role-schema': _endpoint(
        '/api/v1/orchestrations/role-schema', 'GET', 'authoring-contract',
        query_args=(('role', 0),)),
    'plan': _endpoint(
        '/api/v1/orchestrations/plan', 'POST', 'plan',
        body_args=(
            (_SELECTION['inlineField'], 0),
            (_SELECTION['storedIdField'], 1),
        )),
    'run-start': _endpoint(
        '/api/v1/orchestrations/run', 'POST', 'run-start',
        body_args=(
            (_SELECTION['inlineField'], 0),
            (_SELECTION['inputField'], 1),
            (_SELECTION['storedIdField'], 2),
            (_SELECTION['originField'], 3),
        )),
    'run-poll': _endpoint(
        '/api/v1/orchestrations/run/poll/<task_id>', 'GET', 'run-poll',
        path_args=_path('task_id'),
        query_args=((_REPLAY['queryField'], 1),)),
    'run-abort': _endpoint(
        '/api/v1/orchestrations/run/abort/<task_id>', 'POST', 'mutation',
        path_args=_path('task_id')),
    'human-approve': _endpoint(
        '/api/v1/orchestrations/run/human-approve', 'POST', 'mutation',
        body_args=(
            (_HUMAN_GATE['requestIdField'], 0),
            (_HUMAN_GATE['approvalField'], 1),
        )),
    'human-input': _endpoint(
        '/api/v1/orchestrations/run/human-input', 'POST', 'mutation',
        body_args=(
            (_HUMAN_GATE['requestIdField'], 0),
            (_HUMAN_GATE['inputField'], 1),
        )),
    'task-list': _endpoint(
        '/api/v1/orchestrations/tasks', 'GET', 'task-list',
        query_args=(('status', 0), ('orch_id', 1), ('limit', 2))),
    'task-read': _endpoint(
        '/api/v1/orchestrations/tasks/<run_id>', 'GET', 'task-read',
        path_args=_path('run_id')),
    'task-create': _endpoint(
        '/api/v1/orchestrations/tasks', 'POST', 'task-create',
        body_args=(
            (_SELECTION['inlineField'], 0),
            (_SELECTION['inputField'], 1),
            (_SELECTION['storedIdField'], 2),
            (_SELECTION['originField'], 3),
        )),
    'task-events': _endpoint(
        '/api/v1/orchestrations/tasks/<run_id>/events', 'GET', 'task-events',
        path_args=_path('run_id'),
        query_args=((_REPLAY['queryField'], 1),)),
    'task-abort': _endpoint(
        '/api/v1/orchestrations/tasks/<run_id>/abort', 'POST', 'mutation',
        path_args=_path('run_id')),
    'task-remove': _endpoint(
        '/api/v1/orchestrations/tasks/<run_id>', 'DELETE', 'mutation',
        path_args=_path('run_id')),
})


__all__ = ['ORCHESTRATION_HTTP_ENDPOINTS']
