"""The storage wire rejects lossy, non-standard JSON numbers."""

import math

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage.protocol import canonical_json, encode_frame, recv_frame


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('value', [math.nan, math.inf, -math.inf])
def test_storage_encoders_reject_non_finite_numbers(value):
    document = {'nested': [{'value': value}]}

    with pytest.raises(StorageError, match='non-finite JSON number'):
        canonical_json(document)
    with pytest.raises(StorageError, match='non-finite JSON number'):
        encode_frame({'payload': document})


def test_storage_encoders_preserve_finite_numbers():
    document = {'small': -1.25, 'large': 1.0e100}

    assert orjson.loads(canonical_json(document)) == document
    assert encode_frame({'payload': document})


def test_receive_preallocates_one_buffer_and_admits_before_payload_bytes():
    wire = encode_frame({'payload': {'text': 'fragmented'}})

    class FragmentedSocket:
        def __init__(self):
            self.offset = 0

        def recv(self, _size):
            raise AssertionError('the retained transport must use recv_into')

        def recv_into(self, target, _size=0):
            count = min(3, len(target), len(wire) - self.offset)
            if count <= 0:
                return 0
            target[:count] = wire[self.offset:self.offset + count]
            self.offset += count
            return count

    transport = FragmentedSocket()
    admitted = []
    message = recv_frame(
        transport,
        before_payload=lambda size: admitted.append((size, transport.offset)),
    )

    assert message == {'payload': {'text': 'fragmented'}}
    assert admitted == [(len(wire) - 4, 4)]
