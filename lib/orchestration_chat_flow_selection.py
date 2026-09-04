"""Policy and definition resolution for Flow-backed chat selection.

This module is the single backend owner of the configuration seam between the
conversation toolbar and orchestration chat execution.  It deliberately knows
nothing about threads, tasks, ``FlowExecutor`` or event translation.
"""

from __future__ import annotations

from typing import Final

from lib.log import get_logger
from lib.orchestration.errors import DefinitionServiceError


logger = get_logger(__name__)


CHAT_FLOW_ENTRY_SELECTED: Final = 'selected'
CHAT_FLOW_ENTRY_AUTOPILOT: Final = 'autopilot'
CHAT_FLOW_ENTRY_NONE: Final = ''

# These are the canonical flows exposed by the conversation toolbar.  The
# authoring catalogue contains additional templates which are not chat modes.
CHAT_FLOW_BUILTINS: Final = frozenset({'autopilot'})


def select_chat_flow_entry(
    config: dict | None,
) -> str:
    """Project chat configuration into one engine entry kind.

    Explicit selections always win. Goal Mode has one execution owner:
    FlowExecutor. There is no rollout branch back into the retired standalone
    virtual-user state machine.
    """
    config = config or {}
    if (config.get('flowDefinition') or config.get('flowBuiltin')
            or config.get('flowId')):
        return CHAT_FLOW_ENTRY_SELECTED
    if config.get('autopilot'):
        return CHAT_FLOW_ENTRY_AUTOPILOT
    return CHAT_FLOW_ENTRY_NONE


def resolve_chat_flow_definition(
    config: dict,
    *,
    definition_service,
) -> tuple[dict | None, str]:
    """Resolve inline, built-in or stored chat selection through one service."""
    name = config.get('flowBuiltin')
    name = name if isinstance(name, str) else ''
    stored_id = config.get('flowId')
    stored_id = stored_id if isinstance(stored_id, str) else ''
    try:
        resolved = definition_service.resolve(
            inline=config.get('flowDefinition'),
            builtin=name,
            stored_id=stored_id,
            require_inline_nodes=True,
        )
    except DefinitionServiceError as error:
        # A selected stored flow remains a soft chat capability: preserve the
        # live fallback when its repository is unavailable. Programmer errors
        # still propagate so they are never mislabeled as missing definitions.
        logger.warning(
            '[FlowChat] failed to resolve stored flow %r: %s',
            stored_id,
            error,
        )
        return None, ''
    if resolved.definition is not None:
        return resolved.definition, resolved.source
    if name:
        logger.warning('[FlowChat] unknown flowBuiltin %r — ignoring', name)
    if stored_id:
        logger.warning(
            '[FlowChat] flowId %r not found in store — ignoring', stored_id)
    return None, ''


__all__ = [
    'CHAT_FLOW_BUILTINS',
    'CHAT_FLOW_ENTRY_AUTOPILOT',
    'CHAT_FLOW_ENTRY_NONE',
    'CHAT_FLOW_ENTRY_SELECTED',
    'resolve_chat_flow_definition',
    'select_chat_flow_entry',
]
