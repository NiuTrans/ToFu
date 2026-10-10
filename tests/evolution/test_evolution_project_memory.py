"""Exploration task configuration works with real local-memory acquisition."""

import pytest

from tests.evolution.test_evolution_runs import runtime as runtime

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_memory_store(tmp_path, monkeypatch):
    import lib.memory.storage._dirs as dirs

    monkeypatch.setattr(dirs, '_server_data_dir', lambda: str(tmp_path / 'data'))
    dirs._migrated_roots.clear()
    dirs._server_store_migrated = False
    yield
    dirs._migrated_roots.clear()
    dirs._server_store_migrated = False


@pytest.mark.parametrize('memory_enabled', [True, False])
def test_executor_project_config_reaches_memory_prefetch_and_composer(
        runtime, tmp_path, memory_enabled):
    import lib.memory.storage as storage
    from lib.tasks_pkg.context_composer import compose_task_context
    from lib.tasks_pkg.orchestrator._memory_prefetch import maybe_run_memory_prefetch

    project = tmp_path / 'project'
    project.mkdir()
    memory = storage.create_memory(
        'ParserState recovery',
        description='ParserState recovery in lib/parser/state.py',
        body='Preserve rejected recovery paths.', scope='project',
        project_path=str(project))
    runs, _, tasks = runtime
    runs.submit_run(
        {'objective': 'Fix ParserState in lib/parser/state.py',
         'project_path': str(project)}, user_id=123)
    task = tasks[0]
    cfg = task['config']
    # Headless personal memory remains opt-in. Exercise an explicit grant as
    # well as the default-off path without changing the production default.
    assert cfg['memoryEnabled'] is False
    cfg = {**cfg, 'memoryEnabled': memory_enabled}
    task['config'] = cfg
    messages = [{'role': 'user', 'content': 'Fix ParserState in lib/parser/state.py'}]
    maybe_run_memory_prefetch(
        task=task, cfg=cfg, messages=messages, tool_list=None,
        project_path=cfg['projectPath'], project_enabled=cfg['projectEnabled'],
        memory_enabled=memory_enabled, has_real_tools=True, injected_tool_calls=0)
    assert task['_memoryPrefetch']['phase'] == ('done' if memory_enabled else 'skipped')
    expected_ids = [memory['id']] if memory_enabled else []
    assert [row['id'] for row in task['_prefetchedMemories']] == expected_ids
    compose_task_context(
        messages, user_id=123, model='test', task=task,
        project_path=cfg['projectPath'], project_enabled=cfg['projectEnabled'],
        memory_enabled=memory_enabled, has_real_tools=True)
    rendered = '\n'.join(str(m.get('content')) for m in messages)
    assert ('Preserve rejected recovery paths.' in rendered) is memory_enabled
