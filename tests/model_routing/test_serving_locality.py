"""Cloud/local identity follows the bound serving route, including proxies."""
from types import SimpleNamespace

import pytest

from lib.model_routing.serving_locality import (
    candidate_is_local_inference, is_local_inference,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('facts,expected', [
    ({'protocol': 'local', 'base_url': 'https://sglang.example/v1'}, True),
    ({'brand': 'local', 'base_url': 'https://vllm.example/v1'}, True),
    ({'credential_kind': 'local_identity', 'base_url': 'https://models.example/v1'}, True),
    ({'base_url': 'http://127.0.0.1:8000/v1'}, True),
    ({'base_url': 'http://10.0.0.8:30000/v1'}, True),
    ({'base_url': 'http://serving.internal/v1'}, True),
    ({'base_url': 'http://8.8.8.8:8000/v1'}, True),
    ({'brand': 'openai', 'base_url': 'http://127.0.0.1:8080/v1'}, False),
    ({'credential_kind': 'oauth', 'base_url': 'http://127.0.0.1:8080/v1'}, False),
    ({'brand': 'deepseek', 'base_url': 'https://api.deepseek.com'}, False),
    ({'base_url': 'https://unknown-cloud.example/v1'}, False),
])
def test_serving_identity(facts, expected):
    assert is_local_inference(**facts) is expected


def test_compiled_candidate_keeps_explicit_local_brand_on_remote_host():
    candidate = SimpleNamespace(provider={'brand': 'local'},
        connection={'protocol': 'openai', 'base_url': 'https://serving.example/v1'},
        credential={'kind': 'api_key'})
    assert candidate_is_local_inference(candidate)


@pytest.mark.parametrize('local', [False, True])
def test_native_binding_uses_issued_serving_identity(local):
    from lib.llm_dispatch.native_context_scope import native_context_scope
    from lib.llm_dispatch.context_policy import task_local_inference
    task = {'id': 'native-idle', '_userId': 7, 'model': 'model', 'config': {}}
    metadata = {'owner_user_id': 7, 'provider_id': 'provider',
                'logical_model': 'model', 'wire_model': 'model',
                'context_window': 100_000, 'local_inference': local}
    with native_context_scope(task, metadata, 1000):
        assert task_local_inference(task) is local
