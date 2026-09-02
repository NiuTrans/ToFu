"""Fail-closed redaction and record-size bounds for durable diagnostics.

The formatter is intentionally independent from the rest of the application:
it runs on messages from business code, third-party libraries and early boot
paths.  It removes common credential carriers before text reaches a durable
sink and applies a middle-truncation bound so one echoed payload can never
create a multi-megabyte log record while still preserving traceback heads and
terminal exception lines.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Mapping, Sequence


_BEARER_RE = re.compile(r'(?i)\b((?:Bearer|Basic)\s+)([^\s,;\]\[{}"\']{4,})')
# Key/value text appears as shell assignments, Python reprs and JSON.  Match
# the complete key (including provider prefixes such as ``github_token``), and
# allow the authentication scheme to sit inside a quoted value.
_NAMED_SECRET_RE = re.compile(
    r'(?i)(["\']?(?:[a-z0-9_.-]+[_-])?'
    r'(?:api[_-]?keys?|access[_-]?token|auth[_-]?token|authorization|bearer|'
    r'client[_-]?secret|cookie|credentials?|password|passwd|private[_-]?key|'
    r'proxy[_-]?authorization|refresh[_-]?token|secret|session[_-]?key|token|'
    r'x[_-]?api[_-]?key)["\']?\s*[:=]\s*)'
    r'(["\']?)(?:(?:Bearer|Basic)\s+)?([^\s,"\';}\]]{1,})(["\']?)')
_HEADER_LINE_RE = re.compile(
    r'(?im)(\b(?:authorization|proxy-authorization|x-api-key|api-key|cookie|'
    r'set-cookie)\s*:\s*)[^\r\n]+')
_ENV_ASSIGN_RE = re.compile(
    r'(?m)^(\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*)(.*)$')
_QUERY_RE = re.compile(
    r'(?i)([?&][a-z0-9_.%-]*(?:api[_-]?keys?|access[_-]?key(?:[_-]?id)?|'
    r'access[_-]?token|auth[_-]?token|authorization|code|credentials?|'
    r'key(?:[_-]?pair[_-]?id)?|password|passwd|samlresponse|session[_-]?key|'
    r'sig|signature|secret|token)=)'
    r'([^&#\s]+)')
_OPENAI_KEY_RE = re.compile(r'\b(?:sk|rk)-[A-Za-z0-9_-]{8,}\b')
_TOFU_KEY_RE = re.compile(r'\btofu_(?:live|admin|key)_[A-Za-z0-9_-]{6,}\b')
_AWS_KEY_RE = re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')
_GITHUB_SLACK_KEY_RE = re.compile(
    r'(?i)\b(?:github_pat_|gh[pousr]_|xox[baprs]-)[A-Za-z0-9._-]{12,}\b')
_GOOGLE_KEY_RE = re.compile(r'\bAIza[0-9A-Za-z_-]{20,}\b')
_JWT_RE = re.compile(
    r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b')
_URL_PASSWORD_RE = re.compile(
    r'(?i)([a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(@)')
_DATA_URL_RE = re.compile(r'data:[^\s,;]{1,120};base64,[A-Za-z0-9+/=_-]{80,}')
_PRIVATE_KEY_RE = re.compile(
    r'-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----',
    re.DOTALL)
_PRIVATE_KEY_OPEN_RE = re.compile(
    r'-----BEGIN [^-\n]*PRIVATE KEY-----.*', re.DOTALL)
_PRIVATE_KEY_TAIL_RE = re.compile(
    r'(?m)(?:^[A-Za-z0-9+/=]{20,}\r?\n)+'
    r'-----END [^-\n]*PRIVATE KEY-----')

_SENSITIVE_KEYS = frozenset({
    'api_key', 'apikey', 'authorization', 'proxy_authorization', 'x_api_key',
    'access_token', 'refresh_token', 'auth_token', 'bearer', 'password',
    'passwd', 'secret', 'client_secret', 'private_key', 'cookie', 'cookies',
    'credential', 'credentials',
})
_NON_SECRET_TOKEN_KEYS = frozenset({
    'token_count', 'token_counts', 'tokens', 'input_tokens', 'output_tokens',
    'max_tokens', 'request_id', 'task_id', 'trace_id', 'key_id', 'user_id',
})
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
    if tail_start > 0:
        # Never expose an orphaned suffix of a credential that began in the
        # omitted middle (``password=<very long single line>``). Tracebacks and
        # JSONL retain complete terminal lines; an oversized single-line body
        # deliberately retains no unclassifiable tail.
        boundary = tail_text.find('\n')
        tail_text = tail_text[boundary + 1:] if boundary >= 0 else ''
    omitted = len(text) - len(head_text) - len(tail_text)
    marker = f'\n… <log policy omitted {omitted} chars> …\n'
    return head_text + marker + tail_text


def redact_text(value: object, *, max_chars: int | None = None) -> str:
    """Redact credential-shaped text and optionally enforce a size bound."""
    text = _safe_string(value)
    if max_chars is not None:
        # Bound before regex work.  Anything removed cannot leak; retained head
        # and tail still pass through every redactor below.
        text = bound_text(text, max_chars)
    text = _PRIVATE_KEY_RE.sub('<redacted-private-key>', text)
    text = _PRIVATE_KEY_OPEN_RE.sub('<redacted-private-key>', text)
    text = _PRIVATE_KEY_TAIL_RE.sub('<redacted-private-key>', text)
    text = _DATA_URL_RE.sub('<redacted-data-url>', text)
    text = _ENV_ASSIGN_RE.sub(
        lambda match: match.group(1) + '<redacted>'
        if sensitive_field_name(match.group(2)) else match.group(0),
        text)
    text = _NAMED_SECRET_RE.sub(r'\1\2<redacted>\4', text)
    text = _HEADER_LINE_RE.sub(r'\1<redacted>', text)
    text = _BEARER_RE.sub(r'\1<redacted>', text)
    text = _QUERY_RE.sub(r'\1<redacted>', text)
    text = _URL_PASSWORD_RE.sub(r'\1<redacted>\3', text)
    text = _OPENAI_KEY_RE.sub('<redacted-api-key>', text)
    text = _TOFU_KEY_RE.sub('<redacted-tofu-key>', text)
    text = _AWS_KEY_RE.sub('<redacted-access-key>', text)
    text = _GITHUB_SLACK_KEY_RE.sub('<redacted-access-token>', text)
    text = _GOOGLE_KEY_RE.sub('<redacted-api-key>', text)
    text = _JWT_RE.sub('<redacted-jwt>', text)
    # Replacement markers can be a few characters longer than a short secret
    # token (for example ``Bearer abcdefgh`` → ``Bearer <redacted>``). Reapply
    # the ceiling after redaction so the advertised physical-record bound is
    # exact, not approximate.
    if max_chars is not None:
        text = bound_text(text, max_chars)
    return text


def sensitive_field_name(name: object) -> bool:
    raw_name = _safe_string(name)
    if raw_name.startswith(_UNPRINTABLE_PREFIX):
        return True
    snake = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', raw_name.strip())
    normalized = re.sub(r'[^a-z0-9]+', '_', snake.lower()).strip('_')
    if normalized in _NON_SECRET_TOKEN_KEYS:
        return False
    return (normalized in _SENSITIVE_KEYS
            or normalized.endswith(('_password', '_passwd', '_secret',
                                    '_cookie', '_credential', '_private_key',
                                    '_secret_key', '_access_key'))
            or normalized.endswith('_token')
            or normalized.endswith('_api_key')
            or normalized.startswith((
                'api_key_', 'authorization_', 'cookie_', 'credential_',
                'password_', 'passwd_', 'private_key_', 'secret_',
            )))


def sanitize_value(value: object, *, field_name: object = '', depth: int = 0,
                   max_depth: int = 5, max_items: int = 50,
                   max_string_chars: int = 2_000):
    """Return a JSON-safe, bounded and recursively redacted value."""
    if sensitive_field_name(field_name):
        return '<redacted>'
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else '<non-finite-number>'
    if isinstance(value, str):
        return redact_text(value, max_chars=max_string_chars)
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
                safe_key = redact_text(key, max_chars=128)
                output[safe_key] = sanitize_value(
                    item, field_name=safe_key, depth=depth + 1,
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
    return redact_text(value, max_chars=max_string_chars)


class RedactingFormatter(logging.Formatter):
    """A normal formatter with durable-sink redaction and record bounds."""

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
        return redact_text(
            rendered,
            max_chars=(self.max_chars if self.max_chars is not None
                       else log_record_max_chars()),
        )


__all__ = [
    'RedactingFormatter', 'bound_text', 'log_record_max_chars', 'redact_text',
    'sanitize_value', 'sensitive_field_name',
]
