"""Backend-owned authoring catalogue and canonical built-in definitions.

This module is the application boundary for every value the Studio needs to
author a graph without recreating backend defaults.  Persistence, validation
and definition resolution live in their focused application modules.
"""

from __future__ import annotations

import copy

from lib.orchestration._control_specs import CONTROL_KINDS
from lib.orchestration._definition_contract import SCHEMA_ID
from lib.orchestration._role_axes import KNOWN_ROLES
from lib.orchestration._role_specs import (
    VALID_PARAM_KINDS,
    role_param_schema,
)
from lib.orchestration.authoring_builtin_registry import (
    build_builtin_definition,
    builtin_names,
)
from lib.orchestration.authoring_contract_registry import (
    AUTHORING_OBJECT_SECTION_NAMES,
    RUNTIME_CONTRACT_SECTION_NAMES,
    contract_section_registry,
    contract_section_registry_schema,
    rolling_optional_section_fields,
)
from lib.orchestration.authoring_contract_sections import (
    authoring_object_sections,
    node_authoring_defaults,
)
from lib.orchestration._runtime_params import node_runtime_defaults
from lib.orchestration.authoring_contract_schema import (
    authoring_contract_response_schema,
    authoring_object_section_schemas,
    node_authoring_defaults_schema,
)
from lib.orchestration.wire_formats import AUTHORING_CONTRACT_FORMAT


def authoring_contract() -> dict:
    """Return the complete detached Orchestration Studio contract."""
    sections = authoring_object_sections()
    role_names = sorted(KNOWN_ROLES)
    return {
        'format': AUTHORING_CONTRACT_FORMAT,
        'roleNames': role_names,
        'generic': copy.deepcopy(role_param_schema('__generic__')),
        'kinds': sorted(VALID_PARAM_KINDS),
        'schema': SCHEMA_ID,
        'controls': copy.deepcopy(CONTROL_KINDS),
        'builtins': list(builtin_names()),
        'contractSections': contract_section_registry(),
        **sections,
    }


__all__ = [
    'AUTHORING_OBJECT_SECTION_NAMES', 'authoring_contract',
    'authoring_object_sections', 'authoring_object_section_schemas',
    'authoring_contract_response_schema',
    'build_builtin_definition', 'builtin_names',
    'node_authoring_defaults', 'node_authoring_defaults_schema',
    'node_runtime_defaults',
    'RUNTIME_CONTRACT_SECTION_NAMES',
    'contract_section_registry', 'contract_section_registry_schema',
    'rolling_optional_section_fields',
]
