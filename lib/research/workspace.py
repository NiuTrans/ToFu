"""Durable research-production workspace for one auto-research direction.

Auto-research artifacts answer "what may be worth pursuing".  This module owns
what happens after promotion: the frozen experiment protocol, bounded run log,
claim/evidence ledger, manuscript plan, and compilation/submission readiness.
It deliberately does not reuse ``lib.experiments``: that domain evaluates Tofu
product strategies, whereas these records are user-authored scientific work.
"""

from __future__ import annotations

import copy
import time
import uuid
from collections.abc import Mapping
from typing import Any

from lib.identity import require_user_id
from lib.storage.errors import StorageError

from .persistence import research_direction_hash
from .program import (
    MAX_CLAIMS,
    MAX_PROGRAM_BYTES,
    MAX_RUNS,
    MAX_TEXT,
    PROGRAM_CONTRACT_VERSION,
    default_program_fields,
    encoded_program_size,
    normalize_program_fields,
)

WORKSPACE_CONTRACT_VERSION = PROGRAM_CONTRACT_VERSION
MAX_WORKSPACE_BYTES = MAX_PROGRAM_BYTES


def _storage(*, write: bool = False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


def _text(value: Any, *, maximum: int = MAX_TEXT) -> str:
    return str(value or '').strip()[:maximum]


def empty_workspace(direction: str, lang: str = 'en') -> dict[str, Any]:
    return {
        'contract_version': WORKSPACE_CONTRACT_VERSION,
        'revision': 0,
        'direction': _text(direction, maximum=2_000),
        'lang': 'zh' if lang == 'zh' else 'en',
        'stage': 'selection',
        'selected_idea_id': '',
        'hypothesis': '',
        **default_program_fields(),
        'updated_at': 0,
    }


def normalize_workspace(
    direction: str,
    lang: str,
    raw: Mapping[str, Any] | None,
    *,
    revision: int,
) -> dict[str, Any]:
    base = empty_workspace(direction, lang)
    raw = raw if isinstance(raw, Mapping) else {}
    base['revision'] = max(0, int(revision))
    stage = _text(raw.get('stage'), maximum=40)
    if stage in {'selection', 'experiment', 'evidence', 'writing', 'submission'}:
        base['stage'] = stage
    base['selected_idea_id'] = _text(raw.get('selected_idea_id'), maximum=160)
    base['hypothesis'] = _text(raw.get('hypothesis'))
    base.update(normalize_program_fields(raw))
    base['updated_at'] = max(0, int(raw.get('updated_at') or 0))
    size = encoded_program_size(base)
    if size > MAX_WORKSPACE_BYTES:
        raise ValueError(f'research workspace exceeds {MAX_WORKSPACE_BYTES} bytes')
    return base


def load_workspace(direction: str, lang: str = 'en', *, user_id: int) -> dict[str, Any]:
    paper_hash = research_direction_hash(direction)
    if not paper_hash:
        return empty_workspace(direction, lang)
    row = _storage().query('research.workspace.get', {
        'user_id': require_user_id(user_id, context='research workspace owner'),
        'paper_hash': paper_hash,
        'lang': 'zh' if lang == 'zh' else 'en',
    })
    if not isinstance(row, Mapping):
        return empty_workspace(direction, lang)
    return normalize_workspace(
        direction, lang, row.get('workspace'), revision=int(row.get('revision') or 0))


def save_workspace(
    direction: str,
    lang: str,
    workspace: Mapping[str, Any],
    *,
    expected_revision: int,
    user_id: int,
) -> dict[str, Any]:
    if isinstance(expected_revision, bool) or int(expected_revision) < 0:
        raise ValueError('expected_revision must be a non-negative integer')
    paper_hash = research_direction_hash(direction)
    if not paper_hash:
        raise ValueError('direction is required')
    next_revision = int(expected_revision) + 1
    normalized = normalize_workspace(direction, lang, copy.deepcopy(workspace), revision=next_revision)
    normalized['updated_at'] = int(time.time())
    result = _storage(write=True).command(
        'research.workspace.put',
        {
            'user_id': require_user_id(user_id, context='research workspace owner'),
            'paper_hash': paper_hash,
            'lang': normalized['lang'],
            'expected_revision': int(expected_revision),
            'workspace': normalized,
            'updated_at': normalized['updated_at'],
        },
        f'research.workspace.put:{paper_hash}:{expected_revision}:{uuid.uuid4().hex}',
    )
    if not isinstance(result, Mapping):
        raise StorageError('database_internal', 'Research workspace commit returned no document')
    return normalize_workspace(
        direction, lang, result.get('workspace'), revision=int(result.get('revision') or 0))


__all__ = [
    'MAX_CLAIMS', 'MAX_RUNS', 'MAX_WORKSPACE_BYTES', 'WORKSPACE_CONTRACT_VERSION',
    'empty_workspace', 'load_workspace', 'normalize_workspace', 'save_workspace',
]
