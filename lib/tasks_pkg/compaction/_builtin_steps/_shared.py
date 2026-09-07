# HOT_PATH
"""Shared helpers for the built-in Layer-1 compaction steps.

Small, LLM-free utilities used across the step submodules (thinking /
tool-results / interstitial / images / assistant).  They live here so
each step submodule can import them without duplication.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def _log_id(conv_id: str) -> str:
    return conv_id[:8] if conv_id else '?'
