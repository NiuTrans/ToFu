"""Bounded contracts shared by memory tools, APIs, storage, and search.

This dependency-free module is the single source of truth for model-created
memory payload limits.  It validates before storage discovery or mutation so a
bad request cannot spend a full-corpus scan, create a partial file, or begin a
destructive merge.  Existing durable files remain readable; the limits govern
new or explicitly replaced fields and never reclaim user data.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from lib.error_envelope import BadRequest


MEMORY_NAME_MAX_CHARS = 160
MEMORY_DESCRIPTION_MAX_CHARS = 512
MEMORY_BODY_MAX_CHARS = 32_768
MEMORY_FRONTMATTER_READ_MAX_CHARS = 65_536
MEMORY_TAG_MAX_ITEMS = 32
MEMORY_TAG_MAX_CHARS = 64
# Supplied IDs may belong to legacy flat files or skill-package directories,
# so retain the common filesystem component ceiling. Newly generated flat-file
# IDs use the smaller bound below, leaving ample room for collision suffixes
# and the ``.md`` extension.
MEMORY_ID_MAX_CHARS = 255
MEMORY_ID_MAX_BYTES = 255
MEMORY_GENERATED_ID_MAX_BYTES = 192
MEMORY_MERGE_MAX_ITEMS = 32
MEMORY_SEARCH_QUERY_MAX_CHARS = 2_048
MEMORY_SEARCH_BODY_MAX_CHARS = 2_000
MEMORY_SEARCH_TOP_K_MIN = 1
MEMORY_SEARCH_TOP_K_MAX = 50
MEMORY_SEARCH_TOP_K_DEFAULT = 30


def _bounded_text(
    value: Any,
    *,
    field: str,
    max_chars: int,
    allow_blank: bool = True,
    single_line: bool = False,
) -> str:
    if not isinstance(value, str):
        raise BadRequest(f'{field} must be a string', field=field)
    if not allow_blank and not value.strip():
        raise BadRequest(f'{field} must not be empty', field=field)
    if len(value) > max_chars:
        raise BadRequest(
            f'{field} exceeds the {max_chars:,}-character limit', field=field)
    if single_line and ('\n' in value or '\r' in value):
        raise BadRequest(f'{field} must be a single line', field=field)
    return value


def normalize_memory_tags(
    tags: Any,
    *,
    allow_none: bool = False,
    field: str = 'tags',
) -> list[str] | None:
    """Return a detached, bounded tag list; optionally preserve ``None``."""
    if tags is None and allow_none:
        return None
    if tags is None:
        return []
    if (not isinstance(tags, Sequence)
            or isinstance(tags, (str, bytes, bytearray))):
        raise BadRequest(f'{field} must be an array of strings', field=field)
    if len(tags) > MEMORY_TAG_MAX_ITEMS:
        raise BadRequest(
            f'{field} exceeds the {MEMORY_TAG_MAX_ITEMS}-item limit', field=field)

    normalized: list[str] = []
    seen: set[str] = set()
    for index, tag in enumerate(tags):
        value = _bounded_text(
            tag,
            field=f'{field}[{index}]',
            max_chars=MEMORY_TAG_MAX_CHARS,
            allow_blank=False,
            single_line=True,
        )
        # The retained frontmatter format represents lists as comma-separated
        # bracket contents. A comma inside one tag would silently round-trip as
        # two tags, so reject the ambiguous value instead of corrupting it.
        if ',' in value:
            raise BadRequest(f'{field}[{index}] must not contain a comma', field=field)
        if value in seen:
            raise BadRequest(f'{field} contains duplicate value {value!r}', field=field)
        seen.add(value)
        normalized.append(value)
    return normalized


def normalize_memory_payload(
    *,
    name: Any,
    description: Any,
    body: Any,
    tags: Any,
    scope: Any,
    allow_tags_none: bool = False,
) -> tuple[str, str, str, list[str] | None, str]:
    """Validate and detach every persisted field before a write starts."""
    normalized_name = _bounded_text(
        name, field='name', max_chars=MEMORY_NAME_MAX_CHARS,
        allow_blank=False, single_line=True)
    normalized_description = _bounded_text(
        description, field='description',
        max_chars=MEMORY_DESCRIPTION_MAX_CHARS, single_line=True)
    normalized_body = _bounded_text(
        body, field='body', max_chars=MEMORY_BODY_MAX_CHARS)
    normalized_tags = normalize_memory_tags(
        tags, allow_none=allow_tags_none)
    if scope not in ('global', 'project'):
        raise BadRequest("scope must be 'global' or 'project'", field='scope')
    return (
        normalized_name,
        normalized_description,
        normalized_body,
        normalized_tags,
        scope,
    )


def normalize_memory_updates(updates: Any) -> dict[str, Any]:
    """Validate fields a caller explicitly replaces; ignore legacy extras."""
    if not isinstance(updates, Mapping):
        raise BadRequest('updates must be an object', field='updates')
    normalized = dict(updates)
    text_limits = {
        'name': (MEMORY_NAME_MAX_CHARS, False, True),
        'description': (MEMORY_DESCRIPTION_MAX_CHARS, True, True),
        'body': (MEMORY_BODY_MAX_CHARS, True, False),
    }
    for field, (limit, allow_blank, single_line) in text_limits.items():
        if field in normalized:
            normalized[field] = _bounded_text(
                normalized[field], field=field, max_chars=limit,
                allow_blank=allow_blank, single_line=single_line)
    if 'tags' in normalized:
        normalized['tags'] = normalize_memory_tags(normalized['tags'])
    for field in ('requires_bins', 'requires_env'):
        if field in normalized:
            normalized[field] = normalize_memory_tags(
                normalized[field], field=field)
    if 'enabled' in normalized and not isinstance(normalized['enabled'], bool):
        raise BadRequest('enabled must be a boolean', field='enabled')
    return normalized


def validate_memory_id(memory_id: Any, *, field: str = 'memory_id') -> str:
    """Validate an opaque basename without imposing a new ID alphabet."""
    value = _bounded_text(
        memory_id, field=field, max_chars=MEMORY_ID_MAX_CHARS,
        allow_blank=False)
    if len(value.encode('utf-8')) > MEMORY_ID_MAX_BYTES:
        raise BadRequest(
            f'{field} exceeds the {MEMORY_ID_MAX_BYTES}-byte UTF-8 limit', field=field)
    if ('\x00' in value or '/' in value or '\\' in value
            or value in ('.', '..')):
        raise BadRequest(f'{field} must be a filesystem basename', field=field)
    return value


def normalize_merge_memory_ids(memory_ids: Any) -> list[str]:
    """Validate a finite, unique merge set before reading the corpus."""
    if (not isinstance(memory_ids, Sequence)
            or isinstance(memory_ids, (str, bytes, bytearray))):
        raise BadRequest('memory_ids must be an array of memory IDs', field='memory_ids')
    if len(memory_ids) < 2:
        raise BadRequest('merge_memories requires at least 2 memory IDs', field='memory_ids')
    if len(memory_ids) > MEMORY_MERGE_MAX_ITEMS:
        raise BadRequest(
            'merge_memories accepts at most '
            f'{MEMORY_MERGE_MAX_ITEMS} memory IDs', field='memory_ids')
    normalized = [
        validate_memory_id(memory_id, field=f'memory_ids[{index}]')
        for index, memory_id in enumerate(memory_ids)
    ]
    if len(set(normalized)) != len(normalized):
        raise BadRequest('memory_ids must be unique', field='memory_ids')
    return normalized


def normalize_memory_search(query: Any, top_k: Any) -> tuple[str, int]:
    """Bound query work and preserve the historical direct-call top-k clamp."""
    normalized_query = _bounded_text(
        query, field='query', max_chars=MEMORY_SEARCH_QUERY_MAX_CHARS)
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        raise BadRequest('top_k must be an integer', field='top_k')
    normalized_top_k = max(
        MEMORY_SEARCH_TOP_K_MIN,
        min(top_k, MEMORY_SEARCH_TOP_K_MAX),
    )
    return normalized_query, normalized_top_k


def truncate_utf8(value: str, max_bytes: int) -> str:
    """Truncate at a UTF-8 code-point boundary, never in the middle of one."""
    encoded = value.encode('utf-8')
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode('utf-8', errors='ignore')


__all__ = [
    'MEMORY_BODY_MAX_CHARS',
    'MEMORY_DESCRIPTION_MAX_CHARS',
    'MEMORY_GENERATED_ID_MAX_BYTES',
    'MEMORY_FRONTMATTER_READ_MAX_CHARS',
    'MEMORY_ID_MAX_BYTES',
    'MEMORY_ID_MAX_CHARS',
    'MEMORY_MERGE_MAX_ITEMS',
    'MEMORY_NAME_MAX_CHARS',
    'MEMORY_SEARCH_QUERY_MAX_CHARS',
    'MEMORY_SEARCH_BODY_MAX_CHARS',
    'MEMORY_SEARCH_TOP_K_DEFAULT',
    'MEMORY_SEARCH_TOP_K_MAX',
    'MEMORY_SEARCH_TOP_K_MIN',
    'MEMORY_TAG_MAX_CHARS',
    'MEMORY_TAG_MAX_ITEMS',
    'normalize_memory_payload',
    'normalize_memory_search',
    'normalize_memory_tags',
    'normalize_memory_updates',
    'normalize_merge_memory_ids',
    'truncate_utf8',
    'validate_memory_id',
]
