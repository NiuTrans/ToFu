"""Proof bundle assembly for one owned task.

Read-only fold: the terminal task snapshot supplies subject and verdict,
plus one evidence item per available authority. Missing evidence downgrades
``coverage`` to ``partial`` with explicit reasons — never zero-filled,
never fabricated (Turn Trace ``eventsAvailable:false`` precedent). The
bundle proves an execution trail exists and is untampered; whether the
deliverable is CORRECT stays with its own reviewers and tests.
"""
from __future__ import annotations

import time

from .contract import limit, validate_bundle
from .digests import bundle_id, sha256_digest
from .errors import ProofError
from .evidence import (
    collect_request_round_digest,
    enumerate_request_rounds,
    file_receipt_fact,
    read_turn_trace_document,
)


def _optional_str(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _error_kind(error) -> str | None:
    if isinstance(error, dict):
        kind = error.get('kind') or error.get('code')
        return str(kind)[:64] if kind else 'error'
    if isinstance(error, str) and error:
        return 'error'
    return None


def assemble_task_proof(task_id: str, *, owner_user_id: int,
                        max_rounds: int | None = None) -> dict | None:
    """Assemble one proof bundle for ``task_id``.

    Returns ``None`` when the task is not visible to ``owner_user_id`` —
    foreign and missing tasks stay indistinguishable (the
    ``task_results.replay_get`` owner-fence precedent), so the route can
    answer 404 without leaking another owner's existence.
    """
    if not isinstance(owner_user_id, int) \
            or isinstance(owner_user_id, bool) or owner_user_id < 1:
        raise ProofError(
            'proof_owner', 'Proof assembly requires a positive owner')
    from lib.tasks_pkg.durable_chat_replay import load_durable_chat_task
    snapshot = load_durable_chat_task(task_id, user_id=owner_user_id)
    if snapshot is None:
        return None
    max_rounds = limit('maxRounds') if max_rounds is None else int(max_rounds)

    reasons = []
    evidence = []

    # Turn Trace: the permanent per-attempt timing authority.
    trace_document = read_turn_trace_document(task_id, owner_user_id)
    if trace_document is None:
        reasons.append({'evidence': 'turn_trace', 'reason': 'unavailable'})
    else:
        evidence.append({'kind': 'turn_trace',
                         'ref': {'taskId': task_id},
                         'digest': sha256_digest(trace_document)})

    # Request Inspector rounds: what the model actually saw, per round.
    round_rows = enumerate_request_rounds(task_id)
    if round_rows is None:
        reasons.append({'evidence': 'request_round', 'reason': 'unavailable'})
    else:
        if len(round_rows) > max_rounds:
            reasons.append({'evidence': 'request_round',
                            'reason': 'round-cap'})
        for row in round_rows[:max_rounds]:
            digest = collect_request_round_digest(
                task_id, owner_user_id, row['roundNum'], row['turn'])
            if digest is None:
                reasons.append({'evidence': 'request_round',
                                'reason': 'read-error'})
                continue
            evidence.append({'kind': 'request_round',
                             'ref': {'taskId': task_id,
                                     'roundNum': row['roundNum'],
                                     'turn': row['turn']},
                             'digest': digest})

    # Project file receipts: the settlement-metadata file-change fact.
    metadata = snapshot.metadata if isinstance(snapshot.metadata, dict) else {}
    fact = file_receipt_fact(task_id, metadata)
    if fact is None:
        reasons.append({'evidence': 'file_receipt', 'reason': 'unavailable'})
    else:
        evidence.append({'kind': 'file_receipt',
                         'ref': {'taskId': task_id},
                         'digest': sha256_digest(fact)})

    reasons = reasons[:limit('maxReasons')]
    identity = {'taskId': task_id,
                'turnId': _optional_str(
                    (trace_document or {}).get('turnId')),
                'attemptId': _optional_str(
                    (trace_document or {}).get('attemptId'))}
    bundle = {
        'version': 1,
        'owner': owner_user_id,
        'assembledAt': int(time.time() * 1000),
        'subject': {
            'kind': 'turn',
            'identity': identity,
            'contentDigest': sha256_digest({
                'content': snapshot.content or '',
                'thinking': snapshot.thinking or '',
            }),
        },
        'evidence': evidence[:limit('maxEvidenceItems')],
        'verdicts': {'task': {
            'status': str(snapshot.status or ''),
            'finishReason': _optional_str(metadata.get('finishReason')),
            'errorKind': _error_kind(snapshot.error),
            'model': _optional_str(metadata.get('model')),
            'usage': metadata.get('usage')
                     if isinstance(metadata.get('usage'), dict) else None,
        }},
        'coverage': 'partial' if reasons else 'full',
        'coverageReasons': reasons,
        'contentIncluded': False,
    }
    bundle['bundleId'] = bundle_id(bundle)
    return validate_bundle(bundle)
