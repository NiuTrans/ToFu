"""Configured windows survive route binding, owner scoping and wire fallback."""
from types import SimpleNamespace

import pytest

from lib.llm_dispatch.context_policy import pinned_context_profile, slot_context_window
from lib.llm_dispatch.provider_pin import provider_pin
from lib.llm_dispatch.slot import Slot
from lib.tasks_pkg.compaction._tokens import _get_context_limit

pytestmark = pytest.mark.unit


def slot(window=1_000_000, owner=7, pin='request-a', **kwargs):
    return Slot(key_name='test', api_key='', model='gateway-flash',
                logical_model='deepseek-v4.1-flash', capabilities={'text'},
                context_window=window, provider_id=pin,
                routing_provider_id='gateway', routing_owner_user_id=owner,
                request_scoped=True, **kwargs)


def pool(monkeypatch, slots):
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher',
                        lambda: SimpleNamespace(slots=slots))
    monkeypatch.setattr('lib.context_limits.resolve_learned_context_limit',
                        lambda provider, model, window: window)


def test_configured_one_million_reaches_compaction_with_gateway_alias(monkeypatch):
    pool(monkeypatch, [slot(), slot(32_000, owner=8, pin='request-b')])
    task = {'_userId': 7, '_pinned_provider_id': 'request-a',
            'config': {'model': 'deepseek-v4.1-flash'}}
    assert _get_context_limit(task) == 1_000_000
    with provider_pin('request-a'):
        from lib.tasks_pkg.compaction._tokens import resolve_model_context_limit
        assert resolve_model_context_limit('gateway-flash', 'gateway') == 1_000_000


def test_unscoped_read_cannot_borrow_another_owners_model_configuration(monkeypatch):
    pool(monkeypatch, [slot()])
    assert pinned_context_profile('deepseek-v4.1-flash') is None
    assert pinned_context_profile('deepseek-v4.1-flash',
                                  owner_user_id=8, pin='request-a') is None


def test_offering_and_learned_restrictions_win(monkeypatch):
    pool(monkeypatch, [slot(), slot(200_000)])
    assert pinned_context_profile('deepseek-v4.1-flash', pin='request-a')['window'] == 200_000
    monkeypatch.setattr('lib.context_limits.resolve_learned_context_limit',
                        lambda provider, model, window: 180_000)
    assert slot_context_window(slot()) == 180_000
    monkeypatch.setattr('lib.context_limits.resolve_learned_context_limit',
                        lambda provider, model, window: 2_000_000)
    assert slot_context_window(slot()) == 1_000_000


def test_candidate_metadata_carries_declared_offering_window():
    from lib.model_routing.inference import candidate_inference_metadata
    candidate = SimpleNamespace(model={'context_window': 1_000_000},
                                offering={'context_window': 200_000},
                                connection={}, deployment={})
    assert candidate_inference_metadata(candidate)['context_window'] == 200_000


def test_selected_slot_window_reaches_clamp_and_transport_recount(monkeypatch):
    from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot
    from lib.llm.body._admission import enforce_body_input_limit
    from lib.llm_errors import PromptTooLongError
    from lib.token_counter.evidence import ADMITTED_INPUT_TOKENS_KEY as count_key
    pool(monkeypatch, [])
    monkeypatch.setattr('lib.token_counter.count_tokens', lambda *a, **k: {'tokens': 200_000})
    source = {'model': 'gateway-flash', 'messages': [{'role': 'user', 'content': 'history'}],
              'max_tokens': 128_000, count_key: 200_000}
    def adapt(target):
        return _adapt_stream_body_for_slot(target, source, True, tools=None,
            max_tokens=128_000, temperature=1, thinking_enabled=False,
            preset='medium', effort=None)
    body = adapt(slot())
    assert body['max_tokens'] > 1024  # unknown output model cap still applies
    assert enforce_body_input_limit(body, model='gateway-flash', provider_id='gateway') == 200_000
    with pytest.raises(PromptTooLongError):
        adapt(slot(100_000))
    assert '_route_context_window' not in source


def test_live_slot_replacement_does_not_leave_a_stale_context_cache(monkeypatch):
    slots = [slot()]
    pool(monkeypatch, slots)
    assert pinned_context_profile('deepseek-v4.1-flash', pin='request-a')['window'] == 1_000_000
    slots[:] = [slot(128_000)]
    assert pinned_context_profile('deepseek-v4.1-flash', pin='request-a')['window'] == 128_000
