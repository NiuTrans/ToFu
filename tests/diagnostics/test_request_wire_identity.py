"""A retry's wire diagnostic belongs only to its own provider usage."""
import pytest

pytestmark = pytest.mark.unit


def _events(*, stamped=True):
    events = [{'type': 'messages_snapshot', 'ts_ms': 1, 'payload': {
        'kind': 'request', 'roundNum': 1, 'messages': [], 'tools': [],
    }}]
    for seq in (1, 2):
        events.append({'type': 'tool_wire_projection', 'ts_ms': seq * 10, 'payload': {
            'roundNum': 1, 'parentSpanId': 'model:attempt:1', 'dispatchSeq': seq,
            'toolNames': [f'tool_{seq}'], 'toolCount': seq,
            'schemaFingerprint': f'fingerprint-{seq}',
        }})
        usage = {'prompt_tokens': 100, 'completion_tokens': 1}
        if stamped:
            usage.update(request_span_id='model:attempt:1', request_dispatch_seq=seq)
        events.append({'type': 'round_usage', 'ts_ms': seq * 10 + 1, 'payload': {
            'roundNum': 1, 'usage': usage, 'tokensIn': 100, 'tokensOut': 1,
        }})
    return events


def test_retry_usage_joins_its_dispatch_not_last_round_projection():
    from lib.tasks_pkg.request_inspector import _fold_event_rows
    rows, _ = _fold_event_rows(_events())
    row = rows[0]
    assert row['wireProjectionStatus'] == 'ambiguous'
    assert 'wireToolsCount' not in row
    assert [a['wireProjection']['toolNames'] for a in row['attempts']] == [
        ['tool_1'], ['tool_2']]
    assert all(a['wireProjectionStatus'] == 'request_identity' for a in row['attempts'])


def test_unstamped_multi_attempt_record_is_not_falsely_attributed():
    from lib.tasks_pkg.request_inspector import _fold_event_rows
    rows, _ = _fold_event_rows(_events(stamped=False))
    assert all(a['wireProjectionStatus'] == 'ambiguous' for a in rows[0]['attempts'])
    assert all('wireProjection' not in a for a in rows[0]['attempts'])


def test_multiple_physical_wires_in_dispatch_are_ambiguous():
    from lib.tasks_pkg.request_inspector import _fold_event_rows
    events = _events()
    duplicate = dict(events[1], payload=dict(events[1]['payload'], toolNames=['changed']))
    events.insert(2, duplicate)
    rows, _ = _fold_event_rows(events)
    assert rows[0]['attempts'][0]['wireProjectionStatus'] == 'ambiguous'
    assert 'wireProjection' not in rows[0]['attempts'][0]
    assert rows[0]['attempts'][1]['wireProjection']['toolNames'] == ['tool_2']


def test_payload_keeps_all_retry_projections_without_selecting_last(monkeypatch):
    from lib.tasks_pkg import request_inspector as inspector
    monkeypatch.setattr(inspector, '_read_events', lambda _, rebuild=True: (_events(), True))
    payload = inspector.get_request_payload('test', 1)
    assert payload['wireProjectionStatus'] == 'ambiguous'
    assert 'wireProjection' not in payload
    assert [p['dispatchSeq'] for p in payload['wireProjections']] == [1, 2]


def test_explicit_identity_never_falls_back_to_other_request():
    from lib.tasks_pkg.request_inspector import _fold_event_rows
    events = _events()
    events[2]['payload']['usage']['request_span_id'] = 'missing-request'
    rows, _ = _fold_event_rows(events)
    assert rows[0]['attempts'][0]['wireProjectionStatus'] == 'missing'
    assert 'wireProjection' not in rows[0]['attempts'][0]
