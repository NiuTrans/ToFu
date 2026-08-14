"""Compatibility facade for focused orchestration application services.

Stored-definition application logic lives in ``definition_service.py`` and
repository-free authoring lives in ``authoring_service.py`` while runtime
execution lives in ``runtime_service.py``. Their public symbols remain
re-exported here for rolling extensions; production adapters import focused
owners directly.
"""

from __future__ import annotations

from lib.orchestration.authoring_contract import (
    AUTHORING_OBJECT_SECTION_NAMES,
    RUNTIME_CONTRACT_SECTION_NAMES,
    authoring_contract,
    authoring_object_sections,
    build_builtin_definition,
    builtin_names,
    node_authoring_defaults,
    role_authoring_contract,
)
from lib.orchestration.authoring_service import (
    AuthoringBuiltinResult,
    AuthoringPlanResult,
    AuthoringServiceError,
    OrchestrationAuthoringService,
    compose_definition,
    inspect_builtin_definition,
    layout_authoring_definition,
    plan_authoring_definition,
)
from lib.orchestration.application_services import (
    OrchestrationApplicationServices,
)
from lib.orchestration.definition_service import (
    DefinitionDeleteResult,
    DefinitionServiceError,
    DefinitionWriteResult,
    OrchestrationDefinitionService,
    ResolvedDefinition,
    resolve_definition,
)
from lib.orchestration.definition_inspection import (
    PreparedDefinition,
    inspect_definition,
    prepare_definition,
)
from lib.orchestration.durable_projection import (
    DurableProjectionError,
    DurableRunProjection,
)
from lib.orchestration.runtime_service import (
    FlowEventSink,
    FlowRunOutcome,
    create_flow_executor,
    execute_flow,
    execute_runtime_flow,
    finish_runtime,
    spawn_runtime_flow,
)
from lib.orchestration.runtime_start_service import (
    OrchestrationRuntimeStartService,
    RuntimeStartError,
)
from lib.orchestration.runtime_mutation_service import (
    OrchestrationRuntimeMutationService,
    RuntimeMutationError,
)
from lib.orchestration.human_gate_service import (
    HumanGateServiceError,
    OrchestrationHumanGateService,
)
from lib.orchestration.definition_wire_contracts import (
    definition_entry_contract,
    definition_entry_summary,
    definition_list_contract,
    definition_request_schema,
    definition_write_conflict,
    definition_write_contract,
    parse_definition_write_precondition,
    project_definition_entry,
    project_definition_list,
)
from lib.orchestration.inspection_wire_contract import (
    inspection_response_fields,
)
from lib.orchestration.runtime_wire_contracts import (
    RUNTIME_START_KINDS,
    project_runtime_start,
    runtime_start_contract,
)
from lib.orchestration.wire_formats import (
    AUTHORING_CONTRACT_FORMAT,
    DEFINITION_ENTRY_FORMAT,
    DEFINITION_LIST_FORMAT,
    DEFINITION_WRITE_FORMAT,
    INSPECTION_FORMAT,
    RUNTIME_START_FORMAT,
)


__all__ = [
    'DefinitionServiceError', 'AuthoringServiceError',
    'ResolvedDefinition', 'DefinitionWriteResult', 'DefinitionDeleteResult',
    'PreparedDefinition', 'prepare_definition',
    'OrchestrationDefinitionService', 'FlowRunOutcome', 'FlowEventSink',
    'DurableProjectionError', 'DurableRunProjection',
    'builtin_names', 'role_authoring_contract', 'node_authoring_defaults',
    'AUTHORING_OBJECT_SECTION_NAMES', 'authoring_contract',
    'RUNTIME_CONTRACT_SECTION_NAMES',
    'authoring_object_sections', 'definition_request_schema',
    'definition_entry_summary', 'definition_list_contract',
    'definition_entry_contract', 'project_definition_list',
    'project_definition_entry',
    'definition_write_contract', 'definition_write_conflict',
    'parse_definition_write_precondition',
    'build_builtin_definition', 'AuthoringBuiltinResult',
    'AuthoringPlanResult',
    'OrchestrationAuthoringService',
    'OrchestrationApplicationServices',
    'compose_definition', 'inspect_builtin_definition',
    'layout_authoring_definition', 'plan_authoring_definition',
    'resolve_definition', 'inspect_definition', 'inspection_response_fields',
    'AUTHORING_CONTRACT_FORMAT', 'INSPECTION_FORMAT',
    'DEFINITION_WRITE_FORMAT', 'DEFINITION_LIST_FORMAT',
    'DEFINITION_ENTRY_FORMAT',
    'RUNTIME_START_FORMAT', 'RUNTIME_START_KINDS',
    'runtime_start_contract', 'project_runtime_start',
    'create_flow_executor', 'execute_flow', 'finish_runtime',
    'execute_runtime_flow', 'spawn_runtime_flow',
    'RuntimeStartError', 'OrchestrationRuntimeStartService',
    'RuntimeMutationError', 'OrchestrationRuntimeMutationService',
    'HumanGateServiceError', 'OrchestrationHumanGateService',
]
