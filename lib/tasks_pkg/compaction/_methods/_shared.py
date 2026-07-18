# HOT_PATH
"""Shared helpers for the experimental compaction method steps.

These small, LLM-free utilities are used across the method families
(dedup / fold / drop / summarize / prune / tail).  They live here so each
step submodule can import them without duplication.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def _log_id(conv_id: str) -> str:
    return conv_id[:8] if conv_id else '?'


def _content_str(msg: dict) -> str | None:
    """Return the message's text content as a string, joining multimodal
    text blocks.  Returns None when there is no text to operate on."""
    c = msg.get('content', '')
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = [b.get('text', '') for b in c
                 if isinstance(b, dict) and b.get('type') == 'text']
        return '\n'.join(parts) if parts else None
    return None


def _already_compacted(text: str) -> bool:
    head = text[:80]
    return (text.startswith('[') and
            ('compacted' in head or 'superseded' in head
             or text.startswith('[Persisted to:')))
