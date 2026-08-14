"""lib/orchestration — Orchestration definition schema + validator (facade package).

An *orchestration definition* is the declarative graph a user authors in
the frontend Orchestration Studio (``static/js/orchestration.js``). It
describes a topology of ROLE agents and CONTROL nodes wired by directed
edges — an endpoint-style loop, a fan-out/synthesize flow, etc.

This package is the **contract seam**: it owns the schema constants and a
pure ``validate_definition()`` that both the REST store
(``routes/api_v1/orchestrations.py``) and the execution engine
(``lib/orchestration_engine.py``) import. Keeping validation here (not in
the route) means the engine validates with the exact same rules the
authoring API enforced.

The definition is intentionally NOT executed here. Per CLAUDE.md the
frontend authors JSON; the backend stores + validates it now, and the
swarm-backed interpreter (``lib/orchestration_engine.py``) consumes it.

Schema (``tofu.orchestration/v1``)::

    {
      "schema": "tofu.orchestration/v1",
      "name":   "Endpoint Loop",
      "nodes": [
        {"id": "planner1", "type": "role", "role": "planner",
         "name": "Planner", "pos": {"x": 1, "y": 2}, "params": {...}},
        {"id": "loop1", "type": "control", "kind": "loop",
         "pos": {...}, "params": {"max_iterations": 10, ...}}
      ],
      "edges": [{"from": "planner1", "to": "loop1"}]
    }

This file is a PURE RE-EXPORT FACADE. The implementations live in the
sub-modules; ``from lib.orchestration import X`` continues to work
byte-identically for every public + consumer-imported symbol, including the
private helpers (``_USER_EMIT_ROLES``, ``_GENERIC_ROLE_SCHEMA``,
``_ROLE_INFRA_KEYS``, ``_PLANNER_ROLES``, ``_coerce_list``,
``_validate_node_io``, ``_validate_role_params``, ``_validate_subflow_node``,
``_f``, ``_objective_field``) that tests or siblings reference:

  * ``_io``       — the typed node I/O contract axis (VALID_IO_TYPES,
                    node_output_names, parse_io_ref, _validate_node_io,
                    _coerce_list, DEFAULT_OUTPUT_NAME, IO_START_REF).
  * ``field_spec_*`` / ``field_values`` — FieldSpec schema, values and validation.
  * ``_control_specs`` — control kinds, backend-authored FieldSpecs and their
                         field validation contract.
  * ``_definition_contract`` — schema identity, node kinds and structural caps.
  * ``_node_validation`` — node/FieldSpec/I/O validation composition.
  * ``_edge_validation`` — edge references and direction constraints.
  * ``_execution_projection`` — role brief and opening chat-phase projections.
  * ``_topology_diagnostics`` — non-blocking graph execution-hazard analysis.
  * ``_subflow_contract`` — nesting cap and recursive subflow-node rules.
  * ``_defaults`` — detached role/control/subflow authoring-param builders.
  * ``_role_axes`` — role names, execution options and emits/scope resolution.
  * ``_role_specs`` — role FieldSpecs, bounds and structured-field validation.
  * ``_role_personas`` — read-only runtime persona projection.
  * ``_roles`` — compatibility facade over those focused role owners.
  * ``_validate`` — whole-definition validation orchestration.
  * ``_builtin_definitions`` — server-authored reference graph templates.
  * ``_chat_projection`` — authored graph to chat presentation mode.
  * ``_subflow_expansion`` — inline-subflow macro expansion.
  * ``_build`` — compatibility facade over those focused owners.
  * ``_layout``   — layout_definition.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Typed node I/O contract axis
# ═══════════════════════════════════════════════════════════════════════════════

from lib.orchestration.io_contract import (  # noqa: E402,F401
    VALID_IO_TYPES,
    IO_TYPE_ORDER,
    MAX_IO_PORTS,
    IO_PORT_NAME_CONTRACT,
    DEFAULT_OUTPUT_NAME,
    IO_START_REF,
    IO_AUTHORING_PRESETS,
    io_contract_schema,
)
from lib.orchestration.io_values import (  # noqa: E402,F401
    _coerce_list,
    node_output_names,
    parse_io_ref,
)
from lib.orchestration.io_validation import (  # noqa: E402,F401
    _validate_node_io,
)

# Runtime event protocol (live + durable consumers).
from lib.orchestration.events import (  # noqa: E402,F401
    EVENT_SCHEMA,
    event_run_status,
    event_spec,
    is_durable_event,
    runtime_event_contract,
)

# Durable run-header lifecycle protocol.
from lib.orchestration.run_status import (  # noqa: E402,F401
    INITIAL_RUN_STATUS,
    RUN_STATUS_SCHEMA,
    RUN_STATUS_ORDER,
    RUN_STATUS_CATEGORIES,
    VALID_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    is_run_status,
    is_terminal_run_status,
    run_status_contract,
    run_status_contract_schema,
)
from lib.orchestration.run_store_port import (  # noqa: E402,F401
    ORCHESTRATION_RUN_EVENT_PAGE_LIMIT,
    RunEventPage,
    OrchestrationRunStoreError,
    OrchestrationRunStorePort,
    bind_orchestration_run_store,
)
from lib.orchestration.definition_store_port import (  # noqa: E402,F401
    DefinitionStoreConcurrencyError,
    DefinitionStoreMutationPort,
    OrchestrationDefinitionStorePort,
    bind_orchestration_definition_store,
)
from lib.orchestration.database_run_store import (  # noqa: E402,F401
    DatabaseOrchestrationRunStore,
)
from lib.orchestration.sidecar_run_store import (  # noqa: E402,F401
    SidecarOrchestrationRunStore,
)
from lib.orchestration.runtime_ports import (  # noqa: E402,F401
    OrchestrationTaskRuntimePort,
    OrchestrationRuntimePort,
    OrchestrationDefinitionLookupPort,
    OrchestrationRunTransitionPort,
    OrchestrationDurableRunPort,
    OrchestrationDefinitionProvider,
    OrchestrationDurableRunProvider,
)
from lib.orchestration.runtime_mutation_service import (  # noqa: E402,F401
    RuntimeMutationError,
    OrchestrationRuntimeMutationService,
)
from lib.orchestration.run_service import (  # noqa: E402,F401
    RUN_MUTATION_NOT_FOUND,
    RUN_MUTATION_TERMINAL,
    RUN_MUTATION_ACTIVE,
    RUN_MUTATION_CONFLICT,
    RUN_MUTATION_PERSISTENCE_FAILED,
    RunServiceError,
    RunMutationResult,
    RunReplayResult,
    OrchestrationRunService,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Canonical new-node params  (from ._defaults)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.orchestration._defaults import (  # noqa: E402,F401
    node_authoring_params,
    role_node_params,
    control_node_params,
    all_control_node_params,
    subflow_node_params,
)

from lib.orchestration._runtime_params import (  # noqa: E402,F401
    node_runtime_defaults,
    resolve_node_runtime_param,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Focused role contracts (legacy ._roles remains a compatibility facade)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.orchestration._role_axes import (  # noqa: E402,F401
    DEFAULT_ROLE_ISOLATION,
    DEFAULT_ROLE_TIER,
    EXECUTION_OPTION_ORDER,
    KNOWN_ROLES,
    VALID_EMITS,
    VERIFIER_ROLES,
    _USER_EMIT_ROLES,
    VALID_TIERS,
    VALID_ISOLATION,
    VALID_SCOPES,
    resolve_emits,
    resolve_isolation,
    resolve_scope,
    resolve_tier,
)
from lib.orchestration._role_specs import (  # noqa: E402,F401
    MAX_OBJECTIVE_LEN,
    MAX_LIST_ITEMS,
    MAX_LIST_ITEM_LEN,
    VALID_PARAM_KINDS,
    _f,
    _objective_field,
    _GENERIC_ROLE_SCHEMA,
    ROLE_PARAM_SCHEMA,
    _ROLE_INFRA_KEYS,
    _validate_role_params,
    role_param_schema,
)
from lib.orchestration._role_personas import role_persona  # noqa: E402,F401


# ═══════════════════════════════════════════════════════════════════════════════
#  Control-node authoring + validation contract  (from ._control_specs)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.orchestration._control_specs import (  # noqa: E402,F401
    CONTROL_KINDS,
    CONTROL_PARAM_SCHEMA,
    DEFAULT_HUMAN_APPROVAL_TIMEOUT,
    control_param_schema,
    resolve_control_param,
    VALID_ARTIFACT_FORMATS,
    VALID_HUMAN_MODES,
    MAX_ARTIFACT_PATH_LEN,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Execution brief + opening phase projections  (from ._execution_projection)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.orchestration._execution_projection import (  # noqa: E402,F401
    render_role_brief,
    _PLANNER_ROLES,
    first_executed_role,
    initial_phase_for_flow,
)

from lib.orchestration._definition_contract import (  # noqa: E402,F401
    SCHEMA_ID,
    NODE_TYPE_ORDER,
    MAX_NAME_LEN,
    MAX_NODES,
)

from lib.orchestration._subflow_contract import (  # noqa: E402,F401
    MAX_SUBFLOW_DEPTH,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Schema constants + the pure validator  (from ._validate)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.orchestration._validate import (  # noqa: E402,F401
    _validate_subflow_node,
    validate_definition,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Canonical flow builders + subflow expansion  (from ._build)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.orchestration._builtin_definitions import (  # noqa: E402,F401
    build_blank_definition,
    build_endpoint_definition,
    build_autopilot_definition,
    build_fanout_definition,
    build_adversarial_definition,
)
from lib.orchestration._chat_projection import (  # noqa: E402,F401
    chat_projection_for_flow,
)
from lib.orchestration._subflow_expansion import expand_subflows  # noqa: E402,F401


# ═══════════════════════════════════════════════════════════════════════════════
#  Canvas layout  (from ._layout)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.orchestration._layout import (  # noqa: E402,F401
    has_definition_layout,
    layout_definition,
    project_definition_layout,
)


__all__ = [
    'SCHEMA_ID', 'NODE_TYPE_ORDER', 'KNOWN_ROLES', 'CONTROL_KINDS',
    'VALID_TIERS', 'VALID_ISOLATION', 'VALID_ARTIFACT_FORMATS', 'VALID_HUMAN_MODES',
    'DEFAULT_HUMAN_APPROVAL_TIMEOUT',
    'DEFAULT_ROLE_TIER', 'DEFAULT_ROLE_ISOLATION',
    'EXECUTION_OPTION_ORDER', 'VERIFIER_ROLES',
    'VALID_EMITS', 'VALID_SCOPES', 'MAX_SUBFLOW_DEPTH', 'resolve_emits',
    'resolve_tier', 'resolve_isolation', 'resolve_scope',
    'ROLE_PARAM_SCHEMA', 'VALID_PARAM_KINDS',
    'VALID_IO_TYPES', 'IO_TYPE_ORDER', 'MAX_IO_PORTS', 'IO_PORT_NAME_CONTRACT',
    'DEFAULT_OUTPUT_NAME',
    'IO_START_REF', 'IO_AUTHORING_PRESETS', 'io_contract_schema',
    'node_authoring_params', 'role_node_params', 'control_node_params',
    'all_control_node_params',
    'subflow_node_params', 'node_runtime_defaults',
    'resolve_node_runtime_param',
    'node_output_names', 'parse_io_ref',
    'role_param_schema', 'control_param_schema', 'resolve_control_param',
    'role_persona',
    'render_role_brief',
    'first_executed_role', 'initial_phase_for_flow',
    'validate_definition', 'expand_subflows',
    'has_definition_layout', 'layout_definition', 'project_definition_layout',
    'build_blank_definition', 'build_endpoint_definition',
    'build_autopilot_definition', 'build_fanout_definition',
    'build_adversarial_definition',
    'chat_projection_for_flow',
    'RUN_STATUS_SCHEMA', 'RUN_STATUS_ORDER', 'INITIAL_RUN_STATUS',
    'RUN_STATUS_CATEGORIES',
    'VALID_RUN_STATUSES', 'TERMINAL_RUN_STATUSES', 'is_run_status',
    'is_terminal_run_status', 'run_status_contract',
    'run_status_contract_schema',
    'ORCHESTRATION_RUN_EVENT_PAGE_LIMIT',
    'RunEventPage', 'OrchestrationRunStoreError',
    'OrchestrationRunStorePort', 'DatabaseOrchestrationRunStore',
    'SidecarOrchestrationRunStore',
    'bind_orchestration_run_store',
    'DefinitionStoreConcurrencyError', 'DefinitionStoreMutationPort',
    'OrchestrationDefinitionStorePort',
    'bind_orchestration_definition_store',
    'OrchestrationTaskRuntimePort', 'OrchestrationRuntimePort',
    'OrchestrationDefinitionLookupPort',
    'OrchestrationRunTransitionPort', 'OrchestrationDurableRunPort',
    'OrchestrationDefinitionProvider', 'OrchestrationDurableRunProvider',
    'RUN_MUTATION_NOT_FOUND', 'RUN_MUTATION_TERMINAL', 'RUN_MUTATION_ACTIVE',
    'RUN_MUTATION_CONFLICT', 'RUN_MUTATION_PERSISTENCE_FAILED',
    'RunServiceError', 'RunMutationResult', 'RunReplayResult',
    'OrchestrationRunService',
    'RuntimeMutationError', 'OrchestrationRuntimeMutationService',
]
