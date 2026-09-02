#!/usr/bin/env python3
"""tests/test_motion_video_p3.py — P3 slice: burn-in, scenes-only engine,
scene regen, scene endpoints, paper video abstract.

Provider seams are faked; the storyboard / template / verify_spec gates run
for real. The burn-in happy-path command shape is asserted via a fake
ffmpeg; a real libass render smoke is included but skipped when the static
ffmpeg or a CJK font is unavailable.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time

import pytest

pytest_plugins = ('tests._artifact_sidecar',)

from lib import motion_video as mv

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1

SRT = """1
00:00:01,000 --> 00:00:03,000
第一句话。

2
00:00:03,000 --> 00:00:05,000
第二句话。
"""


def _make_exe(dirpath: str, name: str, body: str) -> str:
    path = os.path.join(dirpath, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def _fake_ffmpeg(tmp_path):
    marker = tmp_path / 'ffmpeg_args.txt'
    # APPENDS: burn_in_subtitles now also extracts frames for the safe-box
    # verification, so more than one invocation reaches this fake. Overwriting
    # would leave only the LAST call and hide the burn command under test.
    cli = _make_exe(str(tmp_path), 'ffmpeg', f"""#!/bin/sh
echo "$@" >> "{marker}"
for a in "$@"; do out="$a"; done
printf 'video' > "$out"
exit 0
""")
    return cli, marker


# ══════════════════════════════════════════════════════════
#  burn-in
# ══════════════════════════════════════════════════════════

def test_burn_in_command_shape(monkeypatch, tmp_path):
    video = tmp_path / 'v.mp4'
    video.write_bytes(b'vv')
    srt = tmp_path / 'f.srt'
    srt.write_text('1\n00:00:00,000 --> 00:00:01,000\n你好\n', encoding='utf-8')
    out = tmp_path / 'burned.mp4'
    ffmpeg, marker = _fake_ffmpeg(tmp_path)
    monkeypatch.setattr('lib.motion_video._env.ffmpeg_bin', lambda: ffmpeg)
    monkeypatch.setattr('lib.motion_video._gates.probe_video',
                        lambda path, **kw: {'codec': 'h264', 'width': 1080,
                                            'height': 1440, 'fps': 30.0,
                                            'duration': 4.0,
                                            'has_audio': False})
    res = mv.burn_in_subtitles(str(video), str(srt), str(out),
                               fontsdir='/fonts')
    assert res['ok'] is True, res
    args = marker.read_text()
    assert 'subtitles=' in args
    assert 'libx264' in args and '-c:v copy' not in args  # re-encode, not copy
    assert 'fontsdir=' in args
    # The burn must consume a geometry-bound ASS document, never the bare SRT:
    # a style-less SRT is what made libass assume 384x288 and clip the line
    # off both frame edges (see tests/test_motion_video_subtitle_geometry.py).
    burn_cmd = next(ln for ln in args.splitlines() if 'subtitles=' in ln)
    assert '.ass' in burn_cmd, burn_cmd
    assert 'f.srt' not in burn_cmd, burn_cmd


def test_burn_in_missing_inputs(tmp_path):
    res = mv.burn_in_subtitles('/nonexistent/v.mp4', '/nonexistent/x.srt',
                               str(tmp_path / 'o.mp4'))
    assert res['ok'] is False
    assert 'missing video file' in res['detail']


def test_escape_filter_path():
    esc = mv._concat._escape_filter_path("/a/b:c/d'e.mp4") \
        if hasattr(mv, '_concat') else None
    import lib.motion_video._concat as concat_mod
    esc = concat_mod._escape_filter_path("/a/b:c/d'e.mp4")
    assert '\\:' in esc and "\\'" in esc


def _black_clip(tmp_path, ffmpeg):
    import subprocess as sp
    video = tmp_path / 'black.mp4'
    sp.run([ffmpeg, '-y', '-f', 'lavfi', '-i', 'color=black:s=320x240:d=1',
            '-pix_fmt', 'yuv420p', str(video)], check=True,
           capture_output=True, timeout=60)
    return video


def _first_frame_png(tmp_path, ffmpeg, src, name):
    import subprocess as sp
    png = tmp_path / name
    sp.run([ffmpeg, '-y', '-i', str(src), '-frames:v', '1', str(png)],
           check=True, capture_output=True, timeout=60)
    return png


@pytest.mark.skipif(not mv.ffmpeg_bin(), reason='ffmpeg unavailable')
def test_burn_in_real_render(tmp_path):
    """REAL libass render: 1s black clip + one CJK subtitle → non-black pixels.

    Env-honest (2026-07-26): a box with no CJK-covering font now gets an
    explicit ``font_missing`` from burn_in_subtitles (the silent no-op is
    dead) — skip there, run fully where CJK glyphs exist.
    """
    ffmpeg = mv.ffmpeg_bin()
    video = _black_clip(tmp_path, ffmpeg)
    srt = tmp_path / 't.srt'
    srt.write_text('1\n00:00:00,000 --> 00:00:01,000\n测试字幕\n',
                   encoding='utf-8')
    out = tmp_path / 'burned.mp4'
    res = mv.burn_in_subtitles(str(video), str(srt), str(out))
    if res.get('category') == 'font_missing':
        pytest.skip('no font with CJK coverage on this box '
                    '(font_missing is the honest answer here)')
    assert res['ok'] is True, res
    assert res['duration'] == pytest.approx(1.0, abs=0.5)
    before = _first_frame_png(tmp_path, ffmpeg, video, 'b.png')
    after = _first_frame_png(tmp_path, ffmpeg, out, 'a.png')
    assert before.read_bytes() != after.read_bytes()


@pytest.mark.skipif(not mv.ffmpeg_bin(), reason='ffmpeg unavailable')
def test_burn_in_latin_real_render(tmp_path):
    """REAL libass render of Latin text — proves the FONTCONFIG_FILE root
    fix end-to-end on boxes whose fontconfig config was previously missing
    (before the fix this burned NOTHING and still exited 0). Skips only on
    boxes with literally zero resolvable fonts."""
    ffmpeg = mv.ffmpeg_bin()
    video = _black_clip(tmp_path, ffmpeg)
    srt = tmp_path / 'en.srt'
    srt.write_text('1\n00:00:00,000 --> 00:00:01,000\nHello world\n',
                   encoding='utf-8')
    out = tmp_path / 'burned.mp4'
    res = mv.burn_in_subtitles(str(video), str(srt), str(out))
    if res.get('category') == 'font_missing':
        pytest.skip('zero fonts resolvable on this box')
    assert res['ok'] is True, res
    before = _first_frame_png(tmp_path, ffmpeg, video, 'b.png')
    after = _first_frame_png(tmp_path, ffmpeg, out, 'a.png')
    assert before.read_bytes() != after.read_bytes()


def test_burn_in_font_failure_detected(tmp_path, monkeypatch):
    """Unit: rc=0 + libass 'failed to find any fallback' in stderr →
    category font_missing (the silent no-op is promoted to a loud,
    actionable failure). No real ffmpeg involved."""
    import lib.motion_video._concat as MC

    video = tmp_path / 'v.mp4'
    video.write_bytes(b'mp4')
    srt = tmp_path / 't.srt'
    srt.write_text('1\n00:00:00,000 --> 00:00:01,000\n测试\n', encoding='utf-8')

    def fake_run(args, **kw):
        # libass draws nothing but exits 0 and still writes the video
        with open(args[-1], 'wb') as f:
            f.write(b'mp4')
        return {'rc': 0, 'category': '', 'elapsed': 0.1,
                'err': ('[Parsed_subtitles_0] fontselect: failed to find any '
                        'fallback with glyph 0x6D4B for font: (Arial, 400, 0)')}

    # Pure unit test: _run_ffmpeg is faked, so any non-empty ffmpeg path
    # satisfies the env gate (a box without ffmpeg — e.g. public CI — must
    # still exercise the font-failure classifier, not die at env_missing).
    monkeypatch.setattr('lib.motion_video._env.ffmpeg_bin',
                        lambda: '/fake/ffmpeg')
    monkeypatch.setattr('lib.motion_video._gates.probe_video',
                        lambda p, **kw: None)
    monkeypatch.setattr(MC, '_run_ffmpeg', fake_run)
    res = mv.burn_in_subtitles(str(video), str(srt), str(tmp_path / 'o.mp4'))
    assert res['ok'] is False
    assert res['category'] == 'font_missing', res
    assert 'font' in res['detail']
    # the no-op output must NOT be promoted to the final path
    assert not (tmp_path / 'o.mp4').exists()


def test_burn_in_font_detection_NEUTER(tmp_path, monkeypatch):
    """NEUTER: amputate the stderr scan → the SAME libass failure sails
    through as ok — the scan is what kills the silent no-op."""
    import lib.motion_video._concat as MC

    video = tmp_path / 'v.mp4'
    video.write_bytes(b'mp4')
    srt = tmp_path / 't.srt'
    srt.write_text('1\n00:00:00,000 --> 00:00:01,000\n测试\n', encoding='utf-8')

    def fake_run(args, **kw):
        with open(args[-1], 'wb') as f:
            f.write(b'mp4')
        return {'rc': 0, 'category': '', 'elapsed': 0.1,
                'err': 'fontselect: failed to find any fallback with glyph 0x0'}

    # See the companion test: the fake _run_ffmpeg makes any non-empty
    # ffmpeg path sufficient — no real binary required on CI boxes.
    monkeypatch.setattr('lib.motion_video._env.ffmpeg_bin',
                        lambda: '/fake/ffmpeg')
    monkeypatch.setattr(MC, '_run_ffmpeg', fake_run)
    monkeypatch.setattr(MC, '_font_burn_failed', lambda err: False)
    monkeypatch.setattr('lib.motion_video._gates.probe_video',
                        lambda p, **kw: {'duration': 1.0, 'has_audio': False})
    res = mv.burn_in_subtitles(str(video), str(srt), str(tmp_path / 'o.mp4'))
    assert res['ok'] is True, res


def test_build_render_env_fontconfig_fallback(monkeypatch):
    """Fix A: no system fonts.conf + conda one present → FONTCONFIG_FILE
    injected; system config present → no injection; the operator's own
    FONTCONFIG_FILE always wins."""
    import sys as _sys
    from lib.motion_video import _env as EN

    conda_conf = os.path.join(_sys.prefix, 'etc', 'fonts', 'fonts.conf')
    monkeypatch.setattr(EN, 'ffmpeg_bin', lambda: '')
    monkeypatch.setattr(EN, 'ffprobe_bin', lambda: '')
    monkeypatch.setattr(EN, 'chrome_bin', lambda: '')
    monkeypatch.setattr(EN, '_conda_gui_lib_dir', lambda: '')
    monkeypatch.delenv('FONTCONFIG_FILE', raising=False)

    # chromium_env.fontconfig_paths() gates on ``os.path.isdir('/etc/fonts')``,
    # NOT on isfile of the .conf — so BOTH path probes must be scripted or the
    # case split depends on the host (rootless dev box: no /etc/fonts; ubuntu
    # CI: /etc/fonts present). Drive isdir explicitly so each case means the
    # same thing on every box.
    real_isdir = os.path.isdir

    def _isdir_without_system_fonts(p):
        return False if p == '/etc/fonts' else real_isdir(p)

    def _isdir_with_system_fonts(p):
        return True if p == '/etc/fonts' else real_isdir(p)

    # case 1: no system fontconfig dir, only the conda config exists → injected
    monkeypatch.setattr(os.path, 'isdir', _isdir_without_system_fonts)
    monkeypatch.setattr(os.path, 'isfile', lambda p: p == conda_conf)
    env = EN.build_render_env(base={})
    assert env.get('FONTCONFIG_FILE') == conda_conf

    # case 2: system config present → no injection
    monkeypatch.setattr(os.path, 'isdir', _isdir_with_system_fonts)
    monkeypatch.setattr(os.path, 'isfile',
                        lambda p: p == '/etc/fonts/fonts.conf')
    env = EN.build_render_env(base={})
    assert 'FONTCONFIG_FILE' not in env

    # case 3: operator override wins even when the fallback would apply
    monkeypatch.setattr(os.path, 'isdir', _isdir_without_system_fonts)
    monkeypatch.setattr(os.path, 'isfile', lambda p: p == conda_conf)
    env = EN.build_render_env(base={'FONTCONFIG_FILE': '/custom/fonts.conf'})
    assert env['FONTCONFIG_FILE'] == '/custom/fonts.conf'


# ══════════════════════════════════════════════════════════
#  engine: scenes-only + burn-in passthrough
# ══════════════════════════════════════════════════════════

def _scenes_only_task(tmp_path, scenes, **over):
    from lib.motion_video.runtime import _new_motion_task, _motion_task_id
    scenes_path = tmp_path / 'scenes.json'
    scenes_path.write_text(json.dumps(scenes), encoding='utf-8')
    kw = dict(srt_path='', workdir=str(tmp_path / 'job'), voice='',
              speed=None, alignment='loose', narration=False,
              quality='draft', parallel=2, width=1080, height=1440,
              scenes_path=str(scenes_path))
    kw.update(over)
    return _new_motion_task(_motion_task_id(), **kw, user_id=TEST_OWNER_USER_ID)


def _fake_media(monkeypatch):
    def fake_render(project_dir, output, **kw):
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
    monkeypatch.setattr(
        'lib.motion_video.concat_mp4s',
        lambda inputs, output, **kw: _touch(output) or {
            'ok': True, 'output': output, 'duration': 8.0, 'mode': 'copy',
            'elapsed': 0.1})
    monkeypatch.setattr(
        'lib.motion_video.mux_audio_video',
        lambda v, a, output, **kw: _touch(output) or {
            'ok': True, 'output': output, 'duration': 8.0, 'elapsed': 0.1})


def _touch(path):
    with open(path, 'wb') as f:
        f.write(b'mp4')
    return None


def test_engine_scenes_only_run(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_motion_task
    _fake_media(monkeypatch)
    scenes = [{'id': 'scene-001', 'start': 0.0, 'end': 3.0, 'text': '第一句。'},
              {'id': 'scene-002', 'start': 3.0, 'end': 6.0, 'text': '第二句。'}]
    task = _scenes_only_task(tmp_path, scenes)
    run_motion_task(task)
    assert task['status'] == 'done', task.get('error')
    phases = [e.get('phase') for e in task['events'] if e['type'] == 'phase']
    assert 'parse' not in phases          # no SRT → no parse phase
    assert phases[0] == 'storyboard'
    assert os.path.isfile(task['result']['srt_path'])  # sidecar still built


def test_engine_passes_real_overlap_plan_and_renders_visual_handle(
        monkeypatch, tmp_path):
    from lib.motion_video.engine import run_motion_task
    _fake_media(monkeypatch)
    seen = {}

    def fake_concat(inputs, output, **kwargs):
        seen['transitions'] = kwargs.get('transitions')
        _touch(output)
        return {'ok': True, 'output': output, 'duration': 6.0,
                'mode': 'transition', 'elapsed': 0.1}

    monkeypatch.setattr('lib.motion_video.concat_mp4s', fake_concat)
    scenes = [
        {'id': 'scene-001', 'start': 0.0, 'end': 3.0, 'text': '第一句。'},
        {'id': 'scene-002', 'start': 3.0, 'end': 6.0, 'text': '第二句。',
         'transition_in': 'dissolve'},
    ]
    task = _scenes_only_task(tmp_path, scenes)
    run_motion_task(task)
    assert task['status'] == 'done', task.get('error')
    persisted = json.loads(
        (tmp_path / 'job' / 'scenes.json').read_text(encoding='utf-8'))
    assert persisted[0]['content_duration_s'] == pytest.approx(3.0)
    assert persisted[0]['render_duration_s'] == pytest.approx(3.4)
    assert persisted[0]['outgoing_handle_s'] == pytest.approx(0.4)
    assert seen['transitions'][0]['ffmpeg'] == 'fade'
    assert seen['transitions'][0]['duration_s'] == pytest.approx(0.4)
    first_html = (tmp_path / 'job' / 'scenes' / 'scene-001' /
                  'index.html').read_text(encoding='utf-8')
    assert 'data-duration="3.4"' in first_html
    sidecar = (tmp_path / 'job' / 'final.srt').read_text(encoding='utf-8')
    assert '00:00:03,000' in sidecar


def test_audio_plan_api_rejects_ambiguous_or_missing_inputs(
        flask_client, tmp_path):
    both = flask_client.post('/api/v1/motion/videos', json={
        'srt': SRT, 'audio_plan': {'bgm': {}},
        'audio_plan_path': str(tmp_path / 'plan.json'),
    })
    # Missing path is caught before ambiguity becomes a worker-side error.
    assert both.status_code == 400
    malformed = flask_client.post('/api/v1/motion/videos', json={
        'srt': SRT, 'audio_plan': ['not', 'an', 'object'],
    })
    assert malformed.status_code == 400
    missing = flask_client.post('/api/v1/motion/videos', json={
        'srt': SRT, 'audio_plan_path': str(tmp_path / 'missing.json'),
    })
    assert missing.status_code == 400


def test_engine_scenes_only_gap_rejected(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_motion_task
    _fake_media(monkeypatch)
    scenes = [{'id': 'scene-001', 'start': 0.0, 'end': 3.0, 'text': '一。'},
              {'id': 'scene-002', 'start': 4.0, 'end': 6.0, 'text': '二。'}]
    task = _scenes_only_task(tmp_path, scenes)
    run_motion_task(task)
    assert task['status'] == 'error'
    assert 'storyboard gate' in str(task['error'])


def test_engine_burn_in_step(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_motion_task
    _fake_media(monkeypatch)
    calls = []
    monkeypatch.setattr(
        'lib.motion_video.burn_in_subtitles',
        lambda v, s, output, **kw: calls.append((v, output)) or _touch(output)
        or {'ok': True, 'output': output, 'duration': 8.0, 'elapsed': 0.1})
    scenes = [{'id': 'scene-001', 'start': 0.0, 'end': 3.0, 'text': '一。'}]
    task = _scenes_only_task(tmp_path, scenes, narration=False)
    task['burn_in'] = True
    run_motion_task(task)
    assert task['status'] == 'done', task.get('error')
    assert calls, 'burn_in_subtitles must be called when burn_in=True'
    assert any(e.get('phase') == 'burn_in' for e in task['events'])


# ══════════════════════════════════════════════════════════
#  scene regen worker
# ══════════════════════════════════════════════════════════

def _seed_job(tmp_path):
    job = tmp_path / 'job'
    scenes = [
        {'id': 'scene-001', 'start': 0.0, 'end': 3.0,
         'text': '价格是一项关键证据。',
         'shot_recipe': 'hook-counter-burst',
         'motion_family': 'metric-impact', 'shot_energy': 5,
         'qa_progresses': [0.05, 0.46, 0.78, 0.94],
         'plan_findings': []},
        {'id': 'scene-002', 'start': 3.0, 'end': 6.0,
         'text': '这意味着更高的效率。',
         'shot_recipe': 'messaging-multi-phrase',
         'motion_family': 'kinetic-type', 'shot_energy': 3,
         'qa_progresses': [0.06, 0.46, 0.8, 0.95],
         'plan_findings': []},
    ]
    (job / 'audio').mkdir(parents=True)
    (job / 'scenes.json').write_text(json.dumps(scenes), encoding='utf-8')
    for sc in scenes:
        d = job / 'scenes' / sc['id']
        d.mkdir(parents=True)
        (d / 'index.html').write_text(
            '<div data-composition-id="main" data-duration="3.0" '
            'data-width="1080" data-height="1440"></div>', encoding='utf-8')
        (d / f"{sc['id']}.mp4").write_bytes(b'old')
    (job / 'audio' / 'narration.wav').write_bytes(b'wav')
    (job / 'final.mp4').write_bytes(b'old-final')
    return job, scenes


def test_scene_regen_happy(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_regen_task
    from lib.motion_video.runtime import _new_motion_task, _motion_task_id
    _fake_media(monkeypatch)
    job, _sc = _seed_job(tmp_path)
    task = _new_motion_task(_motion_task_id(), srt_path='', workdir=str(job),
                            voice='', speed=None, alignment='loose',
                            narration=True, quality='draft', parallel=1,
                            width=1080, height=1440, user_id=TEST_OWNER_USER_ID)
    task['scene_id'] = 'scene-002'
    task['regen_of'] = 'motion_original'
    task['narration'] = True
    run_scene_regen_task(task)
    assert task['status'] == 'done', task.get('error')
    result = task['result']
    assert result['regen_of'] == 'motion_original'
    assert result['scene_id'] == 'scene-002'
    assert (job / 'final.mp4').read_bytes() == b'mp4'  # replaced by assembly


def test_scene_regen_missing_scene_errors(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_regen_task
    from lib.motion_video.runtime import _new_motion_task, _motion_task_id
    _fake_media(monkeypatch)
    job, _sc = _seed_job(tmp_path)
    task = _new_motion_task(_motion_task_id(), srt_path='', workdir=str(job),
                            voice='', speed=None, alignment='loose',
                            narration=False, quality='draft', parallel=1,
                            width=1080, height=1440, user_id=TEST_OWNER_USER_ID)
    task['scene_id'] = 'scene-999'
    run_scene_regen_task(task)
    assert task['status'] == 'error'


# ══════════════════════════════════════════════════════════
#  HTTP: scenes list / scene file / regen
# ══════════════════════════════════════════════════════════

def _done_task_with_job(flask_client, monkeypatch, tmp_path):
    job, scenes = _seed_job(tmp_path)
    sidecar = tmp_path / 'final.srt'
    sidecar.write_text('1\n00:00:00,000 --> 00:00:03,000\n一。\n',
                       encoding='utf-8')

    def fake_worker(task):
        from lib.motion_video.runtime import _motion_runtime
        task['result'] = {'final_path': str(job / 'final.mp4'),
                          'srt_path': str(sidecar), 'duration': 6.0,
                          'scenes': 2, 'narrated': False,
                          'workdir': str(job)}
        _motion_runtime.finish(task['task_id'], result=task['result'])
    monkeypatch.setattr('lib.motion_video.engine.run_motion_task', fake_worker)
    body = flask_client.post('/api/v1/motion/videos',
                             json={'srt': SRT, 'narration': False,
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


def test_scenes_list_and_file(flask_client, monkeypatch, tmp_path):
    tid, job = _done_task_with_job(flask_client, monkeypatch, tmp_path)
    r = flask_client.get(f'/api/v1/motion/videos/{tid}/scenes')
    assert r.status_code == 200
    scenes = r.get_json()['scenes']
    assert len(scenes) == 2
    assert all(s['has_video'] and s['has_composition'] for s in scenes)
    assert scenes[0]['shot_recipe'] == 'hook-counter-burst'
    assert scenes[0]['motion_family'] == 'metric-impact'
    assert scenes[0]['shot_energy'] == 5
    assert scenes[0]['qa_progresses'] == [0.05, 0.46, 0.78, 0.94]
    r = flask_client.get(f'/api/v1/motion/videos/{tid}/scenes/scene-001/file')
    assert r.status_code == 200
    assert r.data == b'old'
    r = flask_client.get(f'/api/v1/motion/videos/{tid}/scenes/scene-999/file')
    assert r.status_code == 404


def test_shot_recipe_catalog_endpoint(flask_client):
    r = flask_client.get('/api/v1/motion/shot-recipes')
    assert r.status_code == 200
    body = r.get_json()
    assert body['contract_version'] == 'motion-shot-v1'
    assert body['count'] == len(body['recipes']) == 13
    demo = next(item for item in body['recipes']
                if item['id'] == 'demo-page-scroll-spotlight')
    assert demo['motion_family'] == 'product-demo'
    assert len(demo['qa_progresses']) == 4
    assert demo['hold_s'] > 0
    assert demo['constraints']


def test_audio_contract_endpoint(flask_client):
    r = flask_client.get('/api/v1/motion/audio-contract')
    assert r.status_code == 200
    body = r.get_json()
    assert body['contract_version'] == 'motion-audio-v1'
    assert body['template']['bgm']['asset']
    assert body['template']['mix']['loudness_lufs'] == -14


def test_scene_regen_endpoint(flask_client, monkeypatch, tmp_path):
    tid, job = _done_task_with_job(flask_client, monkeypatch, tmp_path)
    fired = []
    monkeypatch.setattr('lib.motion_video.engine.run_scene_regen_task',
                        lambda task: fired.append(task['scene_id']))
    r = flask_client.post(
        f'/api/v1/motion/videos/{tid}/scenes/scene-002/regen')
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] and body['regen_of'] == tid
    t0 = time.time()
    while not fired and time.time() - t0 < 5:
        time.sleep(0.05)
    assert fired == ['scene-002']


# ══════════════════════════════════════════════════════════
#  paper video abstract
# ══════════════════════════════════════════════════════════

REPORT = ('# 研究报告\n\n本文提出新方法,在 130 亿参数规模上验证。\n\n' * 30 +
         '## 消融实验\n\n成绩提升 3.2 个百分点。\n\n' * 30)


def test_build_abstract_scenes_bounds():
    scenes = mv  # placeholder removed below
    from lib.paper.video_abstract import build_abstract_scenes
    scenes = build_abstract_scenes(REPORT, max_scenes=8)
    assert 1 <= len(scenes) <= 8
    cursor = 0.0
    for sc in scenes:
        assert sc['start'] == pytest.approx(cursor)
        dur = sc['end'] - sc['start']
        assert 3.0 <= dur <= 15.0
        cursor = sc['end']
    assert mv.check_storyboard(
        scenes, (scenes[0]['start'], scenes[-1]['end'])) == []
    assert '#' not in scenes[0]['text']  # markdown stripped


def test_build_abstract_scenes_empty():
    from lib.paper.video_abstract import build_abstract_scenes
    assert build_abstract_scenes('') == []
    assert build_abstract_scenes('### \n\n```') == []


def _insert_report(phash, lang='zh'):
    import uuid
    from lib.storage import get_storage_client
    get_storage_client(write=True).command('paper.report.upsert', {
        'user_id': TEST_OWNER_USER_ID,
        'paper_hash': phash,
        'lang': lang,
        'report': REPORT,
        'model': 'm',
        'meta': {},
        'created_at': int(time.time()),
    }, f'paper.video.report:{uuid.uuid4().hex}')


def test_paper_video_start_flow(flask_client, monkeypatch, tmp_path):
    import uuid
    phash = uuid.uuid4().hex[:16]
    # no report → report_required
    r = flask_client.post('/api/v1/paper/video/start',
                          json={'paper_hash': phash, 'narration': False})
    assert r.get_json().get('report_required') is True

    _insert_report(phash)
    monkeypatch.setattr('lib.motion_video.engine.run_motion_task',
                        lambda task: None)
    r = flask_client.post('/api/v1/paper/video/start',
                          json={'paper_hash': phash, 'narration': False})
    body = r.get_json()
    assert body['ok'] is True
    assert body['task_id'].startswith('motion_')
    assert body['scenes'] >= 1
    assert body['source_kind'].startswith('report')

    r = flask_client.post('/api/v1/paper/video/start', json={})
    assert r.status_code == 400
    r = flask_client.post('/api/v1/paper/video/start', json={})
    assert r.status_code == 400


def test_paper_video_model_threaded_and_deduped(flask_client, monkeypatch,
                                                tmp_path):
    """The video start route's `model` must reach the beat-writer AND the
    task, and it rides the dedup key: same model joins the in-flight task,
    a different model starts a NEW one (cache-key-skew family)."""
    import uuid
    import lib.paper.video_abstract as VA

    phash = uuid.uuid4().hex[:16]
    _insert_report(phash)
    seen = {}

    def fake_build(text, **kw):
        seen['model'] = kw.get('model')
        return [{'id': 'scene-001', 'start': 0.0, 'end': 3.0,
                 'text': '一。', 'on_screen': '要点', 'visual': ''}]

    monkeypatch.setattr(VA, 'build_abstract_scenes', fake_build)
    monkeypatch.setattr('lib.motion_video.engine.run_motion_task',
                        lambda task: None)
    monkeypatch.setattr('lib.motion_video._env.motion_root',
                        lambda: str(tmp_path))

    r1 = flask_client.post('/api/v1/paper/video/start',
                           json={'paper_hash': phash, 'narration': False,
                                 'model': 'm-alpha'})
    b1 = r1.get_json()
    assert b1['ok'] and b1['task_id'], b1
    assert seen['model'] == 'm-alpha'
    from lib.motion_video.runtime import _motion_runtime
    t1 = _motion_runtime.get(b1['task_id'])
    assert t1 and (t1.get('model') or '') == 'm-alpha'

    # same key incl. model → dedup join
    r2 = flask_client.post('/api/v1/paper/video/start',
                           json={'paper_hash': phash, 'narration': False,
                                 'model': 'm-alpha'})
    b2 = r2.get_json()
    assert b2.get('deduped') is True and b2['task_id'] == b1['task_id']

    # a DIFFERENT model must NOT join — the user asked for a new film
    r3 = flask_client.post('/api/v1/paper/video/start',
                           json={'paper_hash': phash, 'narration': False,
                                 'model': 'm-beta'})
    b3 = r3.get_json()
    assert b3['ok'] and b3['task_id'] != b1['task_id']
    assert not b3.get('deduped')

    # and the lookup surfaces the making-model for the panel's badge
    r4 = flask_client.get(f'/api/v1/paper/video/lookup?paper_hash={phash}')
    b4 = r4.get_json()
    assert b4['found'] and (b4.get('model') or '') in ('m-alpha', 'm-beta')


def test_job_manifest_carries_model(tmp_path):
    """model is a manifest field: a crash-resume must not silently swap the
    user's pick for the dispatcher default."""
    from lib.motion_video.engine import write_job_manifest
    from lib.production.jobs import read_manifest
    from lib.motion_video.runtime import _motion_task_id, _new_motion_task

    task = _new_motion_task(_motion_task_id(), srt_path='',
                            workdir=str(tmp_path), voice='', speed=None,
                            alignment='loose', narration=False,
                            quality='draft', parallel=1, width=1080,
                            height=1440, user_id=TEST_OWNER_USER_ID)
    task['model'] = 'm-alpha'
    write_job_manifest(task, kind='paper', state='running')
    m = read_manifest(str(tmp_path))
    assert m and (m.get('model') or '') == 'm-alpha'


def test_engine_compose_passes_task_model_to_author(monkeypatch, tmp_path):
    """The compose stage must hand the task's model to the scene author's
    dispatch — otherwise only the beats honor the user's pick and the
    per-scene compositions silently come from the default model."""
    from lib.motion_video.engine import run_motion_task
    from lib.motion_video._template import render_scene_html
    _fake_media(monkeypatch)
    monkeypatch.setattr('lib.motion_video.engine._scene_gate_findings',
                        lambda *a, **k: [])
    captured = {}

    def fake_author(sc, scene_dir, **kw):
        captured['model'] = kw.get('model')
        html = render_scene_html(sc, width=1080, height=1440, duration=3.0,
                                 scene_index=1, total_scenes=1)
        return {'ok': True, 'html': html, 'mode': 'authored',
                'rounds': 1, 'tokens': 10, 'detail': ''}

    monkeypatch.setattr('lib.motion_video._scene_author.author_scene',
                        fake_author)
    scenes = [{'id': 'scene-001', 'start': 0.0, 'end': 3.0, 'text': '一。'}]
    task = _scenes_only_task(tmp_path, scenes)
    task['scene_author'] = True
    task['model'] = 'm-alpha'
    run_motion_task(task)
    assert task['status'] == 'done', task.get('error')
    assert captured.get('model') == 'm-alpha'


def test_paper_video_lookup(flask_client):
    import uuid
    from lib.motion_video.runtime import _new_motion_task, _motion_runtime
    phash = uuid.uuid4().hex[:16]
    # nothing yet → found False, report_available False
    r = flask_client.get(f'/api/v1/paper/video/lookup?paper_hash={phash}')
    body = r.get_json()
    assert body['ok'] and body['found'] is False
    # seed a finished motion task tagged with this paper
    task = _new_motion_task('motion_lookup1', srt_path='', workdir='/x',
                            voice='', speed=None, alignment='loose',
                            narration=False, quality='draft', parallel=1,
                            width=1080, height=1440, user_id=TEST_OWNER_USER_ID)
    task['paper_hash'] = phash
    _motion_runtime.finish('motion_lookup1',
                           result={'final_path': '/x/final.mp4',
                                   'srt_path': '/x/final.srt', 'duration': 6.0})
    r = flask_client.get(f'/api/v1/paper/video/lookup?paper_hash={phash}')
    body = r.get_json()
    assert body['found'] is True
    assert body['running'] is False
    assert body['task_id'] == 'motion_lookup1'
    assert body['result']['final_path'] == '/x/final.mp4'
    r = flask_client.get('/api/v1/paper/video/lookup')
    assert r.status_code == 400
