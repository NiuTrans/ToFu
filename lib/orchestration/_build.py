"""Compatibility facade for orchestration builders and graph projections.

Implementations are split by responsibility. Internal consumers should import
the focused owner directly; legacy imports from ``._build`` remain valid.
"""

from lib.orchestration._builtin_definitions import (  # noqa: F401
    build_adversarial_definition,
    build_autopilot_definition,
    build_blank_definition,
    build_endpoint_definition,
    build_fanout_definition,
)
from lib.orchestration._chat_projection import (  # noqa: F401
    chat_projection_for_flow,
)
from lib.orchestration._subflow_expansion import expand_subflows  # noqa: F401


__all__ = [
    'build_blank_definition',
    'build_endpoint_definition',
    'build_autopilot_definition',
    'build_fanout_definition',
    'build_adversarial_definition',
    'chat_projection_for_flow',
    'expand_subflows',
]
