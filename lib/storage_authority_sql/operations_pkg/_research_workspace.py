"""Versioned Research Foundry workspace operations.

The generic ``storage_records`` table supplies the portable document/version
primitive.  This module owns the explicit user boundary and optimistic compare
and swap semantics for direction-scoped scientific workspaces.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import orjson

from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters.base import Session
from lib.storage_authority_sql.operations_pkg._common import _integer, _required_text
from lib.storage_authority_sql.operations_pkg._runs import _json_text

_NAMESPACE = 'research.workspace.v1'


def _record_key(user_id: int, paper_hash: str, lang: str) -> str:
    return f'{user_id}:{paper_hash}:{lang}'


def _research_workspace_get(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, 'user_id', minimum=1)
    paper_hash = _required_text(payload, 'paper_hash', 128)
    lang = _required_text(payload, 'lang', 8)
    row = session.fetch_one(
        'SELECT value_json, version, updated_at_ms FROM storage_records '
        'WHERE namespace = ? AND record_key = ?',
        (_NAMESPACE, _record_key(user_id, paper_hash, lang)),
    )
    if row is None:
        return None
    try:
        encoded = row['value_json']
        workspace = dict(encoded) if isinstance(encoded, Mapping) else orjson.loads(encoded)
    except (TypeError, orjson.JSONDecodeError) as exc:
        raise StorageError(
            'database_integrity', 'Stored research workspace is invalid') from exc
    if not isinstance(workspace, dict):
        raise StorageError(
            'database_integrity', 'Stored research workspace is not a document')
    return {
        'workspace': workspace,
        'revision': int(row['version'] or 0),
        'updated_at_ms': int(row['updated_at_ms'] or 0),
    }


def _research_workspace_put(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, 'user_id', minimum=1)
    paper_hash = _required_text(payload, 'paper_hash', 128)
    lang = _required_text(payload, 'lang', 8)
    expected_revision = _integer(payload, 'expected_revision', minimum=0)
    updated_at = _integer(payload, 'updated_at', minimum=0)
    workspace = payload.get('workspace')
    if not isinstance(workspace, Mapping):
        raise StorageError('database_protocol_error', 'Invalid research workspace')

    record_key = _record_key(user_id, paper_hash, lang)
    session.lock_key(_NAMESPACE, record_key)
    current = session.fetch_one(
        'SELECT version, value_json FROM storage_records '
        'WHERE namespace = ? AND record_key = ?',
        (_NAMESPACE, record_key),
    )
    current_revision = int(current['version'] or 0) if current is not None else 0
    if current_revision != expected_revision:
        raise StorageError(
            'database_conflict',
            f'Research workspace revision advanced to {current_revision}',
        )
    compilation = workspace.get('compilation') or {}
    reference = compilation.get('build') if isinstance(compilation, Mapping) else None
    encoded = current['value_json'] if current is not None else {}
    previous = dict(encoded) if isinstance(encoded, Mapping) else orjson.loads(encoded)
    previous_compilation = previous.get('compilation') or {}
    previous_reference = previous_compilation.get('build') if isinstance(previous_compilation, Mapping) else None
    proof = payload.get('artifact_proof')
    if proof is not None:
        from ._paper_artifacts import retain_research_build
        if compilation.get('mode') != 'paper_build' or compilation.get('status') != 'passing':
            raise StorageError('database_protocol_error', 'Retained compilation must be verified and passing')
        retain_research_build(session, user_id=user_id, reference=reference, proof=proof)
    elif reference != previous_reference:
        raise StorageError('database_protocol_error', 'Research PDF reference requires retained artifact evidence')
    next_revision = current_revision + 1
    encoded = _json_text(dict(workspace))
    updated_at_ms = updated_at * 1000
    if current is None:
        session.execute(
            'INSERT INTO storage_records('
            'namespace, record_key, value_json, version, updated_at_ms) '
            'VALUES (?, ?, ?, ?, ?)',
            (_NAMESPACE, record_key, encoded, next_revision, updated_at_ms),
        )
    else:
        changed = session.execute(
            'UPDATE storage_records SET value_json = ?, version = ?, '
            'updated_at_ms = ? WHERE namespace = ? AND record_key = ? '
            'AND version = ?',
            (encoded, next_revision, updated_at_ms, _NAMESPACE, record_key,
             current_revision),
        )
        if changed != 1:
            raise StorageError(
                'database_conflict', 'Research workspace changed concurrently')
    return {
        'workspace': dict(workspace),
        'revision': next_revision,
        'updated_at_ms': updated_at_ms,
    }


__all__ = ['_research_workspace_get', '_research_workspace_put']
