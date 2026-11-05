"""One application service consumes chat and automation without an API carrier."""
import threading
import uuid

import pytest

from lib.identity import PrincipalContext
from tests.automations.test_runtime_application import wait_until

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def test_composition_registers_existing_domain_owners_without_opening_storage():
    from lib.durable_worker.runtime import create_service
    def forbidden(**kwargs):
        pytest.fail('composition opened storage before startup')
    service = create_service(client_factory=forbidden)
    assert {registration.task_kind for registration in service._registrations} == {
        'automation-clock', 'automation-event', 'automation-run', 'conversation-attempt'}
    assert service.stop()


def test_idle_common_service_executes_and_joins_a_durably_dispatched_chat(
        semantic_authority, monkeypatch):
    from lib import storage, turn_lifecycle as turns
    from lib.conversation_sync import task_start
    from lib.conversation_sync.execution_identity import capture_execution_identity
    from lib.durable_worker.runtime import create_service
    from lib.tasks_pkg.manager import append_event, persist_task_result, stamp_chat_task_terminal
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    factory, owner, _ = semantic_authority
    monkeypatch.setattr(turns, '_turn_client', lambda **kwargs: factory(**kwargs))
    monkeypatch.setattr(storage, 'get_storage_client', lambda **kwargs: factory(**kwargs))
    monkeypatch.setattr(task_start, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(turns, 'build_api_messages', lambda *args, **kwargs: [{'role': 'user', 'content': 'work'}])
    empty = threading.Event()
    executed = []
    class Client:
        def __init__(self, client):
            self.client = client
        def __getattr__(self, name):
            return getattr(self.client, name)
        def query(self, operation, *args, **kwargs):
            value = self.client.query(operation, *args, **kwargs)
            if operation == 'worker_job.claim_horizon' and value['claimableInMs'] is None:
                empty.set()
            return value
    def run(task):
        executed.append(task['id'])
        task['content'] = 'full common executor output'
        append_event(task, {'type': 'delta', 'content': task['content']})
        stamp_chat_task_terminal(task, status='done', finish_reason='stop')
        append_event(task, {'type': 'done', 'finishReason': 'stop'})
        assert persist_task_result(task)
    monkeypatch.setattr('lib.tasks_pkg.orchestrator.api.run_task', run)
    principal = PrincipalContext.user(subject_id='chat-composition', owner_user_id=owner)
    identity = uuid.uuid4().hex
    pair = turns.create_turn_pair(identity, command_id=identity,
        input_projection={'content': 'work'},
        config=capture_execution_identity({'model': 'controlled'}, owner, principal), user_id=owner,
        conversation_defaults={'allowCreate': True, 'title': 'Common worker', 'settings': {}})
    service = create_service(client_factory=lambda **kwargs: Client(factory(**kwargs)), max_workers=1)
    task_id = 'conversation-attempt:' + pair['attempt']['attemptId']
    try:
        assert service.start()
        assert empty.wait(8)
        turns.dispatch_attempt_to_worker(principal, pair['attempt']['attemptId'])
        def complete():
            job = factory().query('worker_job.get', {'task_id': task_id, 'user_id': owner})
            return job if job and job['status'] == 'succeeded' else None
        wait_until(complete)
        assert executed == [task_id]
        assert chat_task_runtime.get(task_id)['_activeWorkerCount'] == 0
        assert turns.get_attempt(pair['attempt']['attemptId'], user_id=owner)['status'] == 'completed'
    finally:
        assert service.stop()
        chat_task_runtime.discard(task_id)
