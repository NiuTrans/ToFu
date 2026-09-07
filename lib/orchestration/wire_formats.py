"""Stable orchestration wire-format identifiers with one physical owner."""

AUTHORING_CONTRACT_FORMAT = 'tofu.orchestration.authoring-contract/v1'
DEFINITION_FORMAT = 'tofu.orchestration/v1'
INSPECTION_FORMAT = 'tofu.orchestration.inspection/v1'
DEFINITION_WRITE_FORMAT = 'tofu.orchestration.definition-write/v1'
DEFINITION_LIST_FORMAT = 'tofu.orchestration.definition-list/v1'
DEFINITION_ENTRY_FORMAT = 'tofu.orchestration.definition-entry/v1'
EVENTS_FORMAT = 'tofu.orchestration.events/v1'
RUN_STATUS_FORMAT = 'tofu.orchestration.run-status/v1'
TRACE_FORMAT = 'tofu.orchestration.trace/v1'
OUTCOME_FORMAT = 'tofu.orchestration.outcome/v1'
MUTATION_FORMAT = 'tofu.orchestration.mutation/v1'
TASK_REPLAY_FORMAT = 'tofu.task-replay/v1'
FIELD_VALUE_FORMAT = 'tofu.orchestration.field-value/v1'
RUNTIME_START_FORMAT = 'tofu.orchestration.runtime-start/v1'
DURABLE_RUN_FORMAT = 'tofu.orchestration.durable-run/v1'


_ORCHESTRATION_WIRE_FORMATS = {
    'authoring-contract': AUTHORING_CONTRACT_FORMAT,
    'definition': DEFINITION_FORMAT,
    'inspection': INSPECTION_FORMAT,
    'definition-write': DEFINITION_WRITE_FORMAT,
    'definition-list': DEFINITION_LIST_FORMAT,
    'definition-entry': DEFINITION_ENTRY_FORMAT,
    'events': EVENTS_FORMAT,
    'run-status': RUN_STATUS_FORMAT,
    'trace': TRACE_FORMAT,
    'outcome': OUTCOME_FORMAT,
    'mutation': MUTATION_FORMAT,
    'task-replay': TASK_REPLAY_FORMAT,
    'field-value': FIELD_VALUE_FORMAT,
    'runtime-start': RUNTIME_START_FORMAT,
    'durable-run': DURABLE_RUN_FORMAT,
}


def orchestration_wire_formats() -> dict[str, str]:
    """Return a detached registry shared with browser parity checks."""
    return dict(_ORCHESTRATION_WIRE_FORMATS)


__all__ = [
    'AUTHORING_CONTRACT_FORMAT',
    'DEFINITION_FORMAT',
    'INSPECTION_FORMAT',
    'DEFINITION_WRITE_FORMAT',
    'DEFINITION_LIST_FORMAT',
    'DEFINITION_ENTRY_FORMAT',
    'EVENTS_FORMAT',
    'RUN_STATUS_FORMAT',
    'TRACE_FORMAT',
    'OUTCOME_FORMAT',
    'MUTATION_FORMAT',
    'TASK_REPLAY_FORMAT',
    'FIELD_VALUE_FORMAT',
    'RUNTIME_START_FORMAT',
    'DURABLE_RUN_FORMAT',
    'orchestration_wire_formats',
]
