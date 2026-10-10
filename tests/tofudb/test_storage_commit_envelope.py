"""Native wire clients expose raw diagnostics and validated semantic values."""
import json
import pytest
from tofu_db.wire import StorageV2Response, StorageV2ProtocolError
from tofu_db.generated_v2 import MAX_COMMIT_NOTICE_KEYS

pytestmark = pytest.mark.unit


def response(value):
    return StorageV2Response(0, 1, json.dumps(value).encode(), None, None, False)


@pytest.mark.parametrize('value', [None, False, 0, {}, [], {'version': 1}])
def test_value_preserves_plain_and_wrapped_semantic_results(value):
    assert response(value).value() == value
    envelope = {'_storageCommitContract': 'storage.committed-events/v1', 'value': value,
                'events': [{'contract': 'storage.commit-notice/v1'}]}
    wrapped = response(envelope)
    assert wrapped.value() == value
    assert wrapped.json() == envelope


@pytest.mark.parametrize('envelope', [
    {'_storageCommitContract': 'unknown', 'value': 1, 'events': []},
    {'_storageCommitContract': 'storage.committed-events/v1', 'events': []},
    {'_storageCommitContract': 'storage.committed-events/v1', 'value': 1, 'events': [True]},
    {'_storageCommitContract': 'storage.committed-events/v1', 'value': 1,
     'events': [{}] * (MAX_COMMIT_NOTICE_KEYS + 1)},
])
def test_value_rejects_malformed_private_envelopes(envelope):
    with pytest.raises(StorageV2ProtocolError):
        response(envelope).value()
