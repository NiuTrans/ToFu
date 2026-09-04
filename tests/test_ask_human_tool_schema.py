"""Canonical ask_human schema — autonomy, bounded input, and mode validity."""

from __future__ import annotations

from jsonschema import Draft7Validator
import pytest

from lib.tools.gateway import sanitize_wire_tools, tool_schema_tokens
from lib.tools.human_guidance import ASK_HUMAN_TOOL


pytestmark = pytest.mark.unit


def test_schema_keeps_autonomy_and_markdown_qr_contracts():
    schema = ASK_HUMAN_TOOL['function']
    desc = schema['description'].lower()
    params = schema['parameters']
    props = params['properties']
    question = props['question']['description']

    for phrase in (
            'pause and ask', 'irreversible decision', 'subjective preference',
            'product intent', 'unavailable from the conversation, files, and tools',
            'inspect context', 'quick read/grep', 'sensible reversible default',
            'do not ask for something you can decide', 'free text or choices'):
        assert phrase in desc, phrase
    assert 'MARKDOWN' in question
    assert '![alt](/api/images/name.png)' in question
    assert 'lib.qr.qr_login_question(url)' in question
    assert 'never base64' in question
    assert props['response_type']['enum'] == ['free_text', 'choice']
    assert params['type'] == 'object'
    assert params['required'] == ['question', 'response_type']


def test_schema_bounds_user_visible_and_persisted_payloads():
    props = ASK_HUMAN_TOOL['function']['parameters']['properties']
    options = props['options']
    option_props = options['items']['properties']

    assert props['question']['maxLength'] == 32768
    assert options['minItems'] == 1 and options['maxItems'] == 16
    assert option_props['label']['maxLength'] == 1024
    assert option_props['description']['maxLength'] == 8192
    assert options['items']['required'] == ['label']


def test_schema_requires_nonempty_options_only_for_choice_mode():
    params = ASK_HUMAN_TOOL['function']['parameters']
    Draft7Validator.check_schema(params)
    validator = Draft7Validator(params)

    assert validator.is_valid({
        'question': 'What should I call this?',
        'response_type': 'free_text',
    })
    assert validator.is_valid({
        'question': 'Pick one',
        'response_type': 'choice',
        'options': [{'label': 'A'}, {'label': 'B', 'description': 'Safer'}],
    })
    assert not validator.is_valid({
        'question': 'Pick one',
        'response_type': 'choice',
    })
    assert not validator.is_valid({
        'question': 'Pick one',
        'response_type': 'choice',
        'options': [],
    })

    wire = [ASK_HUMAN_TOOL]
    assert sanitize_wire_tools(wire) is wire
    kimi_parameters = sanitize_wire_tools(
        wire, model='kimi-k3')[0]['function']['parameters']
    assert kimi_parameters['type'] == 'object'
    assert 'anyOf' not in kimi_parameters


def test_schema_stays_within_resident_token_budget():
    tokens = tool_schema_tokens([ASK_HUMAN_TOOL])
    assert tokens <= 325, (
        f'ask_human schema costs {tokens} tokens; compact repeated autonomy, '
        'choice, and QR prose without weakening input bounds')
