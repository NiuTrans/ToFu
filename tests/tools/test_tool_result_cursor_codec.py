"""Stateless cursor scope, prior-version replay and authenticated positions."""
import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from lib.storage.errors import StorageError
from lib.storage_authority_sql.operations_pkg import _tool_result_query as codec

pytestmark = pytest.mark.unit
VECTOR = json.loads((Path(__file__).parents[1] / 'fixtures' /
                     'tool_result_cursor_protocol.json').read_text())


class KeySession:
    def __init__(self, key=bytes(VECTOR['keyBytes'])):
        self.key = key

    def fetch_one(self, *args):
        return {'value_json': json.dumps(self.key.hex())}


def payload(**changes):
    owner, reference, _operation, query, selector = VECTOR['binding']
    return dict(user_id=owner, artifact_ref=reference, query=query,
                selector=selector, now_ms=VECTOR['nowMs']) | changes


@pytest.mark.parametrize('version', ['legacyCursor', 'previousCursor', 'currentCursor'])
def test_prior_and_current_cursor_vectors_replay_exact_positions(version):
    value = payload(cursor=VECTOR['sql'][version])
    assert codec.decode_cursor(KeySession(), value, 'search') == VECTOR['sql']['position']


def test_current_cursor_vector_authenticates_scope_without_repeating_it():
    state = VECTOR['sql']['position']
    cursor = codec.encode_cursor(KeySession(), payload(), 'search', state)
    assert cursor == VECTOR['sql']['currentCursor']
    raw = cursor.split('.')[1]
    assert base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)).hex() == VECTOR['sql']['positionBytesHex']
    expanded = codec.encode_cursor(KeySession(), payload(
        query='🙂' * 200, selector='/' + '段' * 1023), 'search', state)
    assert len(expanded) == len(cursor)
    assert expanded != cursor


@pytest.mark.parametrize('version', ['legacyCursor', 'previousCursor', 'currentCursor'])
@pytest.mark.parametrize('changes,operation', [
    ({'user_id': 8}, 'search'),
    ({'artifact_ref': 'tool-result:' + 'b' * 64}, 'search'),
    ({'query': 'different'}, 'search'),
    ({'selector': '/different'}, 'search'),
    ({}, 'read'),
])
def test_every_scope_field_remains_authenticated(version, changes, operation):
    with pytest.raises(StorageError):
        codec.decode_cursor(KeySession(), payload(
            cursor=VECTOR['sql'][version], **changes), operation)


@pytest.mark.parametrize('version', ['legacyCursor', 'previousCursor', 'currentCursor'])
def test_wrong_key_and_observed_expiry_refuse(version):
    value = payload(cursor=VECTOR['sql'][version])
    with pytest.raises(StorageError):
        codec.decode_cursor(KeySession(b'x' * 32), value, 'search')
    with pytest.raises(StorageError):
        codec.decode_cursor(KeySession(), value | {
            'now_ms': VECTOR['sql']['position']['expiry']}, 'search')


@pytest.mark.parametrize('mutation', ['position', 'signature', 'prefix', 'padding', 'suffix'])
def test_current_cursor_tampering_and_noncanonical_encoding_refuse(mutation):
    prefix, body, signature = VECTOR['sql']['currentCursor'].split('.')
    if mutation == 'position':
        state = VECTOR['sql']['position'] | {'o': 12}
        body = base64.urlsafe_b64encode(json.dumps(state).encode()).decode().rstrip('=')
    elif mutation == 'signature':
        signature = ('A' if signature[0] != 'A' else 'B') + signature[1:]
    elif mutation == 'prefix':
        prefix = 'trq1'
    elif mutation == 'padding':
        signature += '='
    cursor = '.'.join((prefix, body, signature))
    if mutation == 'suffix':
        cursor += '.extra'
    with pytest.raises(StorageError):
        codec.decode_cursor(KeySession(), payload(cursor=cursor), 'search')


@pytest.mark.parametrize('changes', [
    {'r': [0] * 10}, {'r': [128]}, {'p': -1}, {'o': True},
    {'expiry': -1}, {'partial': 0}, {'unknown': 1},
])
def test_authenticated_invalid_positions_still_refuse(changes):
    with pytest.raises(StorageError):
        cursor = codec.encode_cursor(KeySession(), payload(), 'search',
                                     VECTOR['sql']['position'] | changes)
        codec.decode_cursor(KeySession(), payload(cursor=cursor), 'search')


def test_compact_position_preserves_every_field_with_less_wire_overhead():
    state = VECTOR['sql']['position']
    cursor = codec.encode_cursor(KeySession(), payload(), 'search', state)
    assert cursor.startswith('trq3.')
    assert len(cursor) < len(VECTOR['sql']['previousCursor']) * .6
    assert codec.decode_cursor(KeySession(), payload(cursor=cursor), 'search') == state
    # Fixed independent byte vector: flags, route length/indices, p/o/skip/expiry.
    body = cursor.split('.')[1]
    assert base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)).hex() == (
        '00020102030b00c0949dffbc31')


@pytest.mark.parametrize('position_hex', VECTOR['sql']['invalidPositionBytesHex'])
def test_authenticated_malformed_compact_positions_refuse(position_hex):
    body = bytes.fromhex(position_hex)
    frame = (b'tofu.tool-result-query/trq3\0'
             + json.dumps(VECTOR['binding'], ensure_ascii=False, separators=(',', ':')).encode()
             + b'\0' + body)
    signature = hmac.new(bytes(VECTOR['keyBytes']), frame, hashlib.sha256).digest()
    encoded = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
    cursor = 'trq3.' + encoded(body) + '.' + encoded(signature)
    with pytest.raises(StorageError) as caught:
        codec.decode_cursor(KeySession(), payload(cursor=cursor), 'search')
    assert caught.value.code == codec.CONTRACT['failures']['invalid_cursor']['storageCode']


@pytest.mark.parametrize('offset', [0, 127, 128, 16383, 16384, 2**32, 2**53+1, 2**64-1])
def test_compact_positions_keep_uint64_boundaries_and_partial_state(offset):
    state = {'r': [127] * 9, 'p': offset, 'o': offset, 'skip': offset,
             'expiry': 0, 'partial': True}
    cursor = codec.encode_cursor(KeySession(), payload(), 'search', state)
    assert codec.decode_cursor(KeySession(), payload(cursor=cursor), 'search') == state
