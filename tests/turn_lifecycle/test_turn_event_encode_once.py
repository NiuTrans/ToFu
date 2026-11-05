"""Each persisted turn-event value is encoded once (executable specification).

``turn.event.record`` measures its byte bounds with ``len`` of exactly the
bytes the storage clients splice (``lib.storage.protocol.FrozenPayload``):
the inline-evidence decision, the Attempt-event document bound and the batch
event budget read the same compact UTF-8 encoding the authority receives, and
the inline evidence copy and the carried row share one encoding. A counting
instrument over the single admitted encoder proves the count; differential
checks prove the requests both clients send are unchanged.
"""
import json
from collections import Counter

import orjson
import pytest

from lib.storage.protocol import (
    FrozenPayload, canonical_json, encode_frame, encode_json_value, wire_payload,
)
from lib.storage.v2_semantics import request_payload
from tests.turn_lifecycle.test_task_event_delivery import _task, _discard
from tofu_db.wire import _payload_bytes

pytest_plugins = ['tests.turn_lifecycle.test_task_event_delivery']
pytestmark = pytest.mark.unit


@pytest.fixture
def encodes(monkeypatch):
    """Encode-count instrument: encodings per value object (kept alive)."""
    from lib.storage import protocol
    counts, held = Counter(), []
    original = protocol._json_value_bytes

    def counting(value):
        counts[id(value)] += 1
        held.append(value)
        return original(value)

    monkeypatch.setattr(protocol, '_json_value_bytes', counting)
    return counts


@pytest.fixture
def stdlib_wire(monkeypatch):
    """Payloads the storage.v2 encoder serialized with the stdlib fallback."""
    import tofu_db.wire as wire
    calls = []
    original = wire._stdlib_payload_bytes

    def recording(payload):
        calls.append(payload)
        return original(payload)

    monkeypatch.setattr(wire, '_stdlib_payload_bytes', recording)
    return calls


def _capture(storage, monkeypatch, operation='turn.event.record'):
    sent = []
    original = storage.client.command

    def command(name, payload, *args, **kwargs):
        if name == operation:
            sent.append(payload)
        return original(name, payload, *args, **kwargs)

    monkeypatch.setattr(storage.client, 'command', command)
    return sent


def _snapshot(round_num, messages):
    return {'type': 'messages_snapshot', 'kind': 'request', 'roundNum': round_num,
            'turn': 'request:encode-once', 'model': 'test-model',
            'label': f'request {round_num}', 'messages': messages,
            'tools': [{'type': 'function', 'function': {
                'name': 'read_file', 'description': 'd' * 300,
                'parameters': {'type': 'object'}}}]}


_HISTORY = [{'role': 'system', 'content': 'system prompt'},
            {'role': 'user', 'content': 'u' * 70_000}]
_GROWN = [*_HISTORY, {'role': 'assistant', 'content': 'calling read_file'},
          {'role': 'tool', 'tool_call_id': 'call-1', 'content': '结果 "quoted"\n' * 50}]


def _record(turn_service, task, sequence, frame):
    from lib.tasks_pkg.event_log import project_persistent_event
    carrier = {'task_id': task['id'], 'sequence': sequence,
               'event': project_persistent_event(task['id'], frame)}
    assert turn_service.record_task_event(task, frame, task_event=carrier) == 'carried'


def _finish(task):
    from lib.tasks_pkg.snapshot_delta import forget_projector_task
    forget_projector_task(task['id'])
    _discard(task)


def test_snapshot_delta_is_encoded_once_for_its_copy_carrier_and_bound(
        turn_service, storage, monkeypatch, encodes, stdlib_wire):
    _, task = _task(turn_service, storage, 'encode-once')
    sent = _capture(storage, monkeypatch)
    try:
        _record(turn_service, task, 0, _snapshot(1, _HISTORY))
        grown = _snapshot(2, _GROWN)
        _record(turn_service, task, 1, grown)
        payload = sent[-1]
        assert isinstance(payload, FrozenPayload)
        evidence = payload['task_event']['event']
        assert evidence['prefixLen'] == len(_HISTORY)
        inline = payload['event_payload']['event']
        assert inline == evidence and inline is not evidence
        # One encoding of the delta serves the inline copy, the carried row
        # and the Attempt-event bound; the patch is measured and sent once;
        # the full frame is encoded only to measure the inline bound.
        assert encodes[id(evidence)] == 1
        assert encodes[id(inline)] == 0
        assert encodes[id(payload['projection_patch'])] == 1
        assert encodes[id(grown)] == 1
        delta = encode_json_value(evidence)
        assert delta in payload.encoded['task_event']
        assert delta in payload.encoded['event_payload']
        # storage.v2 splices the bytes; it never re-encodes the command.
        assert [body for body in stdlib_wire if 'event_payload' in body] == []
    finally:
        _finish(task)


def _thawed(payload):
    """The plain request the pre-change producer sent for this command."""
    return orjson.loads(orjson.dumps(dict(payload)))


def test_requests_both_clients_send_are_unchanged(turn_service, storage, monkeypatch):
    _, task = _task(turn_service, storage, 'encode-once-wire')
    sent = _capture(storage, monkeypatch)
    frames = [
        {'type': 'phase', 'phase': 'calling', 'detail': '模型'},
        {'type': 'delta', 'content': '思考中 "quoted"\n\t\u2028 tail'},
        {'type': 'tool_start', 'toolName': 'read_file', 'roundNum': 1},
        {'type': 'tool_compacted', 'roundNum': 1, 'summary': '摘要 ' * 2000},
        _snapshot(1, _HISTORY),
        _snapshot(2, _GROWN),
        {'type': 'tool_result', 'toolName': 'read_file', 'roundNum': 1,
         'ratio': 0.25, 'tiny': 1e-05, 'big': 1e16},
    ]
    try:
        for sequence, frame in enumerate(frames):
            _record(turn_service, task, sequence, frame)
        assert len(sent) == len(frames)
        for payload in sent:
            plain = _thawed(payload)
            # storage.v1 frames were already orjson: unchanged bytes.
            assert (encode_frame({'payload': wire_payload(payload)})
                    == encode_frame({'payload': plain}))
            new = _payload_bytes(request_payload('turn.event.record', payload, owner_id=1))
            old = _payload_bytes(request_payload('turn.event.record', plain, owner_id=1))
            stdlib = json.dumps(plain, separators=(',', ':'), ensure_ascii=False).encode()
            if stdlib == orjson.dumps(plain):
                assert new == old
            else:
                # Only float spellings differ (``1e-05`` vs ``0.00001``); both
                # authorities store the parsed value.
                assert orjson.loads(new) == orjson.loads(old)
        stored = storage.client.query('event.list', {
            'task_id': task['id'], 'after_sequence': -1, 'limit': 100})
        last = stored[-1]['event']
        assert (last['ratio'], last['tiny'], last['big']) == (0.25, 1e-05, 1e16)
    finally:
        _finish(task)


def test_inline_snapshot_bound_measures_the_stored_utf8_bytes(
        turn_service, storage, monkeypatch):
    """The 64 KiB inline bound counts stored bytes, not ASCII escapes.

    CJK text is three UTF-8 bytes but a six-character ``\\uXXXX`` escape; the
    former measure moved such frames to their delta form at a third of the
    stored size. ASCII frames decide exactly as before.
    """
    _, task = _task(turn_service, storage, 'encode-once-utf8')
    sent = _capture(storage, monkeypatch)
    cjk = [{'role': 'system', 'content': 'system prompt'},
           {'role': 'user', 'content': '证' * 15_000}]
    try:
        _record(turn_service, task, 0, _snapshot(1, cjk))
        grown = _snapshot(2, [*cjk, {'role': 'assistant', 'content': 'ok'}])
        assert len(orjson.dumps(grown)) <= 64 * 1024 < len(json.dumps(grown, separators=(',', ':')))
        _record(turn_service, task, 1, grown)
        assert sent[-1]['event_payload']['event'] is grown
        ascii_grown = _snapshot(3, [*cjk, {'role': 'assistant', 'content': 'ok'},
                                    {'role': 'user', 'content': 'a' * 30_000}])
        assert len(orjson.dumps(ascii_grown)) > 64 * 1024
        _record(turn_service, task, 2, ascii_grown)
        assert sent[-1]['event_payload']['event']['prefixLen'] == 3
    finally:
        _finish(task)


def test_batch_entries_send_and_budget_their_single_encoding(
        turn_service, storage, monkeypatch, stdlib_wire):
    _, task = _task(turn_service, storage, 'encode-once-batch')
    sent = _capture(storage, monkeypatch, 'turn.event.record_batch')
    try:
        assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'starting'})
        entries = []
        for sequence, event in enumerate([
                {'type': 'phase', 'phase': 'calling'},
                {'type': 'tool_compacted', 'roundNum': 1, 'summary': '摘要' * 3000},
                {'type': 'round_end', 'roundNum': 1, 'reason': 'tool_calls', 'ratio': 0.5}]):
            wire = {**event, 'seq': sequence, 'taskId': task['id'],
                    'attemptId': task['_attemptId']}
            entries.append(turn_service.TaskEventBatchEntry(
                raw_event=wire, sequence=sequence,
                projection_source=turn_service.capture_task_projection_source(task, wire)))
        assert turn_service.record_task_events(task, entries) == ['carried'] * 3
        (payload,) = sent
        encoded = [encode_json_value(entry.wire_body()) for entry in payload['entries']]
        assert payload.encoded['entries'] == b'[' + b','.join(encoded) + b']'
        # The event budget charges the same length the former canonical
        # encoding of each entry had, so chunking decisions are unchanged.
        assert [len(entry) for entry in encoded] == [
            len(canonical_json(_thawed(entry))) for entry in payload['entries']]
        assert [body for body in stdlib_wire if 'entries' in body] == []
    finally:
        _discard(task)


def test_managed_lane_keeps_settled_snapshot_messages_for_the_delta_projector(
        turn_service, storage, owner, monkeypatch):
    """Producer snapshots survive the lane detach as the same settled
    messages, so the projector encodes only new messages, and the stored
    delta rows equal those of plain (unsettled) messages byte for byte."""
    import lib.tasks_pkg.snapshot_delta as snapshot_delta
    from lib.tasks_pkg import event_delivery
    from lib.tasks_pkg.manager import _events as manager
    from lib.tasks_pkg.round_evidence import begin_round_evidence, build_round_evidence
    _, task = _task(turn_service, storage, 'encode-once-lane')
    encoded = []
    original_canon = snapshot_delta._canon

    def counted(value):
        encoded.append(value)
        return original_canon(value)

    monkeypatch.setattr(snapshot_delta, '_canon', counted)
    history = [{'role': 'system', 'content': 'system prompt'},
               {'role': 'user', 'content': '问题 ' * 500}]
    plain = snapshot_delta.SnapshotProjector()
    try:
        for number in range(1, 5):
            ref = begin_round_evidence(task, task['id'], number)
            prepared = orjson.loads(orjson.dumps(history))
            event = build_round_evidence(task, [], ref=ref, kind='request', model='m',
                                         prepared_messages=prepared)
            expected = plain.project('plain-oracle', orjson.loads(orjson.dumps(event)))
            encoded.clear()
            manager.append_event(task, event)
            event_delivery.flush_task_events(task)
            assert len(encoded) == (len(history) if number == 1 else 2)
            stored = storage.client.query('event.list', {
                'task_id': task['id'], 'after_sequence': -1, 'limit': 100})[-1]['event']
            for key in ('prefixLen', 'prefixHash', 'messageCount', 'newMessages'):
                assert stored.get(key) == expected.get(key)
            history = [*history, {'role': 'assistant', 'content': f'step {number}'},
                       {'role': 'tool', 'tool_call_id': f'c{number}', 'content': '结果\n' * 50}]
    finally:
        _finish(task)


def test_an_unencodable_snapshot_is_refused_before_dispatch_and_shed(
        turn_service, storage, monkeypatch):
    """No client can encode a lone surrogate: the refusal happens before
    dispatch with the same error class, and the evidence-shedding path keeps
    the frame's projection patch and an explicitly degraded row."""
    _, task = _task(turn_service, storage, 'encode-once-surrogate')
    sent = _capture(storage, monkeypatch)
    try:
        _record(turn_service, task, 0, _snapshot(1, [
            {'role': 'user', 'content': 'lone \ud800 surrogate'}]))
        assert len(sent) == 1 and 'event' not in sent[0]['event_payload']
        stored = storage.client.query('event.list', {
            'task_id': task['id'], 'after_sequence': -1, 'limit': 10})[-1]['event']
        assert stored['degraded'] is True
        assert 'Storage frame is not serializable' in stored['degradedReason']
        assert task['_debugEvidenceOmittedCount'] == 1
    finally:
        _finish(task)


def test_attempt_event_bound_reads_the_spliced_bytes(monkeypatch):
    from lib import turn_lifecycle
    monkeypatch.setattr(turn_lifecycle, '_ATTEMPT_EVENT_ENVELOPE_RESERVE_BYTES', 100)
    evidence = {'type': 'tool_compacted', 'summary': '摘要 "q"\n' * 400}
    patch = {'baseRevision': 1, 'targetRevision': 2, 'operations': [{'op': 'set', 'value': 'é'}]}
    needed = len(orjson.dumps(evidence)) + len(orjson.dumps(patch)) + 100
    for bound, kept in ((needed, True), (needed - 1, False)):
        monkeypatch.setattr(turn_lifecycle, '_ATTEMPT_EVENT_DOCUMENT_MAX_BYTES', bound)
        encoding = turn_lifecycle._CommandEncoding()
        command = {'event_payload': {'phase': None, 'event': evidence},
                   'projection_patch': patch}
        shed = turn_lifecycle._shed_oversized_inline_evidence(command, encoding)
        assert (shed is None) is kept
        assert ('event' in command['event_payload']) is kept
        frozen = encoding.frozen(command)
        assert orjson.loads(frozen.encoded['event_payload']) == command['event_payload']
