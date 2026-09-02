"""Framework-free durable orchestration run service contracts."""

from __future__ import annotations

import os

import pytest
from unittest.mock import patch

from lib.orchestration.run_service import (
    RUN_MUTATION_ACTIVE,
    RUN_MUTATION_CONFLICT,
    RUN_MUTATION_NOT_FOUND,
    RUN_MUTATION_PERSISTENCE_FAILED,
    RUN_MUTATION_TERMINAL,
    OrchestrationRunService,
    RunServiceError,
)
from lib.orchestration.run_store_port import (
    ORCHESTRATION_RUN_EVENT_PAGE_LIMIT,
    OrchestrationRunStorePort,
    RunEventPage,
    bind_orchestration_run_store,
)


pytestmark = pytest.mark.unit
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))


class FakePersistence:
    def __init__(self):
        self.runs = {}
        self.events = {}
        self.updated = []
        self.deleted = []
        self.allow_update = True
        self.allow_delete = True

    def new_run_id(self):
        return 'run_new'

    def create_run(self, run_id, **values):
        self.runs[run_id] = dict(
            id=run_id, status='pending', terminal=False, **values)
        return True

    def get_run(self, run_id):
        run = self.runs.get(run_id)
        return dict(run) if run else None

    def list_runs(self, **filters):
        return [dict(run) for run in self.runs.values()
                if not filters.get('status')
                or run['status'] == filters['status']]

    def append_event(self, run_id, seq, event):
        self.events.setdefault(run_id, []).append(dict(event, seq=seq))
        return True

    def project_event(self, run_id, seq, event, status=''):
        if not self.append_event(run_id, seq, event):
            return False
        if not status:
            return True
        return self.update_status(run_id, status)

    def get_events(self, run_id, cursor):
        return [event for event in self.events.get(run_id, [])
                if event['seq'] >= cursor]

    def get_event_page(self, run_id, cursor):
        rows = sorted(
            self.events.get(run_id, []), key=lambda event: event['seq'])
        boundary = rows[-1]['seq'] + 1 if rows else 0
        effective = min(cursor, boundary)
        events = [event for event in rows if event['seq'] >= effective]
        page = events[:ORCHESTRATION_RUN_EVENT_PAGE_LIMIT]
        next_cursor = boundary if len(page) < ORCHESTRATION_RUN_EVENT_PAGE_LIMIT \
            else min(boundary, page[-1]['seq'] + 1)
        return RunEventPage(
            events=page,
            next_cursor=next_cursor,
            cursor_reset=cursor > boundary,
            caught_up=next_cursor >= boundary,
        )

    def update_status(self, run_id, status, **values):
        self.updated.append((run_id, status, values))
        if not self.allow_update:
            return False
        self.runs[run_id].update(status=status,
                                 terminal=status in {'done', 'error', 'aborted'})
        return True

    def delete_run(self, run_id):
        self.deleted.append(run_id)
        if not self.allow_delete:
            return False
        return self.runs.pop(run_id, None) is not None

    def retire_interrupted_runs(self, error):
        retired = 0
        for run in self.runs.values():
            if run.get('terminal'):
                continue
            run.update(status='error', terminal=True, error=error)
            retired += 1
        return retired


def test_store_port_binds_complete_implementations_and_rejects_partial_ones():
    persistence = FakePersistence()
    assert bind_orchestration_run_store(persistence) is persistence

    class LegacyEventStore(FakePersistence):
        get_event_page = None

    with pytest.raises(TypeError, match='get_event_page'):
        OrchestrationRunService(LegacyEventStore())


def test_service_source_consumes_one_explicit_persistence_interface():
    source = open(
        os.path.join(ROOT, 'lib/orchestration/run_service.py'),
        encoding='utf-8',
    ).read()
    port = open(
        os.path.join(ROOT, 'lib/orchestration/run_store_port.py'),
        encoding='utf-8',
    ).read()
    policy = open(
        os.path.join(ROOT, 'lib/orchestration/run_lifecycle_policy.py'),
        encoding='utf-8',
    ).read()
    replay_result = open(
        os.path.join(ROOT, 'lib/orchestration/run_replay_result.py'),
        encoding='utf-8',
    ).read()
    context = open(os.path.join(
        ROOT, 'lib/orchestration/run_service_context.py'),
        encoding='utf-8').read()
    queries = open(os.path.join(
        ROOT, 'lib/orchestration/run_query_service.py'),
        encoding='utf-8').read()
    commands = open(os.path.join(
        ROOT, 'lib/orchestration/run_command_service.py'),
        encoding='utf-8').read()
    mutations = open(os.path.join(
        ROOT, 'lib/orchestration/run_mutation_service.py'),
        encoding='utf-8').read()

    assert 'persistence: OrchestrationRunStorePort,' in source
    assert 'OrchestrationRunStorePort | None' not in source
    assert 'bind_orchestration_run_store(persistence)' in source
    assert "hasattr(self._persistence, 'get_event_page')" not in source
    assert 'self._persistence.get_events(' not in source
    assert 'class OrchestrationRunStorePort(Protocol)' in port
    assert 'def get_event_page(' in port
    assert 'def project_event(' in port
    assert 'class RunLifecycle' in policy
    assert 'def classify_transition(' in policy
    assert 'def abort_precondition(' in policy
    assert 'def delete_precondition(' in policy
    assert 'RunMutationResult(' not in source
    assert 'class DurableRunServiceContext' in context
    assert 'class DurableRunQueryService' in queries
    assert 'class DurableRunCommandService' in commands
    assert 'class DurableRunMutationService' in mutations
    assert 'DurableRunQueryService(' in source
    assert 'DurableRunCommandService(' in source
    assert 'DurableRunMutationService(' in source
    assert 'class RunReplayResult' not in source
    assert 'class RunReplayResult' in replay_result
    assert 'def normalize_run_replay_cursor(' in replay_result
    assert 'def project_run_replay_result(' in replay_result
    assert 'safe_replay_cursor' not in source
    assert 'RunLifecycle.from_run(run)' not in source
    assert 'project_run_replay_result(' in queries
    assert 'is_terminal_run_status' not in source + queries + commands
    assert 'orchestration_dependency_call(' in context
    assert 'normalize_envelope(' in context
    assert len(source.splitlines()) < 160
    assert len(queries.splitlines()) < 90
    assert len(commands.splitlines()) < 110
    assert len(mutations.splitlines()) < 110
    assert len(policy.splitlines()) < 220


def test_service_requires_an_explicit_owner_bound_persistence_port():
    with pytest.raises(TypeError):
        OrchestrationRunService()
    source = open(
        os.path.join(ROOT, 'lib/orchestration/run_service.py'),
        encoding='utf-8',
    ).read()
    assert 'database_run_store' not in source
    assert not os.path.exists(os.path.join(ROOT, 'lib/orchestration_runs.py'))


def test_create_list_update_and_replay_share_one_interface():
    persistence = FakePersistence()
    service = OrchestrationRunService(persistence)
    run_id = service.new_id()
    assert service.create(
        run_id, definition={'nodes': []}, input_text='go') is True
    assert service.append_event(run_id, 0, {'type': 'start'})
    assert service.append_event(run_id, 2, {'type': 'done'})

    replay = service.replay(run_id, -10)
    assert replay is not None
    assert replay.next_cursor == 3
    assert replay.payload() == {
        'format': 'tofu.task-replay/v1',
        'ok': True,
        'events': [
            {'type': 'start', 'seq': 0},
            {'type': 'done', 'seq': 2},
        ],
        'next_cursor': 3,
        'status': 'pending',
        'done': False,
        'cursor': {'requested': 0, 'next': 3, 'reset': False},
        'caught_up': True,
    }
    assert service.list(status='pending')[0]['id'] == run_id
    assert service.update_status(run_id, 'done', final='ok')


def test_event_and_nonterminal_header_share_one_service_command():
    persistence = FakePersistence()
    service = OrchestrationRunService(persistence)
    service.create('run', definition={'nodes': []})

    assert service.project_event(
        'run', 0, {'type': 'flow_start'}, 'running')
    assert persistence.events['run'] == [
        {'type': 'flow_start', 'seq': 0},
    ]
    assert persistence.runs['run']['status'] == 'running'

    with pytest.raises(RunServiceError, match='invalid orchestration run status'):
        service.project_event(
            'run', 1, {'type': 'flow_complete'}, 'dnne')
    assert len(persistence.events['run']) == 1


def test_replay_resets_future_cursor_so_subsequent_events_are_delivered():
    persistence = FakePersistence()
    service = OrchestrationRunService(persistence)
    service.create('run', definition={'nodes': []})
    service.append_event('run', 0, {'type': 'start'})

    corrected = service.replay('run', 500)
    assert corrected is not None
    assert corrected.payload()['cursor'] == {
        'requested': 500, 'next': 1, 'reset': True,
    }

    service.append_event('run', 1, {'type': 'progress'})
    resumed = service.replay('run', corrected.next_cursor)
    assert resumed is not None
    assert [event['type'] for event in resumed.events] == ['progress']
    assert resumed.payload()['cursor'] == {
        'requested': 1, 'next': 2, 'reset': False,
    }


def test_terminal_replay_reuses_header_read_without_enriching_active_pages():
    persistence = FakePersistence()
    service = OrchestrationRunService(persistence)
    service.create('run', definition={'nodes': []})

    active = service.replay('run')
    assert active is not None
    assert 'run' not in active.payload()

    persistence.runs['run'].update(
        status='done', terminal=True, final='complete', error=None,
    )
    terminal = service.replay('run')
    assert terminal is not None
    terminal_run = terminal.payload()['run']
    assert terminal_run['id'] == 'run'
    assert terminal_run['status'] == 'done'
    assert terminal_run['terminal'] is True
    assert terminal_run['final'] == 'complete'
    assert terminal_run['outcome']['category'] == 'success'


def test_terminal_replay_drains_bounded_event_pages_before_final_snapshot():
    persistence = FakePersistence()
    service = OrchestrationRunService(persistence)
    service.create('run', definition={'nodes': []})
    for sequence in range(ORCHESTRATION_RUN_EVENT_PAGE_LIMIT + 1):
        service.append_event('run', sequence, {'type': 'step_delta'})
    persistence.runs['run'].update(status='done', terminal=True)

    first = service.replay('run', 0)
    assert first is not None
    assert len(first.events) == ORCHESTRATION_RUN_EVENT_PAGE_LIMIT
    assert first.done is True
    assert first.caught_up is False
    assert 'run' not in first.payload()

    final = service.replay('run', first.next_cursor)
    assert final is not None
    assert len(final.events) == 1
    assert final.done is True
    assert final.caught_up is True
    assert final.payload()['run']['id'] == 'run'


def test_terminal_replay_publishes_snapshot_on_an_exact_full_final_page():
    persistence = FakePersistence()
    service = OrchestrationRunService(persistence)
    service.create('run', definition={'nodes': []})
    for sequence in range(ORCHESTRATION_RUN_EVENT_PAGE_LIMIT):
        service.append_event('run', sequence, {'type': 'step_delta'})
    persistence.runs['run'].update(status='done', terminal=True)

    page = service.replay('run', 0)

    assert page is not None
    assert len(page.events) == ORCHESTRATION_RUN_EVENT_PAGE_LIMIT
    assert page.caught_up is True
    assert page.payload()['run']['id'] == 'run'


def test_get_and_list_publish_explicit_terminal_outcomes():
    from lib.orchestration.outcome_domain import classify_terminal_outcome

    persistence = FakePersistence()
    incomplete = classify_terminal_outcome(
        'completed', reported_ok=False,
        reported_stop_reason='max_iterations')
    persistence.runs.update({
        'done': {
            'id': 'done', 'status': 'done', 'terminal': True, 'error': None,
        },
        'incomplete': {
            'id': 'incomplete', 'status': 'error', 'terminal': True,
            'error': incomplete.error_envelope,
        },
        'active': {
            'id': 'active', 'status': 'running', 'terminal': False,
        },
    })
    service = OrchestrationRunService(persistence)

    assert service.get('done')['outcome']['category'] == 'success'
    assert service.get('incomplete')['outcome']['category'] == 'incomplete'
    rows = {run['id']: run for run in service.list()}
    assert rows['done']['outcome']['format'] == \
        'tofu.orchestration.outcome/v1'
    assert rows['incomplete']['outcome']['finish_reason'] == 'incomplete'
    assert 'outcome' not in rows['active']


def test_get_list_and_terminal_replay_share_backend_layout_projection():
    persistence = FakePersistence()
    persistence.runs['legacy'] = {
        'id': 'legacy', 'status': 'done', 'terminal': True,
        'definition': {
            'nodes': [
                {'id': 'start', 'kind': 'start'},
                {'id': 'worker'},
                {'id': 'stop', 'kind': 'stop'},
            ],
            'edges': [
                {'from': 'start', 'to': 'worker'},
                {'from': 'worker', 'to': 'stop'},
            ],
        },
    }
    service = OrchestrationRunService(persistence)

    direct = service.get('legacy')['definition']
    listed = service.list()[0]['definition']
    replayed = service.replay('legacy').payload()['run']['definition']

    expected = [(40, 30), (40, 180), (40, 330)]
    for definition in (direct, listed, replayed):
        assert [
            (node['pos']['x'], node['pos']['y'])
            for node in definition['nodes']
        ] == expected
    assert all('pos' not in node for node in
               persistence.runs['legacy']['definition']['nodes'])


def test_create_propagates_persistence_failure():
    persistence = FakePersistence()
    persistence.create_run = lambda _run_id, **_values: False
    service = OrchestrationRunService(persistence)

    assert service.create('run', definition={'nodes': []}) is False


def test_create_new_owns_id_allocation_and_header_persistence():
    persistence = FakePersistence()
    run_id = OrchestrationRunService(persistence).create_new(
        definition={'nodes': []}, input_text='go', orch_id='orch_1')

    assert run_id == 'run_new'
    assert persistence.runs[run_id]['input_text'] == 'go'
    assert persistence.runs[run_id]['orch_id'] == 'orch_1'


def test_persistence_exceptions_share_one_service_error_boundary():
    persistence = FakePersistence()
    service = OrchestrationRunService(persistence)
    persistence.new_run_id = lambda: (_ for _ in ()).throw(OSError('offline'))
    with pytest.raises(RunServiceError, match='allocate'):
        service.new_id()

    persistence = FakePersistence()
    service = OrchestrationRunService(persistence)
    persistence.create_run = lambda *_args, **_values: (
        (_ for _ in ()).throw(OSError('offline')))
    with pytest.raises(RunServiceError, match='create run'):
        service.create('run', definition={})

    persistence = FakePersistence()
    service = OrchestrationRunService(persistence)
    persistence.append_event = lambda *_args: (
        (_ for _ in ()).throw(OSError('offline')))
    with pytest.raises(RunServiceError, match='append event'):
        service.append_event('run', 3, {'type': 'flow_start'})

    persistence.runs['done'] = {
        'id': 'done', 'status': 'done', 'terminal': True,
    }
    persistence.delete_run = lambda _run_id: (
        (_ for _ in ()).throw(OSError('offline')))
    with pytest.raises(RunServiceError, match='delete run'):
        service.delete('done')


def test_status_transition_classifies_commit_retry_race_and_failure():
    persistence = FakePersistence()
    service = OrchestrationRunService(persistence)
    persistence.runs['run'] = {
        'id': 'run', 'status': 'running', 'terminal': False,
    }

    committed = service.transition_status('run', 'done', final='answer')
    assert committed.ok and committed.run_status == 'done'

    # An exact terminal retry is already satisfied even when the storage
    # primitive reports no changed row.
    persistence.runs['run']['final'] = 'answer'
    persistence.allow_update = False
    retried = service.transition_status('run', 'done', final='answer')
    assert retried.ok and retried.run_status == 'done'

    persistence.runs['run'].update(status='aborted', terminal=True)
    raced = service.transition_status('run', 'done', final='answer')
    assert not raced.ok and raced.reason == RUN_MUTATION_CONFLICT
    assert raced.run_status == 'aborted'

    persistence.runs['active'] = {
        'id': 'active', 'status': 'running', 'terminal': False,
    }
    failed = service.transition_status('active', 'paused')
    assert not failed.ok
    assert failed.reason == RUN_MUTATION_PERSISTENCE_FAILED


def test_terminal_error_writes_share_one_closed_envelope_boundary():
    persistence = FakePersistence()
    persistence.runs['run'] = {
        'id': 'run', 'status': 'running', 'terminal': False,
    }
    service = OrchestrationRunService(persistence)

    assert service.update_status('run', 'error', error={
        'kind': 'future_runtime_kind', 'message': 'worker failed',
    }) is True

    stored = persistence.updated[-1][2]['error']
    assert stored['kind'] == 'generic'
    assert stored['message'] == 'worker failed'
    assert stored['context'] == 'run status update'
    assert stored['source'] == 'orchestration:run-service'

    persistence.allow_update = False
    persistence.runs['run'].update(
        status='error', terminal=True, error=stored)
    retry = service.transition_status('run', 'error', error=stored)
    assert retry.ok and retry.run_status == 'error'


def test_status_transition_wraps_backend_exceptions():
    persistence = FakePersistence()
    persistence.runs['run'] = {
        'id': 'run', 'status': 'running', 'terminal': False,
    }
    persistence.update_status = lambda *_args, **_values: (
        (_ for _ in ()).throw(OSError('database offline')))

    with pytest.raises(RunServiceError):
        OrchestrationRunService(persistence).transition_status('run', 'done')


def test_service_rejects_unknown_status_before_persistence():
    persistence = FakePersistence()
    persistence.runs['run'] = {
        'id': 'run', 'status': 'running', 'terminal': False,
    }
    service = OrchestrationRunService(persistence)

    with pytest.raises(RunServiceError, match='invalid orchestration run status'):
        service.update_status('run', 'dnne')
    with pytest.raises(RunServiceError, match='invalid orchestration run status'):
        service.transition_status('run', 'dnne')
    with pytest.raises(RunServiceError, match='invalid orchestration run status'):
        service.list(status='dnne')
    assert persistence.updated == []


def test_startup_recovery_retires_only_nonterminal_runs():
    persistence = FakePersistence()
    persistence.runs.update({
        'pending': {'id': 'pending', 'status': 'pending', 'terminal': False},
        'paused': {'id': 'paused', 'status': 'paused', 'terminal': False},
        'done': {'id': 'done', 'status': 'done', 'terminal': True},
    })
    reason = {'kind': 'worker_lost', 'message': 'server restarted'}

    retired = OrchestrationRunService(persistence).retire_interrupted(
        error=reason)

    assert retired == 2
    assert persistence.runs['pending']['status'] == 'error'
    error = persistence.runs['paused']['error']
    assert error['kind'] == 'worker_lost'
    assert error['message'] == 'server restarted'
    assert error['context'] == 'run startup recovery'
    assert error['source'] == 'orchestration:run-service'
    assert persistence.runs['done']['status'] == 'done'


def test_startup_recovery_does_not_collapse_storage_failure_to_zero():
    persistence = FakePersistence()
    persistence.retire_interrupted_runs = lambda _error: None
    with pytest.raises(RunServiceError):
        OrchestrationRunService(persistence).retire_interrupted(
            error='server restarted')


def test_read_failures_do_not_masquerade_as_empty_or_missing():
    persistence = FakePersistence()
    persistence.get_run = lambda _run_id: (_ for _ in ()).throw(
        OSError('database offline'))
    service = OrchestrationRunService(persistence)
    with pytest.raises(RunServiceError):
        service.get('run')
    with pytest.raises(RunServiceError):
        service.replay('run')

    persistence = FakePersistence()
    persistence.list_runs = lambda **_filters: (_ for _ in ()).throw(
        OSError('database offline'))
    with pytest.raises(RunServiceError):
        OrchestrationRunService(persistence).list()

    persistence = FakePersistence()
    persistence.runs['run'] = {
        'id': 'run', 'status': 'running', 'terminal': False,
    }
    persistence.get_event_page = lambda *_args: (_ for _ in ()).throw(
        OSError('database offline'))
    with pytest.raises(RunServiceError):
        OrchestrationRunService(persistence).replay('run')


def test_run_projection_defects_are_not_mislabeled_as_storage_failures():
    persistence = FakePersistence()
    persistence.runs['run'] = {
        'id': 'run', 'status': 'done', 'terminal': True,
    }
    with patch(
        'lib.orchestration.run_service.project_run_header_outcome',
        side_effect=RuntimeError('projection bug'),
    ):
        service = OrchestrationRunService(persistence)
        with pytest.raises(RuntimeError, match='projection bug'):
            service.get('run')
        with pytest.raises(RuntimeError, match='projection bug'):
            service.list()


def test_abort_distinguishes_missing_terminal_success_and_fenced_race():
    persistence = FakePersistence()
    aborts = []

    class RuntimeMutations:
        @staticmethod
        def abort(run_id):
            aborts.append(run_id)
            return type('Result', (), {'ok': True})()

    service = OrchestrationRunService(
        persistence, runtime_mutation=RuntimeMutations())

    missing = service.abort('missing')
    assert missing.reason == RUN_MUTATION_NOT_FOUND
    assert aborts == []

    persistence.runs['done'] = {
        'id': 'done', 'status': 'done', 'terminal': True,
    }
    terminal = service.abort('done')
    assert terminal.reason == RUN_MUTATION_TERMINAL
    assert terminal.run_status == 'done'
    assert aborts == []

    persistence.runs['live'] = {
        'id': 'live', 'status': 'running', 'terminal': False,
    }
    accepted = service.abort('live')
    assert accepted.ok and accepted.run_status == 'aborted'
    assert aborts == ['live']

    persistence.runs['race'] = {
        'id': 'race', 'status': 'running', 'terminal': False,
    }
    persistence.allow_update = False
    failed = service.abort('race')
    assert not failed.ok and failed.reason == RUN_MUTATION_PERSISTENCE_FAILED
    assert failed.run_status == 'running'

    persistence.runs['terminal-race'] = {
        'id': 'terminal-race', 'status': 'running', 'terminal': False,
    }

    def finish_during_abort(run_id, _status, **_values):
        persistence.runs[run_id].update(status='done', terminal=True)
        return False

    persistence.update_status = finish_during_abort
    raced = service.abort('terminal-race')
    assert not raced.ok and raced.reason == RUN_MUTATION_CONFLICT
    assert raced.run_status == 'done'


def test_abort_runtime_dependency_is_injected_at_composition_time():
    persistence = FakePersistence()
    persistence.runs['live'] = {
        'id': 'live', 'status': 'running', 'terminal': False,
    }
    class RuntimeMutations:
        def __init__(self):
            self.aborts = []

        def abort(self, run_id):
            self.aborts.append(run_id)
            return type('Result', (), {'ok': True})()

    mutations = RuntimeMutations()
    service = OrchestrationRunService(
        persistence, runtime_mutation=mutations)

    result = service.abort('live')

    assert result.ok and result.run_status == 'aborted'
    assert mutations.aborts == ['live']


def test_abort_does_not_commit_when_shared_runtime_mutation_is_rejected():
    persistence = FakePersistence()
    persistence.runs['live'] = {
        'id': 'live', 'status': 'running', 'terminal': False,
    }

    class RuntimeMutations:
        @staticmethod
        def abort(_run_id):
            return type('Result', (), {'ok': False})()

    result = OrchestrationRunService(
        persistence, runtime_mutation=RuntimeMutations()).abort('live')

    assert result.reason == RUN_MUTATION_CONFLICT
    assert persistence.updated == []


def test_active_abort_without_runtime_dependency_is_configuration_error():
    persistence = FakePersistence()
    persistence.runs['live'] = {
        'id': 'live', 'status': 'running', 'terminal': False,
    }

    with pytest.raises(
        RunServiceError, match='runtime abort dependency is unavailable',
    ):
        OrchestrationRunService(persistence).abort('live')


def test_delete_distinguishes_absence_from_persistence_failure():
    persistence = FakePersistence()
    service = OrchestrationRunService(persistence)
    assert service.delete('missing').reason == RUN_MUTATION_NOT_FOUND

    persistence.runs['live'] = {
        'id': 'live', 'status': 'running', 'terminal': False,
    }
    active = service.delete('live')
    assert active.reason == RUN_MUTATION_ACTIVE
    assert active.run_status == 'running'
    assert persistence.deleted == []

    persistence.runs['run'] = {
        'id': 'run', 'status': 'error', 'terminal': True,
    }
    persistence.allow_delete = False
    failed = service.delete('run')
    assert failed.reason == RUN_MUTATION_PERSISTENCE_FAILED
    persistence.allow_delete = True
    assert service.delete('run').ok


def test_canonical_terminal_status_survives_legacy_missing_flag():
    persistence = FakePersistence()
    persistence.runs['legacy'] = {'id': 'legacy', 'status': 'done'}
    service = OrchestrationRunService(persistence)

    replay = service.replay('legacy')
    assert replay is not None and replay.done
    assert service.abort('legacy').reason == RUN_MUTATION_TERMINAL
    assert service.delete('legacy').ok


def test_http_adapter_uses_run_service_instead_of_direct_persistence_calls():
    run_service = open(os.path.join(
        ROOT, 'lib/orchestration/run_service.py'), encoding='utf-8').read()
    route = open(os.path.join(
        ROOT, 'routes/api_v1/orchestrations.py'), encoding='utf-8').read()
    task_routes = open(os.path.join(
        ROOT, 'routes/api_v1/orchestration_task_routes.py'),
        encoding='utf-8',
    ).read()
    mutation_routes = open(os.path.join(
        ROOT, 'routes/api_v1/orchestration_mutation_routes.py'),
        encoding='utf-8',
    ).read()
    task_http = open(os.path.join(
        ROOT, 'routes/api_v1/orchestration_task_http.py'),
        encoding='utf-8',
    ).read()
    runtime_start = open(os.path.join(
        ROOT, 'lib/orchestration/runtime_start_service.py'),
        encoding='utf-8',
    ).read()
    runtime_start_http = open(os.path.join(
        ROOT, 'routes/api_v1/orchestration_runtime_start_http.py'),
        encoding='utf-8',
    ).read()
    assert 'def _run_instances() -> OrchestrationRunService' in route
    assert route.count('run_service=_services.runs') == 2
    assert 'def _run_provider() -> OrchestrationRunService' in route
    assert 'return _run_instances()' in route
    assert 'run_service=lambda:' not in route
    assert 'from lib import orchestration_runs as runs' not in task_routes
    assert 'run_service().replay(run_id, cursor)' in task_routes
    assert 'run_service().abort(run_id)' in mutation_routes
    assert 'runtime.abort' not in mutation_routes
    assert 'runtime_mutation=_services.runtime_mutations(ctx.owner_user_id)' \
        in route
    assert 'runtime_abort' not in run_service
    assert 'run_service().delete(run_id)' in mutation_routes
    assert 'runs.create_new(' in runtime_start
    assert "runtime_start_request_response(\n            " \
        "'api_v1.orchestrations.start_task'" in task_routes
    assert 'runtime_start_service().start(' in runtime_start_http
    assert 'def durable_run_service_call(' not in task_http
    assert task_routes.count('orchestration_service_response(') == 3
    assert mutation_routes.count(
        'orchestration_mutation_service_response(') == 5
    assert "@orchestration_route(blueprint, 'task-create')" in task_routes
    assert "@orchestration_route(blueprint, 'task-list')" in task_routes
    assert "'/api/v1/orchestrations/tasks" not in route
    server = open(os.path.join(ROOT, 'lib/server_assembly.py'), encoding='utf-8').read()
    assert 'retire_interrupted_orchestration_runs(' in server
