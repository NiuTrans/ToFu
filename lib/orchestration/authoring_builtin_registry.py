"""Canonical built-in definition registry exposed by authoring services."""

from __future__ import annotations

from collections.abc import Callable

from lib.orchestration._builtin_definitions import (
    build_adversarial_definition,
    build_autopilot_definition,
    build_blank_definition,
    build_endpoint_definition,
    build_fanout_definition,
)


_BUILTIN_BUILDERS: dict[str, Callable[..., dict]] = {
    'endpoint': build_endpoint_definition,
    'autopilot': build_autopilot_definition,
    'fanout': build_fanout_definition,
    'adversarial': build_adversarial_definition,
    'blank': build_blank_definition,
}


def builtin_names() -> tuple[str, ...]:
    return tuple(_BUILTIN_BUILDERS)


def build_builtin_definition(name: str, **kwargs) -> dict | None:
    """Build one detached backend-authored reference graph by name."""
    builder = _BUILTIN_BUILDERS.get(str(name or '').strip())
    return builder(**kwargs) if builder is not None else None


__all__ = ['build_builtin_definition', 'builtin_names']
