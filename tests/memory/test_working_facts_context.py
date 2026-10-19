"""Working-fact evidence enters the existing renderer and owner HTTP boundary."""
import asyncio
import copy
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from tests.memory.test_working_facts import facts as facts, artifact as artifact, storage, turn_service, record, query, owner, PRINCIPAL
from lib.tasks_pkg.context_composer import working_facts as context

pytestmark = pytest.mark.unit


def task_for(artifact):
    return {'id': 'fact-context-task', '_userId': 1, 'convId': artifact['conv_id'],
            '_principalContext': PRINCIPAL.to_payload(), 'config': {}, 'model': 'fixture'}


def latest_text(messages):
    return '\n'.join(block.get('text', '') for block in messages[-1]['content'])


@pytest.fixture
def quiet_notes(monkeypatch):
    # Inspector transport is not the authority under test; keep actual manifest
    # retention and rendering while avoiding an unregistered fixture task event.
    import lib.tasks_pkg.context_composer as composer
    monkeypatch.setattr(composer, '_record_session_context_note', lambda *a: None)
    monkeypatch.setattr(composer, '_record_committed_context_notes', lambda *a: None)


def test_context_reuses_visible_facts_then_restores_corrections_after_compaction(facts, quiet_notes):
    fact = record(facts)['facts'][0]
    task = task_for(facts)
    messages = [{'role': 'user', 'content': 'Continue the report'}]
    first = context.refresh_working_facts(messages, task=task, model='fixture')
    assert fact['id'] in latest_text(messages)
    assert first.manifest[0]['provenance']['facts'][0]['revision'] == 1
    before = copy.deepcopy(messages)
    again = context.refresh_working_facts(messages, task=task, model='fixture')
    assert messages == before
    assert not again.manifest[0]['appended']
    owner(facts, fact, 'correct', text='Use the corrected customer totals')
    changed = context.refresh_working_facts(messages, task=task, model='fixture')
    assert changed.manifest[0]['appended']
    assert 'Use the corrected customer totals' in latest_text(messages)
    assert 'owner_asserted' in latest_text(messages)
    # A compacted message list contains no owned block witness. Reacquire from
    # the selected authority instead of trusting a summary's old claim.
    messages[:] = [{'role': 'user', 'content': 'Continue after compaction'}]
    context.refresh_working_facts(messages, task=task, model='fixture')
    assert 'Use the corrected customer totals' in latest_text(messages)
    assert task['_contextManifest'][-1]['provenance']['taskId'] == task['id']


def test_no_acquisition_inside_tool_batch_or_transient_scope(facts, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError('No provider may be submitted for an ineligible carrier')
    monkeypatch.setattr(context.context_provider_executor, 'submit', forbidden)
    task = task_for(facts)
    messages = [{'role': 'assistant', 'tool_calls': [{'id': 'pending', 'function': {'name': 'read_files'}}]}]
    assert context.refresh_working_facts(messages, task=task, model='fixture') is None
    task['_transientRuntime'] = True
    assert context.refresh_working_facts([], task=task, model='fixture') is None


def test_deadline_and_cancel_exclude_late_acquisition(facts, quiet_notes, monkeypatch):
    fact = record(facts)['facts'][0]
    future = Future()
    future.set_running_or_notify_cancel()
    monkeypatch.setattr(context.context_provider_executor, 'submit', lambda *a, **kw: future)
    original_limit = context.limit
    monkeypatch.setattr(context, 'limit', lambda name: .02 if name == 'acquisitionSeconds' else original_limit(name))
    task = task_for(facts); messages = [{'role': 'user', 'content': 'Continue'}]
    context.refresh_working_facts(messages, task=task, model='fixture')
    assert 'have not been revalidated' in latest_text(messages)
    snapshot = copy.deepcopy(messages)
    future.set_result(query(facts))
    assert messages == snapshot
    assert fact['id'] not in latest_text(messages)
    task['aborted'] = True
    assert context.refresh_working_facts(messages, task=task, model='fixture') is None


def test_http_owner_correction_scope_and_revision(facts, monkeypatch):
    from quart import Quart, g, request
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from lib.identity import PrincipalContext
    from routes.api_v1.working_facts import api_v1_working_facts_bp
    fact = record(facts)['facts'][0]
    app = Quart(__name__)
    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(api_v1_working_facts_bp, name='facts_v4')))
    @app.before_request
    def identity():
        owner_id = request.headers.get('X-Test-Owner')
        if owner_id:
            g.auth_ctx = SimpleNamespace(is_authenticated=True, name='test owner', owner_user_id=int(owner_id),
                key_id='test', tenant_id=None, scopes=['chat'], has_scope=lambda scope: scope == 'chat')
            g.principal_context = PrincipalContext.user(subject_id='reviewer', owner_user_id=int(owner_id))
    async def exercise():
        client = app.test_client(); url = '/api/v4/conversations/' + facts['conv_id'] + '/working-facts'
        headers = {'X-Test-Owner': '1'}
        assert (await client.get(url)).status_code == 401
        assert (await client.get(url, headers={'X-Test-Owner': '8'})).status_code == 404
        response = await client.get(url, headers=headers)
        assert response.status_code == 200
        current = (await response.get_json())['data']
        proposal = {'action': 'record', 'revision': current['revision'], 'kind': 'observation',
                    'text': fact['text'], 'sources': fact['sources']}
        assert (await client.post(url, headers=headers, json=proposal | {'id': 'invented'})).status_code == 404
        assert (await client.post(url, headers=headers, json=proposal | {'text': 'A paraphrase'})).status_code == 400
        assert (await client.post(url, headers={'X-Test-Owner': '8'}, json=proposal | {'id': 'invented'})).status_code == 404
        unchanged = await client.get(url, headers=headers)
        assert (await unchanged.get_json())['data'] == current

        from lib.memory.working_facts import file_sources
        def forbidden(*a, **k):
            pytest.fail('HTTP file-source proposal caused an ungranted filesystem read')
        monkeypatch.setattr(file_sources, 'read_project_snapshot', forbidden)
        forged = {'action': 'record', 'revision': current['revision'], 'text': 'private text',
            'kind': 'observation', 'sources': [{'kind': 'file', 'id': 'forged', 'version': 'a' * 64,
                'path': '/private/never-opened', 'quote': 'private text'}]}
        assert (await client.post(url, headers=headers, json=forged)).status_code == 409
        command = {'action': 'correct', 'revision': current['revision'], 'id': fact['id'],
                   'text': 'Corrected by the owner', 'note': 'Verified with the customer'}
        response = await client.post(url, headers=headers, json=command)
        assert response.status_code == 200
        assert (await response.get_json())['data']['facts'][0]['status'] == 'owner_asserted'
        assert (await client.post(url, headers=headers, json=command)).status_code == 409
        assert (await client.post(url, headers={'X-Test-Owner': '8'}, json=command)).status_code == 404
    asyncio.run(exercise())


def test_global_budget_omits_initial_facts_but_preserves_corrections(facts, quiet_notes):
    fact = record(facts)['facts'][0]
    task = task_for(facts)
    messages = [{'role': 'user', 'content': 'Continue'}]
    context.refresh_working_facts(messages, task=task, model='fixture')
    task['config'] = {'context': {'globalBudgetTokens': 1}}
    owner(facts, fact, 'correct', text='A correction must supersede old evidence')
    result = context.refresh_working_facts(messages, task=task, model='fixture')
    assert 'A correction must supersede old evidence' in latest_text(messages)
    assert result.manifest[0]['appended']
    fresh = [{'role': 'user', 'content': 'Continue'}]
    result = context.refresh_working_facts(fresh, task=task_for(facts) | {'config': task['config']}, model='fixture')
    assert not result.manifest[0].get('appended', False)
    assert result.manifest[0]['reason'] == 'global_budget_exhausted'


def test_actual_native_round_admission_observes_owner_correction(facts, quiet_notes):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from lib.agent_core.native_process import RoundRequest
    from lib.tasks_pkg.native_context import NativeContextPolicy
    fact = record(facts)['facts'][0]
    task = task_for(facts) | {'abort_event': threading.Event(), 'status': 'running'}
    async def run(executor):
        policy = NativeContextPolicy(principal=PRINCIPAL, task=task, executor=executor)
        policy.bind_route({'owner_user_id': 1, 'logical_model': 'fixture', 'wire_model': 'fixture-wire',
                           'context_window': 131072, 'provider_id': 'fixture-provider'}, {'max_tokens': 1024}, {})
        try:
            messages = [{'role': 'user', 'content': 'Continue the report'}]
            prepared = await policy.prepare(RoundRequest(PRINCIPAL, task['id'], 'request', 0, 1, messages))
            assert fact['id'] in str(prepared)
            assert task['_lastPromptAdmission']['action'] == 'admit'
            owner(facts, fact, 'correct', text='Corrected during the task')
            second = await policy.prepare(RoundRequest(PRINCIPAL, task['id'], 'next', 1, len(prepared), prepared))
            assert 'Corrected during the task' in str(second)
            receipt = next(row for row in reversed(task['_contextManifest']) if row['id'] == 'working_facts')
            assert receipt['provenance']['facts'][0]['revision'] == 2
        finally:
            policy.close()
    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(run(executor))


def test_selection_keeps_complete_newest_facts_and_reports_omissions():
    import json
    from lib.token_counter import count_text
    view = {'revision': 256, 'facts': [
        {'id': 'wf_' + str(i), 'revision': 1, 'text': 'Observed fact with context. ' * 40,
         'kind': 'observation', 'status': 'observed', 'reasons': [], 'updatedAt': i,
         'sources': [{'kind': 'artifact', 'id': 'source', 'version': 'a' * 64, 'quote': 'omitted source body'}]}
        for i in range(256)]}
    text, bindings = context._text(view, 'fixture')
    parsed = json.loads(text)
    assert count_text(text, model='fixture') <= context.limit('contextTokens')
    assert 0 < len(bindings) < 256
    assert parsed['omitted'] == 256 - len(bindings)
    assert bindings[0]['id'] == 'wf_255'
    assert parsed['facts'][0]['text'] == view['facts'][-1]['text']
    assert 'quote' not in parsed['facts'][0]['sources'][0]
