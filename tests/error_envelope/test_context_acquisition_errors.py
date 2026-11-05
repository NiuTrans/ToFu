"""Required local context failure is distinct from a provider request timeout."""
import json

import pytest
from jsonschema import validate

from lib.agent_core.context_errors import TurnRequirementsDeadlineError
from lib.error_envelope import from_exception, from_json, to_json, typed_error_envelope_schema
from lib.llm.llm_error_format import format_llm_error_for_user

pytestmark = pytest.mark.unit


def test_requirement_deadline_retains_local_attribution_through_round_error_and_json():
    error = TurnRequirementsDeadlineError(15)
    error.__cause__ = RuntimeError('synthetic-private-context')
    envelope = format_llm_error_for_user(error, model='synthetic-model',
        context='task-fatal', source='orchestrator')
    assert envelope['kind'] == 'timeout'
    assert envelope['source'] == 'context-acquisition'
    assert envelope['failureStage'] == 'turn_requirements'
    assert envelope['origin'] == 'local'
    assert envelope['providerReached'] is False
    assert envelope['timeoutSeconds'] == 15
    assert envelope['model'] == '' and envelope['context'] == 'task-fatal'
    assert envelope['retryable'] is True
    assert '本地' in envelope['message'] and '尚未发送本轮模型请求' in envelope['message']
    assert 'titleKey' not in envelope and 'hintKey' not in envelope
    assert 'synthetic-private-context' not in json.dumps(envelope)
    assert 'faster model' not in envelope['hint'].lower()
    validate(envelope, typed_error_envelope_schema())
    assert from_json(to_json(envelope)) == envelope


def test_provider_timeout_text_never_becomes_a_local_requirement_failure():
    envelope = from_exception(TimeoutError(
        'Current task requirements could not be read before the context deadline'),
        model='synthetic-model', source='llm-stream')
    assert envelope['kind'] == 'timeout'
    assert envelope['source'] == 'llm-stream' and envelope['model'] == 'synthetic-model'
    assert 'providerReached' not in envelope and 'failureStage' not in envelope
    assert envelope['titleKey'] == 'err.k.timeout.title'


@pytest.mark.parametrize('detail', [
    'Mailbox input scope is no longer available',
    'Working fact input scope is no longer available',
])
def test_input_scope_denial_retains_local_attribution(detail):
    from lib.tasks_pkg.message_input_receipts import MessageInputScopeError

    error = MessageInputScopeError(detail)
    error.__cause__ = PermissionError('synthetic-private-authority-detail')
    envelope = format_llm_error_for_user(error, model='synthetic-model',
        context='request-rejected', source='llm-stream')
    assert envelope['kind'] == 'internal'
    assert envelope['source'] == 'context-admission'
    assert envelope['origin'] == 'local'
    assert envelope['providerReached'] is False
    assert envelope['failureStage'] == 'before_dispatch'
    assert envelope['retryable'] is False
    assert envelope['model'] == '' and envelope['detail'] == detail
    assert '本地' in envelope['message'] and 'HTTP 400' not in envelope['message']
    assert 'titleKey' not in envelope and 'hintKey' not in envelope
    assert 'synthetic-private-authority-detail' not in json.dumps(envelope)
    validate(envelope, typed_error_envelope_schema())
    assert from_json(to_json(envelope)) == envelope
