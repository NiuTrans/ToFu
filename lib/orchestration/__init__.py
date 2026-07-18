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
  * ``_roles``    — the role axis (KNOWN_ROLES, VALID_EMITS/SCOPES/TIERS,
                    ROLE_PARAM_SCHEMA, resolve_emits, resolve_scope,
                    role_param_schema, role_persona, VALID_PARAM_KINDS).
  * ``_validate`` — schema constants + validate_definition, render_role_brief,
                    first_executed_role, initial_phase_for_flow, CONTROL_KINDS,
                    SCHEMA_ID, MAX_SUBFLOW_DEPTH.
  * ``_build``    — build_endpoint_definition, build_autopilot_definition,
                    expand_subflows.
  * ``_layout``   — layout_definition.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Typed node I/O contract axis  (from ._io)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.orchestration._io import (  # noqa: E402,F401
    VALID_IO_TYPES,
    MAX_IO_PORTS,
    DEFAULT_OUTPUT_NAME,
    IO_START_REF,
    _coerce_list,
    node_output_names,
    parse_io_ref,
    _validate_node_io,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Role axis: schema, emits/scope, per-role params  (from ._roles)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.orchestration._roles import (  # noqa: E402,F401
    KNOWN_ROLES,
    VALID_EMITS,
    _USER_EMIT_ROLES,
    VALID_TIERS,
    VALID_ISOLATION,
    VALID_SCOPES,
    MAX_OBJECTIVE_LEN,
    MAX_LIST_ITEMS,
    MAX_LIST_ITEM_LEN,
    VALID_PARAM_KINDS,
    _f,
    _objective_field,
    _GENERIC_ROLE_SCHEMA,
    ROLE_PARAM_SCHEMA,
    resolve_emits,
    resolve_scope,
    _ROLE_INFRA_KEYS,
    _validate_role_params,
    role_param_schema,
    role_persona,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Schema constants + the pure validator  (from ._validate)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.orchestration._validate import (  # noqa: E402,F401
    SCHEMA_ID,
    MAX_SUBFLOW_DEPTH,
    CONTROL_KINDS,
    VALID_ARTIFACT_FORMATS,
    VALID_HUMAN_MODES,
    MAX_ARTIFACT_PATH_LEN,
    MAX_NAME_LEN,
    MAX_NODES,
    render_role_brief,
    _PLANNER_ROLES,
    first_executed_role,
    initial_phase_for_flow,
    _validate_subflow_node,
    validate_definition,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Canonical flow builders + subflow expansion  (from ._build)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.orchestration._build import (  # noqa: E402,F401
    build_endpoint_definition,
    build_autopilot_definition,
    expand_subflows,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Canvas layout  (from ._layout)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.orchestration._layout import layout_definition  # noqa: E402,F401


__all__ = [
    'SCHEMA_ID', 'KNOWN_ROLES', 'CONTROL_KINDS',
    'VALID_TIERS', 'VALID_ISOLATION', 'VALID_ARTIFACT_FORMATS', 'VALID_HUMAN_MODES',
    'VALID_EMITS', 'VALID_SCOPES', 'MAX_SUBFLOW_DEPTH', 'resolve_emits',
    'resolve_scope', 'ROLE_PARAM_SCHEMA', 'VALID_PARAM_KINDS',
    'VALID_IO_TYPES', 'MAX_IO_PORTS', 'DEFAULT_OUTPUT_NAME', 'IO_START_REF',
    'node_output_names', 'parse_io_ref',
    'role_param_schema', 'role_persona', 'render_role_brief',
    'first_executed_role', 'initial_phase_for_flow',
    'validate_definition', 'expand_subflows',
    'layout_definition', 'build_endpoint_definition', 'build_autopilot_definition',
]
