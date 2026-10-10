"""tests/motion_video/test_motion_video_edit.py — instruction-driven scene edit pins.

Executable specification for the conversational edit path (B1/B2):

* ``run_scene_edit_task`` seeds the author loop from the CURRENT frame with
  the user's instruction, runs the no-regression commit, then re-renders
  only that scene and re-assembles the final video at its stable URL.
* An unchanged composition (the revision graded no better) finishes
  honestly with ``applied: False`` — no re-render is burned.
* A degraded author loop fails the edit WITHOUT touching the original
  composition or the deliverable.
* ``resolve_revision_parent`` keeps a finished job revisable after a
  restart (disk-manifest fallback, P-UX4) and refuses foreign/running jobs.
"""

from __future__ import annotations

import json
import os
import re
import time

import pytest

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1


def _touch(path, payload=b'mp4'):
    with open(path, 'wb') as f:
        f.write(payload)
    return None


def _fake_media(monkeypatch):
    def fake_render(project_dir, output, **kw):
        _touch(output)
        return {'ok': True, 'output': output, 'elapsed': 0.1,
                'render_time_s': 0.1, 'category': '', 'detail': ''}
    monkeypatch.setattr('lib.motion_video.render_project', fake_render)

    def fake_probe(path, **kw):
        idx = os.path.join(os.path.dirname(path), 'index.html')
        dur = 3.0
        if os.path.isfile(idx):
            m = re.search(r'data-duration="([0-9.]+)"',
                          open(idx, encoding='utf-8').read())
            if m:
                dur = float(m.group(1))
        return {'codec': 'h264', 'width': 1080, 'height': 1440, 'fps': 30.0,
                'duration': dur, 'has_audio': False}
    monkeypatch.setattr('lib.motion_video.probe_video', fake_probe)
    monkeypatch.setattr(
        'lib.motion_video.concat_mp4s',
        lambda inputs, output, **kw: _touch(output) or {
            'ok': True, 'output': output, 'duration': 6.0, 'mode': 'copy',
            'elapsed': 0.1})
    monkeypatch.setattr(
        'lib.motion_video.mux_audio_video',
        lambda v, a, output, **kw: _touch(output) or {
            'ok': True, 'output': output, 'duration': 6.0, 'elapsed': 0.1})


# Grades (lib/motion_video/_quality.scene_grade): inline SVG makes a
# composition authored_rich; without any graphics it is authored_bare.
# _ORIGINAL/_REVISED are both rich (a revision at the SAME grade is not a
# regression and wins the commit); the regression case revises to bare.
_ORIGINAL_HTML = (
    '<div data-composition-id="main" data-duration="3.0" '
    'data-width="1080" data-height="1440"><h1>旧标题</h1>'
    '<svg viewBox="0 0 10 10"><rect width="10" height="10"/></svg></div>')
_REVISED_HTML = (
    '<div data-composition-id="main" data-duration="3.0" '
    'data-width="1080" data-height="1440"><h1>新标题</h1>'
    '<svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="5"/></svg></div>')


def _seed_job_at(job):
    scenes = [
        {'id': 'scene-001', 'start': 0.0, 'end': 3.0, 'text': '第一句。'},
        {'id': 'scene-002', 'start': 3.0, 'end': 6.0, 'text': '第二句。'},
    ]
    (job / 'audio').mkdir(parents=True)
    (job / 'scenes.json').write_text(json.dumps(scenes), encoding='utf-8')
    for sc in scenes:
        d = job / 'scenes' / sc['id']
        d.mkdir(parents=True)
        (d / 'index.html').write_text(_ORIGINAL_HTML, encoding='utf-8')
        (d / f"{sc['id']}.mp4").write_bytes(b'old')
    (job / 'audio' / 'narration.wav').write_bytes(b'wav')
    (job / 'final.mp4').write_bytes(b'old-final')
    return job, scenes


def _seed_job(tmp_path):
    return _seed_job_at(tmp_path / 'job')


def _edit_task(tmp_path, job, **over):
    from lib.motion_video.runtime import _new_motion_task, _motion_task_id
    task = _new_motion_task(_motion_task_id(), srt_path='', workdir=str(job),
                            voice='', speed=None, alignment='loose',
                            narration=False, quality='draft', parallel=1,
                            width=1080, height=1440,
                            user_id=TEST_OWNER_USER_ID)
    task['scene_id'] = 'scene-001'
    task['edit_of'] = 'motion_original'
    task['instruction'] = '把标题改成新标题'
    task.update(over)
    return task


def _stub_author(monkeypatch, *, mode='authored', html=_REVISED_HTML,
                 record=None):
    def fake_author_scene(scene, scene_dir, **kw):
        if record is not None:
            record['kw'] = kw
            record['scene_dir'] = scene_dir
        return {'ok': True, 'mode': mode, 'html': html,
                'rounds': 2, 'tokens': 1234, 'detail': '',
                'craft_reads': []}
    monkeypatch.setattr('lib.motion_video._scene_author.author_scene',
                        fake_author_scene)
    drafts = {}

    def fake_save_draft(scene_dir, html_text):
        drafts['scene_dir'] = scene_dir
        drafts['html'] = html_text
    monkeypatch.setattr('lib.motion_video._scene_author.save_draft',
                        fake_save_draft)
    monkeypatch.setattr(
        'lib.motion_video._runtime_assets.localise_gsap_html',
        lambda html_text, scene_dir: html_text)
    monkeypatch.setattr('lib.motion_video.check_composition_html',
                        lambda html_text: [])
    return drafts


# ══════════════════════════════════════════════════════════
#  run_scene_edit_task
# ══════════════════════════════════════════════════════════

def test_scene_edit_happy(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_edit_task
    _fake_media(monkeypatch)
    record = {}
    drafts = _stub_author(monkeypatch, record=record)
    job, _scenes = _seed_job(tmp_path)
    task = _edit_task(tmp_path, job)
    run_scene_edit_task(task)

    assert task['status'] == 'done', task.get('error')
    result = task['result']
    assert result['applied'] is True
    assert result['edit_of'] == 'motion_original'
    assert result['scene_id'] == 'scene-001'
    # The author resumed from the CURRENT frame with the instruction.
    assert drafts['html'] == _ORIGINAL_HTML
    assert record['kw'].get('edit_instruction') == '把标题改成新标题'
    assert record['kw'].get('transient_attempts') == 1
    # The revised composition won the commit and the film was re-assembled.
    scene_html = (job / 'scenes' / 'scene-001' /
                  'index.html').read_text(encoding='utf-8')
    assert scene_html == _REVISED_HTML
    assert (job / 'final.mp4').read_bytes() == b'mp4'
    # Sibling scene untouched.
    sibling = (job / 'scenes' / 'scene-002' /
               'index.html').read_text(encoding='utf-8')
    assert sibling == _ORIGINAL_HTML


def test_scene_edit_regression_keeps_original_without_rerender(
        monkeypatch, tmp_path):
    """The no-regression commit refuses a worse revision; the edit finishes
    applied:False and the deliverable is NOT rebuilt."""
    from lib.motion_video.engine import run_scene_edit_task
    _fake_media(monkeypatch)
    # The author "revises" but the commit grades it worse (a bare-bones
    # one-node card vs the richer original).
    _stub_author(monkeypatch,
                 html='<div data-duration="3.0"></div>')
    job, _scenes = _seed_job(tmp_path)
    task = _edit_task(tmp_path, job)
    run_scene_edit_task(task)

    assert task['status'] == 'done', task.get('error')
    result = task['result']
    assert result['applied'] is False
    assert 'kept' in result['reason'] or 'original' in result['reason']
    scene_html = (job / 'scenes' / 'scene-001' /
                  'index.html').read_text(encoding='utf-8')
    assert scene_html == _ORIGINAL_HTML
    # final.mp4 was NOT re-assembled (the old bytes survive).
    assert (job / 'final.mp4').read_bytes() == b'old-final'


def test_scene_edit_degraded_author_fails_clean(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_edit_task
    _fake_media(monkeypatch)
    _stub_author(monkeypatch, mode='template',
                 html='<div data-duration="3.0">fallback</div>')
    job, _scenes = _seed_job(tmp_path)
    task = _edit_task(tmp_path, job)
    run_scene_edit_task(task)

    assert task['status'] == 'error'
    assert 'could not be applied' in str(task.get('error'))
    scene_html = (job / 'scenes' / 'scene-001' /
                  'index.html').read_text(encoding='utf-8')
    assert scene_html == _ORIGINAL_HTML
    assert (job / 'final.mp4').read_bytes() == b'old-final'


def test_scene_edit_missing_instruction_errors(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_edit_task
    _fake_media(monkeypatch)
    _stub_author(monkeypatch)
    job, _scenes = _seed_job(tmp_path)
    task = _edit_task(tmp_path, job, instruction='')
    run_scene_edit_task(task)
    assert task['status'] == 'error'
    assert 'instruction' in str(task.get('error'))


def test_scene_edit_missing_scene_errors(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_edit_task
    _fake_media(monkeypatch)
    _stub_author(monkeypatch)
    job, _scenes = _seed_job(tmp_path)
    task = _edit_task(tmp_path, job, scene_id='scene-999')
    run_scene_edit_task(task)
    assert task['status'] == 'error'


# ══════════════════════════════════════════════════════════
#  resolve_revision_parent — live / disk / ownership / lifecycle (B2)
# ══════════════════════════════════════════════════════════

def _manifest_job(tmp_path, *, state='done', user_id=TEST_OWNER_USER_ID,
                  task_id='motion_diskjob'):
    """A finished-on-disk job at ``<motion_root>/jobs/<task_id>`` with a
    job.json manifest — the layout resolve_revision_parent scans after a
    restart (motion_root is monkeypatched to tmp_path by the caller)."""
    from lib.production.jobs import write_manifest
    job, _scenes = _seed_job_at(tmp_path / 'jobs' / task_id)
    write_manifest(str(job), {
        'task_id': task_id, 'user_id': user_id, 'width': 1080,
        'height': 1440, 'quality': 'draft', 'narration': False,
        'burn_in': False, 'voice': '', 'alignment': 'loose',
    }, fields=('task_id', 'user_id', 'width', 'height', 'quality',
               'narration', 'burn_in', 'voice', 'alignment'),
        kind='scenes', state=state, log_label='test')
    return job


def test_resolve_parent_live_done(monkeypatch, tmp_path):
    from lib.motion_video.engine import resolve_revision_parent
    from lib.motion_video.runtime import _motion_runtime, _new_motion_task, _motion_task_id
    task = _new_motion_task(_motion_task_id(), srt_path='',
                            workdir=str(tmp_path), voice='', speed=None,
                            alignment='loose', narration=False,
                            quality='draft', parallel=1, width=1080,
                            height=1440, user_id=TEST_OWNER_USER_ID)
    _motion_runtime.finish(task['task_id'], result={'workdir': str(tmp_path)})
    params, workdir, error = resolve_revision_parent(
        task['task_id'], TEST_OWNER_USER_ID)
    assert error == '' and workdir == str(tmp_path)
    assert params['width'] == 1080


def test_resolve_parent_live_running_is_not_done(monkeypatch, tmp_path):
    from lib.motion_video.engine import resolve_revision_parent
    from lib.motion_video.runtime import _new_motion_task, _motion_task_id
    task = _new_motion_task(_motion_task_id(), srt_path='',
                            workdir=str(tmp_path), voice='', speed=None,
                            alignment='loose', narration=False,
                            quality='draft', parallel=1, width=1080,
                            height=1440, user_id=TEST_OWNER_USER_ID)
    _params, _workdir, error = resolve_revision_parent(
        task['task_id'], TEST_OWNER_USER_ID)
    assert error == 'not_done'


def test_resolve_parent_unknown_is_not_found():
    from lib.motion_video.engine import resolve_revision_parent
    _params, _workdir, error = resolve_revision_parent(
        'motion_no_such_job', TEST_OWNER_USER_ID)
    assert error == 'not_found'


def test_resolve_parent_disk_manifest_after_restart(
        monkeypatch, tmp_path):
    """B2: a job that finished before the process restarted stays revisable
    via its job.json anchor — the live runtime knows nothing about it."""
    from lib.motion_video.engine import resolve_revision_parent
    job = _manifest_job(tmp_path, state='done')
    monkeypatch.setattr('lib.motion_video._env.motion_root',
                        lambda: str(tmp_path))
    params, workdir, error = resolve_revision_parent(
        'motion_diskjob', TEST_OWNER_USER_ID)
    assert error == ''
    assert workdir == str(job)
    assert params['width'] == 1080


def test_resolve_parent_disk_running_is_not_done(monkeypatch, tmp_path):
    from lib.motion_video.engine import resolve_revision_parent
    _manifest_job(tmp_path, state='running')
    monkeypatch.setattr('lib.motion_video._env.motion_root',
                        lambda: str(tmp_path))
    _params, _workdir, error = resolve_revision_parent(
        'motion_diskjob', TEST_OWNER_USER_ID)
    assert error == 'not_done'


def test_resolve_parent_disk_foreign_owner_is_not_found(
        monkeypatch, tmp_path):
    from lib.motion_video.engine import resolve_revision_parent
    _manifest_job(tmp_path, state='done', user_id=999)
    monkeypatch.setattr('lib.motion_video._env.motion_root',
                        lambda: str(tmp_path))
    _params, _workdir, error = resolve_revision_parent(
        'motion_diskjob', TEST_OWNER_USER_ID)
    assert error == 'not_found'


# ══════════════════════════════════════════════════════════
#  spawn_scene_revision field shape
# ══════════════════════════════════════════════════════════

def test_spawn_edit_subtask_fields(monkeypatch, tmp_path):
    from lib.motion_video.engine import spawn_scene_revision
    spawned = {}
    monkeypatch.setattr(
        'lib.motion_video.runtime._motion_runtime.spawn',
        lambda task_id, fn, task: spawned.update(
            {'task_id': task_id, 'fn': fn, 'task': task}))
    params = {'width': 1080, 'height': 1440, 'quality': 'standard',
              'narration': True, 'burn_in': True,
              'burn_in_fontsdir': '/fonts', 'voice': 'v1', 'speed': 1.1,
              'alignment': 'strict', 'model': 'kimi-k3'}
    sub_id, kind = spawn_scene_revision(
        'motion_parent', 'scene-002', params, str(tmp_path),
        owner_user_id=TEST_OWNER_USER_ID, instruction='换成深色背景')
    assert kind == 'edit'
    sub = spawned['task']
    assert sub['instruction'] == '换成深色背景'
    assert sub['edit_of'] == 'motion_parent'
    assert sub['model'] == 'kimi-k3'
    assert sub['scene_id'] == 'scene-002'
    assert sub['burn_in'] is True
    assert sub['burn_in_fontsdir'] == '/fonts'
    assert sub['width'] == 1080 and sub['height'] == 1440
    from lib.motion_video.engine import run_scene_edit_task
    assert spawned['fn'] is run_scene_edit_task
    assert spawned['task_id'] == sub_id


def test_spawn_regen_subtask_has_no_instruction(monkeypatch, tmp_path):
    from lib.motion_video.engine import spawn_scene_revision
    spawned = {}
    monkeypatch.setattr(
        'lib.motion_video.runtime._motion_runtime.spawn',
        lambda task_id, fn, task: spawned.update(
            {'fn': fn, 'task': task}))
    params = {'width': 1080, 'height': 1440, 'quality': 'standard',
              'narration': False}
    _sub_id, kind = spawn_scene_revision(
        'motion_parent', 'scene-001', params, str(tmp_path),
        owner_user_id=TEST_OWNER_USER_ID)
    assert kind == 'regen'
    sub = spawned['task']
    assert sub['regen_of'] == 'motion_parent'
    assert 'instruction' not in sub
    from lib.motion_video.engine import run_scene_regen_task
    assert spawned['fn'] is run_scene_regen_task


# ══════════════════════════════════════════════════════════
#  HTTP: edit endpoint + disk-fallback regen (B2)
# ══════════════════════════════════════════════════════════

def _done_task_with_job(flask_client, monkeypatch, tmp_path):
    job, _scenes = _seed_job(tmp_path)

    def fake_worker(task):
        from lib.motion_video.runtime import _motion_runtime
        task['result'] = {'final_path': str(job / 'final.mp4'),
                          'duration': 6.0, 'workdir': str(job)}
        _motion_runtime.finish(task['task_id'], result=task['result'])
    monkeypatch.setattr('lib.motion_video.engine.run_motion_task', fake_worker)
    body = flask_client.post('/api/v4/motion/videos',
                             json={'srt': ('1\n00:00:00,000 --> '
                                           '00:00:03,000\n一。\n\n'
                                           '2\n00:00:03,000 --> '
                                           '00:00:06,000\n二。\n'),
                                   'narration': False,
                                   'parallel': 1, 'quality': 'draft'})
    tid = body.get_json()['task_id']
    t0 = time.time()
    from lib.motion_video.runtime import _motion_runtime
    while time.time() - t0 < 10:
        t = _motion_runtime.get(tid)
        if t and t['status'] == 'done':
            break
        time.sleep(0.05)
    return tid, job


def test_scene_edit_endpoint_spawns_edit_task(
        flask_client, monkeypatch, tmp_path):
    tid, _job = _done_task_with_job(flask_client, monkeypatch, tmp_path)
    fired = []
    monkeypatch.setattr('lib.motion_video.engine.run_scene_edit_task',
                        lambda task: fired.append(
                            (task['scene_id'], task['instruction'])))
    r = flask_client.post(
        f'/api/v4/motion/videos/{tid}/scenes/scene-001/edit',
        json={'instruction': '标题换成“价格证据”'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] and body['edit_of'] == tid
    assert body['scene_id'] == 'scene-001'
    t0 = time.time()
    while not fired and time.time() - t0 < 5:
        time.sleep(0.05)
    assert fired == [('scene-001', '标题换成“价格证据”')]


def test_scene_edit_endpoint_requires_instruction(
        flask_client, monkeypatch, tmp_path):
    tid, _job = _done_task_with_job(flask_client, monkeypatch, tmp_path)
    r = flask_client.post(
        f'/api/v4/motion/videos/{tid}/scenes/scene-001/edit', json={})
    assert r.status_code == 400


def test_scene_edit_endpoint_running_job_rejected(
        flask_client, monkeypatch, tmp_path):
    def slow_worker(task):
        time.sleep(30)
    monkeypatch.setattr('lib.motion_video.engine.run_motion_task', slow_worker)
    body = flask_client.post('/api/v4/motion/videos',
                             json={'srt': ('1\n00:00:00,000 --> '
                                           '00:00:03,000\n一。\n'),
                                   'narration': False, 'parallel': 1,
                                   'quality': 'draft'})
    tid = body.get_json()['task_id']
    from lib.motion_video.runtime import _motion_runtime
    t0 = time.time()
    while time.time() - t0 < 5:
        t = _motion_runtime.get(tid)
        if t and t['status'] == 'running':
            break
        time.sleep(0.05)
    r = flask_client.post(
        f'/api/v4/motion/videos/{tid}/scenes/scene-001/edit',
        json={'instruction': 'x'})
    assert r.status_code == 400
    _motion_runtime.abort(tid)


def test_scene_regen_endpoint_disk_fallback_after_restart(
        flask_client, monkeypatch, tmp_path):
    """B2 pin: with no live task (restart), regen rebuilds params from the
    job.json manifest instead of 404ing."""
    _manifest_job(tmp_path, state='done', task_id='motion_diskregen')
    monkeypatch.setattr('lib.motion_video._env.motion_root',
                        lambda: str(tmp_path))
    fired = []
    monkeypatch.setattr(
        'lib.motion_video.runtime._motion_runtime.spawn',
        lambda task_id, fn, task: fired.append('spawned:' + task['scene_id']))
    r = flask_client.post(
        '/api/v4/motion/videos/motion_diskregen/scenes/scene-001/regen')
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] and body['regen_of'] == 'motion_diskregen'
    assert 'spawned:scene-001' in fired


# ══════════════════════════════════════════════════════════
#  set_narration — deterministic single-scene narration swap (B3)
# ══════════════════════════════════════════════════════════

def _seed_narrated_job(tmp_path):
    """A finished narrated job: scenes.json + manifest + per-scene WAVs +
    sidecar SRT + silent/burned/final mp4s."""
    job, scenes = _seed_job(tmp_path)
    from lib.motion_video import _audio as narration_audio
    contract = narration_audio._manifest_request_contract(
        voice=None, speed=None, alignment='loose',
        tail_pad=narration_audio._DEFAULT_TAIL_PAD)
    manifest = {
        'ok': True, 'manifest_version':
            narration_audio._NARRATION_MANIFEST_VERSION,
        'request': contract, 'alignment': 'loose', 'scenes': [],
    }
    for sc in scenes:
        wav = job / 'audio' / f"{sc['id']}.wav"
        wav.write_bytes(b'wav')
        manifest['scenes'].append({
            'scene_id': sc['id'], 'wav': str(wav), 'text_chars': 4,
            'text_sha256': narration_audio._scene_text_sha256(sc['text']),
            'audio_duration': 2.5,
            'srt_duration': sc['end'] - sc['start'],
            'target_duration': 3.0, 'overflow': 0.0,
            'wav_bytes': 3, 'wav_sha256': 'x' * 64,
        })
    (job / 'audio' / 'manifest.json').write_text(
        json.dumps(manifest), encoding='utf-8')
    (job / 'audio' / 'narration.wav').write_bytes(b'old-narration')
    (job / 'final_silent.mp4').write_bytes(b'silent')
    (job / 'final.srt').write_text(
        '1\n00:00:00,000 --> 00:00:03,000\n第一句。\n\n'
        '2\n00:00:03,000 --> 00:00:06,000\n第二句。\n',
        encoding='utf-8')
    return job, scenes


def _fake_tts(monkeypatch, *, audio_dur=2.0):
    def fake_synthesize(scenes_list, out_dir, **kw):
        sc = scenes_list[0]
        wav_path = os.path.join(out_dir, f"{sc['id']}.wav")
        with open(wav_path, 'wb') as f:
            f.write(b'new-wav-bytes')
        return {'ok': True, 'degraded': False, 'scenes': [
            {'scene_id': sc['id'], 'wav': wav_path,
             'text_chars': len(sc['text']),
             'audio_duration': audio_dur,
             'srt_duration': sc['end'] - sc['start'],
             'target_duration': sc['end'] - sc['start'], 'overflow': 0.0}]}
    monkeypatch.setattr(
        'lib.motion_video._audio.synthesize_scene_narrations',
        fake_synthesize)
    monkeypatch.setattr(
        'lib.motion_video._audio.concat_narrations',
        lambda wavs, out_path, **kw: _touch(out_path, b'new-narration')
        or {'ok': True, 'output': out_path, 'duration': 6.0})


def test_narration_edit_happy(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_narration_edit_task
    _fake_media(monkeypatch)
    _fake_tts(monkeypatch, audio_dur=2.0)
    job, scenes = _seed_narrated_job(tmp_path)
    task = _edit_task(tmp_path, job, instruction='这是全新的口播文案。')
    run_scene_narration_edit_task(task)

    assert task['status'] == 'done', task.get('error')
    result = task['result']
    assert result['applied'] is True and result['op'] == 'set_narration'
    assert result['audio_duration_s'] == 2.0
    # Scene WAV swapped, narration track rebuilt, film re-muxed in place.
    assert (job / 'audio' / 'scene-001.wav').read_bytes() == b'new-wav-bytes'
    assert (job / 'audio' / 'narration.wav').read_bytes() == b'new-narration'
    assert (job / 'final.mp4').read_bytes() == b'mp4'
    # Storyboard + sidecar cue carry the new text; sibling cue untouched.
    board = json.loads((job / 'scenes.json').read_text(encoding='utf-8'))
    assert board[0]['text'] == '这是全新的口播文案。'
    assert board[1]['text'] == '第二句。'
    srt = (job / 'final.srt').read_text(encoding='utf-8')
    assert '这是全新的口播文案。' in srt and '第二句。' in srt
    # The manifest entry matches the new speech (resume stays consistent).
    manifest = json.loads(
        (job / 'audio' / 'manifest.json').read_text(encoding='utf-8'))
    entry = next(e for e in manifest['scenes']
                 if e['scene_id'] == 'scene-001')
    assert entry['audio_duration'] == 2.0
    assert entry['wav_bytes'] == len(b'new-wav-bytes')
    # The frame was NOT re-composed.
    assert (job / 'scenes' / 'scene-001' /
            'index.html').read_text(encoding='utf-8') == _ORIGINAL_HTML


def test_narration_edit_overflow_is_an_honest_rejection(
        monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_narration_edit_task
    _fake_media(monkeypatch)
    _fake_tts(monkeypatch, audio_dur=9.9)   # scene is 3.0s
    job, _scenes = _seed_narrated_job(tmp_path)
    task = _edit_task(tmp_path, job, instruction='长到塞不下的口播。')
    run_scene_narration_edit_task(task)

    assert task['status'] == 'error'
    assert 'shorten' in str(task.get('error'))
    # Nothing was swapped: WAV, storyboard, SRT and film all keep old bytes.
    assert (job / 'audio' / 'scene-001.wav').read_bytes() == b'wav'
    board = json.loads((job / 'scenes.json').read_text(encoding='utf-8'))
    assert board[0]['text'] == '第一句。'
    assert (job / 'final.mp4').read_bytes() == b'old-final'


def test_narration_edit_requires_a_narrated_job(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_narration_edit_task
    _fake_media(monkeypatch)
    _fake_tts(monkeypatch)
    job, _scenes = _seed_job(tmp_path)   # no audio/manifest.json
    task = _edit_task(tmp_path, job, instruction='新口播。')
    run_scene_narration_edit_task(task)
    assert task['status'] == 'error'
    assert 'manifest' in str(task.get('error'))


def test_spawn_narration_edit_kind(monkeypatch, tmp_path):
    from lib.motion_video.engine import spawn_scene_revision
    spawned = {}
    monkeypatch.setattr(
        'lib.motion_video.runtime._motion_runtime.spawn',
        lambda task_id, fn, task: spawned.update({'fn': fn, 'task': task}))
    params = {'width': 1080, 'height': 1440, 'quality': 'standard',
              'narration': True}
    _sub_id, kind = spawn_scene_revision(
        'motion_parent', 'scene-001', params, str(tmp_path),
        owner_user_id=TEST_OWNER_USER_ID, instruction='新口播。',
        op='set_narration')
    assert kind == 'narration_edit'
    from lib.motion_video.engine import run_scene_narration_edit_task
    assert spawned['fn'] is run_scene_narration_edit_task
    assert spawned['task']['instruction'] == '新口播。'
    assert 'model' not in spawned['task']


# ══════════════════════════════════════════════════════════
#  scene composition snapshots + revert (B5)
# ══════════════════════════════════════════════════════════

def test_edit_snapshots_original_composition(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_edit_task
    from lib.motion_video.scene_versions import list_scene_versions
    _fake_media(monkeypatch)
    _stub_author(monkeypatch)
    job, _scenes = _seed_job(tmp_path)
    task = _edit_task(tmp_path, job)
    run_scene_edit_task(task)

    assert task['status'] == 'done', task.get('error')
    assert task['result']['version'] == 1
    scene_dir = job / 'scenes' / 'scene-001'
    snap = (scene_dir / '.versions' / '1.html').read_text(encoding='utf-8')
    assert snap == _ORIGINAL_HTML
    rows = list_scene_versions(str(scene_dir))
    assert rows[0]['version'] == 1 and rows[0]['op'] == 'edit'


def test_scene_revert_restores_and_reassembles(monkeypatch, tmp_path):
    from lib.motion_video.engine import (
        run_scene_edit_task, run_scene_revert_task)
    _fake_media(monkeypatch)
    _stub_author(monkeypatch)
    job, _scenes = _seed_job(tmp_path)
    edit = _edit_task(tmp_path, job)
    run_scene_edit_task(edit)
    assert (job / 'scenes' / 'scene-001' /
            'index.html').read_text(encoding='utf-8') == _REVISED_HTML

    (job / 'final.mp4').write_bytes(b'edited-final')
    task = _edit_task(tmp_path, job)
    task['version'] = 1
    run_scene_revert_task(task)

    assert task['status'] == 'done', task.get('error')
    result = task['result']
    assert result['applied'] is True and result['op'] == 'revert'
    assert result['version'] == 1
    assert (job / 'scenes' / 'scene-001' /
            'index.html').read_text(encoding='utf-8') == _ORIGINAL_HTML
    assert (job / 'final.mp4').read_bytes() == b'mp4'
    # The snapshot survives its own revert (bounce between versions).
    assert (job / 'scenes' / 'scene-001' / '.versions' /
            '1.html').read_text(encoding='utf-8') == _ORIGINAL_HTML


def test_scene_revert_unknown_version_errors(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_revert_task
    _fake_media(monkeypatch)
    job, _scenes = _seed_job(tmp_path)
    task = _edit_task(tmp_path, job)
    task['version'] = 7
    run_scene_revert_task(task)
    assert task['status'] == 'error'
    assert 'not found' in str(task.get('error'))


# ══════════════════════════════════════════════════════════
#  set_subtitle_style — deterministic job-level subtitle restyle (B3b)
# ══════════════════════════════════════════════════════════

def test_apply_style_overrides_validates_vocabulary_and_ranges():
    from lib.motion_video._subtitle import (
        apply_style_overrides, style_for_frame, SUBTITLE_STYLE_KEYS)
    base = style_for_frame(1080, 1440)
    st = apply_style_overrides(base, {'font_scale': 1.2, 'margin_v': 40,
                                      'outline': 3, 'font_name': 'MiSans'})
    assert st.font_px == round(base.font_px * 1.2)
    assert st.margin_v == 40 and st.outline == 3
    assert st.font_name == 'MiSans' and st.font_file == ''
    assert base.font_px != st.font_px          # original untouched
    assert SUBTITLE_STYLE_KEYS == frozenset(
        ('font_scale', 'font_px', 'margin_v', 'outline', 'font_name'))

    for bad in ({'position': 'top'}, {'font_scale': 3.0}, {'font_px': 4},
                {'margin_v': -1}, {'outline': 99}):
        try:
            apply_style_overrides(base, bad)
            raise AssertionError(f'{bad} must be rejected')
        except ValueError:
            pass


def _seed_burned_job(tmp_path):
    job, scenes = _seed_narrated_job(tmp_path)
    (job / 'final_burned.mp4').write_bytes(b'burned')
    return job, scenes


def _style_task(tmp_path, job, style):
    from lib.motion_video.runtime import _new_motion_task, _motion_task_id
    task = _new_motion_task(_motion_task_id(), srt_path='', workdir=str(job),
                            voice='', speed=None, alignment='loose',
                            narration=True, quality='draft', parallel=1,
                            width=1080, height=1440,
                            user_id=TEST_OWNER_USER_ID)
    task['edit_of'] = 'motion_original'
    task['burn_in'] = True
    task['style'] = style
    return task


def test_subtitle_style_task_happy(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_subtitle_style_task
    _fake_media(monkeypatch)
    burn_calls = []
    monkeypatch.setattr(
        'lib.motion_video.burn_in_subtitles',
        lambda v, s, output, **kw: burn_calls.append(kw) or _touch(output)
        or {'ok': True, 'output': output, 'duration': 6.0, 'elapsed': 0.1})
    job, _scenes = _seed_burned_job(tmp_path)
    style = {'font_scale': 1.3, 'margin_v': 60, 'outline': 4}
    task = _style_task(tmp_path, job, style)
    run_subtitle_style_task(task)

    assert task['status'] == 'done', task.get('error')
    result = task['result']
    assert result['applied'] is True
    assert result['op'] == 'set_subtitle_style'
    assert result['style'] == style
    # The burn received the user's overrides.
    assert burn_calls[0]['style_overrides'] == style
    # Persisted for every later revision path.
    record = json.loads((job / 'subtitle_style.json').read_text(
        encoding='utf-8'))
    assert record['overrides'] == style
    assert (job / 'final.mp4').read_bytes() == b'mp4'


def test_subtitle_style_task_rejects_non_burnin_job(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_subtitle_style_task
    _fake_media(monkeypatch)
    job, _scenes = _seed_burned_job(tmp_path)
    task = _style_task(tmp_path, job, {'font_scale': 1.2})
    task['burn_in'] = False
    run_subtitle_style_task(task)
    assert task['status'] == 'error'
    assert 'burn_in' in str(task.get('error'))
    assert not (job / 'subtitle_style.json').exists()


def test_subtitle_style_task_rejects_bad_overrides(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_subtitle_style_task
    _fake_media(monkeypatch)
    job, _scenes = _seed_burned_job(tmp_path)
    task = _style_task(tmp_path, job, {'font_scale': 9.9})
    run_subtitle_style_task(task)
    assert task['status'] == 'error'
    assert 'font_scale' in str(task.get('error'))
    assert not (job / 'subtitle_style.json').exists()


def test_revision_paths_honor_pinned_subtitle_style(monkeypatch, tmp_path):
    """A regen after the style op must re-burn with the user's overrides —
    never silently revert subtitles to the defaults."""
    from lib.motion_video.engine import run_scene_regen_task
    _fake_media(monkeypatch)
    burn_calls = []
    monkeypatch.setattr(
        'lib.motion_video.burn_in_subtitles',
        lambda v, s, output, **kw: burn_calls.append(kw) or _touch(output)
        or {'ok': True, 'output': output, 'duration': 6.0, 'elapsed': 0.1})
    job, _scenes = _seed_burned_job(tmp_path)
    (job / 'subtitle_style.json').write_text(
        json.dumps({'overrides': {'font_px': 66}}), encoding='utf-8')
    from lib.motion_video.runtime import _new_motion_task, _motion_task_id
    task = _new_motion_task(_motion_task_id(), srt_path='', workdir=str(job),
                            voice='', speed=None, alignment='loose',
                            narration=True, quality='draft', parallel=1,
                            width=1080, height=1440,
                            user_id=TEST_OWNER_USER_ID)
    task['scene_id'] = 'scene-001'
    task['burn_in'] = True
    task['regen_of'] = 'motion_original'
    run_scene_regen_task(task)
    assert task['status'] == 'done', task.get('error')
    assert burn_calls[0]['style_overrides'] == {'font_px': 66}


# ══════════════════════════════════════════════════════════
#  swap_asset — deterministic scene media replacement (B3c)
# ══════════════════════════════════════════════════════════

def test_scene_media_refs_detection(tmp_path):
    from lib.motion_video.engine import _scene_media_refs
    html = tmp_path / 'index.html'
    html.write_text(
        '<img src="assets/stock_aa.jpg"><video src="assets/clip.mp4"></video>'
        '<img src="https://cdn.example.com/x.png">'
        '<img src="data:image/png;base64,xx"><img src="../escape.png">'
        '<img src="assets/stock_aa.jpg">', encoding='utf-8')
    assert _scene_media_refs(str(html)) == ['assets/stock_aa.jpg',
                                            'assets/clip.mp4']


def _seed_asset_job(tmp_path, html=None):
    job, scenes = _seed_job(tmp_path)
    scene_dir = job / 'scenes' / 'scene-001'
    (scene_dir / 'assets').mkdir(exist_ok=True)
    (scene_dir / 'assets' / 'old.jpg').write_bytes(b'old-image-bytes')
    composition = (html or
                   '<div data-composition-id="main" data-duration="3.0" '
                   'data-width="1080" data-height="1440">'
                   '<img src="assets/old.jpg"></div>')
    (scene_dir / 'index.html').write_text(composition, encoding='utf-8')
    return job, scenes


def _asset_task(tmp_path, job, **over):
    task = _edit_task(tmp_path, job)
    task['query'] = '蓝色城市的夜景'
    task['kind'] = 'stock'
    task.update(over)
    return task


def test_asset_swap_stock_same_suffix_replaces_in_place(
        monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_asset_swap_task
    _fake_media(monkeypatch)
    monkeypatch.setattr(
        'lib.production.stock_media.resolve_stock_media',
        lambda request: {'ok': True, 'data': b'new-image-bytes',
                         'suffix': '.jpg', 'media_kind': 'image'})
    monkeypatch.setattr('lib.motion_video.check_composition_html',
                        lambda html: [])
    job, _scenes = _seed_asset_job(tmp_path)
    task = _asset_task(tmp_path, job)
    run_scene_asset_swap_task(task)

    assert task['status'] == 'done', task.get('error')
    result = task['result']
    assert result['applied'] is True and result['op'] == 'swap_asset'
    assert result['asset'] == 'assets/old.jpg'   # same path, composition kept
    scene_dir = job / 'scenes' / 'scene-001'
    assert (scene_dir / 'assets' / 'old.jpg').read_bytes() == b'new-image-bytes'
    assert 'assets/old.jpg' in (scene_dir / 'index.html').read_text(
        encoding='utf-8')
    assert (job / 'final.mp4').read_bytes() == b'mp4'


def test_asset_swap_generate_rewrites_reference(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_asset_swap_task
    _fake_media(monkeypatch)

    def _fake_generate(prompt, scene_dir, **kw):
        # The real generator materialises the file before returning the
        # scene-relative path (an invented path renders a blank frame).
        import os as _os
        path = _os.path.join(scene_dir, 'assets', 'gen_new.png')
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(b'gen-image-bytes')
        return 'assets/gen_new.png'
    monkeypatch.setattr(
        'lib.motion_video._scene_author._generate_scene_asset',
        _fake_generate)
    monkeypatch.setattr('lib.motion_video.check_composition_html',
                        lambda html: [])
    job, _scenes = _seed_asset_job(tmp_path)
    task = _asset_task(tmp_path, job, kind='generate')
    run_scene_asset_swap_task(task)

    assert task['status'] == 'done', task.get('error')
    assert task['result']['asset'] == 'assets/gen_new.png'
    html = (job / 'scenes' / 'scene-001' /
            'index.html').read_text(encoding='utf-8')
    assert 'assets/gen_new.png' in html
    assert 'assets/old.jpg' not in html


def test_asset_swap_requires_target_for_multi_ref(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_asset_swap_task
    _fake_media(monkeypatch)
    job, _scenes = _seed_asset_job(
        tmp_path,
        html='<div data-duration="3.0">'
             '<img src="assets/old.jpg"><img src="assets/other.png"></div>')
    (job / 'scenes' / 'scene-001' / 'assets' /
     'other.png').write_bytes(b'other')
    task = _asset_task(tmp_path, job)
    run_scene_asset_swap_task(task)
    assert task['status'] == 'error'
    assert 'target' in str(task.get('error'))
    assert 'assets/old.jpg' in str(task.get('error'))


def test_asset_swap_honest_when_no_media(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_asset_swap_task
    _fake_media(monkeypatch)
    job, _scenes = _seed_asset_job(
        tmp_path, html='<div data-duration="3.0"><h1>纯文字</h1></div>')
    task = _asset_task(tmp_path, job)
    run_scene_asset_swap_task(task)
    assert task['status'] == 'error'
    assert 'rewrite' in str(task.get('error'))


def test_spawn_asset_swap_kind(monkeypatch, tmp_path):
    from lib.motion_video.engine import spawn_scene_revision
    spawned = {}
    monkeypatch.setattr(
        'lib.motion_video.runtime._motion_runtime.spawn',
        lambda task_id, fn, task: spawned.update({'fn': fn, 'task': task}))
    params = {'width': 1080, 'height': 1440, 'quality': 'standard',
              'narration': False}
    _sub_id, kind = spawn_scene_revision(
        'motion_parent', 'scene-001', params, str(tmp_path),
        owner_user_id=TEST_OWNER_USER_ID, instruction='夜景',
        op='swap_asset',
        style={'kind': 'generate', 'target': 'assets/old.jpg'})
    assert kind == 'asset_swap'
    from lib.motion_video.engine import run_scene_asset_swap_task
    assert spawned['fn'] is run_scene_asset_swap_task
    assert spawned['task']['query'] == '夜景'
    assert spawned['task']['kind'] == 'generate'
    assert spawned['task']['target'] == 'assets/old.jpg'
