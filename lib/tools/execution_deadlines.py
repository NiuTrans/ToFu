"""Derive execution deadlines from model-visible bounded-wait parameters."""
from __future__ import annotations

import math
from collections.abc import Mapping

# Liveness budget for collecting a wait result and unwinding its handler.
TOOL_WAIT_COMPLETION_ALLOWANCE_MS = 60_000
_WAIT_PARAMETERS = frozenset({'timeout_seconds', 'wait_s'})


def declared_wait_seconds(tool_schema, *, arguments: Mapping | None = None) -> float | None:
    """Return this call's wait bound, or its worst case when arguments are omitted."""
    function = tool_schema.get('function', tool_schema)
    parameters = function.get('parameters', {})
    properties = parameters.get('properties', {})
    bounds = []
    for name, parameter in properties.items():
        if name not in _WAIT_PARAMETERS or parameter.get('type') not in {'integer', 'number'}:
            continue
        maximum = parameter.get('maximum')
        if maximum is None:
            continue
        if (not isinstance(maximum, (int, float)) or isinstance(maximum, bool)
                or not math.isfinite(maximum)):
            raise ValueError(f'{function.get("name", "tool")} wait parameter {name} lacks a finite maximum')
        if arguments is None:
            requested = maximum
        else:
            requested = arguments.get(name, parameter.get('default', 0))
        if not isinstance(requested, (int, float)) or isinstance(requested, bool):
            requested = maximum
        bounds.append(min(maximum, max(0.0, requested)))
    return max(bounds, default=None)


def wait_execution_timeout_ms(tool_schema, *, arguments: Mapping | None = None) -> int | None:
    """Cover the complete wait plus its completion path; snapshots stay ordinary calls."""
    wait_seconds = declared_wait_seconds(tool_schema, arguments=arguments)
    if wait_seconds is None or wait_seconds <= 0:
        return None
    return math.ceil(wait_seconds * 1000) + TOOL_WAIT_COMPLETION_ALLOWANCE_MS
