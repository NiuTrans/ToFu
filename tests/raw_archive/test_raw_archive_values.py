"""Wire capture keeps original request values and response bytes."""
import json
from types import MappingProxyType

import pytest

from lib.raw_archive import RawArchiveCapture, _json_bytes

pytestmark = pytest.mark.unit


def test_request_capture_preserves_all_credentials_and_large_strings():
    original = {'api_key': 'sample-key', 'authorization': 'Bearer sample',
                'nested': {'password': 'sample', 'cookie': 'sample'},
                'content': 'A' * (4 * 1024 * 1024)}
    assert json.loads(_json_bytes(original)) == original


def test_capture_adapts_json_values_without_secret_field_filters():
    value = MappingProxyType({'secret': b'original', 'items': ('password=sample',)})
    assert json.loads(_json_bytes(value)) == {'secret': 'original', 'items': ['password=sample']}


def test_response_capture_keeps_bytes_without_text_decode_and_encode():
    capture = RawArchiveCapture({}, {'api_key': 'sample'}, transport_attempt=0, trace_id='sample')
    original = b'password=sample\x00\xff'
    try:
        capture.append_response(original)
        capture._response_file.seek(0)
        assert capture._response_file.read() == original
    finally:
        capture.discard()
