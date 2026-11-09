"""Claude routes with a route profile keep their cache breakpoints.

``chat_cache_breakpoints`` is an OpenAI wire feature: only an explicit
declaration other than ``supported`` on that wire skips ``cache_control``
(docs/modules/llm_io.md). ``route_feature`` is closed-world and answers False
both for an undeclared feature and for a feature of another wire, so gating on
``is False`` stripped every breakpoint from every profiled route. Live
2026-11-03 21:20 onward: every ``kunlunbedrock.claude-opus-5.5-m17`` request
carried 0 markers and every round read and wrote 0 cache tokens.
"""
from __future__ import annotations

import pytest

from lib.llm.cache import add_cache_breakpoints

pytestmark = pytest.mark.unit

_MEITUAN_FACE = {'features': {'cache_affinity_mt_session_id': 'supported'}}


def _body(profile):
    body = {
        'model': 'claude-opus-5.5',
        'messages': [
            {'role': 'system', 'content': 'You are helpful. ' * 200},
            {'role': 'user', 'content': 'first question ' * 50},
            {'role': 'assistant', 'content': 'first answer ' * 50},
            {'role': 'user', 'content': 'second question ' * 50},
        ],
        'tools': [{'type': 'function', 'function': {
            'name': 'read_files', 'description': 'read',
            'parameters': {'type': 'object', 'properties': {}}}}],
    }
    if profile is not None:
        body['_route_inference_profile'] = profile
    return body


def _markers(body) -> int:
    return str(body).count('cache_control')


@pytest.mark.parametrize('protocol', ['anthropic', 'openai'])
@pytest.mark.parametrize('profile', [None, {'features': {}}, _MEITUAN_FACE],
                         ids=['no-profile', 'empty-profile', 'yourprovider-face'])
def test_claude_route_without_a_declaration_keeps_breakpoints(profile, protocol):
    body = _body(profile)
    add_cache_breakpoints(body, api_protocol=protocol)
    assert _markers(body) > 0


def test_explicit_unsupported_on_the_openai_wire_skips_breakpoints():
    body = _body({'features': {'chat_cache_breakpoints': 'unsupported'}})
    add_cache_breakpoints(body, api_protocol='openai')
    assert _markers(body) == 0


def test_an_openai_wire_declaration_does_not_govern_the_anthropic_wire():
    body = _body({'features': {'chat_cache_breakpoints': 'unsupported'}})
    add_cache_breakpoints(body, api_protocol='anthropic')
    assert _markers(body) > 0


def test_final_anthropic_wire_of_a_profiled_claude_route_carries_markers():
    from lib.llm._sse_core import prepare_request
    plan = prepare_request(
        _body(_MEITUAN_FACE), api_key='unit-test-key',
        base_url='https://example.invalid', api_protocol='anthropic')
    assert 'cache_control' in str(plan.body.get('system')) + str(
        plan.body.get('messages')) + str(plan.body.get('tools'))
