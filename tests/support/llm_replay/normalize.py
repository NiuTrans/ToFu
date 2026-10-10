"""Environment-insensitive request normalization for LLM record/replay.

The recorder and the replayer MUST compute request identity through this one
module so environment differences (checkout path, tmp root, home directory,
the current date) never break replay matching.  Normalization is deliberately
small and stable: when a legitimate product change alters the request itself
the rule is **fix fixtures, not normalizers** — re-record the scenario and
review the diff instead of teaching this module a new special case.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile

# ISO calendar dates (``2026-09-17``) plus the long renderings a composed
# system prompt uses when it spells the date out for the model
# (``September 17, 2026`` / ``17 September 2026``).  A leading weekday name
# (``Thursday, September 17, 2026``) is covered because the month-anchored
# pattern matches the tail.
_MONTHS = (
    r'Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|'
    r'Nov(?:ember)?|Dec(?:ember)?'
)
_DATE_PATTERNS = (
    re.compile(r'\b\d{4}-\d{2}-\d{2}\b'),
    re.compile(rf'\b(?:{_MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?,\s*\d{{4}}\b'),
    re.compile(rf'\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})\s+\d{{4}}\b'),
)


def _path_replacements(cwd: str) -> list[tuple[str, str]]:
    """Ordered (needle, placeholder) path rewrites, longest first."""
    pairs: list[tuple[str, str]] = []
    if cwd:
        pairs.append((os.path.normpath(str(cwd)), '{{CWD}}'))
    tmp = os.path.normpath(tempfile.gettempdir())
    pairs.append((tmp, '{{CWD}}'))
    home = os.path.normpath(os.path.expanduser('~'))
    if home and home != os.sep:
        pairs.append((home, '{{HOME}}'))
    # Longest first so a nested cwd wins over its tmp/home parent.
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    # Drop duplicates (cwd may BE the tmp dir) while preserving order.
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for needle, placeholder in pairs:
        if needle and needle not in seen:
            seen.add(needle)
            unique.append((needle, placeholder))
    return unique


def _normalize_text(text: str, replacements: list[tuple[str, str]]) -> str:
    for needle, placeholder in replacements:
        if needle and needle in text:
            text = text.replace(needle, placeholder)
    for pattern in _DATE_PATTERNS:
        text = pattern.sub('{{DATE}}', text)
    return text


def normalize_value(value, *, cwd: str):
    """Deep-normalize one JSON-ish value for environment-stable digests.

    Strings are rewritten: absolute ``cwd``/tmp paths become ``{{CWD}}``, the
    user's home directory becomes ``{{HOME}}``, and calendar dates become
    ``{{DATE}}``.  Dicts and lists are recursed; scalars pass through.
    """
    replacements = _path_replacements(cwd)
    return _normalize(value, replacements)


def _normalize(value, replacements: list[tuple[str, str]]):
    if isinstance(value, str):
        return _normalize_text(value, replacements)
    if isinstance(value, dict):
        return {
            _normalize_text(str(key), replacements): _normalize(
                item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize(item, replacements) for item in value]
    return value


def _digest_value(value) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


_DIGEST_FIELDS = ('model', 'messages', 'system', 'tools')


def _normalized_projection(body, *, cwd: str) -> dict:
    """Project the semantic request fields, stripped and normalized.

    ``body`` may be a plain dict or an ``AdmittedRequest`` mapping; internal
    ``_``-prefixed execution metadata never participates in identity.
    """
    from lib.llm.request import strip_internal_request_fields
    wire = strip_internal_request_fields(body)
    projection = {key: wire.get(key) for key in _DIGEST_FIELDS}
    return normalize_value(projection, cwd=cwd)


def normalized_request_digest(body, *, cwd: str) -> str:
    """Content identity of one provider request, environment-independent."""
    return _digest_value(_normalized_projection(body, cwd=cwd))


def normalized_field_digests(body, *, cwd: str) -> dict[str, str]:
    """Per-top-level-field digests, for one-line drift diagnostics."""
    projection = _normalized_projection(body, cwd=cwd)
    return {key: _digest_value(value) for key, value in projection.items()}


def chat_request_proxy(messages, model, extra=None) -> dict:
    """Project one non-streaming ``chat()`` call into digestable body shape.

    ``chat()`` takes ``messages``/``model`` positionally and carries tools
    inside ``extra``; recorder and replayer must reduce the call to the same
    proxy before digesting so ``chat`` steps match stream-step semantics.
    """
    proxy: dict = {'model': model, 'messages': messages}
    tools = (extra or {}).get('tools')
    if tools is not None:
        proxy['tools'] = tools
    return proxy
