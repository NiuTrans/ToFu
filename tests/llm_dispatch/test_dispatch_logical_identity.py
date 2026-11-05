"""Dispatch keys model knowledge and billing on the Offering's logical model.

A v2 Deployment's ``wire_model_id`` is a provider spelling (gateway prefixes,
dated/region suffixes). Exact-key price rows and family knowledge describe the
logical Model, so every ``usage['_dispatch']`` producer stamps both identities
and pricing consumers prefer ``logical_model``; the wire id stays the routing,
health and request-body identity.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

WIRE = 'kunlunbedrock.claude-opus-5.5-m17'
LOGICAL = 'claude-opus-5.5'
PROVIDER = 'unit-logical-identity'


def _slot(model=WIRE, logical_model=LOGICAL, key='k0', **extra):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-test-1234', model=model,
                logical_model=logical_model, capabilities={'text'},
                provider_id=PROVIDER, **extra)


class _FakeDispatcher:
    def __init__(self, slots):
        self._slots = list(slots)
        self.slots = []

    def pick_and_reserve(self, **kwargs):
        if not self._slots:
            return None
        slot = self._slots.pop(0)
        slot.record_request()
        return slot

    def has_capable_slots(self, *args, **kwargs):
        return bool(self._slots)

    def summarize_slots(self, *args, **kwargs):
        return 'fake-slots'


def _usage_with_dispatch(**dispatch):
    return {'input_tokens': 1_000, 'output_tokens': 100,
            'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0,
            '_dispatch': dict(dispatch)}


def test_wire_spelling_has_no_price_row_but_logical_model_does():
    from lib.pricing import lookup_pricing

    assert lookup_pricing(WIRE) is None
    assert lookup_pricing(LOGICAL)['_pricingSource'] == 'model_table'


class TestDispatchProducersStampLogicalModel:
    def test_stream_settlement(self):
        from lib.llm_dispatch.api import _StreamRetryState, _settle_stream_result

        usage = {'completion_tokens': 3}
        _settle_stream_result(_slot(), usage, latency=10.0, ttft=1.0,
                              state=_StreamRetryState(), cache_conv_id='',
                              tag='[t]')
        assert usage['_dispatch']['model'] == WIRE
        assert usage['_dispatch']['logical_model'] == LOGICAL

    def test_env_slot_logical_model_defaults_to_wire(self):
        from lib.llm_dispatch.api import _StreamRetryState, _settle_stream_result

        usage = {'completion_tokens': 3}
        _settle_stream_result(_slot(model='qwen-plus', logical_model=''),
                              usage, latency=10.0, ttft=1.0,
                              state=_StreamRetryState(), cache_conv_id='',
                              tag='[t]')
        assert usage['_dispatch']['logical_model'] == 'qwen-plus'

    def test_nonstream_dispatch_chat(self, monkeypatch):
        import lib.llm as llm
        from lib.llm_dispatch import api

        seen = {}

        def _fake_chat(**kwargs):
            seen.update(kwargs)
            return 'ok', {'prompt_tokens': 5, 'completion_tokens': 2}

        monkeypatch.setattr(api, 'get_dispatcher',
                            lambda: _FakeDispatcher([_slot()]))
        monkeypatch.setattr(llm, 'chat', _fake_chat)
        _, usage = api.dispatch_chat(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')
        assert seen['model'] == WIRE
        assert seen['knowledge_model'] == LOGICAL
        assert usage['_dispatch']['model'] == WIRE
        assert usage['_dispatch']['logical_model'] == LOGICAL

    def test_nonstream_request_settlement_prices_logical_model(
            self, monkeypatch):
        """Real dispatch_chat → chat → sealed request → wallet attempt.

        The wire spelling has no price row, so pricing it would leave a
        strict-billing request without an invoice (default_estimate).
        """
        import importlib
        import json
        from types import SimpleNamespace
        from lib.billing.cost import compute_request_cost
        from lib.billing.request_settlement import RequestSettlement
        from lib.llm_dispatch import api

        posted = []
        reply = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'},
                              'finish_reason': 'stop'}],
                 'usage': {'prompt_tokens': 1_000, 'completion_tokens': 100}}

        def post(url, **kwargs):
            posted.append(json.loads(kwargs['data']))
            return SimpleNamespace(status_code=200, headers={},
                                   json=lambda: reply)

        monkeypatch.setattr(importlib.import_module('lib.llm.chat'),
                            'http_post', post)
        monkeypatch.setattr('lib.desktop.egress.route_request',
                            lambda *a, **k: 'direct')
        monkeypatch.setattr('lib.billing.reservation_admission.acknowledge',
                            lambda **identity: None)
        monkeypatch.setattr(api, 'get_dispatcher', lambda: _FakeDispatcher(
            [_slot(base_url='https://example.invalid/v1')]))
        settlement = RequestSettlement(request_id='req-logical', owner=1,
                                       wallet=1, held=10**9)
        settlement.protect()
        attempts = []

        def admit(**counts):
            attempts.append(settlement(**counts))
            return attempts[-1]

        content, _usage = api.dispatch_chat(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
            model_budget_admit=admit)
        assert content == 'ok'
        assert [payload['model'] for payload in posted] == [WIRE]
        assert [(a.model, a.provider_id) for a in attempts] == [(LOGICAL, PROVIDER)]
        attempt = attempts[0]
        assert attempt.verified_complete
        cost = compute_request_cost(attempt.model, provider_id=attempt.provider_id,
                                    raw_usage=attempt.usage)
        assert cost.snapshot.get('source') != 'default_estimate'
        assert cost.micro > 0

    def test_public_run_evidence_keeps_logical_model(self):
        from lib.agent_core.run_contract import project_agent_event_evidence

        projected = project_agent_event_evidence({'usage': _usage_with_dispatch(
            model=WIRE, logical_model=LOGICAL, key_hint='secret-ish')})
        dispatch = projected['usage']['_dispatch']
        assert dispatch['logical_model'] == LOGICAL
        assert 'key_hint' not in dispatch


class TestPricingConsumersPreferLogicalModel:
    def test_child_round_snapshot_prices_dispatch_logical_model(self):
        from lib.tasks_pkg.round_costs import stamp_round_costs

        rows = [{'usage': _usage_with_dispatch(
            model=WIRE, logical_model=LOGICAL, provider_id=PROVIDER)}]
        stamp_round_costs({}, rows, model='')
        cost = rows[0]['cost']
        assert cost['pricingSource'] == 'model_table'
        assert cost['pricingSnapshot']['model'] == LOGICAL

    def test_legacy_dispatch_without_logical_model_keeps_wire_fallback(self):
        from lib.tasks_pkg.round_costs import stamp_round_costs

        rows = [{'usage': _usage_with_dispatch(model=WIRE, provider_id=PROVIDER)}]
        stamp_round_costs({}, rows, model='')
        assert rows[0]['cost']['pricingSnapshot']['model'] == WIRE

    def test_compaction_call_rows_carry_logical_model(self):
        from lib.tasks_pkg.compaction._compaction_usage import (
            pop_compaction_usage, record_compaction_usage,
        )

        conv_id = 'conv-logical-identity'
        pop_compaction_usage(conv_id)
        record_compaction_usage(conv_id, _usage_with_dispatch(
            model=WIRE, logical_model=LOGICAL, provider_id=PROVIDER), kind='L2')
        call = pop_compaction_usage(conv_id)['calls'][0]
        assert call['model'] == LOGICAL
        assert call['provider_id'] == PROVIDER

    def test_summary_receipt_matches_logical_call_price(self, monkeypatch):
        import lib.pricing as pricing
        from lib.tasks_pkg.compaction._summary_cost import (
            price_summary_call, summary_cost_details,
        )

        monkeypatch.setitem(pricing.PROVIDER_PRICING, PROVIDER, {})
        pricing.set_provider_pricing(PROVIDER, LOGICAL, {
            'input': 4.0, 'output': 20.0, 'cacheWriteMul': 1.25,
            'cacheReadMul': 0.05})
        usage = {'prompt_tokens': 10_000, 'completion_tokens': 1_000,
                 '_dispatch': {'model': WIRE, 'logical_model': LOGICAL,
                               'provider_id': PROVIDER}}
        cost = price_summary_call(usage, model=LOGICAL, provider_id=PROVIDER)
        assert cost['known'] is True
        result = summary_cost_details({'_summaryCalls': [
            {'model': LOGICAL, 'provider_id': PROVIDER, 'usage': usage,
             'cost': cost}]})
        assert result['known'] is True
        assert result['costUsd'] == pytest.approx(0.06)
        assert result['calls'][0]['model'] == LOGICAL

    def test_paper_usage_meter_prices_logical_model(self):
        from lib.paper.agent_usage import PaperAgentUsageMeter

        meter = PaperAgentUsageMeter('report')
        meter.record(_usage_with_dispatch(
            model=WIRE, logical_model=LOGICAL, provider_id=PROVIDER))
        assert meter.priced_calls == 1
        assert meter.cost_estimated is False
        assert meter.models == {LOGICAL}


GPT_WIRE = 'kunlunbedrock.gpt-6.1-sol-m17'
GPT_LOGICAL = 'gpt-6.1-sol'
# An opaque gateway spelling carries no family evidence at all.
OPAQUE_CLAUDE_WIRE = 'yuju-opus-55-evaDaily'


class TestDispatchKnowledgeUsesLogicalModel:
    def test_route_output_clamp_uses_logical_family(self):
        from lib.model_info import _clamp_max_tokens, _clamp_route_max_tokens

        assert _clamp_max_tokens(GPT_WIRE, 10**6) == 32_768
        assert _clamp_route_max_tokens(
            GPT_LOGICAL, 10**6, wire_model=GPT_WIRE) == 128_000

    def test_limit_learned_for_wire_spelling_still_applies(self, monkeypatch):
        import lib.model_info._limits as limits

        monkeypatch.setitem(limits._LEARNED_MODEL_LIMITS, GPT_WIRE, 4_096)
        assert limits._clamp_route_max_tokens(
            GPT_LOGICAL, 10**6, wire_model=GPT_WIRE) == 4_096

    def test_limit_learned_for_the_logical_spelling_stays_on_its_deployment(
            self, monkeypatch):
        """Per-name limits are request-spelling evidence from one Deployment.

        A Deployment whose wire id equals the logical id learns under that id;
        another gateway's spelling of the same Model must not inherit it.
        """
        import lib.model_info._limits as limits
        from lib.llm.body import build_body
        from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot

        monkeypatch.setitem(limits._LEARNED_MODEL_LIMITS, GPT_LOGICAL, 4_096)
        messages = [{'role': 'user', 'content': 'hi'}]

        def stream_cap(wire):
            slot = _slot(model=wire, logical_model=GPT_LOGICAL,
                         context_window=1_000_000)
            return _adapt_stream_body_for_slot(
                slot, messages, False, tools=None, max_tokens=10**6,
                temperature=1.0, thinking_enabled=None, preset='medium',
                effort=None)['max_tokens']

        def nonstream_cap(wire):
            return build_body(wire, messages, max_tokens=10**6, stream=False,
                              knowledge_model=GPT_LOGICAL,
                              context_window=1_000_000)['max_tokens']

        assert stream_cap(GPT_LOGICAL) == nonstream_cap(GPT_LOGICAL) == 4_096
        assert stream_cap(GPT_WIRE) == nonstream_cap(GPT_WIRE) == 128_000

    def test_nonstream_body_clamps_on_logical_family_and_keeps_wire_model(
            self, monkeypatch):
        import lib.model_info._limits as limits
        from lib.llm.body import build_body

        messages = [{'role': 'user', 'content': 'hi'}]
        body = build_body(GPT_WIRE, messages, max_tokens=10**6, stream=False,
                          knowledge_model=GPT_LOGICAL, context_window=1_000_000)
        assert body['model'] == GPT_WIRE
        assert body['max_tokens'] == 128_000

        monkeypatch.setitem(limits._LEARNED_MODEL_LIMITS, GPT_WIRE, 4_096)
        body = build_body(GPT_WIRE, messages, max_tokens=10**6, stream=False,
                          knowledge_model=GPT_LOGICAL, context_window=1_000_000)
        assert body['max_tokens'] == 4_096

    def test_fresh_stream_body_keeps_wire_model(self):
        from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot

        slot = _slot(model=GPT_WIRE, logical_model=GPT_LOGICAL,
                     context_window=1_000_000)
        body = _adapt_stream_body_for_slot(
            slot, [{'role': 'user', 'content': 'hi'}], False, tools=None,
            max_tokens=10**6, temperature=1.0, thinking_enabled=None,
            preset='medium', effort=None)
        assert body['model'] == GPT_WIRE
        assert body['max_tokens'] == 128_000
        assert body['_admission_logical_model'] == GPT_LOGICAL

    def test_prebuilt_body_applies_logical_family_tweaks(self):
        from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot

        caller_messages = [{'role': 'user', 'content': 'question'},
                           {'role': 'assistant', 'content': 'partial answer'}]
        slot = _slot(model=OPAQUE_CLAUDE_WIRE, logical_model=LOGICAL,
                     context_window=1_000_000)
        body = _adapt_stream_body_for_slot(
            slot, {'model': LOGICAL, 'messages': caller_messages,
                   'max_tokens': 10**6},
            True, tools=None, max_tokens=None, temperature=None,
            thinking_enabled=None, preset=None, effort=None)
        assert body['model'] == OPAQUE_CLAUDE_WIRE
        assert body['max_tokens'] == 128_000
        # Claude prefill guard ran on a clone; caller history is untouched.
        assert body['messages'][-1]['role'] == 'user'
        assert caller_messages[-1] == {
            'role': 'assistant', 'content': 'partial answer'}

    def test_pinned_context_profile_output_cap_uses_logical_family(
            self, monkeypatch):
        from lib.llm_dispatch.context_policy import pinned_context_profile

        slot = _slot(model=GPT_WIRE, logical_model=GPT_LOGICAL,
                     context_window=1_000_000)

        from lib.llm_dispatch.slot_pool import SlotPool

        class _Dispatcher:
            slots = SlotPool([slot])

        monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher',
                            lambda: _Dispatcher())
        profile = pinned_context_profile(GPT_LOGICAL, pin=PROVIDER)
        assert profile['window'] == 1_000_000
        assert profile['output_cap_tokens'] == 128_000
