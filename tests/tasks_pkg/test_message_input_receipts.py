"""Receipt identity follows actual immutable body text, not pre-compaction IDs."""
import pytest
from lib.tasks_pkg.message_input_receipts import remember, matched_ids, stream_with_receipts
from lib.llm.stream_result import ProviderStreamResult

pytestmark = pytest.mark.unit


def result():
    return ProviderStreamResult.from_legacy({'role': 'assistant', 'content': 'Read'}, 'stop', {})


def test_carrier_removed_by_retry_is_never_input_confirmed():
    task = {}
    messages = [{'role': 'user', 'content': 'Exact original bounded message', '_swarmMessageIds': ['a']}]
    remember(task, messages)
    stream_with_receipts(lambda *a, **kw: result(), task, {'messages': [{'role': 'user', 'content': 'A summary without the original'}]})
    assert task['_verifiedMessageInputIds'] == {'swarm': [], 'project': []}


def test_independent_physical_requests_do_not_reuse_prior_input_ids(monkeypatch):
    # Request occurrence matching is tested independently of repository auth.
    monkeypatch.setattr('lib.tasks_pkg.message_input_receipts.validate_matches', lambda *a: None)
    task = {}
    messages = [{'role': 'user', 'content': 'Original A', '_swarmMessageIds': ['a']},
                {'role': 'user', 'content': 'Original B', '_projectMessageIds': ['b']}]
    remember(task, messages)
    stream_with_receipts(lambda *a, **kw: result(), task, {'input': [{'content': [{'type': 'input_text', 'text': 'Original A'}]}]})
    assert task['_verifiedMessageInputIds']['swarm'] == ['a']
    assert task['_verifiedMessageInputIds']['project'] == []
    stream_with_receipts(lambda *a, **kw: result(), task, {'messages': [{'content': 'Original B'}]})
    assert task['_verifiedMessageInputIds']['swarm'] == []
    assert task['_verifiedMessageInputIds']['project'] == ['b']


def test_private_body_metadata_is_not_model_input():
    task = {}
    remember(task, [{'content': 'Original', '_projectMessageIds': ['a']}])
    assert matched_ids(task, {'_debug_prompt': 'Original'})['project'] == []


def test_failed_physical_attempt_clears_older_verified_ids():
    task = {'_verifiedMessageInputIds': {'swarm': ['old']}}
    def fail(*a, **kw):
        raise RuntimeError('provider rejected before receipt')
    with pytest.raises(RuntimeError):
        stream_with_receipts(fail, task, {})
    assert '_verifiedMessageInputIds' not in task


@pytest.mark.parametrize('body', [
    {'metadata': {'content': 'Original'}},
    {'tools': [{'description': 'Original'}]},
    {'model': 'Original'},
    {'messages': [{'role': 'user', 'image_url': {'url': 'Original'}, 'content': 'other'}]},
])
def test_non_input_fields_cannot_witness_consumption(body):
    task = {}
    remember(task, [{'content': 'Original', '_projectMessageIds': ['a']}])
    stream_with_receipts(lambda *a, **kw: result(), task, body)
    assert task['_verifiedMessageInputIds']['project'] == []


@pytest.mark.parametrize('family', ['swarm', 'project'])
def test_restored_carrier_without_fresh_acquisition_cannot_dispatch(family):
    task = {}
    remember(task, [{'content': 'Restored original',
                     ('_swarmMessageIds' if family == 'swarm' else '_projectMessageIds'): ['old']}])
    called = []
    with pytest.raises((PermissionError, ValueError)):
        stream_with_receipts(lambda *a, **kw: called.append(1) or result(), task,
                             {'messages': [{'content': 'Restored original'}]})
    assert not called
    assert '_verifiedMessageInputIds' not in task


def test_scope_denial_records_prior_paid_usage_without_fallback(monkeypatch):
    from lib.tasks_pkg.llm_fallback import _call as calls
    from lib.tasks_pkg.message_input_receipts import MessageInputScopeError
    from lib.cost import normalize_usage
    failure = MessageInputScopeError('scope revoked before resend')
    failure.usage = {'prompt_tokens': 11, 'completion_tokens': 2,
                     '_extra_billing_rounds': [
                         {'tag': 'R1-FLOOR-DISCARDED-primary',
                          'usage': {'prompt_tokens': 5, 'completion_tokens': 1}}]}
    dispatched = []
    def provider(*a, **kw):
        dispatched.append(1)
        raise failure
    monkeypatch.setattr(calls, 'stream_llm_response', provider)
    monkeypatch.setattr(calls, '_get_fallback_model', lambda task: 'must-not-fallback')
    totals, rounds = {}, []
    task = {'id': 'scope-usage', 'config': {}}
    with pytest.raises(MessageInputScopeError) as caught:
        calls._llm_call_with_fallback(task, {'messages': []}, 'primary', 0, 32,
                                     False, [], [], 'low', False, totals, rounds)
    assert caught.value is failure
    assert dispatched == [1]
    assert len(rounds) == 2 and all(row['responseAuthoring'] is False for row in rounds)
    usage = normalize_usage(totals)
    assert usage['input'] == 16 and usage['output'] == 3
    assert '_verifiedMessageInputIds' not in task
