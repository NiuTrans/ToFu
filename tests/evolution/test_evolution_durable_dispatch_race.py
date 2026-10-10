"""Two recoverers compete through production record CAS on each real backend.

Only task submission and the after-binding crash are controlled. Task birth,
run records, ownership checks and dispatch admission use production owners.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.api


@pytest.mark.parametrize('credential_storage', ['sqlite', 'tofudb'], indirect=True,
                         scope='module')
@pytest.mark.parametrize('scenario', ['competing_recoverers', 'cancel_before_admission'])
def test_real_storage_admits_only_one_recovery_submission(credential_storage, monkeypatch, scenario):
    from lib.evolution import runs
    from lib.tasks_pkg import manager, spawn
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.storage import get_storage_client

    # Match the isolated native authority composition; no production identity override.
    from lib.storage.supervisor import _tofudb_scope_ids
    owner = _tofudb_scope_ids()[0]
    created, submitted, bound = [], [], []
    create = manager.create_task
    save = runs._save

    def create_task(*args, **kwargs):
        task = create(*args, **kwargs)
        created.append(task)
        return task

    def crash_after_binding(value, version, user_id):
        result = save(value, version, user_id)
        if value.get('stage_dispatch', {}).get('state') == 'bound':
            bound.append(value['run_id'])
            raise SystemExit('controlled crash after durable binding')
        return result

    monkeypatch.setattr(manager, 'create_task', create_task)
    monkeypatch.setattr(spawn, 'spawn_task', lambda task: submitted.append(task))
    monkeypatch.setattr(runs, 'resolve_run_model', lambda requested, **kwargs: requested)
    monkeypatch.setattr(runs, 'default_task_loader', lambda tid, user_id: next(
        (task for task in created if task['id'] == tid and task['_userId'] == user_id), None))
    monkeypatch.setattr(runs, '_save', crash_after_binding)
    try:
        with pytest.raises(SystemExit):
            runs.submit_run({'objective': 'Preserve the original experiment.'}, user_id=owner)
        assert len(created) == 1 and submitted == [] and len(bound) == 1
        monkeypatch.setattr(runs, '_save', save)
        run_id = bound[0]
        original = runs.get_run(run_id, user_id=owner)
        read = runs._read
        barrier = Barrier(2)

        def read_together(rid, user_id):
            result = read(rid, user_id)
            if result[0].get('stage_dispatch', {}).get('state') == 'bound':
                barrier.wait(timeout=10)
            return result

        if scenario == 'competing_recoverers':
            monkeypatch.setattr(runs, '_read', read_together)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: runs.advance_run(run_id, user_id=owner), range(2)))
            monkeypatch.setattr(runs, '_read', read)
            assert all(result['run_id'] == run_id for result in results)
            expected_status = 'executing'
            expected_submissions = [created[0]]
        else:
            def cancel_before_admission(value, version, user_id):
                if value.get('stage_dispatch', {}).get('state') == 'admitted':
                    runs.cancel_run(run_id, user_id=owner)
                return save(value, version, user_id)
            monkeypatch.setattr(runs, '_save', cancel_before_admission)
            runs.advance_run(run_id, user_id=owner)
            monkeypatch.setattr(runs, '_save', save)
            expected_status = 'cancelled'
            expected_submissions = []
        persisted = runs.get_run(run_id, user_id=owner)
        assert submitted == expected_submissions and len(created) == 1
        assert persisted['status'] == expected_status
        if scenario == 'competing_recoverers':
            assert persisted['stage_dispatch']['state'] == 'admitted'
        assert persisted['task_id'] == original['task_id']
        assert persisted['episode_id'] == original['episode_id']
        assert persisted['attempt'] == original['attempt']
        runs.advance_run(run_id, user_id=owner)
        assert submitted == expected_submissions
        born = get_storage_client().query('task_results.replay_get',
            {'key': created[0]['id'], 'user_id': owner})
        assert born['task_id'] == created[0]['id']
    finally:
        for task in created:
            task['_executionSession'].settle(ExecutionPhase.CANCELLED, cause='test_cleanup')
            manager.discard_task(task['id'], '')
