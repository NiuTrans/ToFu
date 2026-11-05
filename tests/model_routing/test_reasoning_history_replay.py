"""Prior-reasoning replay is a declared Model fact, not a model-name list.

Some APIs need an assistant turn's ``reasoning_content`` passed back on later
requests: DeepSeek V4 thinking mode rejects a blanked trace with HTTP 400,
Tencent Hy4 asks callers to return it within a tool-call loop, and GLM
preserved thinking / Kimi K2.7 Code ``keep: all`` retain the replayed trace
server-side. ``inference_profile.reasoning.preserve_history`` declares that
requirement (``replay``, ``clear_thinking_false`` or ``keep_all``; ``none``
declares that the trace may be dropped). The predicate reads the selected
route's profile first, then the Model's maintained defaults; DeepSeek wire
spellings outside maintained identities keep DeepSeek's documented rule.
"""
from __future__ import annotations

import pytest

from lib.model_info import model_requires_reasoning_content_replay as requires_replay

pytestmark = pytest.mark.unit


def _profile(**reasoning):
    return {'reasoning': {'support': 'supported', **reasoning}}


@pytest.mark.parametrize('model', ['glm-5.3', 'glm-5.2', 'zai-org/glm-5.3', 'kimi-k2.7-code'])
def test_maintained_declarations_require_replay(model):
    assert requires_replay(model)


@pytest.mark.parametrize('model', ['deepseek-v4-flash', 'deepseek-v4-pro'])
def test_undeclared_deepseek_models_keep_the_family_rule(model):
    assert requires_replay(model)


@pytest.mark.parametrize('model', [
    'gpt-6.1-sol', 'claude-opus-5.5', 'hy4-preview', 'qwen3.8-max', 'unseen-model-9000',
])
def test_models_without_a_declaration_do_not(model):
    assert not requires_replay(model)


def test_deepseek_wire_spellings_outside_maintained_identities_keep_the_family_rule():
    assert requires_replay('deepseek-v4-flash-tencent')


def test_the_selected_route_profile_decides_first():
    assert requires_replay('unseen-model-9000', inference_profile=_profile(preserve_history='replay'))
    assert not requires_replay('deepseek-v4-flash', inference_profile=_profile(preserve_history='none'))
    # A route profile without the declaration defers to maintained knowledge.
    assert requires_replay('glm-5.3', inference_profile=_profile())
    assert not requires_replay('gpt-6.1-sol', inference_profile=_profile())


def test_every_maintained_declaration_uses_the_schema_vocabulary():
    from lib.model_routing.inference import validate_inference_profile

    profile = _profile(wire_format='reasoning_effort', can_disable=False,
                       supported_efforts=['high'], default_effort='high',
                       preserve_history='replay')
    assert validate_inference_profile(profile)['reasoning']['preserve_history'] == 'replay'


def _thinking_tool_loop(rounds):
    messages = [{'role': 'user', 'content': 'go'}]
    for index in range(rounds):
        messages.append({'role': 'assistant', 'content': f'step {index}',
                         'reasoning_content': 'R' * 500,
                         'tool_calls': [{'id': f't{index}', 'type': 'function',
                                         'function': {'name': 'g', 'arguments': '{}'}}]})
        messages.append({'role': 'tool', 'name': 'g', 'tool_call_id': f't{index}',
                         'content': 'x' * 100})
    return messages


@pytest.mark.parametrize(('model', 'kept'), [
    ('kimi-k2.7-code', True), ('glm-5.3', True), ('deepseek-v4-flash', True), ('gpt-6.1-sol', False),
])
def test_compaction_keeps_a_declared_trace_and_strips_the_rest(model, kept):
    import lib.tasks_pkg.compaction._constants as constants
    from lib.tasks_pkg.compaction.api import micro_compact

    messages = _thinking_tool_loop(constants._THINKING_HOT_TAIL + 5)
    micro_compact(messages, conv_id='', task={'model': model, '_userId': 1})

    blanked = sum(1 for message in messages
                  if message['role'] == 'assistant' and message.get('reasoning_content') == '')
    assert (blanked == 0) is kept


@pytest.mark.parametrize(('model', 'kept'), [('glm-5.3', True), ('gpt-6.1-sol', False)])
def test_a_previous_final_answer_carries_its_trace_only_when_declared(model, kept):
    from lib.tasks_pkg.conv_message_builder._transform import _build_assistant_messages

    built = _build_assistant_messages(
        {'role': 'assistant', 'content': 'done', 'thinking': 'final reasoning'}, model=model)
    assert ('reasoning_content' in built[-1]) is kept
