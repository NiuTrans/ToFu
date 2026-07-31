#!/usr/bin/env python3
"""tests/test_motion_video_engine.py — P2b headless pipeline suite.

Covers the zero-LLM / engine / runtime / api_v1 slice
(docs/MOTION_VIDEO_DESIGN.md P2b):

  * build_storyboard — greedy bounds, contiguity by construction (always
    passes the storyboard gate), sentence-final preference, runt merge
  * render_scene_html — passes the composition static gate by construction,
    XSS-escapes scene text, honors adjusted durations
  * run_motion_task — REAL engine driven with provider seams faked
    (hyperframes render / probe / concat / mux / TTS narration); REAL
    storyboard + REAL template + REAL verify_spec run. Phases, artifacts,
    degrade, abort, scene-failure diagnosis + a NEUTER proving the
    per-scene spec gate is load-bearing.
  * runtime dedup index lifecycle
  * HTTP layer (flask_client — REAL app): validation 400s, start, dedup
    join, poll, abort, Range file serving.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

import pytest

from lib import motion_video as mv

pytestmark = pytest.mark.unit

SRT = """1
00:00:01,000 --> 00:00:03,000
第一句话。

2
00:00:03,000 --> 00:00:05,000
第二句话。

3
00:00:05,000 --> 00:00:07,500
第三句也很长一些的话。

4
00:00:07,500 --> 00:00:09,000
收尾。
"""


def _entries():
    return mv.parse_srt(SRT)


# ══════════════════════════════════════════════════════════
#  zero-LLM storyboard
# ══════════════════════════════════════════════════════════

def test_storyboard_contiguous_and_gate_clean():
    scenes = mv.build_storyboard(_entries())
    assert scenes[0]['id'] == 'scene-001'
    # By construction it must pass the agent-facing gate verbatim.
    assert mv.check_storyboard(scenes, mv.total_span(_entries())) == []


def test_storyboard_respects_max_and_sentence_preference():
    entries = _entries()
    scenes = mv.build_storyboard(entries, target_scene=2.0, max_scene=3.0)
    for sc in scenes:
        assert sc['end'] - sc['start'] <= 3.0 + 1e-6
    # sentence-final cues are preferred close points
    assert scenes[0]['text'].endswith('。')


def test_storyboard_trailing_runt_merges():
    entries = _entries()
    scenes = mv.build_storyboard(entries, min_scene=2.0, target_scene=100.0,
                                 max_scene=200.0)
    # one giant scene, no runt left behind
    assert len(scenes) == 1
    assert scenes[0]['end'] == pytest.approx(9.0)


def test_storyboard_empty_and_clamps():
    assert mv.build_storyboard([]) == []
    scenes = mv.build_storyboard(_entries(), min_scene=-1, target_scene=0,
                                 max_scene=0)
    assert mv.check_storyboard(scenes, mv.total_span(_entries())) == []


# ══════════════════════════════════════════════════════════
#  zero-LLM template
# ══════════════════════════════════════════════════════════

def test_template_passes_composition_gate_all_sizes():
    for i, text in enumerate(('短', '中等长度的一句话', '很' * 80, '很' * 200, '')):
        html = mv.render_scene_html(
            {'id': f'scene-{i:03d}', 'start': 0, 'end': 4, 'text': text},
            duration=4.0, scene_index=i + 1, total_scenes=5)
        assert mv.check_composition_html(html) == [], text[:12]
        assert 'data-duration="4.0"' in html


def test_template_escapes_injection():
    html = mv.render_scene_html(
        {'id': 's', 'start': 0, 'end': 4,
         'text': '<script>alert(1)</script>" onclick=x'}, duration=4.0)
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html


# ══════════════════════════════════════════════════════════
#  engine (real worker, faked provider seams)
# ══════════════════════════════════════════════════════════

def _engine_task(tmp_path, **over):
    from lib.motion_video.runtime import _new_motion_task, _motion_task_id
    srt_path = tmp_path / 't.srt'
    srt_path.write_text(SRT, encoding='utf-8')
    kw = dict(srt_path=str(srt_path), workdir=str(tmp_path / 'job'),
              voice='', speed=None, alignment='loose', narration=True,
              quality='draft', parallel=2, width=1080, height=1440)
    kw.update(over)
    return _new_motion_task(_motion_task_id(), **kw)


def _fake_narration(monkeypatch, tmp_path):
    """Fake TTS: per-scene REAL silence WAVs, loose targets == srt durations."""
    import lib.tts as T
    def fake(scenes, out_dir, **kw):
        os.makedirs(out_dir, exist_ok=True)
        out = []
        for sc in scenes:
            dur = float(sc['end']) - float(sc['start'])
            wav = os.path.join(out_dir, f"{sc['id']}.wav")
            with open(wav, 'wb') as f:
                f.write(T.silence_wav_bytes(dur))
            out.append({'scene_id': sc['id'], 'wav': wav,
                        'text_chars': len(sc.get('text', '')),
                        'audio_duration': dur, 'srt_duration': dur,
                        'target_duration': dur, 'overflow': 0.0})
        return {'ok': True, 'degraded': False, 'alignment': 'loose',
                'overflow_total': 0.0, 'scenes': out}
    monkeypatch.setattr('lib.motion_video.synthesize_scene_narrations', fake)


def _fake_media(monkeypatch, *, render_ok=True):
    """Fake render/concat/mux; probe reads the REAL composed data-duration
    so the REAL verify_spec gate runs against real timings."""
    def fake_render(project_dir, output, **kw):
        if not render_ok:
            return {'ok': False, 'category': 'chrome', 'detail': 'boom'}
        with open(output, 'wb') as f:
            f.write(b'mp4')
        return {'ok': True, 'output': output, 'elapsed': 0.1,
                'render_time_s': 0.1, 'category': '', 'detail': ''}
    monkeypatch.setattr('lib.motion_video.render_project', fake_render)

    def fake_probe(path, **kw):
        idx = os.path.join(os.path.dirname(path), 'index.html')
        dur = 4.0
        if os.path.isfile(idx):
            m = re.search(r'data-duration="([0-9.]+)"',
                          open(idx, encoding='utf-8').read())
            if m:
                dur = float(m.group(1))
        return {'codec': 'h264', 'width': 1080, 'height': 1440, 'fps': 30.0,
                'duration': dur, 'has_audio': False}
    monkeypatch.setattr('lib.motion_video.probe_video', fake_probe)

    def fake_concat(inputs, output, **kw):
        with open(output, 'wb') as f:
            f.write(b'mp4')
        return {'ok': True, 'output': output, 'duration': 8.0, 'mode': 'copy',
                'elapsed': 0.1}
    monkeypatch.setattr('lib.motion_video.concat_mp4s', fake_concat)

    def fake_mux(video, audio, output, **kw):
        with open(output, 'wb') as f:
            f.write(b'mp4')
        return {'ok': True, 'output': output, 'duration': 8.0, 'elapsed': 0.1}
    monkeypatch.setattr('lib.motion_video.mux_audio_video', fake_mux)


def test_engine_full_chain(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_motion_task
    _fake_narration(monkeypatch, tmp_path)
    _fake_media(monkeypatch)
    task = _engine_task(tmp_path)
    run_motion_task(task)

    assert task['status'] == 'done', task.get('error')
    phases = [e.get('phase') for e in task['events'] if e['type'] == 'phase']
    assert phases == ['parse', 'storyboard', 'narrate', 'compose', 'concat',
                      'mux']
    result = task['result']
    assert result['narrated'] is True
    assert result['scenes'] >= 1
    assert os.path.isfile(result['final_path'])
    # storyboard + compositions + sidecar really written
    job = task['workdir']
    assert os.path.isfile(os.path.join(job, 'scenes.json'))
    assert os.path.isfile(result['srt_path'])
    scenes = json.load(open(os.path.join(job, 'scenes.json'), encoding='utf-8'))
    for sc in scenes:
        idx = os.path.join(job, 'scenes', sc['id'], 'index.html')
        assert os.path.isfile(idx), sc['id']
    # sidecar carries the (loose-adjusted) timeline
    sidecar = open(result['srt_path'], encoding='utf-8').read()
    assert '-->' in sidecar and '第一句话' in sidecar


def test_engine_narration_degraded_continues_silent(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_motion_task
    monkeypatch.setattr('lib.motion_video.synthesize_scene_narrations',
                        lambda *a, **kw: {'ok': False, 'degraded': True,
                                          'detail': 'no tts slot'})
    _fake_media(monkeypatch)
    # Degraded narration auto-burns subtitles (owner 2026-07-26) — fake the
    # ffmpeg burn so this offline test stays offline.
    monkeypatch.setattr('lib.motion_video.burn_in_subtitles',
                        lambda video, srt, output, **kw: (
                            open(output, 'wb').write(b'mp4'),
                            {'ok': True, 'output': output, 'duration': 8.0,
                             'elapsed': 0.1})[1])
    task = _engine_task(tmp_path)
    run_motion_task(task)
    assert task['status'] == 'done', task.get('error')
    # phase_started events also carry .phase — select the phase event itself.
    narrate = [e for e in task['events']
               if e['type'] == 'phase' and e.get('phase') == 'narrate'][0]
    assert narrate['degraded'] is True
    assert task['result']['narrated'] is False


def test_engine_narration_degraded_auto_burns_subtitles(monkeypatch, tmp_path):
    """Narration was REQUESTED but degraded to silent → the text is the only
    information carrier, so the engine auto-burns the sidecar subtitles
    (owner 2026-07-26). The burned text must be the REAL sidecar timeline,
    never a re-estimate."""
    from lib.motion_video.engine import run_motion_task
    monkeypatch.setattr('lib.motion_video.synthesize_scene_narrations',
                        lambda *a, **kw: {'ok': False, 'degraded': True,
                                          'detail': 'no tts slot'})
    _fake_media(monkeypatch)
    burn_calls = []

    def fake_burn(video, srt, output, **kw):
        burn_calls.append((video, srt))
        with open(output, 'wb') as f:
            f.write(b'mp4-burned')
        return {'ok': True, 'output': output, 'duration': 8.0, 'elapsed': 0.1}
    monkeypatch.setattr('lib.motion_video.burn_in_subtitles', fake_burn)

    task = _engine_task(tmp_path)  # narration=True — requested by the user
    run_motion_task(task)
    assert task['status'] == 'done', task.get('error')
    assert task['result']['narrated'] is False
    assert task['result']['burn_in'] is True
    assert task['result']['burn_in_auto'] is True
    assert burn_calls, 'degraded narration did not auto-burn the subtitles'
    _, srt_arg = burn_calls[0]
    assert srt_arg == task['result']['srt_path'], (
        'burn-in must consume the real sidecar timeline, not a re-estimate')
    assert os.path.isfile(srt_arg)
    assert '第一句话' in open(srt_arg, encoding='utf-8').read()


def test_engine_explicit_silent_does_not_auto_burn(monkeypatch, tmp_path):
    """The user ACTIVELY chose narration=False — that is a deliberate silent
    run, not a degrade; the engine must not burn subtitles on it."""
    from lib.motion_video.engine import run_motion_task
    _fake_media(monkeypatch)
    called = []
    monkeypatch.setattr('lib.motion_video.burn_in_subtitles',
                        lambda *a, **k: (called.append(1), {'ok': True})[1])
    task = _engine_task(tmp_path, narration=False)
    run_motion_task(task)
    assert task['status'] == 'done', task.get('error')
    assert task['result']['burn_in'] is False
    assert task['result'].get('burn_in_auto') is False
    assert not called


def test_engine_scene_failure_diagnosed(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_motion_task
    _fake_narration(monkeypatch, tmp_path)
    _fake_media(monkeypatch, render_ok=False)
    task = _engine_task(tmp_path)
    run_motion_task(task)
    assert task['status'] == 'error'
    scene_ids = [e.get('scene_id') for e in task['events']
                 if e['type'] == 'scene_done' and not e.get('ok')]
    assert scene_ids, 'expected per-scene failure events'
    assert task['error'] is not None


def test_engine_abort_between_phases(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_motion_task
    _fake_media(monkeypatch)
    task = _engine_task(tmp_path, narration=False)
    task['abort_event'].set()
    run_motion_task(task)
    assert task['status'] == 'aborted'


def test_engine_spec_gate_NEUTER(monkeypatch, tmp_path):
    """NEUTER: amputate verify_spec → a spec-mismatched scene passes render.
    Proves the per-scene spec gate is load-bearing."""
    from lib.motion_video.engine import run_motion_task
    _fake_narration(monkeypatch, tmp_path)
    _fake_media(monkeypatch)
    # Corrupt the probe: wrong width for every scene mp4.
    monkeypatch.setattr('lib.motion_video.probe_video',
                        lambda path, **kw: {'codec': 'h264', 'width': 640,
                                            'height': 360, 'fps': 30.0,
                                            'duration': 2.0,
                                            'has_audio': False})
    # Control: real gate → engine must fail.
    task = _engine_task(tmp_path)
    run_motion_task(task)
    assert task['status'] == 'error'

    # NEUTER: gate amputated → the same corruption sails through.
    monkeypatch.setattr('lib.motion_video.verify_spec',
                        lambda *a, **kw: [])
    task2 = _engine_task(tmp_path)
    run_motion_task(task2)
    assert task2['status'] == 'done'


# ══════════════════════════════════════════════════════════
#  runtime dedup
# ══════════════════════════════════════════════════════════

def test_runtime_dedup_lifecycle():
    from lib.motion_video.runtime import (
        _motion_index_get,
        _motion_index_register,
        _motion_runtime,
        _new_motion_task,
        _motion_task_id,
    )
    key = ('sha-x', '', 'loose', '1080x1440', True, 'standard')
    tid = _motion_task_id()
    task = _new_motion_task(tid, srt_path='/x', workdir='/x', voice='',
                            speed=None, alignment='loose', narration=True,
                            quality='draft', parallel=1, width=1080,
                            height=1440)
    _motion_index_register(key, tid)
    assert _motion_index_get(key) == tid
    _motion_runtime.finish(tid, result={'ok': True})
    assert task['status'] == 'done'
    assert _motion_index_get(key) is None  # finished → pruned on next get


# ══════════════════════════════════════════════════════════
#  HTTP layer (real app)
# ══════════════════════════════════════════════════════════

def _wait_task(task_id, timeout=15.0):
    from lib.motion_video.runtime import _motion_runtime
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = _motion_runtime.get(task_id)
        if t and t['status'] in ('done', 'error', 'aborted'):
            return t
        time.sleep(0.05)
    raise AssertionError(f'task {task_id} did not finish')


def test_videos_start_validation(flask_client):
    r = flask_client.post('/api/v1/motion/videos', json={})
    assert r.status_code == 400
    r = flask_client.post('/api/v1/motion/videos',
                          json={'srt': SRT, 'aspect': '999x999'})
    assert r.status_code == 400
    r = flask_client.post('/api/v1/motion/videos',
                          json={'srt': SRT, 'alignment': 'weird'})
    assert r.status_code == 400


def test_videos_start_poll_dedup_abort(flask_client, monkeypatch, tmp_path):
    # Instant fake worker: finish with a real file as the result.
    final = tmp_path / 'final.mp4'
    final.write_bytes(b'mp4-bytes')
    sidecar = tmp_path / 'final.srt'
    sidecar.write_text('1\n00:00:01,000 --> 00:00:02,000\nx\n',
                       encoding='utf-8')

    def fake_worker(task):
        from lib.motion_video.runtime import _motion_runtime
        task['result'] = {'final_path': str(final),
                          'srt_path': str(sidecar), 'duration': 1.0,
                          'scenes': 1, 'narrated': False}
        _motion_runtime.finish(task['task_id'], result=task['result'])
    monkeypatch.setattr('lib.motion_video.engine.run_motion_task', fake_worker)

    r = flask_client.post('/api/v1/motion/videos',
                          json={'srt': SRT, 'narration': False,
                                'parallel': 1, 'quality': 'draft'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] and body['deduped'] is False
    tid = body['task_id']

    task = _wait_task(tid)
    assert task['status'] == 'done'

    r = flask_client.get(f'/api/v1/motion/videos/poll/{tid}?cursor=0')
    assert r.status_code == 200
    poll = r.get_json()
    assert poll['done'] is True
    assert poll['result']['final_path'] == str(final)

    # file serving (mp4 + srt sidecar)
    r = flask_client.get(f'/api/v1/motion/videos/{tid}/file')
    assert r.status_code == 200
    assert r.data == b'mp4-bytes'
    r = flask_client.get(f'/api/v1/motion/videos/{tid}/file?part=srt')
    assert r.status_code == 200
    assert b'-->' in r.data


def test_videos_dedup_join(flask_client, monkeypatch):
    hold = threading.Event()

    def hanging_worker(task):
        hold.wait(5)
    monkeypatch.setattr('lib.motion_video.engine.run_motion_task',
                        hanging_worker)
    body = {'srt': SRT + '\n', 'narration': False}
    r1 = flask_client.post('/api/v1/motion/videos', json=body).get_json()
    r2 = flask_client.post('/api/v1/motion/videos', json=body).get_json()
    assert r1['deduped'] is False
    assert r2['deduped'] is True
    assert r1['task_id'] == r2['task_id']
    # abort cleans the hanging worker
    flask_client.post(f"/api/v1/motion/videos/abort/{r1['task_id']}")
    hold.set()
