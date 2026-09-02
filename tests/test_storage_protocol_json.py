"""The storage wire rejects lossy, non-standard JSON numbers."""

import math

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage.protocol import canonical_json, encode_frame


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
