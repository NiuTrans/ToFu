"""JSON-safe diagnostic values and bounded log formatting.

Entry points: text_value, bound_text, sanitize_value, BoundedFormatter.
Standard-library only; no credential detection or content replacement.
Record size, recursive structure and unprintable-value handling stay bounded.
"""
from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping, Sequence

_UNPRINTABLE_PREFIX = '<unprintable:'


def _safe_string(value: object) -> str:
    try:
        return str(value)
    except Exception:
        name = getattr(type(value), '__name__', 'object')
        return '%s%s>' % (_UNPRINTABLE_PREFIX, str(name)[:64])


def log_record_max_chars() -> int:
    try:
        value = int(os.environ.get('TOFU_LOG_RECORD_MAX_CHARS', '') or 16_384)
    except (TypeError, ValueError, OverflowError):
        value = 16_384
    return max(4_096, min(262_144, value))


def bound_text(value: object, max_chars: int) -> str:
    """Keep a bounded head and tail, explicitly recording omitted length."""
    text = _safe_string(value)
    limit = max(128, int(max_chars))
    if len(text) <= limit:
        return text
    # Reserve for the longest possible count. The final omitted count cannot
    # have more digits, so the rendered result remains within ``limit``.
    marker_reserve = f'\n… <log policy omitted {len(text)} chars> …\n'
    available = max(32, limit - len(marker_reserve))
    head = int(available * 0.62)
    tail = available - head
    head_text = text[:head]
    tail_start = len(text) - tail
    tail_text = text[tail_start:]
    omitted = len(text) - len(head_text) - len(tail_text)
    marker = f'\n… <log policy omitted {omitted} chars> …\n'
    return head_text + marker + tail_text


def text_value(value: object, *, max_chars: int | None = None) -> str:
    """Preserve diagnostic text, optionally retaining a bounded head and tail."""
    return _safe_string(value) if max_chars is None else bound_text(value, max_chars)


def sanitize_value(value: object, *, depth: int = 0,
                   max_depth: int = 5, max_items: int = 50,
                   max_string_chars: int = 2_000):
    """Return a JSON-safe value with explicit depth, item and string bounds."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else '<non-finite-number>'
    if isinstance(value, str):
        return text_value(value, max_chars=max_string_chars)
    if isinstance(value, bytes):
        return f'<bytes:{len(value)}>'
    if depth >= max_depth:
        return '<max-depth>'
    if isinstance(value, Mapping):
        try:
            output = {}
            item_limit = max(1, int(max_items))
            value_length = len(value)
            truncated = value_length > item_limit
            retained_limit = item_limit - 1 if truncated else item_limit
            for index, (key, item) in enumerate(value.items()):
                if index >= retained_limit:
                    break
                safe_key = text_value(key, max_chars=128)
                output[safe_key] = sanitize_value(
                    item, depth=depth + 1,
                    max_depth=max_depth, max_items=max_items,
                    max_string_chars=max_string_chars)
            if truncated:
                output['<truncated>'] = (
                    f'{value_length - retained_limit} more field(s)')
            return output
        except Exception:
            return {'<unserializable-mapping>': type(value).__name__[:64]}
    if isinstance(value, Sequence):
        try:
            item_limit = max(1, int(max_items))
            value_length = len(value)
            truncated = value_length > item_limit
            retained_limit = item_limit - 1 if truncated else item_limit
            items = [sanitize_value(
                item, depth=depth + 1, max_depth=max_depth,
                max_items=max_items, max_string_chars=max_string_chars)
                for item in value[:retained_limit]]
            if truncated:
                items.append(
                    f'<{value_length - retained_limit} more item(s)>')
            return items
        except Exception:
            return ['<unserializable-sequence:%s>' % type(value).__name__[:64]]
    return text_value(value, max_chars=max_string_chars)


class BoundedFormatter(logging.Formatter):
    """A normal formatter with a strict physical-record size bound."""

    def __init__(self, *args, max_chars: int | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_chars = max_chars

    def format(self, record: logging.LogRecord) -> str:
        # Format strings used by the production server contain these optional
        # fields.  Supplying defaults here also makes records emitted directly
        # by third-party handlers safe during early boot.
        if not hasattr(record, 'tofu_correlation_prefix'):
            record.tofu_correlation_prefix = ''
        if not hasattr(record, 'tofu_coalesce_note'):
            record.tofu_coalesce_note = ''
        rendered = super().format(record)
        return text_value(
            rendered,
            max_chars=(self.max_chars if self.max_chars is not None
                       else log_record_max_chars()),
        )


__all__ = ['BoundedFormatter', 'bound_text', 'log_record_max_chars', 'text_value', 'sanitize_value']
