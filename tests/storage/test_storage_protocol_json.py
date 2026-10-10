"""The storage wire rejects lossy, non-standard JSON numbers."""

import math
from collections import UserDict
from collections.abc import Mapping
from types import MappingProxyType

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage.protocol import canonical_json, encode_frame, recv_frame, validate_finite_json_numbers


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


def test_storage_encoders_materialize_mapping_views():
    view = MappingProxyType(
        {'name': 'search', 'nested': [MappingProxyType({'k': 'v'})]})
    document = {'cfg': view, 'clean': {'list': [1, 2]}}

    assert orjson.loads(canonical_json(document)) == {
        'cfg': {'name': 'search', 'nested': [{'k': 'v'}]},
        'clean': {'list': [1, 2]},
    }
    frame = encode_frame({'payload': document})
    assert orjson.loads(frame[4:])['payload']['cfg'] == {
        'name': 'search', 'nested': [{'k': 'v'}]}


def test_storage_encoders_still_reject_unserializable_values():
    with pytest.raises(StorageError, match='not serializable'):
        encode_frame({'payload': {'bad': object()}})


@pytest.mark.parametrize('number', [math.nan, math.inf, -math.inf])
def test_admission_rejects_hidden_nonfinite_values_in_custom_and_cyclic_containers(number):
    class DictSubclass(dict):
        pass

    class ListSubclass(list):
        pass

    class FloatSubclass(float):
        pass

    cycle = []
    cycle.extend([cycle, {'deep': FloatSubclass(number)}])
    for document in [UserDict({'deep': number}), MappingProxyType({'deep': number}),
                     DictSubclass(deep=number), ListSubclass([number]), (number,), cycle]:
        with pytest.raises(StorageError, match='non-finite JSON number'):
            validate_finite_json_numbers(document)


def test_admission_visits_shared_mapping_once_and_preserves_materialized_bytes():
    class CountingMapping(UserDict):
        visits = 0

        def values(self):
            self.visits += 1
            return super().values()

    shared = CountingMapping({'finite': 1.25, 'nested': (None, True, 17, 'text')})
    validate_finite_json_numbers([shared, shared])
    assert shared.visits == 1
    document = {'left': MappingProxyType({'array': [None, True, 17, 'text', 1.25]}),
                'right': {'integer': 2**63 - 1}}
    expected = {'left': {'array': [None, True, 17, 'text', 1.25]}, 'right': {'integer': 2**63 - 1}}
    assert orjson.loads(canonical_json(document)) == expected
    assert orjson.loads(encode_frame({'payload': document})[4:])['payload'] == expected


def test_scalar_and_sequence_subclasses_keep_registered_mapping_admission():
    class MappingText(str):
        def values(self):
            return [math.nan]

    class MappingList(list):
        def values(self):
            return [math.inf]

    Mapping.register(MappingText)
    Mapping.register(MappingList)
    for document in [MappingText('finite text'), MappingList([17])]:
        with pytest.raises(StorageError, match='non-finite JSON number'):
            validate_finite_json_numbers(document)
