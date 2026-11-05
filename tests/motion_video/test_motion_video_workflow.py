"""Film delivery uses the shared workflow and validates before publication.

The real worker, stages, gates and files run; codecs and TTS use the existing
provider seams. A successful subprocess is not a delivered, valid film.
"""
from pathlib import Path
import json
import re
import subprocess

import pytest

from lib import motion_video as mv
from lib.motion_video import engine
from tests.motion_video.test_motion_video_engine import (
    _engine_task, _fake_media, _fake_narration,
)

pytestmark = pytest.mark.unit


def _film(tmp_path, monkeypatch, *, narration=False):
    _fake_media(monkeypatch)
    if narration:
        _fake_narration(monkeypatch, tmp_path)
    task = _engine_task(tmp_path, narration=narration)
    task['scene_author'] = False
    # Browser geometry/HyperFrames checks have their own real-media suites.
    # Keep these delivery tests offline while retaining the deterministic
    # composition, scene-spec and final-film gates.
    monkeypatch.setattr(mv, 'check_project', lambda *args, **kwargs: {'ok': True})
    monkeypatch.setattr(mv, 'measure_fill', lambda *args, **kwargs: None)
    original_probe = mv.probe_video

    def probe(path, **kwargs):
        result = original_probe(path, **kwargs)
        if Path(path).parent == Path(task['workdir']):
            scenes = json.loads((Path(task['workdir']) / 'scenes.json').read_text())
            result['duration'] = sum(scene['content_duration_s'] for scene in scenes)
            result['has_audio'] = narration
        return result

    monkeypatch.setattr(mv, 'probe_video', probe)
    final = Path(task['workdir']) / 'final.mp4'
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b'previous-published-film')
    return task, final


def _assert_not_delivered(task, final, *, status='error'):
    assert task['status'] == status, task.get('error')
    assert final.read_bytes() == b'previous-published-film'
    assert not any(event['type'] == 'final' for event in task['events'])
    manifest = json.loads((final.parent / 'job.json').read_text())
    assert manifest['state'] == status


def _offline_author(monkeypatch, task, *, craft_reads=()):
    """Synthetic author response; real composition and telemetry boundaries."""
    from lib.motion_video import _scene_author, _template
    task['scene_author'] = True
    monkeypatch.setattr(_scene_author, 'prepare_parallel_author_dependencies',
                        lambda **kwargs: True)
    monkeypatch.setattr('lib.motion_video._asset_preflight.prepare_scene_assets',
                        lambda *args, **kwargs: {'resolved': [], 'findings': []})
    monkeypatch.setattr(engine, '_visual_qa_round',
                        lambda task, scene, directory, path, html, **kwargs: html)

    def author(scene, scene_dir, **kwargs):
        html = _template.render_scene_html(
            scene, width=kwargs['width'], height=kwargs['height'],
            duration=kwargs['duration'], scene_index=kwargs['scene_index'],
            total_scenes=kwargs['total_scenes'], theme=kwargs.get('theme'))
        html = html.replace(_template.TEMPLATE_MARKER,
                            'data-tofu-composition="authored-fixture"')
        from lib.motion_video._fonts import font_face_css
        fonts = Path(scene_dir) / 'assets'
        fonts.mkdir(exist_ok=True)
        (fonts / 'cjk-sans.woff2').write_bytes(b'wOF2' + b'0' * 64)
        html = html.replace('</head>', '<style>' + font_face_css('assets/cjk-sans.woff2')
                            + "body {font-family: 'Tofu Sans SC', sans-serif;}"
                            + '</style></head>')
        html = html.replace('</body>',
            '<svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="20"/></svg></body>')
        return {'ok': True, 'html': html, 'mode': 'authored', 'rounds': 1,
                'tokens': 10, 'detail': '', 'craft_reads': list(craft_reads)}

    monkeypatch.setattr(_scene_author, 'author_scene', author)


def test_full_film_is_a_declared_workflow(tmp_path, monkeypatch, record_property):
    task, final = _film(tmp_path, monkeypatch)
    engine.run_motion_task(task)
    assert task['status'] == 'done', task.get('error')
    workflow = next(event['workflow'] for event in task['events']
                    if event['type'] == 'workflow_started')
    names = [node['name'] for node in workflow['nodes']]
    assert names == ['storyboard', 'review', 'narration', 'timeline', 'compose',
                     'render', 'concat', 'subtitles', 'mux', 'verify', 'publish']
    completed = [event['stage'] for event in task['events']
                 if event['type'] == 'stage_done']
    assert completed == names
    assert final.read_bytes() == b'mp4'
    assert Path(task['result']['srt_path']).is_file()
    assert (final.parent / 'final_silent.mp4').is_file()
    state_path = final.parent / 'film_state.json'
    state = json.loads(state_path.read_text())
    assert len(state['stages']) == len(names)
    record_property('film_checkpoint_bytes', state_path.stat().st_size)
    record_property('film_scenes', task['result']['scenes'])


@pytest.mark.parametrize('bad_probe', [None, {'duration': 99.0}, {'width': 640},
                                      {'fps': float('nan')}, {'has_audio': False}])
def test_invalid_final_media_never_replaces_a_published_film(
        tmp_path, monkeypatch, bad_probe):
    task, final = _film(tmp_path, monkeypatch, narration=True)
    original_probe = mv.probe_video

    def probe(path, **kwargs):
        result = original_probe(path, **kwargs)
        if Path(path).parent == final.parent:
            return None if bad_probe is None else {**result, **bad_probe}
        return result

    monkeypatch.setattr(mv, 'probe_video', probe)
    engine.run_motion_task(task)
    _assert_not_delivered(task, final)
    assert any(event['type'] == 'stage_failed' and event['stage'] == 'verify'
               for event in task['events'])


@pytest.mark.parametrize('abort', [False, True])
def test_mux_failure_or_abort_preserves_published_film(tmp_path, monkeypatch, abort):
    task, final = _film(tmp_path, monkeypatch, narration=True)

    def mux(video, audio, output, **kwargs):
        Path(output).write_bytes(b'incomplete-new-film')
        if abort:
            task['abort_event'].set()
            return {'ok': True}
        raise RuntimeError('injected mux failure')

    monkeypatch.setattr(mv, 'mux_audio_video', mux)
    engine.run_motion_task(task)
    _assert_not_delivered(task, final, status='aborted' if abort else 'error')


def test_missing_mux_output_cannot_reuse_old_final(tmp_path, monkeypatch):
    task, final = _film(tmp_path, monkeypatch, narration=True)
    monkeypatch.setattr(mv, 'mux_audio_video', lambda *args, **kwargs: {'ok': True})
    engine.run_motion_task(task)
    _assert_not_delivered(task, final)


def test_cancel_after_verification_never_publishes(tmp_path, monkeypatch):
    task, final = _film(tmp_path, monkeypatch)
    emit = engine._emit

    def observe(task, event):
        emit(task, event)
        if event['type'] == 'stage_done' and event['stage'] == 'verify':
            task['abort_event'].set()

    monkeypatch.setattr(engine, '_emit', observe)
    engine.run_motion_task(task)
    _assert_not_delivered(task, final, status='aborted')


def test_changed_candidate_is_rejected_before_publication(tmp_path, monkeypatch):
    task, final = _film(tmp_path, monkeypatch)
    emit = engine._emit

    def observe(task, event):
        emit(task, event)
        if event['type'] == 'stage_done' and event['stage'] == 'verify':
            (final.parent / 'final.pending.mp4').write_bytes(b'changed-after-probe')

    monkeypatch.setattr(engine, '_emit', observe)
    engine.run_motion_task(task)
    _assert_not_delivered(task, final)


def test_verification_checkpoint_failure_prevents_publication(tmp_path, monkeypatch):
    from lib.production import stages
    task, final = _film(tmp_path, monkeypatch)
    commit = stages._commit

    def fail_commit(path, state, stage, artifact):
        if stage.name == 'verify':
            raise OSError('injected checkpoint failure')
        return commit(path, state, stage, artifact)

    monkeypatch.setattr(stages, '_commit', fail_commit)
    engine.run_motion_task(task)
    _assert_not_delivered(task, final)


def test_reentry_reuses_valid_narration_and_rendered_scenes(tmp_path, monkeypatch):
    task, final = _film(tmp_path, monkeypatch, narration=True)
    engine.run_motion_task(task)
    assert task['status'] == 'done', task.get('error')

    def must_reuse(*args, **kwargs):
        raise AssertionError('validated work was repeated')

    monkeypatch.setattr(mv, 'synthesize_scene_narrations', must_reuse)
    monkeypatch.setattr(mv, 'render_project', must_reuse)
    resumed = _engine_task(tmp_path, narration=True)
    engine.run_motion_task(resumed)
    assert resumed['status'] == 'done', resumed.get('error')
    assert final.is_file()
    assert any(event.get('resumed') for event in resumed['events']
               if event['type'] == 'scene_done')


def test_real_codecs_deliver_verified_timeline(tmp_path, monkeypatch, record_property):
    """Real encode/concat/audio mux/probe; color frames replace browser capture.

    This proves the media boundary, not visual design or real-model quality.
    Only an already-installed codec is used; the test never provisions tools.
    """
    from lib.motion_video._env import ffmpeg_bin
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        pytest.skip('no installed FFmpeg')
    media_providers = {name: getattr(mv, name) for name in (
        'probe_video', 'concat_mp4s', 'mux_audio_video')}
    task, final = _film(tmp_path, monkeypatch, narration=True)
    task.update(width=320, height=180)
    for name, provider in media_providers.items():
        monkeypatch.setattr(mv, name, provider)

    def render(project_dir, output, **kwargs):
        html = (Path(project_dir) / 'index.html').read_text()
        duration = float(re.search(r'data-duration="([0-9.]+)"', html)[1])
        subprocess.run([
            ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
            f'color=c=navy:s=320x180:r=30:d={duration}', '-an', '-c:v',
            'libx264', '-pix_fmt', 'yuv420p', '-y', output,
        ], check=True, capture_output=True, timeout=60)
        return {'ok': True, 'output': output}

    monkeypatch.setattr(mv, 'render_project', render)
    engine.run_motion_task(task)
    assert task['status'] == 'done', task.get('error')
    probe = mv.probe_video(str(final))
    assert probe['duration'] == pytest.approx(8.0, abs=0.15)
    assert (probe['width'], probe['height']) == (320, 180)
    assert probe['has_audio'] is True
    record_property('silent_baseline_bytes', (final.parent / 'final_silent.mp4').stat().st_size)
    record_property('published_video_bytes', final.stat().st_size)
