"""Executable contract for bounded outbound conversion outcomes."""
from copy import deepcopy

import pytest

from lib.llm.conversion import ProtocolConversionError
from lib.llm.anthropic_outbound import (
    openai_body_to_anthropic, openai_body_to_anthropic_checked,
)
from lib.llm.responses_outbound import (
    openai_body_to_responses, openai_body_to_responses_checked,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('model', ['gpt-5.6', 'gpt-6-astra', 'opaque-wire-name'])
@pytest.mark.parametrize('key', ['effort', 'reasoning_effort', 'reasoning'])
def test_direct_responses_native_effort_must_match_selected_destination(model, key):
    declared = {'reasoning': {'support': 'supported', 'wire_format': 'reasoning_effort',
                             'can_disable': False, 'supported_efforts': ['low', 'high']}}
    body = {'model': model, '_route_inference_profile': declared,
            key: {'effort': 'max'} if key == 'reasoning' else 'max'}
    original = deepcopy(body)
    result = openai_body_to_responses_checked(body)
    assert result.validation_issues == ('unsupported_effort',)
    assert body == original
    declared['reasoning']['supported_efforts'].append('max')
    assert openai_body_to_responses_checked(body).require_payload()['reasoning']['effort'] == 'max'


@pytest.mark.parametrize('body,code', [
    ({'effort': 'low', 'reasoning_effort': 'high'}, 'reasoning_effort_conflict'),
    ({'reasoning_effort': 'low', 'reasoning': {'effort': 'high'}}, 'reasoning_effort_conflict'),
    ({'reasoning': {'bogus_extra': 'SECRET'}}, 'unsupported_reasoning_field'),
    ({'reasoning': {'summary': 'SECRET'}}, 'invalid_reasoning_summary'),
    ({'reasoning': {'mode': 'pro'}, '_route_inference_profile': {}}, 'reasoning_mode_unrepresentable'),
    ({'reasoning': {'context': 'all_turns'}, '_route_inference_profile': {}}, 'reasoning_context_unrepresentable'),
])
def test_native_reasoning_unknown_or_conflicting_fields_never_reach_wire(body, code):
    original = deepcopy(body)
    result = openai_body_to_responses_checked(body)
    assert result.validation_issues == (code,)
    assert 'SECRET' not in repr(result)
    assert body == original


@pytest.mark.parametrize('model,profile', [('gpt-5.6', 'default'), ('gpt-5.5-codex', 'codex')])
def test_legacy_stateful_replay_and_cache_survive_without_public_feature_profile(model, profile):
    item = {'type': 'reasoning', 'id': 'rs-opaque', 'encrypted_content': 'opaque'}
    body = {'model': model, '_conv_id': 'conversation-synthetic',
            'messages': [{'role': 'assistant', 'content': 'answer', '_responses_items': [item]}]}
    payload = openai_body_to_responses_checked(body, profile=profile).require_payload()
    assert item in payload['input']
    assert payload['prompt_cache_key']
    assert 'context_management' not in payload
    if profile == 'codex':
        assert 'context' not in payload['reasoning']
    body['_route_inference_profile'] = {}
    selected = openai_body_to_responses_checked(body, profile=profile)
    if profile == 'codex':
        assert item in selected.require_payload()['input']
    else:
        assert selected.validation_issues == ('mandatory_continuity_unrepresentable',)


def test_codex_route_profile_replays_encrypted_reasoning_across_tool_rounds():
    """Codex's wire binding survives partial catalog inference profiles."""
    route_profile = {'source': 'provider_catalog', 'reasoning': {
        'support': 'supported', 'supported_efforts': ['low', 'max']}}
    body = {'model': 'gpt-6-astra', '_conv_id': 'conversation-synthetic',
            '_route_inference_profile': route_profile,
            'messages': [{'role': 'user', 'content': 'synthetic'}]}
    for round_num in range(3):
        item = {'type': 'reasoning', 'id': f'rs-{round_num}',
                'encrypted_content': 'opaque'}
        body['messages'].extend([
            {'role': 'assistant', '_responses_items': [item],
             'tool_calls': [{'id': f'call-{round_num}', 'type': 'function',
                             'function': {'name': 'tool', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': f'call-{round_num}', 'content': 'ok'},
        ])
        original = deepcopy(body)
        result = openai_body_to_responses_checked(body, profile='codex')
        assert not result.validation_issues
        assert result.payload['prompt_cache_key']
        for prior in range(round_num + 1):
            assert any(part.get('type') == 'reasoning'
                       and part.get('id') == f'rs-{prior}'
                       for part in result.payload['input'])
        assert body == original


@pytest.mark.parametrize('declaration', ['unsupported', 'unknown', None, False, '', {}])
def test_codex_explicit_replay_narrowing_fails_closed(declaration):
    body = {'model': 'gpt-6-astra', '_route_inference_profile': {
        'features': {'responses_reasoning_replay': declaration}},
        'messages': [{'role': 'assistant', '_responses_items': [
            {'type': 'reasoning', 'encrypted_content': 'opaque'}]}]}
    result = openai_body_to_responses_checked(body, profile='codex')
    assert result.validation_issues == ('mandatory_continuity_unrepresentable',)


@pytest.mark.parametrize('declaration', ['unsupported', 'unknown', None, False, '', {}])
def test_codex_explicit_cache_narrowing_fails_closed(declaration):
    body = {'model': 'gpt-6-astra', '_conv_id': 'conversation-synthetic',
            '_route_inference_profile': {
                'features': {'responses_prompt_cache': declaration}},
            'messages': [{'role': 'user', 'content': 'synthetic'}]}
    original = deepcopy(body)
    payload = openai_body_to_responses_checked(body, profile='codex').require_payload()
    assert 'prompt_cache_key' not in payload
    assert body == original


def test_non_codex_responses_route_does_not_inherit_codex_replay():
    body = {'model': 'gpt-6-astra', '_route_inference_profile': {
        'reasoning': {'support': 'supported'}},
        'messages': [{'role': 'assistant', '_responses_items': [
            {'type': 'reasoning', 'encrypted_content': 'opaque'}]}]}
    assert openai_body_to_responses_checked(body).validation_issues == (
        'mandatory_continuity_unrepresentable',)


def test_responses_sampling_omissions_are_reported_without_values_and_sealed():
    from lib.llm.request import seal_request
    result = openai_body_to_responses_checked({'top_k': 5, 'presence_penalty': .2,
        'frequency_penalty': .3, 'temperature': .4}, profile='codex')
    assert result.permitted_conversion_losses == {
        'top_k_omitted': 1, 'presence_penalty_omitted': 1, 'frequency_penalty_omitted': 1,
        'temperature_omitted': 1}
    sealed = seal_request(result.require_payload(), model='test', protocol='responses',
                          input_tokens=1, semantic_id='test')
    assert '_conversion_report' not in sealed


@pytest.mark.parametrize('checked,wrapper', [
    (openai_body_to_anthropic_checked, openai_body_to_anthropic),
    (openai_body_to_responses_checked, openai_body_to_responses),
])
@pytest.mark.parametrize('message', [
    {'role': 'unknown', 'content': 'SECRET'},
    {'role': 'user', 'content': [{'type': 'unknown', 'text': 'SECRET'}]},
    {'role': 'assistant', 'tool_calls': [{'type': 'function', 'function': {
        'name': 'run', 'arguments': 'SECRET'}}]},
    {'role': 'assistant', 'tool_calls': [{'type': 'function', 'function': {
        'name': 'run', 'arguments': '[]'}}]},
    {'role': 'assistant', 'tool_calls': [{'type': 'function', 'function': {'name': 'run'}}]},
])
def test_invalid_inputs_are_checked_and_wrappers_fail_closed(checked, wrapper, message):
    body = {'messages': [message]}
    original = deepcopy(body)
    result = checked(body)
    assert result.payload is None
    assert len(result.validation_issues) == 1
    assert 'SECRET' not in repr(result)
    with pytest.raises(ProtocolConversionError) as error:
        wrapper(body)
    assert 'SECRET' not in str(error.value)
    assert body == original


@pytest.mark.parametrize('checked', [openai_body_to_anthropic_checked, openai_body_to_responses_checked])
def test_optional_reasoning_and_opaque_orphans_are_reported(checked):
    result = checked({'messages': [{'role': 'assistant', 'content': 'answer',
        'reasoning_content': 'SECRET', '_responses_items': [
            {'type': 'program', 'call_id': '', 'code': 'SECRET'},
            {'type': 'future_metadata', 'value': 'SECRET'}]}]})
    assert not result.validation_issues
    assert result.permitted_conversion_losses == {
        'optional_reasoning_omitted': 1, 'orphan_opaque_item_omitted': 1,
        'native_metadata_omitted': 1}
    assert 'SECRET' not in repr(result.permitted_conversion_losses)


@pytest.mark.parametrize('kind', ['program', 'shell_call', 'compaction', 'future_call'])
def test_mandatory_continuity_cannot_be_dropped(kind):
    result = openai_body_to_responses_checked({'messages': [{'role': 'assistant',
        '_responses_items': [{'type': kind, 'id': 'owned'}]}]})
    assert result.validation_issues == ('mandatory_continuity_unrepresentable',)


def test_selected_route_not_model_name_enables_responses_features():
    body = {'model': 'future-model', '_inference_profile': {
        'features': {'responses_reasoning_replay': 'supported'}},
        'messages': [{'role': 'assistant', '_responses_items': [
            {'type': 'reasoning', 'encrypted_content': 'opaque'}]}]}
    result = openai_body_to_responses_checked(body)
    assert not result.validation_issues
    assert result.payload['input'][0]['encrypted_content'] == 'opaque'
    assert result.payload['reasoning']['context'] == 'all_turns'


def test_short_name_and_two_long_names_never_collide():
    names = ['x' * 64, 'x' * 64 + 'a', 'x' * 64 + 'b']
    body = {'tools': [{'type': 'function', 'function': {'name': name}} for name in names]}
    result = openai_body_to_responses_checked(body)
    assert not result.validation_issues
    wire_names = [tool['name'] for tool in result.payload['tools']]
    assert len(set(wire_names)) == 3
    assert all(len(name) <= 64 for name in wire_names)
    assert [result.tool_name_reverse.get(name, name) for name in wire_names] == names


def test_payload_is_independent_of_input():
    body = {'messages': [{'role': 'user', 'content': 'hello'}], 'output_config': {
        'format': {'type': 'json_schema', 'schema': {'type': 'object'}}}}
    original = deepcopy(body)
    result = openai_body_to_anthropic_checked(body)
    result.payload['output_config']['format']['schema']['type'] = 'string'
    assert body == original


def test_sealing_strips_private_conversion_report():
    from lib.llm.request import seal_request
    result = openai_body_to_anthropic_checked({'messages': [
        {'role': 'assistant', 'content': 'answer', 'reasoning_content': 'private'}]})
    sealed = seal_request(result.payload, model='test', protocol='anthropic',
                          input_tokens=1, semantic_id='test')
    assert '_conversion_report' not in sealed


def test_selected_profile_keeps_compiled_effort_verbatim():
    result = openai_body_to_responses_checked({'model': 'gpt-5.6',
        'reasoning_effort': 'ultra', '_inference_profile': {
            'responses': {'feature_profile': 'compatible'},
            'reasoning': {'support': 'supported', 'wire_format': 'reasoning_effort',
                          'supported_efforts': ['ultra']}}})
    assert result.payload['reasoning']['effort'] == 'ultra'
    assert 'include' not in result.payload


@pytest.mark.parametrize('strict', [True, False])
@pytest.mark.parametrize('parallel', [True, False])
def test_explicit_tool_controls_survive_both_protocols(strict, parallel):
    body = {'tools': [{'type': 'function', 'function': {'name': 'run', 'strict': strict}}],
            'parallel_tool_calls': parallel,
            'tool_choice': {'type': 'function', 'function': {'name': 'run'}}}
    before = deepcopy(body)
    anthropic = openai_body_to_anthropic_checked(body).require_payload()
    responses = openai_body_to_responses_checked(body).require_payload()
    assert anthropic['tools'][0]['strict'] is strict
    assert anthropic['tool_choice'] == {
        'type': 'tool', 'name': 'run', 'disable_parallel_tool_use': not parallel}
    assert responses['parallel_tool_calls'] is parallel
    assert responses['tools'][0]['strict'] is strict
    assert body == before


def test_anthropic_schema_and_default_serial_choice_preserve_native_effort():
    schema = {'type': 'object', 'properties': {'answer': {'type': 'string'}}}
    body = {'response_format': {'type': 'json_schema', 'json_schema': {
        'name': 'answer', 'schema': schema, 'strict': True}},
        'parallel_tool_calls': False, 'effort': 'high',
        'output_config': {'effort': 'low'}}
    before = deepcopy(body)
    wire = openai_body_to_anthropic_checked(body).require_payload()
    assert wire['output_config'] == {'effort': 'high', 'format': {
        'type': 'json_schema', 'schema': schema}}
    assert wire['tool_choice'] == {'type': 'auto', 'disable_parallel_tool_use': True}
    wire['output_config']['format']['schema']['type'] = 'string'
    assert body == before


@pytest.mark.parametrize('checked,fields,code', [
    (openai_body_to_anthropic_checked, {'response_format': {'type': 'json_object'}},
     'response_format_unrepresentable'),
    (openai_body_to_anthropic_checked, {'response_format': 'SECRET'},
     'response_format_unrepresentable'),
    (openai_body_to_anthropic_checked, {'response_format': {'type': 'json_schema',
        'json_schema': {'schema': {}, 'strict': False}}}, 'response_format_unrepresentable'),
    (openai_body_to_anthropic_checked, {'response_format': {'type': 'json_schema',
        'json_schema': {'schema': {}}}, 'output_config': {'format': 'SECRET'}},
     'response_format_conflict'),
    (openai_body_to_anthropic_checked, {'parallel_tool_calls': False,
        'tool_choice': {'type': 'auto', 'disable_parallel_tool_use': False}},
     'parallel_tool_calls_conflict'),
    (openai_body_to_anthropic_checked, {'tool_choice': {
        'type': 'auto', 'disable_parallel_tool_use': 'SECRET'}},
     'parallel_tool_calls_boolean_required'),
    (openai_body_to_responses_checked, {'reasoning_effort': 'high',
        'reasoning': {'effort': 'low', 'summary': 'SECRET'}}, 'reasoning_effort_conflict'),
    (openai_body_to_responses_checked, {'reasoning': 'SECRET'}, 'reasoning_object_required'),
    (openai_body_to_responses_checked, {'output_config': {'format': 'SECRET'}},
     'output_config_unrepresentable'),
])
def test_explicit_unrepresentable_controls_fail_redacted_without_mutation(checked, fields, code):
    before = deepcopy(fields)
    result = checked(fields)
    assert result.payload is None
    assert result.validation_issues == (code,)
    assert 'SECRET' not in repr(result)
    with pytest.raises(ProtocolConversionError) as error:
        result.require_payload()
    assert 'SECRET' not in str(error.value)
    assert fields == before


@pytest.mark.parametrize('checked', [openai_body_to_anthropic_checked, openai_body_to_responses_checked])
@pytest.mark.parametrize('fields,code', [
    ({'parallel_tool_calls': 'SECRET'}, 'parallel_tool_calls_boolean_required'),
    ({'tools': [{'type': 'function', 'function': {'name': 'run', 'strict': 'SECRET'}}]},
     'tool_strict_boolean_required'),
])
def test_malformed_boolean_controls_fail(checked, fields, code):
    before = deepcopy(fields)
    assert checked(fields).validation_issues == (code,)
    assert fields == before


@pytest.mark.parametrize('native,code', [
    ('SECRET', 'native_content_list_required'),
    ({}, 'native_content_list_required'),
    ([None], 'native_content_object_required'),
    ([{'type': None}], 'native_content_object_required'),
    ([{'type': 1}], 'native_content_object_required'),
    ([{}], 'native_content_object_required'),
])
def test_responses_cross_protocol_native_blocks_fail_checked(native, code):
    body = {'messages': [{'role': 'assistant', '_anthropic_content_blocks': native}]}
    before = deepcopy(body)
    result = openai_body_to_responses_checked(body)
    assert result.validation_issues == (code,)
    assert result.payload is None
    assert body == before


@pytest.mark.parametrize('role', ['user', 'system', 'developer', 'tool'])
def test_responses_replay_carrier_requires_assistant_role(role):
    body = {'_inference_profile': {'features': {'responses_reasoning_replay': 'supported'}},
            'messages': [{'role': role, '_responses_items': [
                {'type': 'reasoning', 'encrypted_content': 'SECRET'}]}]}
    before = deepcopy(body)
    result = openai_body_to_responses_checked(body)
    assert result.validation_issues == ('native_content_role_invalid',)
    assert body == before


def test_responses_preserves_explicit_native_reasoning_options():
    body = {'effort': 'high', 'reasoning_effort': 'high',
            'reasoning': {'effort': 'high', 'summary': 'detailed'}}
    before = deepcopy(body)
    result = openai_body_to_responses_checked(body)
    assert result.require_payload()['reasoning'] == {'effort': 'high', 'summary': 'detailed'}
    assert body == before


@pytest.mark.parametrize('native', [None, {}, 42, 'SECRET', [None], [{'type': []}]])
@pytest.mark.parametrize('checked', [openai_body_to_anthropic_checked, openai_body_to_responses_checked])
def test_malformed_responses_state_is_checked_before_any_replay(native, checked):
    body = {'_route_inference_profile': {'features': {'responses_reasoning_replay': 'supported'}},
            'messages': [{'role': 'assistant', 'reasoning_content': 'SECRET', '_responses_items': native}]}
    before = deepcopy(body)
    result = checked(body)
    assert result.payload is None
    assert result.validation_issues[0] in {'native_content_list_required', 'native_content_object_required'}
    assert 'SECRET' not in repr(result)
    assert body == before


@pytest.mark.parametrize('fields,code', [
    ({'response_format': 'SECRET'}, 'response_format_unrepresentable'),
    ({'response_format': {'type': 'future'}}, 'response_format_unrepresentable'),
    ({'response_format': {'type': 'json_schema', 'json_schema': {'schema': {}}}},
     'response_format_unrepresentable'),
    ({'text': 'SECRET'}, 'text_config_object_required'),
    ({'response_format': {'type': 'json_object'}, 'text': {'format': {'type': 'text'}}},
     'response_format_conflict'),
])
def test_responses_never_silently_loses_output_format(fields, code):
    result = openai_body_to_responses_checked(fields)
    assert result.validation_issues == (code,)
    assert 'SECRET' not in repr(result)


def test_identical_responses_format_aliases_are_not_a_conflict():
    result = openai_body_to_responses_checked({
        'response_format': {'type': 'json_object'},
        'text': {'format': {'type': 'json_object'}, 'verbosity': 'low'}})
    assert result.require_payload()['text'] == {'format': {'type': 'json_object'}, 'verbosity': 'low'}


@pytest.mark.parametrize('stops', ['END', ['END', 'DONE']])
def test_stop_semantics_are_converted_or_rejected_never_lost(stops):
    body = {'stop': stops}
    before = deepcopy(body)
    expected = [stops] if isinstance(stops, str) else stops
    assert openai_body_to_anthropic_checked(body).require_payload()['stop_sequences'] == expected
    assert openai_body_to_responses_checked(body).validation_issues == ('stop_sequences_unrepresentable',)
    assert body == before


@pytest.mark.parametrize('checked', [openai_body_to_anthropic_checked, openai_body_to_responses_checked])
@pytest.mark.parametrize('fields,code', [
    ({'stop': [42]}, 'stop_sequences_invalid'),
    ({'stop': 'END', 'stop_sequences': ['OTHER']}, 'stop_sequences_conflict'),
])
def test_invalid_stop_aliases_fail_with_fixed_codes(checked, fields, code):
    assert checked(fields).validation_issues == (code,)


@pytest.mark.parametrize('checked', [openai_body_to_anthropic_checked, openai_body_to_responses_checked])
@pytest.mark.parametrize('invalid', [[], {}, False, 0, None])
@pytest.mark.parametrize('field,code', [
    ('tool_type', 'unsupported_tool'),
    ('role', 'unsupported_role'),
    ('choice_type', 'unsupported_tool_choice'),
])
def test_json_discriminators_fail_checked_not_as_unhashable_errors(checked, invalid, field, code):
    bodies = {
        'tool_type': {'tools': [{'type': invalid}]},
        'role': {'messages': [{'role': invalid, 'content': 'SECRET'}]},
        'choice_type': {'tool_choice': {'type': invalid}},
    }
    body = bodies[field]
    before = deepcopy(body)
    result = checked(body)
    assert result.payload is None
    assert result.validation_issues == (code,)
    assert 'SECRET' not in repr(result)
    assert body == before


@pytest.mark.parametrize('checked', [openai_body_to_anthropic_checked, openai_body_to_responses_checked])
@pytest.mark.parametrize('invalid', [{}, '', False, 0])
@pytest.mark.parametrize('field,code', [
    ('tools', 'request_list_required'),
    ('tool_calls', 'tool_calls_list_required'),
])
def test_empty_malformed_containers_are_not_repaired_into_success(checked, invalid, field, code):
    body = ({'tools': invalid} if field == 'tools' else
            {'messages': [{'role': 'assistant', 'content': 'answer', 'tool_calls': invalid}]})
    before = deepcopy(body)
    result = checked(body)
    assert result.validation_issues == (code,)
    assert result.payload is None
    assert body == before


@pytest.mark.parametrize('checked', [openai_body_to_anthropic_checked, openai_body_to_responses_checked])
@pytest.mark.parametrize('schema', [None, [], False, '', 0])
def test_invalid_function_schema_is_not_replaced_by_unconstrained_input(checked, schema):
    result = checked({'tools': [{'type': 'function', 'function': {
        'name': 'run', 'parameters': schema}}]})
    assert result.validation_issues == ('tool_schema_object_required',)


def test_empty_authored_function_schema_is_preserved():
    body = {'tools': [{'type': 'function', 'function': {'name': 'run', 'parameters': {}}}]}
    assert openai_body_to_anthropic_checked(body).require_payload()['tools'][0]['input_schema'] == {}
    assert openai_body_to_responses_checked(body).require_payload()['tools'][0]['parameters'] == {}


def test_required_tool_alias_maps_to_each_native_protocol():
    assert openai_body_to_anthropic_checked({'tool_choice': 'any'}).require_payload()['tool_choice'] == {'type': 'any'}
    assert openai_body_to_responses_checked({'tool_choice': 'any'}).require_payload()['tool_choice'] == 'required'


@pytest.mark.parametrize('name', [None, [], '', False])
def test_native_named_tool_choice_requires_a_name(name):
    assert openai_body_to_anthropic_checked({'tool_choice': {
        'type': 'tool', 'name': name}}).validation_issues == ('tool_name_invalid',)


@pytest.mark.parametrize('projection', [None, 'outdated projection'])
def test_native_visible_text_is_not_optional_metadata(projection):
    body = {'messages': [{'role': 'assistant', 'content': projection,
        '_anthropic_content_blocks': [{'type': 'text', 'text': 'Authoritative answer'}]}]}
    before = deepcopy(body)
    result = openai_body_to_responses_checked(body)
    wire = result.require_payload()
    assert wire['input'] == [{'type': 'message', 'role': 'assistant',
        'content': [{'type': 'output_text', 'text': 'Authoritative answer'}]}]
    assert not result.permitted_conversion_losses
    assert body == before


@pytest.mark.parametrize('completed', [True, False])
def test_generic_body_keeps_authoritative_historical_reasoning_continuity(completed):
    """A settled thinking signature can migrate; a live signature stays fenced."""
    from lib.llm.body import build_body
    message = {'role': 'assistant', 'content': 'Completed visible answer',
               'reasoning_content': 'Earlier reasoning',
               'thinking_signature': 'signed-provider-history'}
    if completed:
        message['_reasoning_continuity'] = 'completed_turn'
    original = deepcopy(message)
    body = build_body(model='gpt-6.1-sol', messages=[
        message, {'role': 'user', 'content': 'Continue the remaining task'},
    ], max_tokens=32, context_window=128000, precomputed_input_tokens=128)
    result = openai_body_to_responses_checked(body, profile='codex')
    if completed:
        assert not result.validation_issues
        assert result.permitted_conversion_losses['historical_thinking_signature_omitted'] == 1
        assert 'Completed visible answer' in str(result.require_payload()['input'])
    else:
        assert result.validation_issues == ('mandatory_continuity_unrepresentable',)
    assert message == original
