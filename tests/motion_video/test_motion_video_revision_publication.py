"""Revision failures must preserve the last published film and its stable URL."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from lib import motion_video as mv
from lib.motion_video import engine
from tests.motion_video.test_motion_video_edit import (
    _edit_task, _fake_media, _fake_tts, _seed_narrated_job,
)

pytestmark = pytest.mark.unit
PUBLISHED = b'previous-complete-published-film'
REPLACEMENT = b'complete-revised-film'


@pytest.fixture(params=['scene', 'narration', 'subtitles'])
def revision(request, tmp_path, monkeypatch):
    _fake_media(monkeypatch)
    _fake_tts(monkeypatch)
    job, _scenes = _seed_narrated_job(tmp_path)
    final = job / 'final.mp4'
    final.write_bytes(PUBLISHED)
    task = _edit_task(tmp_path, job, instruction='更新这一句旁白。')
    task['narration'] = True
    if request.param == 'scene':
        worker = engine.run_scene_regen_task
        task['regen_of'] = 'motion_original'
    elif request.param == 'narration':
        worker = engine.run_scene_narration_edit_task
    else:
        worker = engine.run_subtitle_style_task
        task['burn_in'] = True
        task['style'] = {'font_scale': 1.2}

    def burn(_video, _srt, output, **_kwargs):
        Path(output).write_bytes(b'reburned-video')
        return {'ok': True, 'duration': 6.0}

    original_probe = mv.probe_video

    def probe(path, **kwargs):
        result = original_probe(path, **kwargs)
        if 'scenes' not in Path(path).relative_to(job).parts:
            result = {**result, 'duration': 6.0, 'has_audio': True}
        return result

    outputs = []

    def mux(_video, _audio, output, **_kwargs):
        outputs.append(Path(output))
        Path(output).write_bytes(REPLACEMENT)
        return {'ok': True}

    monkeypatch.setattr(mv, 'burn_in_subtitles', burn)
    monkeypatch.setattr(mv, 'probe_video', probe)
    monkeypatch.setattr(mv, 'mux_audio_video', mux)
    return SimpleNamespace(task=task, final=final, job=job, outputs=outputs,
                           run=lambda: worker(task), worker=worker, probe=probe)


def _assert_not_published(revision, *, status='error'):
    assert revision.task['status'] == status, revision.task.get('error')
    assert revision.final.read_bytes() == PUBLISHED
    assert not any(event['type'] == 'final' for event in revision.task['events'])
    assert all(not path.exists() for path in revision.outputs)
    assert all(not path.parent.exists() for path in revision.outputs)


@pytest.mark.parametrize('failure', ['return_failure', 'raise', 'missing', 'cancel'])
def test_revision_mux_failure_or_cancel_never_touches_published_bytes(
        revision, monkeypatch, failure):
    def mux(_video, _audio, output, **_kwargs):
        output = Path(output)
        revision.outputs.append(output)
        if failure != 'missing':
            output.write_bytes(b'partial-new-file')
        if failure == 'raise':
            raise RuntimeError('injected mux failure')
        if failure == 'cancel':
            revision.task['abort_event'].set()
        return {'ok': failure != 'return_failure', 'detail': 'injected mux failure'}

    monkeypatch.setattr(mv, 'mux_audio_video', mux)
    revision.run()
    _assert_not_published(revision, status='aborted' if failure == 'cancel' else 'error')


@pytest.mark.parametrize('failure', ['invalid', 'mutated', 'cancel'])
def test_revision_verifies_the_same_candidate_before_publication(
        revision, monkeypatch, failure):
    def probe(path, **kwargs):
        result = revision.probe(path, **kwargs)
        if Path(path) in revision.outputs:
            if failure == 'invalid':
                return None
            if failure == 'mutated':
                Path(path).write_bytes(b'changed-during-media-verification')
            if failure == 'cancel':
                revision.task['abort_event'].set()
        return result

    monkeypatch.setattr(mv, 'probe_video', probe)
    revision.run()
    _assert_not_published(revision, status='aborted' if failure == 'cancel' else 'error')


def test_successful_revision_only_exposes_verified_complete_bytes(
        revision, monkeypatch, record_property):
    observed = []

    def probe(path, **kwargs):
        if Path(path) in revision.outputs:
            observed.append(revision.final.read_bytes())
            candidates = list(revision.job.glob('.revision-media-*'))
            assert len(candidates) == 1
            assert list(candidates[0].iterdir()) == [Path(path)]
            record_property('active_candidate_directories', len(candidates))
            record_property('active_candidate_bytes', Path(path).stat().st_size)
        return revision.probe(path, **kwargs)

    monkeypatch.setattr(mv, 'probe_video', probe)
    revision.run()
    assert revision.task['status'] == 'done', revision.task.get('error')
    assert observed == [PUBLISHED]
    assert revision.final.read_bytes() == REPLACEMENT
    assert revision.task['result']['final_path'] == str(revision.final)
    assert revision.task['result']['duration'] == 6.0
    assert all(path != revision.final and not path.exists() for path in revision.outputs)
    assert all(not path.parent.exists() for path in revision.outputs)
    retained = list(revision.job.glob('.revision-media-*'))
    assert not retained
    record_property('retained_candidate_directories', len(retained))
    assert any(event['type'] == 'final' for event in revision.task['events'])


@pytest.mark.parametrize('metadata', [
    {'width': 640}, {'duration': 99.0}, {'has_audio': False},
    {'fps': float('nan')}, {'duration': float('inf')},
])
def test_revision_rejects_invalid_media_metadata(revision, monkeypatch, metadata):
    def probe(path, **kwargs):
        result = revision.probe(path, **kwargs)
        if Path(path) in revision.outputs:
            result = {**result, **metadata}
        return result

    monkeypatch.setattr(mv, 'probe_video', probe)
    revision.run()
    _assert_not_published(revision)


@pytest.mark.parametrize('include_narration', [False, True])
def test_revision_audio_plan_uses_the_same_verified_publication(
        revision, monkeypatch, include_narration):
    plan = {'version': 1, 'duration_s': 6.0, 'tracks': [
        {'kind': 'bgm', 'path': str(revision.job / 'audio' / 'music.wav')}]}
    (revision.job / 'audio_plan.json').write_text(json.dumps(plan))
    revision.task['narration'] = include_narration
    calls = []

    def mix(video, narration, audio_plan, output, **kwargs):
        revision.outputs.append(Path(output))
        calls.append((narration, audio_plan, kwargs['abort_event']))
        assert revision.final.read_bytes() == PUBLISHED
        Path(output).write_bytes(REPLACEMENT)
        return {'ok': True}

    monkeypatch.setattr(mv, 'mix_audio_timeline', mix)
    revision.run()
    assert revision.task['status'] == 'done', revision.task.get('error')
    assert len(calls) == 1
    narration, audio_plan, abort_event = calls[0]
    assert bool(narration) == (include_narration or revision.worker is engine.run_scene_narration_edit_task)
    assert audio_plan == plan
    assert abort_event is revision.task['abort_event']
    assert revision.final.read_bytes() == REPLACEMENT
    assert all(not path.parent.exists() for path in revision.outputs)


def test_cancelled_revision_cannot_clean_or_replace_a_sibling_candidate(
        revision, monkeypatch, tmp_path):
    sibling = _edit_task(tmp_path, revision.job, instruction='另一个有效修订。')
    for field in ('narration', 'burn_in', 'style', 'regen_of'):
        if field in revision.task:
            sibling[field] = revision.task[field]
    entered, release = threading.Event(), threading.Event()
    candidates = {}

    def mux(_video, _audio, output, **kwargs):
        owner = 'stopped' if kwargs['abort_event'] is revision.task['abort_event'] else 'sibling'
        candidates[owner] = Path(output)
        Path(output).write_bytes(REPLACEMENT)
        if owner == 'stopped':
            entered.set()
            assert release.wait(20), 'test failed to release blocked codec'
        return {'ok': True}

    monkeypatch.setattr(mv, 'mux_audio_video', mux)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(revision.run)
        try:
            assert entered.wait(20), 'first revision never entered its codec'
            revision.worker(sibling)
            assert sibling['status'] == 'done', sibling.get('error')
            assert candidates['stopped'] != candidates['sibling']
            assert candidates['stopped'].exists()
            assert not candidates['sibling'].parent.exists()
        finally:
            revision.task['abort_event'].set()
            release.set()
            future.result(timeout=20)
    assert revision.task['status'] == 'aborted'
    assert revision.final.read_bytes() == REPLACEMENT
    assert all(not candidate.parent.exists() for candidate in candidates.values())
    assert not any(event['type'] == 'final' for event in revision.task['events'])


@pytest.mark.parametrize('codec_fails', [False, True])
def test_candidate_cleanup_failure_preserves_the_codec_outcome(
        revision, monkeypatch, caplog, codec_fails):
    from lib.motion_video import revision_media

    remove = revision_media.shutil.rmtree
    withheld = []

    def deny_candidate_cleanup(path, *args, **kwargs):
        if any(Path(path) == output.parent for output in revision.outputs):
            withheld.append(Path(path))
            raise OSError('injected candidate cleanup failure')
        return remove(path, *args, **kwargs)

    if codec_fails:
        def fail(_video, _audio, output, **_kwargs):
            revision.outputs.append(Path(output))
            Path(output).write_bytes(b'incomplete')
            raise RuntimeError('original codec failure')
        monkeypatch.setattr(mv, 'mux_audio_video', fail)
    monkeypatch.setattr(revision_media.shutil, 'rmtree', deny_candidate_cleanup)
    try:
        revision.run()
        assert len(withheld) == 1
        assert 'injected candidate cleanup failure' in caplog.text
        if codec_fails:
            assert revision.task['status'] == 'error'
            assert 'original codec failure' in str(revision.task['error'])
            assert revision.final.read_bytes() == PUBLISHED
        else:
            assert revision.task['status'] == 'done', revision.task.get('error')
            assert revision.final.read_bytes() == REPLACEMENT
    finally:
        monkeypatch.setattr(revision_media.shutil, 'rmtree', remove)
        for path in withheld:
            remove(path)
