"""lib/conv_config/_flow.py — orchestration flow token parsing.

Splits the toolbar ``activeFlow`` token into the ``flowBuiltin`` / ``flowId``
pair that ``lib.orchestration_endpoint_runner.resolve_chat_flow_entry`` reads.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


#: Built-in flow names the toolbar's ``builtin:<name>`` selector maps to.
#: Mirrors the builders registered in lib.orchestration_endpoint_runner.
_KNOWN_FLOW_BUILTINS = frozenset({'endpoint', 'autopilot'})


def _parse_active_flow(value: Any) -> tuple[str, str]:
    """Split the toolbar ``activeFlow`` token into ``(flow_builtin, flow_id)``.

    The frontend Mode dropdown stores ONE string:
      * ``''`` / non-string        → no flow selected → ('', '')
      * ``'builtin:<name>'``       → a canonical flow → (name, '')
      * any other non-empty string → a stored orchestration id → ('', id)

    These map directly onto the ``flowBuiltin`` / ``flowId`` fields that
    ``lib.orchestration_endpoint_runner.resolve_chat_flow_entry`` reads.
    """
    if not isinstance(value, str) or not value:
        return '', ''
    if value.startswith('builtin:'):
        name = value[len('builtin:'):]
        return (name, '') if name in _KNOWN_FLOW_BUILTINS else ('', '')
    return '', value
