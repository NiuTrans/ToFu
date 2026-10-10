"""tests/production/test_t2v_media.py — text-to-video clip fallback pins.

Executable specification for lib/production/t2v_media.py and its two
consumers: the swap_asset t2v kind (video targets only, honest rejection
for stills) and the motion asset preflight's stock-miss fallback (one
bounded clip attempt before a finding; unconfigured degrades cleanly).
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def t2v_env(monkeypatch):
    monkeypatch.setenv('TOFU_T2V_BASE_URL', 'https://t2v.test')
    monkeypatch.setenv('TOFU_T2V_API_KEY', 'sk-test')
    monkeypatch.setenv('TOFU_T2V_MODEL', 'Test-H3')
    monkeypatch.setenv('TOFU_T2V_POLL_S', '0.01')


class _Resp:
    def __init__(self, payload=b'{}', content_type='video/mp4'):
        self.content = payload
        self.headers = {'content-type': content_type}

    def raise_for_status(self):
        return None


def _wire_t2v(monkeypatch, *, task_status='success', video_url='https://cdn.test/c.mp4',
              clip=b'clip-bytes', content_type='video/mp4'):
    import json
    posts = []
    state = {'polls': 0}

    def fake_post(url, **kw):
        posts.append((url, kw.get('json')))
        return _Resp(json.dumps({'id': 'task-1'}).encode())
    monkeypatch.setattr('lib.http_client.http_post', fake_post)

    def fake_get(url, **kw):
        if 'generations/task-1' in url:
            state['polls'] += 1
            return _Resp(json.dumps(
                {'status': task_status, 'video_url': video_url}).encode())
        return _Resp(clip, content_type=content_type)
    monkeypatch.setattr('lib.http_client.http_get', fake_get)
    return posts, state


# ── resolve_t2v_clip ──────────────────────────────────────

def test_unconfigured_degrades_cleanly(monkeypatch):
    for key in ('TOFU_T2V_BASE_URL', 'TOFU_T2V_API_KEY', 'TOFU_T2V_MODEL'):
        monkeypatch.delenv(key, raising=False)
    from lib.production.t2v_media import resolve_t2v_clip
    res = resolve_t2v_clip('城市夜景')
    assert res['ok'] is False
    assert 'TOFU_T2V_BASE_URL' in res['reason']


def test_happy_path_submit_poll_download(t2v_env, monkeypatch):
    posts, state = _wire_t2v(monkeypatch)
    from lib.production.t2v_media import resolve_t2v_clip
    res = resolve_t2v_clip('蓝色城市的夜景')
    assert res['ok'], res
    assert res['data'] == b'clip-bytes'
    assert res['suffix'] == '.mp4'
    assert 'Test-H3' in res['provider']
    assert posts[0][0] == 'https://t2v.test/videos/generations'
    assert posts[0][1]['model'] == 'Test-H3'
    assert posts[0][1]['prompt'] == '蓝色城市的夜景'
    assert state['polls'] >= 1


def test_failed_task_degrades(t2v_env, monkeypatch):
    _wire_t2v(monkeypatch, task_status='failed')
    from lib.production.t2v_media import resolve_t2v_clip
    res = resolve_t2v_clip('x')
    assert res['ok'] is False and 'failed' in res['reason']


def test_poll_timeout_degrades(t2v_env, monkeypatch):
    _wire_t2v(monkeypatch, task_status='processing', video_url='')
    monkeypatch.setenv('TOFU_T2V_TIMEOUT_S', '60')
    # Compress the wait: poll interval is 0.01s; force the deadline by
    # freezing monotonic progression.
    import lib.production.t2v_media as t2v
    ticks = {'t': 0.0}

    def fake_monotonic():
        ticks['t'] += 30.0
        return ticks['t']
    monkeypatch.setattr(t2v.time, 'monotonic', fake_monotonic)
    res = t2v.resolve_t2v_clip('x')
    assert res['ok'] is False and 'did not finish' in res['reason']


def test_oversize_clip_rejected(t2v_env, monkeypatch):
    from lib.production import t2v_media as t2v
    _wire_t2v(monkeypatch, clip=b'x' * (t2v.T2V_MAX_CLIP_BYTES + 1))
    res = t2v.resolve_t2v_clip('x')
    assert res['ok'] is False and 'limit' in res['reason']


def test_non_video_kind_rejected(t2v_env):
    from lib.production.t2v_media import resolve_t2v_clip
    res = resolve_t2v_clip('x', kind='image')
    assert res['ok'] is False and 'video' in res['reason']


# ── swap_asset kind='t2v' ─────────────────────────────────

def _seed_video_asset_job(tmp_path):
    from tests.motion_video.test_motion_video_edit import _seed_job_at
    job = tmp_path / 'job'
    _seed_job_at(job)
    scene_dir = job / 'scenes' / 'scene-001'
    (scene_dir / 'assets').mkdir(exist_ok=True)
    (scene_dir / 'assets' / 'old.mp4').write_bytes(b'old-clip')
    (scene_dir / 'index.html').write_text(
        '<div data-composition-id="main" data-duration="3.0" '
        'data-width="1080" data-height="1440">'
        '<video src="assets/old.mp4" muted playsinline></video>'
        '<svg viewBox="0 0 10 10"><rect width="10" height="10"/></svg></div>',
        encoding='utf-8')
    return job


def _edit_task(tmp_path, job, **over):
    from lib.motion_video.runtime import _new_motion_task, _motion_task_id
    task = _new_motion_task(_motion_task_id(), srt_path='', workdir=str(job),
                            voice='', speed=None, alignment='loose',
                            narration=False, quality='draft', parallel=1,
                            width=1080, height=1440,
                            user_id=TEST_OWNER_USER_ID)
    task['scene_id'] = 'scene-001'
    task['edit_of'] = 'motion_original'
    task['instruction'] = '海浪拍打礁石'
    task['query'] = '海浪拍打礁石'
    task['kind'] = 't2v'
    task.update(over)
    return task


def test_swap_asset_t2v_replaces_clip_bytes(t2v_env, monkeypatch, tmp_path):
    from tests.motion_video.test_motion_video_edit import _fake_media
    _fake_media(monkeypatch)
    _wire_t2v(monkeypatch, clip=b'new-t2v-clip')
    monkeypatch.setattr('lib.motion_video.check_composition_html',
                        lambda html: [])
    from lib.motion_video.engine import run_scene_asset_swap_task
    job = _seed_video_asset_job(tmp_path)
    task = _edit_task(tmp_path, job)
    run_scene_asset_swap_task(task)

    assert task['status'] == 'done', task.get('error')
    result = task['result']
    assert result['applied'] is True and result['kind'] == 't2v'
    assert (job / 'scenes' / 'scene-001' / 'assets' /
            'old.mp4').read_bytes() == b'new-t2v-clip'
    assert (job / 'final.mp4').read_bytes() == b'mp4'


def test_swap_asset_t2v_rejects_still_targets(t2v_env, monkeypatch,
                                              tmp_path):
    from tests.motion_video.test_motion_video_edit import _fake_media, _seed_asset_job
    _fake_media(monkeypatch)
    _wire_t2v(monkeypatch)
    from lib.motion_video.engine import run_scene_asset_swap_task
    job, _scenes = _seed_asset_job(tmp_path)   # image asset (assets/old.jpg)
    task = _edit_task(tmp_path, job)
    run_scene_asset_swap_task(task)
    assert task['status'] == 'error'
    assert 'rewrite' in str(task.get('error'))
    assert (job / 'scenes' / 'scene-001' / 'assets' /
            'old.jpg').read_bytes() == b'old-image-bytes'


# ── preflight fallback ────────────────────────────────────

def test_preflight_stock_miss_falls_back_to_t2v(t2v_env, monkeypatch,
                                                tmp_path):
    monkeypatch.setattr(
        'lib.production.stock_media.resolve_stock_media',
        lambda request: (_ for _ in ()).throw(RuntimeError('provider 429')))
    _wire_t2v(monkeypatch, clip=b'fallback-clip')
    from lib.motion_video._asset_preflight import prepare_scene_assets
    scene_dir = tmp_path / 'scene'
    scene_dir.mkdir()
    scene = {'id': 'scene-001',
             'media_queries': [{'query': '海浪', 'kind': 'video',
                                'semantic_target': '海浪拍打礁石'}]}
    out = prepare_scene_assets(scene, str(scene_dir))
    assert out['findings'] == []
    assert out['resolved'][0]['role'] == 't2v-video'
    assert (scene_dir / out['resolved'][0]['path']).read_bytes() == \
        b'fallback-clip'


def test_preflight_records_both_failures(t2v_env, monkeypatch, tmp_path):
    monkeypatch.setattr(
        'lib.production.stock_media.resolve_stock_media',
        lambda request: (_ for _ in ()).throw(RuntimeError('provider 429')))
    monkeypatch.setattr('lib.production.t2v_media.resolve_t2v_clip',
                        lambda query: {'ok': False, 'reason': 'no route'})
    from lib.motion_video._asset_preflight import prepare_scene_assets
    scene_dir = tmp_path / 'scene'
    scene_dir.mkdir()
    scene = {'id': 'scene-001',
             'media_queries': [{'query': '海浪', 'kind': 'video',
                                'semantic_target': '海浪拍打礁石'}]}
    out = prepare_scene_assets(scene, str(scene_dir))
    assert out['resolved'] == []
    assert any('t2v fallback' in f for f in out['findings'])
