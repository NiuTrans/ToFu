"""Frame-budget compaction contracts for durable toolRounds lanes.

A turn document must stay below one storage frame; once a tool-heavy turn's
round payloads cross the budget, the oldest settled rounds trade their
free-text payloads for honest stubs while replay identity stays intact.
"""

from __future__ import annotations

import json

import pytest

from lib.storage_projection import (
    _string_payload_chars,
    compact_tool_rounds_for_frame_budget,
)
from lib.tool_round_replay import scan_replayable_tool_round_prefix

pytestmark = pytest.mark.unit


def _round(position, *, chars=0, status='done'):
    return {
        'roundNum': position,
        'llmRound': 0,
        'toolCallId': f'call-{position}',
        'toolName': 'run_command',
        'toolArgs': {'command': f'echo {position}'},
        'status': status,
        'toolContent': 'x' * chars,
        'results': [{'output': 'y' * chars, 'exitCode': 0}],
    }


def test_lane_under_budget_is_returned_untouched():
    rounds = [_round(0, chars=100), _round(1, chars=100)]

    assert compact_tool_rounds_for_frame_budget(rounds) is rounds


def test_over_budget_elides_oldest_settled_rounds_only():
    rounds = [_round(i, chars=8_000) for i in range(6)]

    compacted = compact_tool_rounds_for_frame_budget(
        rounds, budget_bytes=20_000, keep_tail=2)

    assert compacted is not rounds
    elided = compacted[0]
    assert elided['_persistCompacted'] is True
    assert elided['toolContent'].startswith('[payload elided')
    assert 'originalChars=8000' in elided['toolContent']
    assert elided['results'][0]['output'].startswith('[payload elided')
    # Non-payload metadata survives compaction byte for byte.
    assert elided['results'][0]['exitCode'] == 0
    for field in ('toolCallId', 'toolName', 'toolArgs', 'status', 'roundNum'):
        assert elided[field] == rounds[0][field]
    # The newest keep_tail rounds keep both payload and identity.
    assert compacted[-1] is rounds[-1]
    assert compacted[-2] is rounds[-2]
    assert _string_payload_chars(compacted) < _string_payload_chars(rounds)


def test_compaction_never_mutates_the_live_rounds():
    rounds = [_round(i, chars=8_000) for i in range(4)]

    compact_tool_rounds_for_frame_budget(rounds, budget_bytes=1_000,
                                         keep_tail=1)

    assert all(item['toolContent'] == 'x' * 8_000 for item in rounds)
    assert all('_persistCompacted' not in item for item in rounds)


def test_in_flight_rounds_are_never_elided():
    rounds = [_round(0, chars=16_000, status='running'),
              _round(1, chars=100)]

    compacted = compact_tool_rounds_for_frame_budget(
        rounds, budget_bytes=1_000, keep_tail=1)

    assert compacted[0] is rounds[0]


def test_compacted_lane_stays_a_valid_replay_prefix():
    rounds = [_round(i, chars=8_000) for i in range(6)]

    compacted = compact_tool_rounds_for_frame_budget(
        rounds, budget_bytes=20_000, keep_tail=2)

    assert scan_replayable_tool_round_prefix(
        compacted).blocked_position is None


def test_frame_compaction_cannot_leave_full_results_in_segment_mirrors():
    import orjson

    from lib.tasks_pkg.segments import (
        segments_to_json,
        tool_use_segment_from_round,
    )
    from lib.turn_projection_segments import projection_with_stable_segments

    rounds = [_round(i, chars=16_000) for i in range(4)]
    source_segments = segments_to_json([
        tool_use_segment_from_round(round_record, position)
        for position, round_record in enumerate(rounds)
    ])
    compacted = compact_tool_rounds_for_frame_budget(
        rounds, budget_bytes=1_000, keep_tail=1,
    )

    normalized = projection_with_stable_segments({
        'segments': source_segments,
        'toolRounds': compacted,
    })
    tool_segments = [
        segment for segment in normalized['segments']
        if segment['type'] == 'tool_use'
    ]

    assert [segment['blockId'] for segment in tool_segments] == [
        f'tool:call-{position}' for position in range(4)
    ]
    assert all(
        tool_segments[position]['result']['content']
        == compacted[position]['toolContent']
        for position in range(3)
    )
    assert source_segments[0]['result']['content'] == 'x' * 16_000
    assert len(orjson.dumps(tool_segments)) < (
        len(orjson.dumps(source_segments)) * 0.35
    )


def _spawn_round(position, *, chars=8_000, snapshot=None, envelope=False,
                 sparse=False):
    handle = {
        'agents': [
            {'id': 'a1', 'role': 'researcher',
             'objective': 'scan the suite ' + 'x' * chars},
            {'id': 'a2', 'role': 'coder', 'objective': 'fix failures'},
        ],
    }
    if envelope:
        payload = {'contractVersion': 'tofu.tool-result/v2', 'items': [handle]}
    elif sparse:
        # The shape tool rounds ACTUALLY persist: the sparse summary_items
        # model projection (lib/tools/result_envelope.py _model_projection)
        # intentionally drops contractVersion.
        payload = {'items': [handle],
                   'summary': 'Launched 2 agent(s) in the background.'}
    else:
        payload = handle
    round_item = {
        'roundNum': position,
        'llmRound': 0,
        'toolCallId': f'call-{position}',
        'toolName': 'spawn_agents',
        'toolArgs': {'agents': [{'id': 'a1'}, {'id': 'a2'}]},
        'status': 'done',
        'toolContent': json.dumps(payload),
        'results': [],
    }
    if snapshot is not None:
        round_item['_swarmSnapshot'] = snapshot
    return round_item


def test_spawn_round_donates_handle_stub_snapshot_before_elision():
    """Eliding a spawn handle must not leave the panel without a roster."""
    rounds = [_spawn_round(0), _round(1, chars=100)]

    compacted = compact_tool_rounds_for_frame_budget(
        rounds, budget_bytes=1_000, keep_tail=1)

    elided = compacted[0]
    assert elided['_persistCompacted'] is True
    assert elided['toolContent'].startswith('[payload elided')
    snap = elided['_swarmSnapshot']
    assert [a['id'] for a in snap['agents']] == ['a1', 'a2']
    assert snap['agents'][0]['role'] == 'researcher'
    assert snap['agents'][0]['objective'].startswith('scan the suite')
    assert all(a['status'] == 'unknown' for a in snap['agents'])
    # version 0 ranks below every driver-produced snapshot, so a real one
    # stamped later still wins the monotonic guard.
    assert snap['version'] == 0
    for field in ('toolCallId', 'toolName', 'toolArgs', 'status', 'roundNum'):
        assert elided[field] == rounds[0][field]


def test_spawn_handle_inside_tool_result_envelope_is_unwrapped():
    rounds = [_spawn_round(0, envelope=True), _round(1, chars=100)]

    compacted = compact_tool_rounds_for_frame_budget(
        rounds, budget_bytes=1_000, keep_tail=1)

    snap = compacted[0]['_swarmSnapshot']
    assert [a['id'] for a in snap['agents']] == ['a1', 'a2']


def test_spawn_handle_inside_sparse_projection_is_unwrapped():
    """Persisted spawn rounds carry the UNMARKED {summary, items} projection;
    the stub salvage must still recover the roster (conv mtgvz7gyrf3pg2)."""
    rounds = [_spawn_round(0, sparse=True), _round(1, chars=100)]

    compacted = compact_tool_rounds_for_frame_budget(
        rounds, budget_bytes=1_000, keep_tail=1)

    snap = compacted[0]['_swarmSnapshot']
    assert [a['id'] for a in snap['agents']] == ['a1', 'a2']


def test_existing_swarm_snapshot_is_never_replaced():
    real = {'agents': [{'id': 'a1', 'status': 'done'}], 'settled': True,
            'agentCount': 1, 'doneCount': 1, 'version': 100001}
    rounds = [_spawn_round(0, snapshot=real), _round(1, chars=100)]

    compacted = compact_tool_rounds_for_frame_budget(
        rounds, budget_bytes=1_000, keep_tail=1)

    assert compacted[0]['_swarmSnapshot'] == real
    assert compacted[0]['toolContent'].startswith('[payload elided')


def test_unparseable_spawn_handle_elides_without_a_snapshot():
    round_item = _spawn_round(0)
    round_item['toolContent'] = 'not json ' + 'x' * 8_000
    rounds = [round_item, _round(1, chars=100)]

    compacted = compact_tool_rounds_for_frame_budget(
        rounds, budget_bytes=1_000, keep_tail=1)

    assert compacted[0]['toolContent'].startswith('[payload elided')
    assert '_swarmSnapshot' not in compacted[0]
