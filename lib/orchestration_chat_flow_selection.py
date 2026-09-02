"""Policy and definition resolution for Flow-backed chat selection.

This module is the single backend owner of the configuration seam between the
conversation toolbar and orchestration chat execution.  It deliberately knows
nothing about threads, tasks, ``FlowExecutor`` or event translation.
"""

from __future__ import annotations

import os
from collections.abc import Callable
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


def _flag_on(name: str) -> bool:
    value = os.environ.get(name, '0').strip().lower()
    return value in ('1', 'true', 'yes', 'on')


def autopilot_via_flow_enabled() -> bool:
    """Return whether the autopilot-mode toggle uses the Flow engine."""
    return _flag_on('TOFU_AUTOPILOT_VIA_FLOW')


def select_chat_flow_entry(
    config: dict | None,
    *,
    autopilot_enabled: bool | Callable[[], bool],
) -> str:
    """Project chat configuration into one engine entry kind.

    Explicit selections always win and are their own opt-in.  The goal-mode
    toggle uses the Flow engine only when its rollout flag is enabled.
    """
    config = config or {}
    if (config.get('flowDefinition') or config.get('flowBuiltin')
            or config.get('flowId')):
        return CHAT_FLOW_ENTRY_SELECTED
    if config.get('autopilot') and _enabled(autopilot_enabled):
        return CHAT_FLOW_ENTRY_AUTOPILOT
    return CHAT_FLOW_ENTRY_NONE


def _enabled(value: bool | Callable[[], bool]) -> bool:
    return bool(value() if callable(value) else value)


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
    'autopilot_via_flow_enabled',
    'resolve_chat_flow_definition',
    'select_chat_flow_entry',
]
