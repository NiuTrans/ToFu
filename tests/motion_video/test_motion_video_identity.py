"""Film/asset input discriminators never replace the runtime's public kind."""

import json

import pytest

from lib.production.runtime import ProductionRuntime
from lib.production.work_unit import project_work_unit

pytestmark = pytest.mark.unit


@pytest.fixture
def motion_identity(tmp_path, monkeypatch):
    from lib.motion_video import runtime

    production = ProductionRuntime('motion-video', id_prefix='motion',
                                   ttl=3600, push_channel=None)
    monkeypatch.setattr(runtime, '_production', production)
    monkeypatch.setattr(runtime, '_motion_runtime', production.runtime)
    monkeypatch.setattr('lib.motion_video._env.motion_root', lambda: str(tmp_path))
    submitted = []
    monkeypatch.setattr(production.runtime, 'spawn',
                        lambda task_id, worker, task: submitted.append(task))
    yield production.runtime, submitted, tmp_path
    for task_id in production.runtime.task_ids():
        production.runtime.finish(task_id, error='test cleanup')


@pytest.mark.parametrize(('manifest_kind', 'source_kind', 'expected'), [
    ('topic', None, 'topic'), ('paper', None, 'paper'),
    ('scenes', None, 'scenes'), ('motion-video', None, 'scenes'),
    ('topic', 'scenes', 'scenes'),
])
def test_resume_preserves_runtime_kind_and_reads_old_manifest(
        motion_identity, manifest_kind, source_kind, expected):
    from lib.motion_video.engine import resume_interrupted_jobs, write_job_manifest

    runtime, submitted, root = motion_identity
    job = root / 'jobs' / 'resumed'
    job.mkdir(parents=True)
    manifest = {'task_id': 'resumed', 'user_id': 11, 'kind': manifest_kind,
                'state': 'running', 'width': 1080, 'height': 1440,
                'interactive_storyboard': True, 'model': 'chosen'}
    if source_kind:
        manifest['source_kind'] = source_kind
    (job / 'job.json').write_text(json.dumps(manifest))
    assert resume_interrupted_jobs() == 1
    task, = submitted
    assert task['kind'] == 'motion-video'
    assert task['source_kind'] == expected
    assert task['model'] == 'chosen' and task['interactive_storyboard'] is True
    assert runtime.get_owned(task['task_id'], user_id=12) is None
    assert resume_interrupted_jobs() == 0
    write_job_manifest(task, kind=expected, state='running')
    saved = json.loads((job / 'job.json').read_text())
    assert saved['kind'] == saved['source_kind'] == expected

    runtime.append_event(task['task_id'], {
        'type': 'storyboard_gate', 'guidance_id': 'review',
        'title': 'Film', 'pages': [{'page': 1, 'key_message': 'Evidence'}],
    })
    unit = project_work_unit(task)
    assert unit['kind'] == 'motion-video'
    assert unit['pendingGate']['guidanceId'] == 'review'
    runtime.finish(task['task_id'], result={'final_path': str(job / 'final.mp4')})
    unit = project_work_unit(task)
    assert unit['artifacts'][0]['href'] == '/api/v4/motion/videos/resumed/file'


def test_asset_swap_keeps_runtime_kind(motion_identity):
    from lib.motion_video.engine import spawn_scene_revision

    _, submitted, root = motion_identity
    task_id, operation = spawn_scene_revision(
        'parent', 'scene-001', {'width': 1080, 'height': 1440}, str(root),
        owner_user_id=11, instruction='A blue sky', op='swap_asset',
        style={'kind': 'generate', 'target': 'assets/sky.jpg'})
    task, = submitted
    assert operation == 'asset_swap' and task['task_id'] == task_id
    assert task['kind'] == 'motion-video' and task['asset_kind'] == 'generate'
    assert project_work_unit(task)['kind'] == 'motion-video'
