"""Requirement admission reads owned metadata, never the growing Turn body."""
import copy
import json
import threading
import time
from concurrent.futures import Future

import pytest

from lib.acceptance import turns
from lib.llm.llm_errors import AbortedError
from lib.tasks_pkg.context_composer import turn_requirements as context
from tests.artifacts.test_turn_review import (
    subject as subject, storage as storage, turn_service as turn_service,
    PRINCIPAL, configure,
)
from tests.memory.test_working_facts_context import quiet_notes as quiet_notes

pytestmark = pytest.mark.unit


def test_current_requirements_do_not_materialize_turn_projection(subject, storage, monkeypatch, tmp_path):
    configure(subject)
    original = storage.client.query
    observations = []

    def measured(operation, payload=None, **kwargs):
        result = original(operation, payload, **kwargs)
        if operation == 'turn.get' and (payload or {}).get('include_projection') is False:
            observations.append(len(json.dumps(result).encode()))
            assert 'projection' not in result, 'Requirement reads must not load the growing Turn body'
        return result

    monkeypatch.setattr(storage.client, 'query', measured)
    measurements = []
    expected = None
    for size in [0, 256 * 1024, 2 * 1024 * 1024]:
        subject.task['content'] = 'x' * size
        subject.service.record_task_event(subject.task, {'type': 'delta', 'content': subject.task['content']})
        # Full default reads still retain the answer; only requirement acquisition
        # omits it. A wall-time measurement is diagnostic, not a timing assertion.
        full = subject.service.get_turn('turn-review', subject.turn_id, user_id=1)
        assert len(full['projection']['content']) == size
        before = len(observations)
        start = time.perf_counter()
        snapshot = turns.read_execution_requirements('turn-review', subject.turn_id,
            subject.task['_attemptId'], subject.task['id'], principal=PRINCIPAL)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert snapshot['objective'] == 'Deliver a checked report'
        expected = snapshot if expected is None else expected
        assert snapshot == expected
        assert len(observations) == before + 1
        measurements.append({'answer_bytes': size, 'elapsed_ms': elapsed_ms,
                             'turn_metadata_json_bytes': observations[-1]})
    assert observations and max(observations) < 4096
    assert max(observations) - min(observations) < 128
    (tmp_path / 'requirement-read-cost.json').write_text(json.dumps(measurements, indent=2))


def test_local_requirement_deadline_waits_as_storage_never_as_a_model_failure(subject, quiet_notes, monkeypatch):
    pending = Future()
    submitted = []
    class Executor:
        def submit(self, *args):
            submitted.append(args)
            return pending
    monkeypatch.setattr(context, 'context_provider_executor', Executor())
    monkeypatch.setattr(context, '_CONTEXT_PROVIDER_DEADLINE_SECONDS', 0)
    subject.task['abort_event'] = threading.Event()
    messages = [{'role': 'user', 'content': 'Continue'}]
    before = copy.deepcopy(messages)
    outcome = {}
    def admit():
        try:
            outcome['value'] = context.refresh_turn_requirements(messages, task=subject.task, model='test-model')
        except BaseException as error:  # the spec inspects the exact refusal
            outcome['error'] = error
    worker = threading.Thread(target=admit, daemon=True)
    worker.start()
    stop = time.monotonic() + 10
    while (subject.task.get('phase') or {}).get('attempt', 0) < 3:
        assert time.monotonic() < stop and worker.is_alive()
        time.sleep(.01)
    # Repeated local deadlines are storage waiting: no task error, no model
    # attribution, no stale prompt and no duplicate read.
    waiting = subject.task['phase']
    assert waiting['detail'] == 'Waiting for storage' and 'model' not in waiting
    assert not outcome and subject.task.get('error') is None and len(submitted) == 1
    subject.task['abort_event'].set()
    worker.join(5)
    assert not worker.is_alive() and isinstance(outcome.get('error'), AbortedError)
    assert subject.task.get('error') is None
    assert pending.cancelled() and messages == before
