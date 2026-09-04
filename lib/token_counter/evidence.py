# HOT_PATH
"""Validated request-local token evidence shared across dispatch stages.

The root prompt-admission boundary may count one complete provider prompt once
and carry that positive integer through retries.  The private body key is an
in-process optimization hint, never a provider-wire field; invalid or absent
values always make consumers fall back to their local estimator.
"""

from __future__ import annotations

from typing import Any


ADMITTED_INPUT_TOKENS_KEY = '_admitted_input_tokens'


def validated_admitted_input_tokens(value: Any) -> int | None:
    """Return trusted positive integer evidence, excluding bool-as-int."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


__all__ = [
    'ADMITTED_INPUT_TOKENS_KEY',
    'validated_admitted_input_tokens',
]
