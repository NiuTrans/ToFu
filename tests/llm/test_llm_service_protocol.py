"""LLMService protocol, dispatch adapter, and consumer binding."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
from lib.protocols import LLMService, resolve_llm_service


pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROTOCOL_BOUND_LLM_CONSUMERS = (
    _REPO_ROOT / 'plugins/tofu-trading/tofu_trading/trading_autopilot/cycle.py',
    _REPO_ROOT / 'plugins/tofu-trading/tofu_trading/trading/intel.py',
)


def test_resolve_llm_service_returns_injected_instance():
    mock = MagicMock()
    mock.chat = MagicMock(return_value=('ok', {}))
    mock.stream = MagicMock()
    assert resolve_llm_service(mock) is mock


def test_resolve_llm_service_default_satisfies_protocol():
    from lib.llm_dispatch.service import DispatchLLMService

    llm = resolve_llm_service(None)
    assert isinstance(llm, DispatchLLMService)
    assert isinstance(llm, LLMService)
    assert resolve_llm_service(None) is llm


def test_dispatch_llm_service_chat_maps_prefer_model(monkeypatch):
    from lib.llm_dispatch.service import DispatchLLMService

    captured: dict[str, object] = {}

    def fake_smart_chat(messages, **kwargs):
        captured['messages'] = messages
        captured.update(kwargs)
        return 'hello', {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', fake_smart_chat)
    content, usage = DispatchLLMService().chat(
        [{'role': 'user', 'content': 'hi'}],
        max_tokens=32,
        capability='cheap',
        prefer_model='flash',
        log_prefix='[Test]',
    )
    assert content == 'hello'
    assert usage['total_tokens'] == 2
    assert captured['model'] == 'flash'
    assert captured['capability'] == 'cheap'
    assert captured['max_tokens'] == 32
    assert captured['log_prefix'] == '[Test]'


def test_dispatch_llm_service_stream_returns_typed_result(monkeypatch):
    from lib.llm_dispatch.service import DispatchLLMService

    def fake_dispatch_stream(body, **kwargs):
        assert body == [{'role': 'user', 'content': 'hi'}]
        assert kwargs['capability'] == 'text'
        return (
            {'role': 'assistant', 'content': 'ok'},
            'stop',
            {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
        )

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', fake_dispatch_stream)
    result = DispatchLLMService().stream(
        [{'role': 'user', 'content': 'hi'}],
        capability='text',
    )
    assert isinstance(result, ProviderStreamResult)
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.is_verified_complete


def test_protocol_bound_consumers_use_resolver_not_dual_path():
    for path in _PROTOCOL_BOUND_LLM_CONSUMERS:
        source = path.read_text(encoding='utf-8')
        assert 'resolve_llm_service' in source, path
        assert 'from lib.llm_dispatch import smart_chat' not in source, path
        assert '_chat_fn' not in source, path
