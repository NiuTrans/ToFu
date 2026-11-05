"""All initial-film entries preserve domain policy at one admission owner."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import inspect
import json
from pathlib import Path
import threading

import pytest

from lib.production.runtime import ProductionRuntime

pytestmark = pytest.mark.unit


@pytest.fixture
def admission(tmp_path, monkeypatch):
    from lib.motion_video import runtime
    from routes.api_v1 import motion

    monkeypatch.delenv('TOFU_MOTION_SCENE_AUTHOR', raising=False)
    production = ProductionRuntime('motion-video', id_prefix='motion',
                                   ttl=3600, push_channel=None)
    monkeypatch.setattr(runtime, '_production', production)
    monkeypatch.setattr(runtime, '_motion_runtime', production.runtime)
    monkeypatch.setattr(motion, '_motion_runtime', production.runtime)
    monkeypatch.setattr('lib.motion_video._env.motion_root', lambda: str(tmp_path))
    submitted = []
    monkeypatch.setattr(production.runtime, 'spawn',
                        lambda task_id, worker, task: submitted.append(task))
    yield production, submitted, tmp_path
    for task_id in production.runtime.task_ids():
        production.runtime.finish(task_id, error='test cleanup')


def _http_start(monkeypatch, payload):
    from quart import Quart
    from routes.api_v1 import motion

    monkeypatch.setattr(motion, 'request_user_id', lambda: 11)
    app = Quart(__name__)

    async def invoke():
        async with app.test_request_context('/api/v1/motion/videos',
                                            method='POST', json=payload):
            response, status = await inspect.unwrap(motion.start_motion_task)()
            return status, await response.get_json()

    return asyncio.run(invoke())


def test_http_topic_policy_and_live_join(admission, monkeypatch):
    from lib.motion_video._scene_author import scene_author_enabled

    _, submitted, root = admission
    payload = {'topic': 'Orbit', 'lang': 'en', 'model': 'chosen-model',
               'aspect': '1920x1080', 'max_scenes': 5, 'narration': False,
               'creative_mode': 'standard'}
    status, first = _http_start(monkeypatch, payload)
    _, second = _http_start(monkeypatch, payload)
    assert status == 200 and first['deduped'] is False
    assert second == {**first, 'deduped': True}
    task, = submitted
    assert task['_userId'] == 11
    assert (task['width'], task['height']) == (1920, 1080)
    assert task['model'] == task['qa_model'] == 'chosen-model'
    assert task['kind'] == 'motion-video' and task['source_kind'] == 'topic'
    assert task['topic'] == 'Orbit'
    assert task['lang'] == 'en' and task['max_scenes'] == 5
    assert task['creative_mode'] == 'standard' and task['narration'] is False
    assert not task.get('interactive_storyboard')
    assert 'scene_author' not in task and scene_author_enabled(task)
    assert list((root / 'jobs').iterdir()) == [Path(task['workdir'])]


def test_http_borrowed_source_validation_and_live_rejoin(admission, monkeypatch):
    production, submitted, root = admission
    source = root / 'source.srt'
    payload = {'srt_path': str(source)}
    status, rejected = _http_start(monkeypatch, payload)
    assert status == 400 and rejected['field'] == 'srt_path'
    assert production.runtime.task_ids() == set()
    source.write_text('字幕')
    _, first = _http_start(monkeypatch, payload)
    source.unlink()
    _, joined = _http_start(monkeypatch, payload)
    assert joined['task_id'] == first['task_id'] and joined['deduped'] is True
    assert len(submitted) == 1


@pytest.mark.parametrize('visual', [None, 'template'])
def test_chat_policy_always_starts_fresh_interactive_task(admission, monkeypatch, visual):
    from lib.tasks_pkg.handlers import motion_video as handler
    from lib.motion_video._scene_author import scene_author_enabled

    _, submitted, _ = admission
    monkeypatch.setattr(handler, '_finalize_tool_round', lambda *_args: None)
    parent = {'_userId': 11}
    args = {'topic': 'Orbit', 'lang': 'en', 'model': 'chosen-model'}
    if visual:
        args['visual_quality'] = visual
    results = []
    for _ in range(2):
        _, content, _ = handler._handle_produce_video(
            parent, None, 'produce_video', 'call', args, 1, {}, {}, None, False)
        results.append(json.loads(content))
    assert all(result['ok'] for result in results), results
    assert results[0]['task_id'] != results[1]['task_id']
    assert len(submitted) == 2
    for task in submitted:
        assert task['_userId'] == 11
        assert task['interactive_storyboard'] is True
        assert task['scene_author'] is (visual != 'template')
        assert scene_author_enabled(task) is (visual != 'template')
        assert task['model'] == task['qa_model'] == 'chosen-model'


def test_paper_policy_validates_before_start_and_force_supersedes(admission, monkeypatch):
    from lib.paper import video_abstract
    from lib.motion_video._scene_author import scene_author_enabled

    _, submitted, _ = admission
    scenes = [{'id': 'scene-001', 'text': 'Evidence', 'start': 0, 'end': 6}]
    owners = []
    monkeypatch.setattr('lib.paper.podcast_engine.worker.has_report',
                        lambda paper, *, user_id: owners.append(user_id) or True)
    monkeypatch.setattr('lib.paper.podcast_engine.worker.load_source_text',
                        lambda *_args, **_kwargs: ('Evidence', 'report_en'))
    monkeypatch.setattr(video_abstract, 'build_abstract_scenes', lambda *_args, **_kwargs: scenes)
    monkeypatch.setattr('lib.motion_video.check_scene_budget', lambda *_args, **_kwargs: [])
    first = video_abstract.start_video_abstract('paper', lang='en', model='chosen', user_id=11)
    joined = video_abstract.start_video_abstract('paper', lang='en', model='chosen', user_id=11)
    forced = video_abstract.start_video_abstract('paper', lang='en', model='chosen', user_id=11, force=True)
    latest = video_abstract.start_video_abstract('paper', lang='en', model='chosen', user_id=11)
    assert joined['task_id'] == first['task_id'] and joined['deduped'] is True
    assert joined['source_kind'] == 'joined' and joined['scenes'] == 0
    assert forced['task_id'] != first['task_id'] and latest['task_id'] == forced['task_id']
    assert owners == [11] * 4 and len(submitted) == 2
    for task in submitted:
        assert task['_userId'] == 11 and task['paper_hash'] == 'paper'
        assert task['model'] == 'chosen' and 'qa_model' not in task
        assert 'scene_author' not in task and scene_author_enabled(task)
        assert json.loads(Path(task['scenes_path']).read_text()) == scenes


def test_claim_scopes_owner_and_materializes_only_winning_source(admission):
    from lib.motion_video.service import start_motion_video

    production, submitted, root = admission
    barrier = threading.Barrier(4)

    def start(_):
        barrier.wait(timeout=5)
        return start_motion_video(user_id=11, dedup_key=('same-source',), srt_text='字幕')

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(start, range(4)))
    assert len({task_id for task_id, _ in results}) == 1
    assert sum(joined for _, joined in results) == 3
    task, = submitted
    assert Path(task['srt_path']).read_text() == '字幕'
    assert len(list((root / 'jobs').iterdir())) == 1
    other_id, joined = start_motion_video(user_id=12, dedup_key=('same-source',), srt_text='字幕')
    assert not joined and other_id != task['task_id']
    assert production.runtime.get_owned(other_id, user_id=11) is None


@pytest.mark.parametrize('failure_at', ['materialize', 'submit'])
def test_failed_start_settles_once_and_releases_identity(admission, monkeypatch, failure_at):
    from lib.motion_video import service

    production, submitted, _ = admission
    failure = OSError('start failed')

    def reject(*_args, **_kwargs):
        raise failure

    with monkeypatch.context() as patch:
        if failure_at == 'materialize':
            patch.setattr(service.os, 'makedirs', reject)
        else:
            patch.setattr(production.runtime, 'spawn', reject)
        with pytest.raises(OSError) as caught:
            service.start_motion_video(user_id=11, dedup_key=('source',), srt_text='SRT')
    assert caught.value is failure and not submitted
    failed_id, = production.runtime.task_ids()
    task = production.runtime.get(failed_id)
    assert task['status'] == 'error'
    assert sum(event['type'] == 'error' for event in task['events']) == 1
    retried_id, joined = service.start_motion_video(user_id=11, dedup_key=('source',), srt_text='SRT')
    assert not joined and retried_id != failed_id


def test_abort_during_preparation_retains_stop_and_failure(admission, monkeypatch):
    from lib.motion_video import service

    production, submitted, _ = admission

    def abort_then_fail(*_args, **_kwargs):
        task_id, = production.runtime.task_ids()
        assert not production.runtime.abort_owned(task_id, user_id=12)
        assert production.runtime.abort_owned(task_id, user_id=11)
        raise OSError('source unavailable after stop')

    monkeypatch.setattr(service.os, 'makedirs', abort_then_fail)
    with pytest.raises(OSError):
        service.start_motion_video(user_id=11, srt_text='SRT')
    task_id, = production.runtime.task_ids()
    task = production.runtime.get(task_id)
    assert task['abort_event'].is_set()
    # TaskRuntime deliberately preserves a concrete failure over a Stop; the
    # admission owner must not suppress that failure or clear cancellation.
    assert task['status'] == 'error'
    assert not submitted
