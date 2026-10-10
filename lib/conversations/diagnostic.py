"""Bounded, revision-fenced diagnostic reads over the selected storage authority.

The API and inspector use header/turn/chunk, never a full hydrated conversation.
SQL and native implementations own byte slicing and owner/revision enforcement.
"""
from __future__ import annotations

from typing import Any

from lib.conversations.repository import _conversation_id, _owner_id

CHUNK_BYTES = 65536
KINDS = frozenset({'header', 'turn', 'chunk'})


def diagnostic_payload(conversation_id: str, *, user_id: int, kind: str = 'header',
                       revision: int | None = None, index: int | None = None,
                       offset: int | None = None, limit: int | None = None,
                       source_token: str | None = None) -> dict[str, Any]:
    """Reject ambiguous cursors before dispatch; storage independently validates."""
    if kind not in KINDS:
        raise ValueError('kind must be header, turn or chunk')
    result: dict[str, Any] = {'conv_id': _conversation_id(conversation_id),
                              'user_id': _owner_id(user_id), 'kind': kind}
    values = {'revision': revision, 'index': index, 'offset': offset, 'limit': limit}
    allowed = set() if kind == 'header' else {'revision', 'index'}
    if kind == 'chunk':
        allowed.update({'offset', 'limit'})
    for name, value in values.items():
        if value is None:
            continue
        if name not in allowed or type(value) is not int or value < 0:
            raise ValueError(f'invalid {name} for {kind} diagnostic read')
        result[name] = value
    if source_token is not None:
        import re
        if kind != 'chunk' or not isinstance(source_token, str) or not re.fullmatch(r'[0-9a-f]{64}', source_token):
            raise ValueError('invalid source_token for diagnostic read')
        result['source_token'] = source_token
    if kind != 'header' and (revision is None or index is None):
        raise ValueError('revision and index are required for turn/chunk reads')
    if kind == 'chunk':
        if offset is None or source_token is None:
            raise ValueError('offset and source_token are required for chunk reads')
        if limit is not None and not 1 <= limit <= CHUNK_BYTES:
            raise ValueError(f'limit must be between 1 and {CHUNK_BYTES}')
        result.setdefault('limit', CHUNK_BYTES)
    return result


def read_diagnostic(conversation_id: str, *, user_id: int, **options: Any) -> dict | None:
    """One read-only semantic call, with authenticated identity kept explicit."""
    from lib.storage import get_storage_client
    payload = diagnostic_payload(conversation_id, user_id=user_id, **options)
    return get_storage_client(write=False).query('conversation.diagnostic', payload)


__all__ = ['CHUNK_BYTES', 'diagnostic_payload', 'read_diagnostic']


def terminalize_restored_projection(value: Any) -> None:
    """Mirror native public_turn's restored-epoch transform after codec replay.

    Native diagnostics stream untouched source bytes. Cross-runtime fixtures
    cover this named wire transform against turn.rs::terminalize_restored_projection.
    """
    runtime_keys = ('_commandPending', '_flowRunId', '_needsStart', '_runId',
                    '_streamCursor', '_streaming', 'activeTaskId', 'approvalRequired',
                    'attemptId', 'isStreaming', 'runId')
    terminal = {'abort', 'aborted', 'completed', 'done', 'error', 'failed',
                'not-run', 'not_run', 'rejected', 'skipped', 'succeeded',
                'success', 'unknown'}
    def visit(node):
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            for key in runtime_keys:
                node.pop(key, None)
            rounds = node.get('toolRounds')
            if isinstance(rounds, list):
                for item in rounds:
                    if isinstance(item, dict) and (
                            not isinstance(item.get('status'), str) or item['status'] not in terminal):
                        item['status'] = 'aborted'
            trace = node.get('timingTrace')
            if isinstance(trace, dict):
                trace.update(status='aborted', running=False)
            if node.get('type') == 'tool_use':
                result = node.get('result')
                if isinstance(result, dict) and (
                        not isinstance(result.get('status'), str) or result['status'] not in terminal):
                    result['status'] = 'aborted'
            for child in node.values():
                visit(child)
    visit(value)
