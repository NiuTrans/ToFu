"""Evidence collectors: one read-only adapter per existing authority.

A collector returns either the authority's bounded fact (or its canonical
``sha256:`` digest), or ``None`` when that evidence is genuinely unavailable
— expired, cleaned up, or unreadable. Collectors never fabricate a value
for missing evidence; the assembler records an explicit coverage reason
instead (Turn Trace honest-empty-state precedent).

Readers are imported lazily inside functions: route blueprints import this
package at registration time, and ``lib.tasks_pkg`` must not be paid for —
or cycled into — that import. Tests patch the source modules directly
(``lib.tasks_pkg.turn_trace.read_persisted_task_trace`` and friends).
"""
from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)
from .digests import sha256_digest

# Payload lanes excluded from the round projection. A bundle is content-free
# by default (design non-goal); these are folded into digests and counts.
_ROUND_PROJECTION_EXCLUDED = frozenset({'messages', 'tools', 'rawArchives'})


def read_turn_trace_document(task_id: str, owner_user_id: int):
    """The permanent owner-scoped attempt trace document, or ``None``."""
    from lib.tasks_pkg.turn_trace import read_persisted_task_trace
    document = read_persisted_task_trace(task_id, user_id=owner_user_id)
    return dict(document) if isinstance(document, dict) else None


def file_receipt_fact(task_id: str, metadata: dict):
    """The settlement-metadata file-change fact, or ``None`` when the task
    authority carries no file-change lane at all.

    An EMPTY ``modifiedFileList`` is a fact (the task changed no files) and
    digests like any other; only an absent lane is unavailable evidence.
    """
    if 'modifiedFileList' not in metadata and 'modifiedFiles' not in metadata:
        return None
    return {
        'taskId': task_id,
        'modifiedFiles': metadata.get('modifiedFiles'),
        'modifiedFileList': metadata.get('modifiedFileList') or [],
    }


def collect_file_receipt_digest(task_id: str, owner_user_id: int):
    """Digest of the file-receipt fact, reloading the owned task snapshot."""
    from lib.tasks_pkg.durable_chat_replay import load_durable_chat_task
    snapshot = load_durable_chat_task(task_id, user_id=owner_user_id)
    metadata = snapshot.metadata if (
        snapshot is not None and isinstance(snapshot.metadata, dict)) else None
    if metadata is None:
        return None
    fact = file_receipt_fact(task_id, metadata)
    return sha256_digest(fact) if fact is not None else None


def collect_request_round_digest(task_id: str, owner_user_id: int,
                                 round_num, turn: str):
    """Digest of one Request Inspector round's content-free projection."""
    from lib.tasks_pkg.request_inspector import get_request_payload
    payload = get_request_payload(task_id, round_num, turn=turn or '',
                                  kind='request', user_id=owner_user_id)
    if not isinstance(payload, dict):
        return None
    return sha256_digest(round_projection(payload))


def round_projection(payload: dict) -> dict:
    """Content-free canonical form of one request-round payload.

    Message/tool/raw bodies never enter the bundle; their digests and counts
    do, so a verifier still detects any change to the bytes the model saw.
    """
    messages = payload.get('messages') or []
    tools = payload.get('tools') or []
    projection = {key: value for key, value in payload.items()
                  if key not in _ROUND_PROJECTION_EXCLUDED}
    projection['messageCount'] = len(messages)
    projection['messagesDigest'] = sha256_digest(messages)
    projection['toolsDigest'] = sha256_digest(tools)
    return projection


def enumerate_request_rounds(task_id: str):
    """``[{'roundNum': n, 'turn': t}]`` in fold order, or ``None``.

    ``None`` covers read failure (``readError``) and unavailable/empty logs
    alike; the assembler records one ``unavailable`` coverage reason. A
    malformed row raises ``ProofError('proof_assembly', ...)`` — that is an
    upstream bug or corruption, not missing evidence.
    """
    from .errors import ProofError
    from lib.tasks_pkg.request_inspector import fold_request_log
    folded = fold_request_log(task_id)
    if not isinstance(folded, dict) or folded.get('readError') \
            or not folded.get('eventsAvailable'):
        return None
    rows = folded.get('requests')
    if not isinstance(rows, list):
        return None
    rounds = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(
                row.get('roundNum'), int) or isinstance(
                    row.get('roundNum'), bool):
            raise ProofError(
                'proof_assembly', 'Malformed request-round row')
        rounds.append({'roundNum': row['roundNum'],
                       'turn': str(row.get('turn') or '')})
    return rounds


def server_evidence_resolver(owner_user_id: int):
    """Recompute one evidence item's digest against the live authorities.

    Returns ``None`` for unreadable or expired evidence — the verifier maps
    that to ``unknown_evidence``, never to a success or failure guess.
    Collector exceptions degrade the same way: verification is an observer,
    it must not fail closed on a storage hiccup.
    """
    def resolve(item: dict):
        kind = item.get('kind')
        ref = item.get('ref') if isinstance(item.get('ref'), dict) else {}
        task_id = ref.get('taskId')
        if not isinstance(task_id, str) or not task_id:
            return None
        try:
            if kind == 'turn_trace':
                document = read_turn_trace_document(task_id, owner_user_id)
                return sha256_digest(document) if document is not None else None
            if kind == 'file_receipt':
                return collect_file_receipt_digest(task_id, owner_user_id)
            if kind == 'request_round':
                return collect_request_round_digest(
                    task_id, owner_user_id, ref.get('roundNum'),
                    ref.get('turn') or '')
        except Exception as _e:
            logger.debug('resolve evidence failed: %s', _e)
            return None
        return None
    return resolve
