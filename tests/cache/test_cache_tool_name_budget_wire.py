"""Root-lane prompt-cache reuse on your-llm-gateway.example.com's Anthropic face.

Root requests to ``kunlunbedrock.claude-opus-5.5-m17`` read 0 cached tokens on
almost every round (2026-10-27/28: 3.4% read share) while sub-agents with an
11-tool surface read ~96%. Raw archives proved the root prefix byte-identical
round over round. A live A/B located the discriminator in the tool *names*: the
gateway stops reading its own writes once ``sum(len(name) + 1)`` exceeds ~492
bytes. Replaying the real root round R61 (124 messages, 117K tokens) as-is read
0 on three sends; the same body with its names held under the budget read
108,654 of 108,654 tokens on both repeats.

This spec pins the sealed provider bytes produced by ``prepare_request``: the
projected surface fits the budget, the tool breakpoint stays on the final tool
and consecutive rounds send identical tools/system bytes.
"""
from copy import deepcopy
import json

import pytest

from lib.llm import tool_wire
from lib.llm._sse_core import prepare_request
from tests.llm.test_tool_wire_name_budget import (
    CODE_FLOOR, ROOT_WIRE_NAMES, SANKUAI_ANTHROPIC, _joined_bytes, _tool)

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_SYSTEM = 'You are a careful engineering agent. ' * 120


def _canonical_round(user_turns):
    catalog = [_tool(name) for name in ROOT_WIRE_NAMES
               if name not in ('search_tools', 'execute_tools')]
    messages = [{'role': 'system', 'content': [{'type': 'text', 'text': _SYSTEM}]}]
    for index, text in enumerate(user_turns):
        if index:
            messages.append({'role': 'assistant', 'content': 'Noted.'})
        messages.append({'role': 'user', 'content': text})
    return {
        'model': 'kunlunbedrock.claude-opus-5.5-m17',
        'messages': messages,
        'max_tokens': 128000,
        'stream': True,
        'thinking': {'type': 'adaptive', 'display': 'summarized'},
        'tools': deepcopy(catalog),
        '_executable_tool_catalog': deepcopy(catalog),
        '_tool_wire_catalog': deepcopy(catalog),
        '_tool_search_mode': 'local',
        '_tool_search_catalog_size': 350,
        '_tool_searchable_count': 300,
        '_tool_discovery_policy_by_name': {
            tool['function']['name']: 'eager' for tool in catalog},
        '_required_direct_tool_names': list(CODE_FLOOR),
    }


def _sealed(body, base_url):
    plan = prepare_request(body, base_url=base_url, api_key='test-key',
                           api_protocol='anthropic')
    return plan.body.wire_body()


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def test_example_corp_root_wire_fits_the_name_budget_with_a_stable_prefix():
    first = _sealed(_canonical_round(['inspect the cache']), SANKUAI_ANTHROPIC)
    names = [tool['name'] for tool in first['tools']]

    assert _joined_bytes(names) <= tool_wire.SANKUAI_ANTHROPIC_TOOL_NAME_BUDGET_BYTES
    assert {'search_tools', 'execute_tools', 'poll', 'kill', *CODE_FLOOR} <= set(names)
    assert first['tools'][-1].get('cache_control')
    assert sum('cache_control' in tool for tool in first['tools']) == 1

    second = _sealed(_canonical_round(['inspect the cache', 'continue']),
                     SANKUAI_ANTHROPIC)
    assert _bytes(second['tools']) == _bytes(first['tools'])
    assert _bytes(second['system']) == _bytes(first['system'])


def test_sent_tool_surface_reaches_usage_and_the_projection_event():
    from lib.llm._sse_core import SSEAccumulator

    diagnostics = []
    body = _canonical_round(['inspect the cache'])
    body['_request_activity_sink'] = diagnostics.append
    plan = prepare_request(body, base_url=SANKUAI_ANTHROPIC, api_key='test-key',
                           api_protocol='anthropic')
    names = [tool['name'] for tool in plan.body.wire_body()['tools']]
    sent = {'count': len(names), 'name_bytes': _joined_bytes(names)}

    projection = next(item for item in diagnostics
                       if item.get('kind') == 'wire_projection')
    assert projection['toolNameBytes'] == sent['name_bytes']
    assert (projection['toolNameBudgetBytes']
            == tool_wire.SANKUAI_ANTHROPIC_TOOL_NAME_BUDGET_BYTES)

    accumulator = SSEAccumulator(plan.body, plan.trace_id, plan.raw_dumper,
                                 plan.wire_translator, plan.t0)
    _, _, usage = accumulator.finalize()
    assert usage['_wire_tool_surface'] == sent


def test_other_anthropic_routes_keep_their_complete_surface():
    wire = _sealed(_canonical_round(['inspect the cache']),
                   'https://api.anthropic.com/v1')
    assert {tool['name'] for tool in wire['tools']} == set(ROOT_WIRE_NAMES)
