"""Replay atomicity, projection digest, canary, and cutover gate contracts."""

from __future__ import annotations

import pytest

from lib.storage_sidecar.logical_replay import (
    CutoverEvidence,
    CutoverPolicy,
    CutoverStage,
    ReplayCheckpoint,
    assess_cutover,
    projection_digest,
    replay_records,
    replay_shadow_page,
    select_canary_read,
)
from lib.storage_sidecar.logical_shadow import LogicalCommitShadow, RECORD_FORMAT


pytestmark = pytest.mark.unit


def _record(sequence: int, *, stream_id: str = 'stream-a') -> dict:
    return {
        'command_id': f'command-{sequence}',
        'committed_at_ms': sequence,
        'event_id': f'event-{sequence}',
        'format': RECORD_FORMAT,
        'operation': 'record.put',
        'owner_user_id': 7,
        'payload': {
            'request': {'key': f'key-{sequence}'},
            'response': {'version': sequence},
        },
        'request_digest': f'{sequence:064x}',
        'sequence': sequence,
        'stream_id': stream_id,
        'tenant_id': 'tenant-a',
    }


class _AtomicTarget:
    def __init__(self, *, fail_once_at: int | None = None):
        self.cursor = ReplayCheckpoint('stream-a')
        self.applied: list[int] = []
        self.fail_once_at = fail_once_at

    def checkpoint(self):
        return self.cursor

    def apply_and_checkpoint(self, record, checkpoint):
        sequence = int(record['sequence'])
        if self.fail_once_at == sequence:
            self.fail_once_at = None
            raise RuntimeError('injected target transaction rollback')
        self.applied.append(sequence)
        self.cursor = checkpoint


def test_replay_resumes_at_atomic_checkpoint_after_failure():
    target = _AtomicTarget(fail_once_at=2)
    with pytest.raises(RuntimeError, match='injected'):
        replay_records([_record(1), _record(2)], target)

    assert target.applied == [1]
    assert target.checkpoint().last_sequence == 1
    result = replay_records([_record(2), _record(3)], target)
    assert target.applied == [1, 2, 3]
    assert result.last_sequence == 3
    assert result.applied_records == 2


def test_replay_rejects_gap_or_foreign_lineage_before_apply():
    target = _AtomicTarget()
    with pytest.raises(ValueError, match='contiguous'):
        replay_records([_record(2)], target)
    with pytest.raises(ValueError, match='lineage'):
        replay_records([_record(1, stream_id='foreign')], target)
    assert target.applied == []


def test_shadow_page_resumes_from_target_checkpoint(tmp_path):
    shadow = LogicalCommitShadow(
        tmp_path / 'replay-page', stream_id='stream-a')
    try:
        for sequence in range(1, 6):
            record = _record(sequence)
            shadow.append(
                operation=record['operation'],
                tenant_id=record['tenant_id'],
                owner_user_id=record['owner_user_id'],
                payload=record['payload'],
                command_id=record['command_id'],
                request_digest=record['request_digest'],
                committed_at_ms=record['committed_at_ms'],
                event_id=record['event_id'],
                expected_sequence=sequence,
            )
        target = _AtomicTarget()
        first = replay_shadow_page(shadow, target, max_records=2)
        second = replay_shadow_page(shadow, target, max_records=2)
        final = replay_shadow_page(shadow, target, max_records=2)
        assert first.complete_input is False
        assert second.complete_input is False
        assert final.complete_input is True
        assert target.applied == [1, 2, 3, 4, 5]
    finally:
        shadow.close()


def test_projection_digest_is_ordered_and_cutover_requires_exact_match():
    source = projection_digest(
        'conversation-list',
        [('a', {'title': 'A'}), ('b', {'title': 'B'})],
    )
    target = projection_digest(
        'conversation-list',
        [('a', {'title': 'A'}), ('b', {'title': 'B'})],
    )
    evidence = CutoverEvidence(
        current_stage=CutoverStage.SHADOW,
        requested_stage=CutoverStage.CANARY_READS,
        explicit_operator_request=True,
        source_sequence=2000,
        sink_sequence=2000,
        replay_sequence=2000,
        pending_records=0,
        publisher_state='ready',
        verified_records=2000,
        source_projection=source,
        target_projection=target,
        rollback_checkpoint_verified=True,
    )
    decision = assess_cutover(evidence, CutoverPolicy())
    assert decision.allowed is True
    assert decision.next_stage is CutoverStage.CANARY_READS
    assert decision.rollback_stage is CutoverStage.DATABASE_AUTHORITY

    divergent = projection_digest(
        'conversation-list',
        [('a', {'title': 'A'}), ('b', {'title': 'changed'})],
    )
    refused = assess_cutover(
        CutoverEvidence(
            **{
                field: getattr(evidence, field)
                for field in evidence.__dataclass_fields__
                if field != 'target_projection'
            },
            target_projection=divergent,
        )
    )
    assert refused.allowed is False
    assert 'source and replay projections differ' in refused.reasons


def test_cutover_fails_closed_on_lag_backlog_or_missing_rollback():
    digest = projection_digest('records', [('one', {'value': 1})])
    decision = assess_cutover(CutoverEvidence(
        current_stage=CutoverStage.CANARY_READS,
        requested_stage=CutoverStage.LOGICAL_AUTHORITY,
        explicit_operator_request=True,
        source_sequence=1000,
        sink_sequence=999,
        replay_sequence=999,
        pending_records=1,
        publisher_state='degraded',
        verified_records=1000,
        source_projection=digest,
        target_projection=digest,
        rollback_checkpoint_verified=False,
    ))
    assert decision.allowed is False
    assert decision.next_stage is CutoverStage.CANARY_READS
    assert len(decision.reasons) == 4


def test_canary_selection_is_stable_and_bounded():
    selected = [
        index
        for index in range(10_000)
        if select_canary_read(str(index), percent=1)
    ]
    assert selected == [
        index
        for index in range(10_000)
        if select_canary_read(str(index), percent=1)
    ]
    assert 70 <= len(selected) <= 130
    assert select_canary_read('anything', percent=0) is False
    assert select_canary_read('anything', percent=100) is True
