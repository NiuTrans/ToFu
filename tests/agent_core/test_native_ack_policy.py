"""Explicit nullable native callback budgets differ from missing authority."""
import json
from pathlib import Path

import jsonschema
import pytest

pytestmark=pytest.mark.unit
SCHEMA=json.loads((Path(__file__).resolve().parents[2]/'contracts/native_agent_stdio_v1.schema.json').read_text())
PROFILES={
    'model_control':({'ack_ms':1},'ack_ms'),
    'terminal_persistence':({'operation_id':'owned','ack_ms':1,'attempts':2,'frame_bytes':4096},'ack_ms'),
    'model_request':({'version':'tofu.native-agent-stdio/v1','type':'model_request','request_id':'reserve',
                      'owner_user_id':7,'task_id':'task','attempt_ordinal':1,'action':'reserve',
                      'input_tokens':0,'max_output_tokens':1,'minimum_output_tokens':1,'expires_monotonic_ns':1},'expires_monotonic_ns'),
}


@pytest.mark.parametrize('name',PROFILES)
@pytest.mark.parametrize('value',[None,1,1000])
def test_explicit_null_or_positive_budget_is_valid(name,value):
    profile,key=PROFILES[name]
    jsonschema.Draft202012Validator(SCHEMA['$defs'][name]).validate(dict(profile,**{key:value}))


@pytest.mark.parametrize('name',PROFILES)
@pytest.mark.parametrize('value',[False,0,-1,'1000',1.5,1<<64])
def test_malformed_budget_refuses(name,value):
    profile,key=PROFILES[name]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(SCHEMA['$defs'][name]).validate(dict(profile,**{key:value}))


@pytest.mark.parametrize('name',PROFILES)
def test_missing_budget_never_means_unbounded(name):
    profile,key=PROFILES[name];value=dict(profile);value.pop(key)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(SCHEMA['$defs'][name]).validate(value)
