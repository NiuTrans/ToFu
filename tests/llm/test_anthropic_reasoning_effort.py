"""Canonical effort survives Messages projection without provider-specific rewrites."""
from __future__ import annotations

import copy
import importlib
import json

import pytest

from lib.llm._sse_core import prepare_request
from lib.llm.anthropic_outbound import openai_body_to_anthropic
from lib.llm.body import build_body

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr('lib.token_counter.count_tokens', lambda *a, **k: {'tokens': 20})
    monkeypatch.setattr(
        'lib.tasks_pkg.compaction._tokens.resolve_model_context_limit',
        lambda *a, **k: 200000)
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **k: 'direct')


@pytest.mark.parametrize('model', ['kimi-k3', 'compatible-model'])
@pytest.mark.parametrize('effort', ['low', 'high', 'max'])
def test_messages_projects_canonical_effort_without_model_name_gate(model, effort):
    body = {'model': model, 'messages': [{'role': 'user', 'content': 'hi'}],
            'reasoning_effort': effort, 'output_config': {'format': {
                'type': 'json_schema', 'schema': {'type': 'object'}}}}
    before = copy.deepcopy(body)
    wire = openai_body_to_anthropic(body)
    assert wire['output_config']['effort'] == effort
    assert wire['output_config']['format'] == before['output_config']['format']
    assert 'reasoning_effort' not in wire and 'effort' not in wire
    assert body == before
    assert wire['output_config'] is not body['output_config']


@pytest.mark.parametrize('fields,expected', [
    ({'reasoning_effort': 'low'}, 'low'),
    ({'reasoning_effort': 'max', 'effort': 'high'}, 'high'),
    ({'effort': 'medium'}, 'medium'),
    ({}, 'max'),
])
def test_canonical_effort_conflict_priority_preserves_native_settings(fields, expected):
    body = {'messages': [{'role': 'user', 'content': 'hi'}],
            'output_config': {'effort': 'max'}, **fields}
    assert openai_body_to_anthropic(body)['output_config']['effort'] == expected


@pytest.mark.parametrize('effort', ['medium', 'xhigh'])
def test_translator_does_not_remap_model_adjusted_rungs(effort):
    wire = openai_body_to_anthropic({'reasoning_effort': effort})
    assert wire['output_config']['effort'] == effort


def test_absent_effort_is_not_synthesized():
    assert 'output_config' not in openai_body_to_anthropic({})


def _assert_wire(wire, protocol, effort):
    if protocol == 'anthropic':
        assert wire['output_config']['effort'] == effort
        assert 'reasoning_effort' not in wire and 'effort' not in wire
    elif protocol == 'responses':
        assert wire['reasoning']['effort'] == effort
        assert 'output_config' not in wire
    else:
        assert wire['reasoning_effort'] == effort
        assert 'output_config' not in wire


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
@pytest.mark.parametrize('effort', ['low', 'high', 'max'])
def test_kimi_effort_reaches_sealed_stream_request(protocol, effort):
    body = build_body('kimi-k3', [{'role': 'user', 'content': 'hi'}],
                      thinking_enabled=True, effort=effort)
    assert body['reasoning_effort'] == effort
    assert 'thinking' not in body
    plan = prepare_request(body, base_url='https://example.invalid/v1',
                           api_protocol=protocol)
    _assert_wire(json.loads(plan.body.payload), protocol, effort)

    assert plan.raw_dumper.reasoning_resolution['actualMode'] == 'exact'
    assert plan.raw_dumper.reasoning_resolution['actualValue'] == effort
    assert '_reasoning_resolution' not in json.loads(plan.body.payload)
    assert body['reasoning_effort'] == effort
    assert 'output_config' not in body


class TransportCaptured(BaseException):
    """Stop before network I/O after observing the actual serialized body."""


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
@pytest.mark.parametrize('effort', ['low', 'high', 'max'])
def test_kimi_effort_reaches_nonstream_http(monkeypatch, protocol, effort):
    chat_module = importlib.import_module('lib.llm.chat')
    captured = []

    def post(url, **kwargs):
        captured.append(json.loads(kwargs['data']))
        raise TransportCaptured()

    monkeypatch.setattr(chat_module, 'http_post', post)
    with pytest.raises(TransportCaptured):
        chat_module.chat([{'role': 'user', 'content': 'hi'}], 'kimi-k3',
                         thinking_enabled=True, effort=effort,
                         api_protocol=protocol, api_key='synthetic',
                         base_url='https://example.invalid/v1', max_retries=0)
    assert len(captured) == 1
    _assert_wire(captured[0], protocol, effort)


@pytest.mark.parametrize('actual', ['high', 'max'])
def test_medium_mapping_is_selected_slot_wire_evidence(actual):
    from lib.llm._sse_core import SSEAccumulator

    profile = {'reasoning': {'support': 'supported',
        'wire_format': 'reasoning_effort', 'can_disable': False,
        'supported_efforts': ['low', 'high', 'max'], 'default_effort': 'max',
        'effort_map': {'medium': actual}}}
    body = build_body('kimi-k3', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth='medium', inference_profile=profile)
    plan = prepare_request(body, base_url='https://example.invalid/v1',
                           api_protocol='anthropic')
    assert json.loads(plan.body.payload)['output_config']['effort'] == actual
    resolution = plan.raw_dumper.reasoning_resolution
    assert resolution['requestedValue'] == 'medium'
    assert resolution['actualValue'] == actual
    acc = SSEAccumulator(plan.body, plan.trace_id, plan.raw_dumper,
                         plan.wire_translator, plan.t0)
    _, _, usage = acc.finalize()
    assert usage['_reasoning_resolution'] == resolution


def test_fallback_recompiles_original_medium_receipt():
    from lib.llm_dispatch._api_stream_state import _readjust_thinking_params

    def profile(actual):
        return {'reasoning': {'support': 'supported',
            'wire_format': 'reasoning_effort', 'can_disable': False,
            'supported_efforts': ['low', 'high', 'max'], 'default_effort': 'max',
            'effort_map': {'medium': actual}}}

    body = build_body('kimi-k3', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth='medium', inference_profile=profile('max'))
    assert body['_reasoning_resolution']['actualValue'] == 'max'
    _readjust_thinking_params(body, 'kimi-k3', '', profile('high'))
    assert body['_reasoning_resolution']['requestedValue'] == 'medium'
    assert body['_reasoning_resolution']['actualValue'] == 'high'
    assert body['reasoning_effort'] == 'high'
