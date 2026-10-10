"""Tool-returned pixels survive Responses conversion, replay and sealing."""
from copy import deepcopy

import pytest

from lib.llm.responses_outbound import openai_body_to_responses_checked

pytestmark = pytest.mark.unit

IMAGE = 'data:image/png;base64,iVBORw0KGgo='


def request_body(image=IMAGE):
    return {'model': 'opaque-model', 'messages': [
        {'role': 'user', 'content': 'Inspect this image.'},
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'call_image', 'type': 'function', 'function': {
                'name': 'inspect_image', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'call_image', 'content': [
            {'type': 'text', 'text': 'Before'},
            {'type': 'image_url', 'image_url': image},
            {'type': 'text', 'text': 'After'}]},
        {'role': 'user', 'content': 'Continue.'},
    ]}


@pytest.mark.parametrize('profile', ['default', 'codex'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('image', [IMAGE, {'url': IMAGE, 'detail': 'high'}])
def test_tool_image_is_native_output_and_retains_order_call_and_pixels(profile, stream, image):
    from lib.llm.request import seal_request
    body = request_body(image)
    before = deepcopy(body)
    result = openai_body_to_responses_checked(body, profile=profile, stream=stream)
    assert not result.validation_issues
    sealed = seal_request(result.payload, model=body['model'], protocol='responses',
                          input_tokens=1, semantic_id='synthetic')
    items = sealed.wire_body()['input']
    output = next(i for i in items if i['type'] == 'function_call_output')
    assert output['call_id'] == 'call_image'
    assert [b['type'] for b in output['output']] == ['input_text', 'input_image', 'input_text']
    assert output['output'][1]['image_url'] == IMAGE
    if isinstance(image, dict):
        assert output['output'][1]['detail'] == 'high'
    assert [output['output'][i]['text'] for i in (0, 2)] == ['Before', 'After']
    assert items[-1]['content'][0]['text'] == 'Continue.'
    assert body == before


@pytest.mark.parametrize('image', [None, {}, {'url': ''}, {'url': 5}])
def test_invalid_tool_image_still_fails_closed(image):
    result = openai_body_to_responses_checked(request_body(image))
    assert result.payload is None
    assert result.validation_issues == ('unsupported_content',)


def test_image_does_not_gain_an_assistant_output_representation():
    body = request_body()
    body['messages'][2]['role'] = 'assistant'
    result = openai_body_to_responses_checked(body)
    assert result.validation_issues == ('unsupported_content',)


def test_program_interpreter_does_not_stringify_pixels_or_bypass_its_text_budget():
    body = request_body()
    body['messages'][1]['tool_calls'][0]['caller'] = {'type': 'program', 'caller_id': 'program'}
    result = openai_body_to_responses_checked(body)
    assert result.payload is None
    assert result.validation_issues == ('programmatic_image_output_unrepresentable',)


def test_direct_call_to_programmatic_eligible_tool_retains_image():
    from lib.llm.responses_outbound._to_responses import _messages_to_input
    items = _messages_to_input(request_body()['messages'], {},
                              programmatic_tool_names={'inspect_image'},
                              default_image_detail='original')
    output = next(i for i in items if i['type'] == 'function_call_output')
    assert output['output'][1]['detail'] == 'original'


@pytest.mark.parametrize('oauth', ['', 'codex'])
def test_shared_request_preparation_passes_tool_pixels_to_wire(monkeypatch, oauth):
    from lib.llm._sse_core import prepare_request
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **k: 'direct')
    monkeypatch.setattr('lib.oauth.outbound.resolve_oauth_request',
                        lambda oauth, body, extra_headers, **kw: ('test-only', {}, body))
    body = request_body({'url': IMAGE})
    plan = prepare_request(body, base_url='https://example.invalid/v1',
                           api_protocol='responses', oauth=oauth)
    output = next(i for i in plan.body.wire_body()['input'] if i['type'] == 'function_call_output')
    assert output['output'][1]['image_url'] == IMAGE
