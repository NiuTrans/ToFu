"""Pure inspection and canonicalization of orchestration definitions.

This module is shared by repository writes, authoring, dry-run preview and
runtime start adapters.  It deliberately has no store or HTTP dependency.
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.orchestration._chat_projection import chat_projection_for_flow
from lib.orchestration._definition_contract import SCHEMA_ID
from lib.orchestration._execution_projection import initial_phase_for_flow
from lib.orchestration._validate import validate_definition
from lib.orchestration.field_values import canonicalize_definition_field_values
from lib.orchestration.wire_formats import INSPECTION_FORMAT


@dataclass(frozen=True)
class PreparedDefinition:
    """One inspected definition and its canonical executable snapshot."""

    definition: dict | None
    inspection: dict

    @property
    def valid(self) -> bool:
        return bool(self.inspection.get('ok'))


def inspect_definition(definition: dict, *, include_plan: bool = False) -> dict:
    """Return the authoring/execution contract for a graph in one response."""
    verdict = validate_definition(definition)
    errors = list(verdict.get('errors') or [])
    warnings = list(verdict.get('warnings') or [])
    raw_diagnostics = verdict.get('diagnostics')
    diagnostics = (
        [dict(item) for item in raw_diagnostics if isinstance(item, dict)]
        if isinstance(raw_diagnostics, list)
        else [
            {'severity': severity, 'code': '', 'path': '', 'message': message}
            for severity, messages in (
                ('error', errors),
                ('warning', warnings),
            )
            for message in messages
        ]
    )
    result = {
        'format': INSPECTION_FORMAT,
        'ok': bool(verdict.get('ok')),
        'errors': errors,
        'warnings': warnings,
        'diagnostics': diagnostics,
        'contract': {
            'schema': SCHEMA_ID,
            'projection': chat_projection_for_flow(definition),
            'initialPhase': initial_phase_for_flow(definition),
            'nodes': len(definition.get('nodes') or [])
                     if isinstance(definition, dict) else 0,
            'edges': len(definition.get('edges') or [])
                     if isinstance(definition, dict) else 0,
        },
    }
    if include_plan:
        from lib.orchestration_plan import compile_plan
        result['plan'] = compile_plan(definition)
    return result


def prepare_definition(definition: dict, *, include_plan: bool = False) \
        -> PreparedDefinition:
    """Inspect once, then canonicalize only a definition that passed."""
    inspection = inspect_definition(definition, include_plan=include_plan)
    canonical = (canonicalize_definition_field_values(definition)
                 if inspection['ok'] else None)
    return PreparedDefinition(canonical, inspection)


__all__ = [
    'PreparedDefinition',
    'inspect_definition',
    'prepare_definition',
]
