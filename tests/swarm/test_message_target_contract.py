"""Message target alternatives survive provider projection and gateway defaults.

Real dispatch and portable mailbox CAS prove that rejected mixed targets have
no effects, and an explicitly corrected exact-run call keeps its original run.
"""
from copy import deepcopy
import importlib
import json

from jsonschema import Draft202012Validator
import pytest

from lib.swarm.message_contract import SEND_AGENT_MESSAGE_TOOL, GET_AGENT_MESSAGES_TOOL
from lib.tools.contracts import (
    compile_execution_contract_documents, validate_tool_arguments_from_documents,
)
from lib.tools.gateway import sanitize_wire_tools
from tests.swarm import test_run_messages as mailbox_tests

context = mailbox_tests.context
TOOLS = [SEND_AGENT_MESSAGE_TOOL, GET_AGENT_MESSAGES_TOOL]
pytestmark = pytest.mark.unit


def arguments(name, *, stable):
    target = ({'retained_agent_id': 'colleague', 'idempotency_key': 'logical-1'}
              if stable else {'agent_id': 'child-a', 'swarm_run_id': 'launch'})
    if name == 'send_agent_message':
        target.update(content='Evidence', idempotency_key='logical-1')
    return target


@pytest.mark.parametrize('model', ['gpt-6.1-sol', 'kimi-k2.5', 'claude-sonnet-4-5'])
def test_target_schemas_remain_callable_on_provider_wire(model):
    before = deepcopy(TOOLS)
    wire = sanitize_wire_tools(TOOLS, model=model)
    assert len(wire) == len(TOOLS)
    for tool in wire:
        name = tool['function']['name']
        validator = Draft202012Validator(tool['function']['parameters'])
        validator.validate(arguments(name, stable=False))
        validator.validate(arguments(name, stable=True))
    assert TOOLS == before


def test_gateway_defaults_allow_exact_run_send_and_receipt_read(context, monkeypatch):
    dispatch = importlib.import_module('lib.swarm.integration._tools')
    monkeypatch.setattr(dispatch, '_cleanup_stale_sessions', lambda: None)
    monkeypatch.setattr(dispatch, '_get_session', lambda key: context.session)
    documents = compile_execution_contract_documents(TOOLS)
    authored = mailbox_tests.args()
    prepared = validate_tool_arguments_from_documents(documents, 'send_agent_message', authored)
    receipt = json.loads(dispatch.execute_swarm_tool('send_agent_message', prepared, context.task))
    assert receipt['state'] == 'submitted'
    assert receipt['agent_id'] == authored['agent_id']
    assert receipt['swarm_run_id'] == authored['swarm_run_id']
    assert 'intent' not in prepared and prepared['kind'] == 'reference'
    read_args = {'agent_id': receipt['agent_id'], 'swarm_run_id': receipt['swarm_run_id'],
                 'message_id': receipt['message_id']}
    read_args = validate_tool_arguments_from_documents(documents, 'get_agent_messages', read_args)
    history = json.loads(dispatch.execute_swarm_tool('get_agent_messages', read_args, context.task))
    assert history['messages'][0]['content'] == authored['content']
    assert len(context.client.rows) == 1


def test_canonical_root_oneof_projects_to_a_callable_kimi_wire():
    """Kimi's MFJS dialect rejects a root combinator; the projection must relax
    it to the intersection-of-requirements shape while the canonical contract
    keeps enforcing the strict oneOf at execution time."""
    from lib.tools.gateway import _normalize_provider_tool_schema

    for tool in TOOLS:
        name = tool['function']['name']
        canonical = tool['function']['parameters']
        assert 'oneOf' in canonical, name
        out, repairs, error = _normalize_provider_tool_schema(tool, model='kimi-k3')
        assert error == ''
        projected = out['function']['parameters']
        assert projected.get('type') == 'object'
        assert 'oneOf' not in projected and 'anyOf' not in projected
        # Every-field properties survive; only branch-universal requirements do.
        properties = projected.get('properties') or {}
        for field in ('agent_id', 'swarm_run_id', 'retained_agent_id'):
            assert field in properties, (name, field)
        if name == 'send_agent_message':
            assert set(projected.get('required') or []) == {'content', 'idempotency_key'}
        else:
            assert not projected.get('required'), name
        # The caller's canonical catalog is never mutated.
        assert 'oneOf' in tool['function']['parameters']


@pytest.mark.parametrize('name', ['send_agent_message', 'get_agent_messages'])
def test_rejected_mixed_target_explains_correction_without_receipt_lookup(context, monkeypatch, name):
    dispatch = importlib.import_module('lib.swarm.integration._tools')
    monkeypatch.setattr(dispatch, '_cleanup_stale_sessions', lambda: None)
    monkeypatch.setattr(dispatch, '_get_session', lambda key: context.session)
    mixed = {**arguments(name, stable=False), **arguments(name, stable=True),
             'scope': 'conversation'}
    if name == 'send_agent_message':
        mixed['intent'] = 'message'
    rejected = json.loads(dispatch.execute_swarm_tool(name, mixed, context.task))
    assert rejected['code'] == 'message_invalid_arguments'
    assert rejected['submission_state'] == 'rejected'
    assert 'remove retained_agent_id, scope and intent' in rejected['next_action']
    assert 'Inspect get_agent_messages' not in rejected['next_action']
    assert context.client.rows == {}


def test_both_target_forms_supplied_is_an_explicit_ambiguity_not_a_silent_choice(context):
    """A call that satisfies BOTH oneOf branches must not silently pick one."""
    documents = compile_execution_contract_documents(TOOLS)
    mixed = {
        'agent_id': 'child-a', 'swarm_run_id': 'launch',
        'retained_agent_id': 'colleague',
        'content': 'Evidence', 'idempotency_key': 'logical-1',
    }
    with pytest.raises(Exception) as error:
        validate_tool_arguments_from_documents(documents, 'send_agent_message', mixed)
    assert 'more than one target form' in str(error.value)


def test_unknown_fields_are_not_misreported_as_mixed_targets(context, monkeypatch):
    dispatch = importlib.import_module('lib.swarm.integration._tools')
    monkeypatch.setattr(dispatch, '_cleanup_stale_sessions', lambda: None)
    malformed = {**arguments('send_agent_message', stable=True), 'typo': 'unexpected'}
    rejected = json.loads(dispatch.execute_swarm_tool('send_agent_message', malformed, context.task))
    assert rejected['code'] == 'message_invalid_arguments'
    assert 'Unknown stable-message fields: typo' in rejected['message']
    assert context.client.rows == {}


def test_uncertain_submission_still_requires_original_receipt_lookup(context, monkeypatch):
    from lib.swarm.run_messages import RunMessageError
    dispatch = importlib.import_module('lib.swarm.integration._tools')
    monkeypatch.setattr(dispatch, '_cleanup_stale_sessions', lambda: None)
    monkeypatch.setattr(dispatch, '_get_session', lambda key: context.session)

    def uncertain(*args, **kwargs):
        raise RunMessageError('message_activation_unknown', 'ACK unavailable', submission_state='unknown')

    monkeypatch.setattr('lib.swarm.message_service.send', uncertain)
    outcome = json.loads(dispatch.execute_swarm_tool('send_agent_message', mailbox_tests.args(), context.task))
    assert outcome['submission_state'] == 'unknown'
    assert 'Inspect get_agent_messages with the original target/key' in outcome['next_action']
    assert 'Correct the arguments' not in outcome['next_action']


@pytest.mark.parametrize('name', ['send_agent_message', 'get_agent_messages'])
@pytest.mark.parametrize('mutation', ['missing_agent', 'missing_run', 'scope', 'intent', 'stable_with_run', 'stable_unknown'])
def test_incomplete_or_mode_incompatible_targets_have_no_effects(context, monkeypatch, name, mutation):
    dispatch = importlib.import_module('lib.swarm.integration._tools')
    monkeypatch.setattr(dispatch, '_cleanup_stale_sessions', lambda: None)
    monkeypatch.setattr(dispatch, '_get_session', lambda key: context.session)
    malformed = arguments(name, stable=False)
    if mutation == 'missing_agent':
        malformed.pop('agent_id')
    elif mutation == 'missing_run':
        malformed.pop('swarm_run_id')
    elif mutation == 'scope':
        malformed['scope'] = 'project'
    elif mutation == 'intent':
        malformed['intent'] = 'message'
    else:
        malformed = arguments(name, stable=True)
        malformed['swarm_run_id' if mutation == 'stable_with_run' else 'unexpected'] = 'launch'
    rejected = json.loads(dispatch.execute_swarm_tool(name, malformed, context.task))
    assert rejected['code'] in {'message_invalid_arguments', 'message_invalid_identity'}
    assert rejected['submission_state'] == 'rejected'
    assert 'Correct the arguments before retrying' in rejected['next_action']
    assert context.client.rows == {}
