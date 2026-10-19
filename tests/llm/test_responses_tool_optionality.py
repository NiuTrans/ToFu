"""Chat-to-Responses translation preserves optional arguments and strict intent."""
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from lib.llm.responses_outbound import openai_body_to_responses_checked
from lib.swarm.message_contract import CONTRACT

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('profile', ['compatible', 'codex'])
@pytest.mark.parametrize('strict', ['omitted', False, True])
def test_conversion_preserves_tool_optionality_without_implicit_strict_normalization(profile, strict):
    tools = deepcopy(CONTRACT['tools'])
    if strict != 'omitted':
        for tool in tools:
            tool['function']['strict'] = strict
    body = {'model': 'gpt-6-astra', 'messages': [{'role': 'user', 'content': 'Inspect'}], 'tools': tools}
    before = deepcopy(body)
    wire = openai_body_to_responses_checked(body, profile=profile).require_payload()
    for tool in wire['tools']:
        assert tool['strict'] is (strict if isinstance(strict, bool) else False)
        request = {'agent_id': 'root', 'swarm_run_id': 'root'}
        if tool['name'] == 'send_agent_message':
            request.update(content='Starting diagnosis', idempotency_key='start')
        Draft202012Validator(tool['parameters']).validate(request)
    assert body == before
