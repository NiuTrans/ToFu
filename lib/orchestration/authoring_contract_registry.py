"""Canonical section registry shared by authoring values and OpenAPI.

The browser receives this document as ``contractSections``.  Keeping its
value and schema projections together prevents response assembly, generators,
and compatibility validation from maintaining parallel section lists.
"""

from __future__ import annotations

from lib.orchestration.contract_schema import contract_snapshot_schema


AUTHORING_OBJECT_SECTION_NAMES = (
    'roles', 'controlSchemas', 'personas', 'defaultEmits',
    'executionOptions', 'nodeDefaults', 'nodeRuntimeDefaults',
    'eventContract', 'runContract',
    'outcomeContract', 'traceContract', 'mutationContract', 'replayContract',
    'inspectionContract', 'definitionListContract',
    'definitionEntryContract', 'runtimeStartContract', 'fieldValueContract',
    'durableRunContract', 'definitionWriteContract', 'requestLimits',
    'ioContract',
)

RUNTIME_CONTRACT_SECTION_NAMES = (
    'requestLimits', 'nodeRuntimeDefaults',
    'eventContract', 'runContract',
    'outcomeContract', 'traceContract', 'mutationContract', 'replayContract',
    'runtimeStartContract', 'durableRunContract',
)

_ROLLING_OPTIONAL_SECTION_FIELDS = {
    'runContract': ('categories',),
    'outcomeContract': ('incompleteStopReasons',),
    'mutationContract': (
        'transportFailureReason', 'clientRetryableReasons',
        'payloadFields',
    ),
    'replayContract': ('caughtUpField',),
    'durableRunContract': ('listEnvelope',),
    'fieldValueContract': ('failureCodes',),
    'ioContract': ('failureCodes',),
    'definitionWriteContract': ('conflictFields',),
}


def rolling_optional_section_fields() -> dict[str, list[str]]:
    """Return detached additive-v1 fields accepted during rolling deploys."""
    return {
        section: list(fields)
        for section, fields in _ROLLING_OPTIONAL_SECTION_FIELDS.items()
    }


def contract_section_registry() -> dict[str, object]:
    """Return the detached backend document published as contractSections."""
    return {
        'authoring': list(AUTHORING_OBJECT_SECTION_NAMES),
        'runtime': list(RUNTIME_CONTRACT_SECTION_NAMES),
        'rollingOptionalFields': rolling_optional_section_fields(),
    }


def _section_name_list_schema(names: tuple[str, ...]) -> dict:
    return {
        'type': 'array',
        'items': {'type': 'string', 'enum': list(names)},
        'minItems': len(names),
        'maxItems': len(names),
        'uniqueItems': True,
    }


def contract_section_registry_schema() -> dict:
    """Return the OpenAPI projection of :func:`contract_section_registry`."""
    registry = contract_section_registry()
    return {
        'type': 'object',
        'required': list(registry),
        'properties': {
            'authoring': _section_name_list_schema(
                AUTHORING_OBJECT_SECTION_NAMES),
            'runtime': _section_name_list_schema(
                RUNTIME_CONTRACT_SECTION_NAMES),
            'rollingOptionalFields': contract_snapshot_schema(
                registry['rollingOptionalFields']),
        },
    }


__all__ = [
    'AUTHORING_OBJECT_SECTION_NAMES',
    'RUNTIME_CONTRACT_SECTION_NAMES',
    'contract_section_registry',
    'contract_section_registry_schema',
    'rolling_optional_section_fields',
]
