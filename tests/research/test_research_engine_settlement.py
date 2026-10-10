"""Research worker settlement follows generic task authority, including Stop races."""
import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('raises', [False, True])
def test_stop_during_recipe_never_publishes_success(tmp_path, monkeypatch, raises):
    from lib.research import engine, recipe, runtime
    task_id = runtime._research_task_id()
    task = runtime._new_research_task(
        task_id, direction='synthetic research', workdir=str(tmp_path),
        lang='en', user_id=1)
    manifests = []
    monkeypatch.setattr(engine, '_write_manifest',
                        lambda task, state: manifests.append(state))

    def build(*args, **kwargs):
        runtime._research_runtime.abort(task_id)
        if raises:
            raise RuntimeError('cooperative cancellation')
        return {'accepted': [{'id': 'late'}], 'rejected': [], 'corpus_size': 1}

    monkeypatch.setattr(recipe, 'build_research_from_direction', build)
    engine.run_research_task(task)
    assert task['status'] == 'aborted'
    assert task['result'] is None
    assert manifests == ['running', 'aborted']
    assert not any(event.get('type') == 'final' for event in task['events'])


def test_already_settled_worker_cannot_restart(tmp_path, monkeypatch):
    from lib.research import engine, recipe, runtime
    task_id = runtime._research_task_id()
    task = runtime._new_research_task(
        task_id, direction='settled research', workdir=str(tmp_path), lang='en', user_id=1)
    runtime._research_runtime.abort(task_id)
    runtime._research_runtime.finish(task_id)
    monkeypatch.setattr(recipe, 'build_research_from_direction',
                        lambda *args, **kwargs: pytest.fail('settled job restarted'))
    monkeypatch.setattr(engine, '_write_manifest',
                        lambda *args: pytest.fail('settled manifest overwritten'))
    engine.run_research_task(task)
    assert task['status'] == 'aborted'
