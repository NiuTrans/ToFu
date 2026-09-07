"""Shared inline, builtin and stored definition selection policy."""

from __future__ import annotations

import copy
from collections.abc import Callable

from lib.orchestration.authoring_contract import build_builtin_definition
from lib.orchestration.definition_results import ResolvedDefinition


def resolve_definition(
    *,
    inline: dict | None = None,
    builtin: str = '',
    stored_id: str = '',
    load_stored: Callable[[str], dict | None] | None = None,
    require_inline_nodes: bool = False,
) -> ResolvedDefinition:
    """Resolve one definition using the common inline→builtin→stored order."""
    if isinstance(inline, dict) and (
            not require_inline_nodes or bool(inline.get('nodes'))):
        return ResolvedDefinition(copy.deepcopy(inline), 'inline')

    if builtin:
        definition = build_builtin_definition(builtin)
        if definition is not None:
            return ResolvedDefinition(definition, f'builtin:{builtin}')

    if stored_id and load_stored is not None:
        definition = load_stored(stored_id)
        if isinstance(definition, dict):
            return ResolvedDefinition(
                copy.deepcopy(definition),
                f'stored:{stored_id}',
                stored_id,
            )

    return ResolvedDefinition(None)


__all__ = ['resolve_definition']
