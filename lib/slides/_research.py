"""Compatibility imports for the shared production research contract.

The implementation moved to :mod:`lib.production.research` so decks and
motion videos consume one evidence bundle. This module intentionally exports
the same function objects for historical slide imports and checkpoints.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.production.research import (
    RESEARCH_RESUME_TTL_S,
    current_fact_errors,
    evidence_checkpoint_version,
    format_research_cards,
    gate_research_bundle,
    research_topic,
    summarise_current_signals,
)

logger = get_logger(__name__)

__all__ = [
    'RESEARCH_RESUME_TTL_S', 'current_fact_errors',
    'evidence_checkpoint_version', 'format_research_cards', 'gate_research_bundle',
    'research_topic', 'summarise_current_signals',
]
