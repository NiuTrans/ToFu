"""Effort contracts at sealed streaming and actual non-stream HTTP boundaries."""
import copy
import importlib
import json

import pytest

from lib.llm.body import build_body
from lib.llm._sse_core import prepare_request
from lib.llm.anthropic_outbound import openai_body_to_anthropic
from lib.llm_dispatch.api import _readjust_thinking_params

pytestmark = pytest.mark.unit
RUNGS = ('off', 'low', 'medium', 'high', 'xhigh', 'max')
ROUTES = (('openai', ''), ('anthropic', ''), ('anthropic', 'claude'))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr('lib.token_counter.count_tokens', lambda *a, **k: {'tokens': 20})
    monkeypatch.setattr(
        'lib.tasks_pkg.compaction._tokens.resolve_model_context_limit',
        lambda *a, **k: 200000)
    monkeypatch.setattr('lib.oauth.outbound.resolve_oauth_request',
                        lambda oauth, body, headers, **kw: ('synthetic', headers, body))
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **k: 'direct')


def assert_wire(wire, protocol, rung):
    expected = None if rung == 'off' else rung
    if protocol == 'anthropic':
        assert 'effort' not in wire
        assert wire.get('output_config', {}).get('effort') == expected
    else:
        assert wire.get('effort') == expected
        assert 'output_config' not in wire
    assert wire['thinking'] == ({'type': 'disabled'} if rung == 'off' else
                               {'type': 'adaptive', 'display': 'summarized'})


@pytest.mark.parametrize('protocol,oauth', ROUTES)
@pytest.mark.parametrize('rung', RUNGS)
@pytest.mark.parametrize('swap', [False, True])
def test_sealed_stream_wire(protocol, oauth, rung, swap):
    body = build_body('claude-opus-5', [{'role': 'user', 'content': 'hi'}],
                      thinking_enabled=rung != 'off', thinking_depth=rung)
    if swap:
        _readjust_thinking_params(body, 'aws.claude-opus-4.7', '')
        body['model'] = 'aws.claude-opus-4.7'
    plan = prepare_request(body, base_url='https://example.invalid/v1',
                           api_protocol=protocol, oauth=oauth, owner_user_id=1)
    assert_wire(json.loads(plan.body.payload), protocol, rung)
    assert ('/messages' in plan.url) == (protocol == 'anthropic')
    assert body.get('effort') == (None if rung == 'off' else rung)
    assert 'output_config' not in body


@pytest.mark.parametrize('rung', RUNGS)
def test_fresh_dispatch_explicit_effort(rung):
    from types import SimpleNamespace
    from lib.llm_dispatch.api import _adapt_stream_body_for_slot
    slot = SimpleNamespace(model='claude-opus-5', protocol='anthropic',
                           thinking_format='', provider_id='')
    body = _adapt_stream_body_for_slot(
        slot, [{'role': 'user', 'content': 'hi'}], False,
        tools=None, max_tokens=4096, temperature=1.0,
        thinking_enabled=rung != 'off', preset='medium', effort=rung)
    plan = prepare_request(body, base_url='https://example.invalid/v1',
                           api_protocol='anthropic')
    assert_wire(json.loads(plan.body.payload), 'anthropic', rung)


class TransportCaptured(BaseException):
    pass


@pytest.mark.parametrize('protocol,oauth', ROUTES)
@pytest.mark.parametrize('rung', RUNGS)
def test_nonstream_final_http(monkeypatch, protocol, oauth, rung):
    chat_module = importlib.import_module('lib.llm.chat')
    captured = []
    def post(url, **kwargs):
        captured.append(json.loads(kwargs['data']))
        raise TransportCaptured()
    monkeypatch.setattr(chat_module, 'http_post', post)
    with pytest.raises(TransportCaptured):
        chat_module.chat([{'role': 'user', 'content': 'hi'}], 'claude-opus-5',
                         thinking_enabled=rung != 'off', effort=rung,
                         api_protocol=protocol, oauth=oauth, owner_user_id=1,
                         base_url='https://example.invalid/v1', max_retries=0)
    assert len(captured) == 1
    assert_wire(captured[0], protocol, rung)


@pytest.mark.parametrize('canonical', [None, 'medium'])
def test_native_output_config_preserved_and_canonical_wins(canonical):
    body = {'model': 'claude-opus-5', 'messages': [{'role': 'user', 'content': 'hi'}],
            'output_config': {'effort': 'high', 'format': {
                'type': 'json_schema', 'schema': {'type': 'object'}}}}
    if canonical:
        body['effort'] = canonical
    before = copy.deepcopy(body)
    out = openai_body_to_anthropic(body)
    assert out['output_config']['effort'] == (canonical or 'high')
    assert out['output_config']['format'] == before['output_config']['format']
    assert 'effort' not in out
    assert body == before
    assert out['output_config'] is not body['output_config']


@pytest.mark.parametrize('rung', RUNGS)
def test_direct_builder_explicit_effort(rung):
    body = build_body('claude-opus-5', [{'role': 'user', 'content': 'hi'}],
                      thinking_enabled=True, effort=rung,
                      thinking_depth='off' if rung == 'off' else None)
    assert_wire(openai_body_to_anthropic(body), 'anthropic', rung)
    # The old preset named low is not an explicit effort selection.
    legacy = build_body('claude-opus-5', [{'role': 'user', 'content': 'hi'}],
                        thinking_enabled=True, preset='low')
    assert legacy['effort'] == 'medium'


@pytest.mark.parametrize('rung', RUNGS)
def test_same_model_reactive_rebuild_preserves_effort(monkeypatch, rung):
    from lib.tasks_pkg.llm_fallback import _call as fallback
    from lib.llm_errors import PromptTooLongError
    monkeypatch.setattr('lib.tasks_pkg.compaction.api.reactive_compact', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.context_composer.checklist.restore_checklist_context',
                        lambda *a, **k: None)
    messages = [{'role': 'user', 'content': 'hi'}]
    body = build_body('claude-opus-5', messages, thinking_enabled=rung != 'off',
                      thinking_depth=rung)
    retry = fallback._prepare_reactive_retry_body(
        {'id': 'synthetic', 'config': {'thinkingDepth': rung}}, body,
        'claude-opus-5', messages, [], 'medium', rung != 'off',
        PromptTooLongError('synthetic'), 'synthetic')
    assert retry is not None
    plan = prepare_request(retry, base_url='https://example.invalid/v1',
                           api_protocol='anthropic')
    assert_wire(json.loads(plan.body.payload), 'anthropic', rung)


@pytest.mark.parametrize('rung', ['low', 'medium', 'high'])
def test_native_explicit_disabled_effort_is_independent_of_ui_off(rung):
    # Official Messages supports effort without thinking at <= high.
    # The converter must not silently remove a deliberate native setting.
    body = {'model': 'claude-opus-5', 'thinking': {'type': 'disabled'},
            'output_config': {'effort': rung, 'format': {'type': 'json_schema',
                'schema': {'type': 'object'}}},
            'messages': [{'role': 'user', 'content': 'hi'}]}
    out = openai_body_to_anthropic(body)
    assert out['thinking'] == {'type': 'disabled'}
    assert out['output_config'] == body['output_config']
    assert 'effort' not in out


def test_native_effort_diagnostic_omits_output_schema():
    from lib.llm.diagnostics import RawSSEDumper
    body = {'model': 'claude-opus-5', 'output_config': {
        'effort': 'max', 'format': {'schema': {'private-field': 'CANARY-SCHEMA'}}},
        'messages': [{'role': 'user', 'content': 'CANARY-PROMPT'}]}
    snapshot = RawSSEDumper('claude-opus-5', 'synthetic', body)._body_snapshot()
    assert snapshot['output_config'] == {'effort': 'max'}
    assert 'CANARY' not in json.dumps(snapshot)
    assert 'private-field' not in json.dumps(snapshot)


@pytest.mark.parametrize('protocol', ['openai', 'responses'])
@pytest.mark.parametrize('rung', RUNGS)
def test_claude_to_other_provider_no_native_leakage(protocol, rung):
    body = build_body('claude-opus-5', [{'role': 'user', 'content': 'hi'}],
                      thinking_enabled=rung != 'off', thinking_depth=rung)
    _readjust_thinking_params(body, 'gpt-5.4', '')
    body['model'] = 'gpt-5.4'
    plan = prepare_request(body, base_url='https://example.invalid/v1',
                           api_protocol=protocol)
    wire = json.loads(plan.body.payload)
    assert 'effort' not in wire
    assert 'output_config' not in wire
    assert 'thinking' not in wire
