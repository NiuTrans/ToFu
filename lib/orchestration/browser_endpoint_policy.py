"""Browser-only method and response policies for orchestration endpoints."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from lib.orchestration.response_required_fields import (
    ORCHESTRATION_RESPONSE_REQUIRED_FIELDS,
)


ORCHESTRATION_RESPONSE_OPTIONS: Mapping[str, str] = MappingProxyType({
    'definition-list': 'normalizeList',
    'definition-read': 'normalizeRead',
    'definition-save': 'normalizeSave',
    'definition-delete': 'normalizeDelete',
    'validation': 'normalizeRead',
    'compose': 'normalizeComposeResult',
    'builtin': 'normalizeBuiltin',
    'layout': 'normalizeLayout',
    'authoring-contract': 'normalizeRead',
    'plan': 'normalizePlan',
    'run-start': 'normalizeStart',
    'run-poll': 'normalizePoll',
    'mutation': 'normalizeMutation',
    'task-list': 'normalizeList',
    'task-read': 'normalizeRead',
    'task-create': 'normalizeCreate',
    'task-events': 'normalizeEvents',
})


ORCHESTRATION_CLIENT_METHODS: Mapping[
    str, tuple[str, str],
] = MappingProxyType({
    'definition-list': ('listResult', 'list'),
    'definition-read': ('getResult', 'get'),
    'definition-create': ('save', 'create'),
    'definition-update': ('save', 'update'),
    'definition-delete': ('remove', 'remove'),
    'validation': ('validateResult', 'validate'),
    'compose': ('composeResult', 'compose'),
    'builtin': ('builtinResult', 'builtin'),
    'layout': ('layoutResult', 'layout'),
    'authoring-contract': ('authoringContractResult', 'authoringContract'),
    'role-schema': ('roleSchemaResult', 'roleSchema'),
    'plan': ('planResult', 'plan'),
    'run-start': ('runResult', 'run'),
    'run-poll': ('runPollResult', 'runPoll'),
    'run-abort': ('runAbort', 'runAbort'),
    'human-approve': ('humanApprove', 'humanApprove'),
    'human-input': ('humanInput', 'humanInput'),
    'task-list': ('taskListResult', 'taskList'),
    'task-read': ('taskGet', 'taskGet'),
    'task-create': ('taskCreate', 'taskCreate'),
    'task-events': ('taskEventsResult', 'taskEvents'),
    'task-abort': ('taskAbort', 'taskAbort'),
    'task-remove': ('taskRemove', 'taskRemove'),
})


__all__ = [
    'ORCHESTRATION_CLIENT_METHODS',
    'ORCHESTRATION_RESPONSE_REQUIRED_FIELDS',
    'ORCHESTRATION_RESPONSE_OPTIONS',
]
