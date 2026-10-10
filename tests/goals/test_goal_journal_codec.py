"""Oversized journal packets grant authority only through complete references."""
from copy import deepcopy
import json

import pytest

from lib.goal_runs.journal_codec import decode_events, encode_event
from lib.orchestration.execution_journal import RecoveryRefused

pytestmark = pytest.mark.unit


def test_unicode_packets_round_trip_under_the_native_event_document_boundary():
    event = {'type': 'execution_effect_completed', 'result': '原始工具证据' * 250000}
    packets = list(encode_event(event))
    assert len(packets) > 2
    assert all(len(json.dumps(packet, ensure_ascii=False).encode()) < 1 << 20
               for packet in packets)
    assert list(decode_events(packets)) == [event]
    assert list(decode_events(packets[:-1])) == []


@pytest.mark.parametrize('damage', ['missing', 'reordered', 'corrupt', 'identity'])
def test_incomplete_or_changed_packets_never_grant_a_completion(damage):
    packets = list(encode_event({'type': 'execution_effect_completed', 'result': 'x' * (2 << 20)}))
    damaged = deepcopy(packets)
    if damage == 'missing':
        damaged.pop(0)
    elif damage == 'reordered':
        damaged[0], damaged[1] = damaged[1], damaged[0]
    elif damage == 'corrupt':
        damaged[0]['data'] = 'eA=='
    else:
        damaged[-1]['type'] = 'execution_journal_start'
    with pytest.raises(RecoveryRefused):
        list(decode_events(damaged))
