"""Current owner requirements survive common admission and wire construction."""
import copy
import json
from concurrent.futures import Future

import pytest

from tests.artifacts.test_turn_review import (subject as subject, storage as storage,
    turn_service as turn_service, configure, PRINCIPAL)
from tests.memory.test_working_facts_context import quiet_notes as quiet_notes
from tests.goals.test_checklist_context import _measure_chars
from lib.acceptance import turns
from lib.identity import PrincipalContext
from lib.llm.llm_errors import AbortedError, ContextCompactionError
from lib.turn_lifecycle import LifecycleNotFound
from lib.storage import StorageError
from lib.tasks_pkg.context_composer import turn_requirements as context
from lib.tasks_pkg.compaction import _prompt_admission as admission

pytestmark = pytest.mark.unit


def refresh(subject, messages):
    return context.refresh_turn_requirements(messages, task=subject.task, model='test-model')


def encoded(messages):
    return json.dumps(messages, ensure_ascii=False)


def test_requirements_reuse_then_owner_change_reaches_responses_wire(subject, quiet_notes):
    from lib.llm.body import build_body
    from lib.llm.responses_outbound._to_responses import openai_body_to_responses
    configured = configure(subject)
    messages = [{'role': 'user', 'content': 'Deliver the report'}]
    first = refresh(subject, messages)
    assert first.manifest[0]['provenance']['requirementsRevision'] == 1
    original = copy.deepcopy(messages)
    refresh(subject, messages)
    assert messages == original
    updated = configure(subject, objective='Use the corrected scope', criteria=[{
        'id': 'constraint', 'kind': 'constraint', 'text': 'Keep all customer names private'}])
    refresh(subject, messages)
    body = build_body('gpt-5.6-sol', messages, max_tokens=2048, tools=[])
    wire, _ = openai_body_to_responses(body, profile='default')
    assert 'Use the corrected scope' in encoded(wire)
    assert 'Keep all customer names private' in encoded(wire)
    assert updated['criteria'][0]['digest'] in encoded(wire)
    assert configured['outputs'][1]['artifactId'] in encoded(wire)
    assert '_contextComposer' not in encoded(wire)
    assert not wire.get('tools')


@pytest.mark.parametrize('compacted', [False, True])
def test_compaction_reacquires_complete_changed_requirements_before_measurement(subject, quiet_notes, monkeypatch, compacted):
    configure(subject)
    _measure_chars(monkeypatch, ceiling=10000)
    monkeypatch.setattr(admission, 'recompose_context_after_compaction', lambda *a, **kw: None)
    def compact(messages, **kwargs):
        configure(subject, objective='Changed while compaction ran')
        messages[:] = [{'role': 'user', 'content': 'Short summary'}]
        return compacted
    monkeypatch.setattr(admission, 'force_compact_if_needed', compact)
    messages = [{'role': 'user', 'content': 'x' * 20000}]
    result = admission.enforce_dispatch_prompt_limit(messages, [], subject.task, round_num=3, model='test-model')
    assert 'Changed while compaction ran' in encoded(messages)
    assert result['totalTokens'] < 10000
    assert subject.task['_contextManifest'][-1]['provenance']['requirementsRevision'] == 2


def test_required_content_is_never_locally_truncated_or_disabled_and_irreducible_prompt_is_refused(subject, quiet_notes, monkeypatch):
    configure(subject, criteria=[{'id': 'large', 'kind': 'constraint', 'text': 'full scope ' * 100}])
    subject.task['config']['context'] = {'disabledBlocks': ['turn_requirements'], 'globalBudgetTokens': 1}
    messages = [{'role': 'user', 'content': 'Continue'}]
    refresh(subject, messages)
    assert 'full scope ' * 100 in encoded(messages)
    _measure_chars(monkeypatch, ceiling=500)
    monkeypatch.setattr(admission, 'recompose_context_after_compaction', lambda *a, **kw: None)
    def compact(working, **kwargs):
        working[:] = [{'role': 'user', 'content': 'tiny'}]
        return False
    monkeypatch.setattr(admission, 'force_compact_if_needed', compact)
    with pytest.raises(ContextCompactionError, match='No main-provider request'):
        admission.enforce_dispatch_prompt_limit(messages, [], subject.task, round_num=2, model='test-model')
    assert 'full scope ' * 100 in encoded(messages)


def test_scope_and_binding_are_verified_before_reading_requirements(subject, quiet_notes):
    configure(subject)
    identity = ('turn-review', subject.turn_id, subject.task['_attemptId'], subject.task['id'])
    for index in range(4):
        altered = list(identity); altered[index] = 'foreign'
        with pytest.raises((LifecycleNotFound, StorageError)) as caught:
            turns.read_execution_requirements(*altered, principal=PRINCIPAL)
        assert 'Deliver a checked report' not in str(caught.value)
    with pytest.raises((LifecycleNotFound, StorageError)):
        turns.read_execution_requirements(*identity, principal=PrincipalContext.user(subject_id='other', owner_user_id=2))
    subject.task['_userId'] = 2
    with pytest.raises(ValueError, match='owner does not match'):
        refresh(subject, [{'role': 'user', 'content': 'Continue'}])


@pytest.fixture(params=[0, 1, 2], ids=['root', 'child', 'grandchild'])
def requirement_consumer(subject, request):
    from lib.tasks_pkg.child_invocation import create_child_invocation, close_unstarted_child_invocation
    children = []
    parent = subject.task
    try:
        for _ in range(request.param):
            parent = create_child_invocation(parent, model='test-model', tools=[], objective='Check report totals',
                event_sink=lambda event: None, abort_check=lambda: False)
            children.append(parent)
        yield parent
    finally:
        for child in reversed(children):
            close_unstarted_child_invocation(child, reason='Fixture complete')


def test_current_generation_reaches_real_http_requests_through_native_context_policy(subject, requirement_consumer, quiet_notes, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from http.server import BaseHTTPRequestHandler
    import threading
    from lib.agent_core.native_process import RoundRequest
    from lib.tasks_pkg.native_context import NativeContextPolicy
    from lib.llm.body import build_body
    from lib.llm.stream import stream_chat
    from tests.support.loopback import LoopbackThreadingHTTPServer
    from lib.desktop import egress
    monkeypatch.setattr(egress, 'route_request', lambda *a, **kw: 'direct')
    bodies = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            bodies.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            wire = b'data: {"choices":[{"delta":{"content":"read"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire))); self.end_headers(); self.wfile.write(wire)
    server = LoopbackThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}); thread.start()
    requirement_consumer.update(abort_event=threading.Event(), model='test-model', config={'model': 'test-model'})
    principal = PrincipalContext.from_payload(requirement_consumer['_principalContext'])
    configure(subject)
    async def run(executor):
        policy = NativeContextPolicy(principal=principal, task=requirement_consumer, executor=executor)
        policy.bind_route({'owner_user_id': 1, 'logical_model': 'test-model', 'wire_model': 'test-model',
            'context_window': 131072, 'provider_id': 'fixture-provider'}, {'max_tokens': 1024}, {})
        messages = [{'role': 'user', 'content': 'Deliver'}]
        try:
            for index in range(2):
                if index:
                    configure(subject, objective='Second scope in actual request')
                    # Simulated historical compaction, followed by actual common
                    # preparation, admission, body, transport and HTTP reception.
                    messages = [{'role': 'user', 'content': 'Shortened history'}]
                request = RoundRequest(principal, requirement_consumer['id'], 'request', index, len(messages), messages)
                await policy.prepare(request)
                body = build_body('test-model', request.messages, max_tokens=1024, tools=[])
                result = await asyncio.to_thread(stream_chat, body, api_key='fixture',
                    base_url=f'http://127.0.0.1:{server.server_port}/v1')
                assert result.is_verified_complete
            def unavailable(*a, **kw): raise TimeoutError('required snapshot unavailable')
            monkeypatch.setattr(turns, 'read_execution_requirements', unavailable)
            request = RoundRequest(principal, requirement_consumer['id'], 'request', 2, len(messages), messages)
            with pytest.raises(TimeoutError, match='required snapshot unavailable'):
                await policy.prepare(request)
        finally:
            policy.close()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            asyncio.run(run(executor))
        assert len(bodies) == 2
        assert 'Deliver a checked report' in encoded(bodies[0])
        assert 'Second scope in actual request' in encoded(bodies[1])
        assert 'Deliver a checked report' not in encoded(bodies[1])
        assert '_contextComposer' not in encoded(bodies)
    finally:
        server.shutdown(); server.server_close(); thread.join(3)
        assert not thread.is_alive()


def test_timeout_abort_and_read_failure_cannot_fall_back_to_old_requirements(subject, quiet_notes, monkeypatch):
    configure(subject)
    messages = [{'role': 'user', 'content': 'Continue'}]; refresh(subject, messages)
    original = copy.deepcopy(messages)
    future = Future(); future.set_running_or_notify_cancel()
    monkeypatch.setattr(context.context_provider_executor, 'submit', lambda *a, **kw: future)
    monkeypatch.setattr(context, '_CONTEXT_PROVIDER_DEADLINE_SECONDS', .01)
    with pytest.raises(TimeoutError): refresh(subject, messages)
    future.set_result({'late': True})
    assert messages == original
    subject.task['aborted'] = True
    with pytest.raises(AbortedError): refresh(subject, messages)
    subject.task['aborted'] = False
    failed = Future(); failed.set_exception(RuntimeError('storage unavailable'))
    monkeypatch.setattr(context.context_provider_executor, 'submit', lambda *a, **kw: failed)
    with pytest.raises(RuntimeError, match='storage unavailable'): refresh(subject, messages)
    assert messages == original


def test_reactive_retry_restores_requirements_and_refuses_unavailable_snapshot(subject, quiet_notes, monkeypatch):
    from lib.tasks_pkg.compaction import api as compaction
    from lib.tasks_pkg.llm_fallback import _call as fallback
    from lib.cgroup_guard import MemoryPressureError
    configure(subject)
    def compact(messages, **kwargs):
        messages[:] = [{'role': 'user', 'content': 'Shortened after memory pressure'}]
        return True
    monkeypatch.setattr(compaction, 'reactive_compact', compact)
    messages = [{'role': 'user', 'content': 'Continue'}]
    body = fallback._prepare_reactive_retry_body(subject.task, {}, 'test-model', messages, [],
        'medium', False, MemoryPressureError('memory'), 'test')
    assert body is not None and 'Deliver a checked report' in encoded(body)
    def unavailable(*a, **kw): raise TimeoutError('requirements unavailable')
    monkeypatch.setattr(context, 'refresh_turn_requirements', unavailable)
    monkeypatch.setattr(fallback, 'build_body', lambda *a, **kw: pytest.fail('No incomplete request'))
    with pytest.raises(TimeoutError, match='requirements unavailable'):
        fallback._prepare_reactive_retry_body(subject.task, {}, 'test-model', messages, [],
            'medium', False, MemoryPressureError('memory'), 'test')


@pytest.mark.parametrize('unavailable', [False, True])
def test_model_fallback_reacquires_changed_scope_and_cannot_bypass_failed_acquisition(subject, quiet_notes, monkeypatch, unavailable):
    from lib.llm.llm_errors import PermissionError_
    from lib.tasks_pkg.llm_fallback import _call as fallback
    from tests.tasks_pkg.test_pool_rescue_fallback import _patch_common, _set_gate
    configure(subject)
    messages = [{'role': 'user', 'content': 'Deliver'}]; refresh(subject, messages)
    calls = []
    def stream(task, body, **kwargs):
        calls.append(copy.deepcopy(body))
        if len(calls) == 1:
            configure(subject, objective='Scope changed during first request')
            if unavailable:
                def failed(*a, **kw): raise TimeoutError('requirements cannot be reacquired')
                monkeypatch.setattr(turns, 'read_execution_requirements', failed)
            raise PermissionError_('Primary unavailable')
        return ({'role': 'assistant', 'content': 'Read the current scope'}, 'stop',
                {'prompt_tokens': 5, 'completion_tokens': 3})
    _patch_common(monkeypatch, stream, fallback='kimi-k3'); _set_gate(monkeypatch, has=True)
    def run():
        return fallback._llm_call_with_fallback(subject.task, {'model': 'test-model', 'messages': messages},
            'test-model', 0, 512, False, [], messages, 'low', False, {}, [])
    if unavailable:
        with pytest.raises(TimeoutError, match='requirements cannot be reacquired'): run()
        assert len(calls) == 1
    else:
        assert run()['finish_reason'] == 'stop'
        assert len(calls) == 2
        assert 'Scope changed during first request' in encoded(calls[1])


def test_delegated_child_reacquires_owner_requirements_without_root_authority(subject, quiet_notes):
    from lib.tasks_pkg.child_invocation import create_child_invocation, close_unstarted_child_invocation
    configured = configure(subject)
    child = create_child_invocation(subject.task, model='test-model', tools=[], objective='Check report totals',
        event_sink=lambda event: None, abort_check=lambda: False)
    try:
        messages = [{'role': 'user', 'content': 'Check report totals'}]
        result = context.refresh_turn_requirements(messages, task=child, model='test-model')
        assert result is not None
        assert configured['objective'] in encoded(messages)
        assert child['_transientRuntime'] is True and child['_invocationStorageFree'] is False
        assert not child.get('_turnId') and not child.get('_attemptId')
        assert child['_unattended'] is True and child['_invocationTools'] == []
        updated = configure(subject, objective='Use the corrected scope')
        context.refresh_turn_requirements(messages, task=child, model='test-model')
        assert updated['objective'] in encoded(messages)
        assert configured['objective'] not in encoded(messages[-1])
        assert 'Context update: turn_requirements' in encoded(messages[-1])
        assert child['_contextManifest'][-1]['provenance']['requirementsRevision'] == 2
    finally:
        close_unstarted_child_invocation(child, reason='Fixture complete')


@pytest.mark.parametrize('legacy_snapshot', [False, True])
def test_restored_assignment_reads_live_requirements_not_frozen_text(subject, quiet_notes, legacy_snapshot):
    from lib.swarm.master import MasterOrchestrator, bind_assignment_context
    from lib.swarm.protocol import SubTaskSpec
    from lib.tasks_pkg.child_invocation import create_child_invocation, close_unstarted_child_invocation
    from lib.tasks_pkg.requirement_source import requirement_source
    configure(subject, objective='Original report scope')
    spec = SubTaskSpec(id='reader', role='general', objective='Check report totals')
    bind_assignment_context(spec, parent_task=subject.task, parent_config=subject.task['config'],
        all_tools=[], model='test-model')
    frozen = json.loads(json.dumps(spec.to_dict()))
    assert 'Original report scope' not in encoded(frozen)
    if legacy_snapshot:
        frozen['retained_execution_context'].pop('requirement_source')
    restored = SubTaskSpec.from_dict(frozen)
    assert restored.retained_assignment_parent is None
    session = MasterOrchestrator(subject.task['id'], subject.task['convId'], [], user_id=1,
        parent_task=subject.task, parent_config=subject.task['config'], model='test-model', all_tools=[])
    proxy = session._make_agent(restored).parent_task
    assert not proxy.get('_turnId') and not proxy.get('_attemptId')
    assert requirement_source(proxy).task == subject.task['id']
    configure(subject, objective='Corrected after process restart')
    child = create_child_invocation(proxy, model='test-model', tools=[], objective=spec.objective,
        event_sink=lambda event: None, abort_check=lambda: False)
    try:
        messages = [{'role': 'user', 'content': spec.objective}]
        context.refresh_turn_requirements(messages, task=child, model='test-model')
        assert 'Corrected after process restart' in encoded(messages)
        assert 'Original report scope' not in encoded(messages)
        assert child['_invocationTools'] == [] and child['_unattended'] is True
    finally:
        close_unstarted_child_invocation(child, reason='Fixture complete')


def test_delegated_source_rejects_scope_substitution_and_superseded_root(subject, quiet_notes):
    from dataclasses import replace
    from lib.tasks_pkg.child_invocation import create_child_invocation, close_unstarted_child_invocation
    from tests.artifacts.test_turn_review import finish
    configure(subject)
    child = create_child_invocation(subject.task, model='test-model', tools=[], objective='Check',
        event_sink=lambda event: None, abort_check=lambda: False)
    try:
        source = child['_turnRequirementSource']
        for corrupted in ({'turn': subject.turn_id}, replace(source, owner=2),
                          replace(source, tenant='foreign'), replace(source, conversation='other')):
            child['_turnRequirementSource'] = corrupted
            with pytest.raises((PermissionError, ValueError)):
                context.refresh_turn_requirements([{'role': 'user', 'content': 'Check'}], task=child, model='test-model')
        child['_turnRequirementSource'] = source
        finish(subject)
        turn = subject.service.get_turn('turn-review', subject.turn_id, user_id=1)
        subject.service.create_attempt('turn-review', subject.turn_id, command_id='new-owner-attempt',
            operation='regenerate', expected_projection_revision=turn['projectionRevision'], user_id=1)
        with pytest.raises(LifecycleNotFound, match='superseded'):
            context.refresh_turn_requirements([{'role': 'user', 'content': 'Check'}], task=child, model='test-model')
    finally:
        close_unstarted_child_invocation(child, reason='Fixture complete')


@pytest.mark.parametrize('task', [{}, {'_transientRuntime': True, '_turnId': 'caller-label'}])
def test_no_requirement_source_does_not_import_durable_acceptance(monkeypatch, task):
    import builtins
    original = builtins.__import__
    def imported(name, *args, **kwargs):
        if name == 'lib.acceptance.turns' or name == 'lib.storage' or name.startswith('lib.storage.'):
            raise AssertionError('no-source context imported durable requirements')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', imported)
    messages = [{'role': 'user', 'content': 'Standalone SDK request'}]
    assert context.refresh_turn_requirements(messages, task=task, model='test-model') is None
    assert messages == [{'role': 'user', 'content': 'Standalone SDK request'}]
