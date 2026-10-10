"""Admission outages preserve settings and only tighten original limits."""
from copy import deepcopy
import hashlib
import time

import pytest

from tests.evolution.test_evolution_runs import runtime as runtime, OWNER

pytestmark = pytest.mark.unit


def _pending(runtime, monkeypatch, tmp_path, *, configure=None):
    from lib.tasks_pkg import manager
    from lib.evolution.stage_launch import read_launch
    runs, _store, tasks = runtime
    project = tmp_path / 'project'
    project.mkdir()
    original_create = manager.create_task
    original_config = runs._task_config
    expected = {}

    def configuration(run, stage):
        result = original_config(run, stage)
        if configure is not None:
            configure(result)
        expected.update(deepcopy(result))
        return result

    monkeypatch.setattr(runs, '_task_config', configuration)
    monkeypatch.setattr(manager, 'create_task', lambda *_a, **_kw:
                        (_ for _ in ()).throw(RuntimeError('controlled admission outage')))
    with pytest.raises(RuntimeError, match='controlled admission outage'):
        runs.submit_run({'objective': 'Keep the complete original request.',
                         'project_path': str(project),
                         'budget': {'max_tokens': 20000, 'max_tool_calls': 100,
                                    'max_wall_clock_seconds': 3600},
                         'review_reserve': {'tokens': 1000, 'tool_calls': 10,
                                            'wall_clock_seconds': 30}},
                        user_id=OWNER, idempotency_key='configuration-outage')
    run = runs.get_run('run-' + hashlib.sha256(
                       b'configuration-outage').hexdigest()[:32], user_id=OWNER)
    assert run['status'] == 'preparing' and not tasks
    intent = read_launch(run['stage_launch'], run_id=run['run_id'], user_id=OWNER)
    monkeypatch.setattr(manager, 'create_task', original_create)
    monkeypatch.setattr(runs, '_task_config', original_config)
    return run, intent, expected


def test_source_upgrade_cannot_widen_prepared_project_or_tools(runtime, monkeypatch, tmp_path):
    runs, _store, tasks = runtime
    run, intent, expected = _pending(runtime, monkeypatch, tmp_path)
    old_factory = runs._task_config

    def broader_configuration(current, stage):
        config = old_factory(current, stage)
        config.update(projectPath=str(tmp_path), projectPaths=[str(tmp_path)],
                      browserEnabled=True, desktopEnabled=True, searchMode='off',
                      fetchEnabled=False, humanGuidanceEnabled=False)
        return config

    monkeypatch.setattr(runs, '_task_config', broader_configuration)
    resumed = runs.advance_run(run['run_id'], user_id=OWNER)
    assert resumed['status'] == 'executing' and len(tasks) == 1
    config = tasks[0]['config']
    assert config['projectPath'] == expected['projectPath']
    assert config['projectPaths'] == expected['projectPaths']
    assert config['searchMode'] == expected['searchMode']
    assert config['fetchEnabled'] == expected['fetchEnabled']
    assert config['humanGuidanceEnabled'] == expected['humanGuidanceEnabled']
    assert not config.get('browserEnabled') and not config.get('desktopEnabled')
    assert tasks[0]['messages'][0]['content'] == intent['prompt']


def test_prepared_limits_never_refresh_upward(runtime, monkeypatch, tmp_path):
    runs, _store, tasks = runtime

    def smaller(config):
        config['executionLimits'].update(max_tokens=500, max_tool_calls=4)

    run, _intent, _expected = _pending(runtime, monkeypatch, tmp_path, configure=smaller)
    runs.advance_run(run['run_id'], user_id=OWNER)
    assert tasks[0]['config']['executionLimits']['max_tokens'] == 500
    assert tasks[0]['config']['executionLimits']['max_tool_calls'] == 4


def test_current_consumption_only_tightens_the_saved_limits(runtime, monkeypatch, tmp_path):
    runs, _store, tasks = runtime

    def ceilings(config):
        config['executionLimits'].update(max_tokens=2000, max_tool_calls=10,
                                          deadline=time.time() + 120)

    run, intent, _expected = _pending(runtime, monkeypatch, tmp_path, configure=ceilings)
    current, version = runs._read(run['run_id'], OWNER)
    current['actuals'].update(tokens=17500, tool_calls=85)
    runs._save(current, version, OWNER)
    runs.advance_run(run['run_id'], user_id=OWNER)
    limits = tasks[0]['config']['executionLimits']
    assert limits['max_tokens'] == 1500 and limits['max_tool_calls'] == 5
    assert limits['deadline'] == intent['task_config']['executionLimits']['deadline']
    assert tasks[0]['_invocationDeadline'] - time.monotonic() < 121


@pytest.mark.parametrize('corruption', ['changed', 'downgrade'])
def test_changed_or_removed_configuration_refuses_dispatch(runtime, monkeypatch, tmp_path, corruption):
    from lib.evolution import stage_launch
    runs, store, tasks = runtime
    run, _intent, _expected = _pending(runtime, monkeypatch, tmp_path)
    key = (stage_launch.NAMESPACE, runs.records.entity_key(
        run['stage_launch']['intent_id'], OWNER, kind='stage launch'))
    if corruption == 'changed':
        store.rows[key]['task_config']['projectPaths'] = [str(tmp_path)]
    else:
        for name in ('task_config', 'task_config_sha256', 'launch_epoch_id'):
            store.rows[key].pop(name)
    refused = runs.advance_run(run['run_id'], user_id=OWNER)
    assert refused['status'] == 'needs_attention' and not tasks
    assert 'stage_launch_recovery_refused' in refused['error']


def test_hook_cannot_expand_the_frozen_project_before_submission(runtime, monkeypatch, tmp_path):
    from lib.tasks_pkg import manager
    runs, _store, tasks = runtime
    real_create = manager.create_task

    def expanded(*args, **kwargs):
        task = real_create(*args, **kwargs)
        task['config']['browserEnabled'] = True
        return task

    monkeypatch.setattr(manager, 'create_task', expanded)
    with pytest.raises(runs.EvolutionStateError, match='permissions differ'):
        runs.submit_run({'objective': 'Do not broaden hook permissions.'}, user_id=OWNER)
    assert not tasks


def test_owner_model_change_creates_linked_successor_without_new_permissions(runtime, monkeypatch, tmp_path):
    from lib.evolution.role_models import update_role_model
    from lib.evolution.stage_launch import read_launch
    from types import SimpleNamespace
    runs, _store, tasks = runtime
    run, intent, _expected = _pending(runtime, monkeypatch, tmp_path)
    model = {'creator_id': 'example', 'model_id': 'approved-model'}
    monkeypatch.setattr('lib.evolution.role_models.model_options', lambda **_kw: [{'model': model}])
    monkeypatch.setattr('lib.model_routing.mint_routed_slot_group', lambda *_a, **_kw:
                        SimpleNamespace(pin_id='approved-model-test', candidates=[]))
    changed = update_role_model(run['run_id'], {'role': 'executor', 'model': model}, user_id=OWNER)
    repeated = update_role_model(run['run_id'], {'role': 'executor', 'model': model}, user_id=OWNER)
    assert repeated['stage_launch'] == changed['stage_launch']
    assert changed['stage_launch']['intent_id'] != run['stage_launch']['intent_id']
    successor = read_launch(changed['stage_launch'], run_id=run['run_id'], user_id=OWNER)
    assert successor['supersedes'] == run['stage_launch']
    assert successor['model_change_request'] == {'role': 'executor', 'model': model}
    assert successor['prompt'] == intent['prompt']
    expected = deepcopy(intent['task_config'])
    expected.update(modelRef=model, model=model['model_id'])
    assert successor['task_config'] == expected
    # Repeated preparation must reuse the explicit owner successor, including
    # when a cold preparer shares the first acknowledged launch epoch.
    current, version = runs._read(run['run_id'], OWNER)
    runs._prepare_stage(current, version, OWNER, stage='executing')
    assert runs.get_run(run['run_id'], user_id=OWNER)['stage_launch'] == changed['stage_launch']
    runs.advance_run(run['run_id'], user_id=OWNER)
    assert tasks[0]['config']['modelRef'] == model
    original = read_launch(run['stage_launch'], run_id=run['run_id'], user_id=OWNER)
    assert original == intent


def test_snapshot_does_not_share_mutable_configuration_with_live_task(runtime, monkeypatch, tmp_path):
    from lib.evolution.stage_launch import read_launch
    runs, _store, tasks = runtime
    run, intent, _expected = _pending(runtime, monkeypatch, tmp_path)
    runs.advance_run(run['run_id'], user_id=OWNER)
    tasks[0]['config']['projectPaths'].append(str(tmp_path))
    assert read_launch(run['stage_launch'], run_id=run['run_id'], user_id=OWNER) == intent


def test_legacy_prompt_only_request_retains_its_existing_protocol(runtime, monkeypatch, tmp_path):
    from lib.evolution import stage_launch
    runs, store, tasks = runtime
    run, intent, _expected = _pending(runtime, monkeypatch, tmp_path)
    key = (stage_launch.NAMESPACE, runs.records.entity_key(
        run['stage_launch']['intent_id'], OWNER, kind='stage launch'))
    for field in ('task_config', 'task_config_sha256', 'launch_epoch_id'):
        store.rows[key].pop(field)
    current, version = runs._read(run['run_id'], OWNER)
    current['stage_launch'].pop('task_config_sha256')
    runs._save(current, version, OWNER)
    resumed = runs.advance_run(run['run_id'], user_id=OWNER)
    assert resumed['status'] == 'executing' and len(tasks) == 1
    assert tasks[0]['messages'][0]['content'] == intent['prompt']
    assert tasks[0]['config']['projectPath'] == run['project_path']


def test_inline_secret_configuration_is_refused_before_task_allocation(runtime, monkeypatch):
    from lib.tasks_pkg import manager
    runs, _store, tasks = runtime
    original = runs._task_config

    def with_secret(run, stage):
        configuration = original(run, stage)
        configuration['custom'] = {'authorization': 'synthetic credential'}
        return configuration

    monkeypatch.setattr(runs, '_task_config', with_secret)
    monkeypatch.setattr(manager, 'create_task', lambda *_a, **_kw: pytest.fail('must not allocate'))
    with pytest.raises(runs.EvolutionStateError, match='inline credentials'):
        runs.submit_run({'objective': 'Keep credentials outside the archive.'}, user_id=OWNER)
    assert not tasks


def test_expired_frozen_deadline_refuses_before_task_allocation(runtime, monkeypatch, tmp_path):
    runs, _store, tasks = runtime
    observed = time.time()
    run, intent, _expected = _pending(runtime, monkeypatch, tmp_path,
        configure=lambda config: config['executionLimits'].update(deadline=observed + 10))
    monkeypatch.setattr(runs.time, 'time', lambda: observed + 20)
    refused = runs.advance_run(run['run_id'], user_id=OWNER)
    assert refused['status'] == 'failed' and not tasks
    assert refused['error'] == 'frozen_execution_deadline_exhausted'
    assert refused['stage_launch'] == run['stage_launch']
    assert intent['task_config']['executionLimits']['deadline'] == observed + 10


def test_hook_cannot_relabel_the_bound_model(runtime, monkeypatch):
    from lib.tasks_pkg import manager
    from types import SimpleNamespace
    runs, _store, tasks = runtime
    monkeypatch.setattr('lib.model_routing.mint_routed_slot_group', lambda *_a, **_kw:
                        SimpleNamespace(pin_id='bound-model-test', candidates=[]))
    real_create = manager.create_task

    def relabeled(*args, **kwargs):
        task = real_create(*args, **kwargs)
        task['config']['model'] = 'different-model'
        return task

    monkeypatch.setattr(manager, 'create_task', relabeled)
    with pytest.raises(runs.EvolutionStateError, match='task route binding'):
        runs.submit_run({'objective': 'Preserve the approved model selection.',
                         'model': {'creator_id': 'example', 'model_id': 'approved-model'}},
                        user_id=OWNER)
    assert not tasks


@pytest.mark.parametrize('ack_lost', [False, True])
def test_model_successor_storage_failure_keeps_committed_authority(runtime, monkeypatch, tmp_path, ack_lost):
    from lib.evolution import stage_launch
    from lib.evolution.role_models import update_role_model
    from lib.storage.errors import StorageError
    runs, _store, tasks = runtime
    run, _intent, _expected = _pending(runtime, monkeypatch, tmp_path)
    model = {'creator_id': 'example', 'model_id': 'approved-model'}
    monkeypatch.setattr('lib.evolution.role_models.model_options', lambda **_kw: [{'model': model}])
    original_put = stage_launch.records.put_record

    def interrupted(namespace, *args, **kwargs):
        if namespace == stage_launch.NAMESPACE:
            if ack_lost:
                original_put(namespace, *args, **kwargs)
            raise StorageError('database_unavailable', 'controlled successor outage')
        return original_put(namespace, *args, **kwargs)

    monkeypatch.setattr(stage_launch.records, 'put_record', interrupted)
    with pytest.raises(StorageError):
        update_role_model(run['run_id'], {'role': 'executor', 'model': model}, user_id=OWNER)
    preserved = runs.get_run(run['run_id'], user_id=OWNER)
    assert preserved['stage_launch'] == run['stage_launch']
    assert preserved.get('role_models') == run.get('role_models')
    assert not tasks
    monkeypatch.setattr(stage_launch.records, 'put_record', original_put)
    changed = update_role_model(run['run_id'], {'role': 'executor', 'model': model}, user_id=OWNER)
    successor = stage_launch.read_launch(changed['stage_launch'], run_id=run['run_id'], user_id=OWNER)
    assert successor['supersedes'] == run['stage_launch']
    assert successor['task_config']['modelRef'] == model
    assert not tasks
