"""Backend-owned authoring catalogue and canonical built-in definitions.

This module is the application boundary for every value the Studio needs to
author a graph without recreating backend defaults.  Persistence, validation
and definition resolution remain in :mod:`lib.orchestration.service`.
"""

from __future__ import annotations

import copy

from lib.orchestration._control_specs import CONTROL_KINDS
from lib.orchestration._definition_contract import SCHEMA_ID
from lib.orchestration._role_axes import KNOWN_ROLES
from lib.orchestration._role_personas import role_persona
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
    role_contract_response_schema,
)
from lib.orchestration.wire_formats import AUTHORING_CONTRACT_FORMAT


def role_authoring_contract(role: str) -> dict:
    """Return the detached Inspector contract for one role."""
    role_name = str(role or '').strip()
    return {
        'role': role_name,
        'fields': copy.deepcopy(role_param_schema(role_name)),
        'persona': copy.deepcopy(role_persona(role_name)),
    }


def authoring_contract() -> dict:
    """Return the complete detached Orchestration Studio contract."""
    sections = authoring_object_sections()
    io_contract = sections['ioContract']
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
        # Compatibility aliases for clients predating the structured contract.
        'ioTypes': list(io_contract['types']),
        'defaultOutput': io_contract['defaultOutput']['name'],
    }


__all__ = [
    'AUTHORING_OBJECT_SECTION_NAMES', 'authoring_contract',
    'authoring_object_sections', 'authoring_object_section_schemas',
    'authoring_contract_response_schema', 'role_contract_response_schema',
    'build_builtin_definition', 'builtin_names',
    'node_authoring_defaults', 'node_authoring_defaults_schema',
    'node_runtime_defaults',
    'role_authoring_contract',
    'RUNTIME_CONTRACT_SECTION_NAMES',
    'contract_section_registry', 'contract_section_registry_schema',
    'rolling_optional_section_fields',
]
