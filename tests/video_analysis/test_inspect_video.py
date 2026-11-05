"""Executable specs for ``inspect_video`` — the read-only video sibling of
``inspect_image``: probe facts, timestamped key frames returned AS IMAGES in
the read_files batch shape, and an optional timestamped transcript. An
audio-only file is a first-class input: probe facts and the transcript as
text, never frames.

Media specs decode REAL synthetic clips (testsrc + sine, a few seconds) and
sine-only audio files with the repository's ffmpeg owner. A host without ffmpeg skips only those specs,
with an explicit reason; path-resolution, validation and registry specs run
everywhere.
"""
from __future__ import annotations

import base64
import io
import os
import re
import subprocess

import pytest

pytestmark = pytest.mark.unit

_FRAME_LINE = re.compile(r'^\s+(\d+)\. (\d{2}:\d{2}) \(([0-9.]+)s\)$', re.M)


def _ffmpeg() -> str:
    from lib.motion_video._env import ffmpeg_bin
    ff = ffmpeg_bin()
    if not ff:
        pytest.skip('ffmpeg unavailable on this host (install imageio-ffmpeg or '
                    'set TOFU_FFMPEG); media-decoding spec skipped')
    return ff


def _make_clip(path, *, seconds=3.0, size='1920x1080', audio=True) -> str:
    ff = _ffmpeg()
    cmd = [ff, '-hide_banner', '-loglevel', 'error', '-y',
           '-f', 'lavfi', '-i', f'testsrc=size={size}:duration={seconds}:rate=10']
    if audio:
        cmd += ['-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}']
    cmd += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'ultrafast']
    if audio:
        cmd += ['-c:a', 'aac', '-shortest']
    cmd.append(str(path))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-500:]
    return str(path)


def _make_audio(path, *, codec='libmp3lame', seconds=3.0) -> str:
    ff = _ffmpeg()
    proc = subprocess.run(
        [ff, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
         '-i', f'sine=frequency=440:duration={seconds}', '-c:a', codec, str(path)],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-500:]
    return str(path)


@pytest.fixture(autouse=True)
def _hermetic_video_env(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_VIDEO_SCRATCH', str(tmp_path / 'scratch'))
    monkeypatch.delenv('TOFU_VIDEO_ANALYSIS', raising=False)
    # A unit spec never reaches a configured speech-to-text provider.
    import lib.transcription as stt
    monkeypatch.setattr(stt, 'transcription_available', lambda **_kw: False)


def _inspect(args, base='', task=None):
    from lib.project_mod import execute_tool
    return execute_tool('inspect_video', args, base, conv_id='c-video',
                        task_id='t-video', task=task)


def _frames(result) -> list[dict]:
    assert isinstance(result, dict), result
    batch = result['__batch_images__']
    return [batch[key] for key in sorted(batch)]


def _frame_times(text: str) -> list[float]:
    return [float(match.group(3)) for match in _FRAME_LINE.finditer(text)]


def _typed_error(result):
    from lib.tools.result_envelope import tool_result_error
    error = tool_result_error(result)
    assert error is not None, f'expected a typed error, got: {str(result)[:300]}'
    assert error.next_action.strip(), error
    return error


def _scratch_jobs() -> list[str]:
    from lib.video_analysis import scratch_root
    root = scratch_root()
    return [name for name in os.listdir(root) if name.startswith('job_')]


# ── Media behavior (real ffmpeg) ─────────────────────────────────────────

def test_whole_video_returns_probe_facts_and_policy_frames_as_batch_images(tmp_path):
    from PIL import Image

    from lib.video_analysis import FRAME_LONG_SIDE_PX, frame_target_for_duration
    clip = _make_clip(tmp_path / 'clip.mp4')

    result = _inspect({'path': clip})

    text = result['_text_content']
    for fact in ('duration 3.00s', '1920x1080', '10 fps', 'video codec h264',
                 'audio yes (aac)'):
        assert fact in text, (fact, text)
    frames = _frames(result)
    assert len(frames) == frame_target_for_duration(3.0)
    times = _frame_times(text)
    assert len(times) == len(frames)
    assert times == sorted(times) and len(set(times)) == len(times)
    assert 0.0 <= times[0] and times[-1] <= 3.0
    for frame in frames:
        assert frame['__screenshot__'] is True
        assert frame['format'] == 'jpeg'
        assert frame['dataUrl'].startswith('data:image/jpeg;base64,')
    raw = base64.b64decode(frames[0]['dataUrl'].split(',', 1)[1])
    with Image.open(io.BytesIO(raw)) as image:
        assert max(image.size) == FRAME_LONG_SIDE_PX
    assert 'Transcript: unavailable (no transcription model is configured)' in text
    assert _scratch_jobs() == []


def test_window_frames_and_transcript_cover_only_the_requested_range(tmp_path, monkeypatch):
    import lib.video_analysis as va
    clip = _make_clip(tmp_path / 'clip.mp4')
    seen = {}

    def fake_transcribe(video_path, scratch_dir, duration_s, **kwargs):
        from lib.motion_video._gates import probe_video
        seen['probe'] = probe_video(video_path)
        seen['duration_s'] = duration_s
        seen['owner'] = kwargs.get('owner_user_id')
        return {'text': 'hello from the window', 'status': 'ok', 'model': 'stt-test'}

    monkeypatch.setattr(va, 'transcribe_track', fake_transcribe)
    result = _inspect({'path': clip, 'start': 1.0, 'end': 2.5}, task={'_userId': 7})

    text = result['_text_content']
    assert 'Window: 00:01–00:02 (1.00s–2.50s) of 00:03' in text
    frames = _frames(result)
    # 1.5s at 10 fps holds 15 source frames: the policy never duplicates one.
    assert len(frames) == va.frame_target_for_duration(1.5, source_fps=10.0) == 15
    times = _frame_times(text)
    assert len(times) == len(frames) and times == sorted(times)
    assert len(set(times)) == len(times)
    assert len({frame['dataUrl'] for frame in frames}) == len(frames)
    assert all(1.0 <= value <= 2.5 for value in times), times
    assert seen['duration_s'] == pytest.approx(1.5, abs=0.05)
    assert seen['probe']['duration'] == pytest.approx(1.5, abs=0.05)
    assert seen['owner'] == 7
    assert 'Transcript (via stt-test):' in text
    assert '[00:01–00:02 (1.00s–2.50s)] hello from the window' in text
    assert _scratch_jobs() == []


def test_transcript_outage_keeps_frames_and_probe_facts(tmp_path, monkeypatch):
    import lib.transcription as stt
    from lib.model_routing import ModelRoutingError
    from lib.video_analysis import frame_target_for_duration
    clip = _make_clip(tmp_path / 'clip.mp4', seconds=2.0, size='320x240')

    def _routing_outage(**_kwargs):
        raise ModelRoutingError('model-routing authority is temporarily unavailable')

    monkeypatch.setattr(stt, 'transcription_available', _routing_outage)
    result = _inspect({'path': clip}, task={'_userId': 7})

    assert len(_frames(result)) == frame_target_for_duration(2.0, source_fps=10.0)
    text = result['_text_content']
    assert 'audio yes (aac)' in text
    assert re.search(r'^Transcript: unavailable \(.*temporarily unavailable.*\)$',
                     text, re.M), text
    assert _scratch_jobs() == []


def test_window_end_past_the_video_is_clamped_and_reported(tmp_path):
    clip = _make_clip(tmp_path / 'clip.mp4', audio=False)

    result = _inspect({'path': clip, 'start': 2, 'end': 60})

    text = result['_text_content']
    assert 'Window: 00:02–00:03 (2.00s–3.00s) of 00:03' in text
    assert 'end clamped to the video duration' in text
    assert all(2.0 <= value <= 3.0 for value in _frame_times(text))


def test_silent_video_reports_explicit_missing_audio(tmp_path, monkeypatch):
    import lib.video_analysis as va
    clip = _make_clip(tmp_path / 'silent.mp4', audio=False)
    monkeypatch.setattr(va, 'transcribe_track', lambda *a, **k: pytest.fail(
        'a video without an audio stream must not be transcribed'))

    text = _inspect({'path': clip})['_text_content']

    assert 'audio no' in text
    assert 'Transcript: unavailable (no audio track)' in text


def test_decoding_happens_on_local_scratch_copy_and_scratch_is_removed(tmp_path, monkeypatch):
    import lib.video_analysis as va
    clip = _make_clip(tmp_path / 'clip.mp4', audio=False)
    real_extract = va.extract_frames
    decoded = []

    def recording_extract(video_path, duration_s, scratch_dir, **kwargs):
        decoded.append((video_path, scratch_dir))
        return real_extract(video_path, duration_s, scratch_dir, **kwargs)

    monkeypatch.setattr(va, 'extract_frames', recording_extract)
    _inspect({'path': clip})

    root = va.scratch_root()
    (video_path, scratch_dir), = decoded
    assert os.path.commonpath([root, video_path]) == root
    assert os.path.commonpath([root, scratch_dir]) == root
    assert os.path.realpath(video_path) != os.path.realpath(clip)
    assert not os.path.exists(scratch_dir)
    assert _scratch_jobs() == []


def test_model_frame_budget_thins_frames_uniformly(tmp_path, monkeypatch):
    import lib.model_info as model_info
    clip = _make_clip(tmp_path / 'clip.mp4', audio=False)
    monkeypatch.setattr(model_info, 'video_frame_budget', lambda model, **_kw: 5)

    result = _inspect({'path': clip}, task={'model': 'vision-test'})

    text = result['_text_content']
    assert len(_frames(result)) == 5
    assert 'Frames: 5 of 16 extracted key frames' in text
    assert len(_frame_times(text)) == 5


def test_text_only_model_gets_probe_and_transcript_without_frame_work(tmp_path, monkeypatch):
    import lib.model_info as model_info
    import lib.video_analysis as va
    clip = _make_clip(tmp_path / 'clip.mp4')
    monkeypatch.setattr(model_info, 'video_frame_budget', lambda model, **_kw: 0)
    monkeypatch.setattr(va, 'extract_frames', lambda *a, **k: pytest.fail(
        'a text-only model must not pay for frame extraction'))

    result = _inspect({'path': clip}, task={'model': 'text-only'})

    assert isinstance(result, str)
    assert 'duration 3.00s' in result
    assert 'Frames: not extracted' in result and 'text-only' in result
    assert 'Transcript: unavailable (no transcription model is configured)' in result


@pytest.mark.parametrize('name, payload, code, wording', [
    ('notes.txt', b'plain text, not a video\n' * 64, 'video_not_a_video',
     'is not a video or audio file'),
    ('broken.mp4', b'\x00\x00\x00\x18ftypmp42' + bytes(range(256)) * 16,
     'video_unreadable', 'could not be decoded as video'),
    ('broken.mp3', b'ID3\x04\x00\x00\x00\x00\x00\x00' + bytes(range(256)) * 16,
     'video_unreadable', 'could not be decoded as audio'),
], ids=['text-file', 'corrupt-mp4', 'corrupt-mp3'])
def test_non_video_and_unreadable_media_are_typed_errors(tmp_path, name, payload,
                                                         code, wording):
    _ffmpeg()
    target = tmp_path / name
    target.write_bytes(payload)

    error = _typed_error(_inspect({'path': str(target)}))

    assert error.code == code
    assert wording in error.message, error.message
    assert _scratch_jobs() == []


# ── Audio-only media (real ffmpeg) ──────────────────────────────────────

_PROBE_LINE = re.compile(
    r'^Probe: duration ([0-9.]+)s \(00:03\) · video no · audio yes \((\w+)\)$', re.M)


@pytest.mark.parametrize('name, codec, audio_codec', [
    ('talk.mp3', 'libmp3lame', 'mp3'),
    ('talk.m4a', 'aac', 'aac'),
    ('talk.aac', 'aac', 'aac'),
    ('talk.wav', 'pcm_s16le', 'pcm_s16le'),
    ('talk.ogg', 'libvorbis', 'vorbis'),
    ('talk.opus', 'libopus', 'opus'),
    ('talk.flac', 'flac', 'flac'),
    ('talk.webm', 'libopus', 'opus'),
], ids=['mp3', 'm4a', 'adts-aac', 'wav', 'ogg-vorbis', 'ogg-opus', 'flac',
        'webm-audio'])
def test_audio_only_file_returns_probe_facts_and_transcript_as_text_without_frames(
        tmp_path, monkeypatch, name, codec, audio_codec):
    import lib.video_analysis as va
    audio = _make_audio(tmp_path / name, codec=codec)
    monkeypatch.setattr(va, 'extract_frames', lambda *a, **k: pytest.fail(
        'an audio-only file has no frames to extract'))

    result = _inspect({'path': audio}, task={'model': 'vision-test'})

    assert isinstance(result, str), 'audio-only results are text, never an image batch'
    assert result.startswith(f'inspect_video: {name}\n')
    match = _PROBE_LINE.search(result)
    assert match, result
    assert float(match.group(1)) == pytest.approx(3.0, abs=0.1)
    assert match.group(2) == audio_codec
    assert 'video codec' not in result
    assert re.search(r'^Window: 00:00–00:03 \(0\.00s–[0-9.]+s\) of 00:03 \(whole audio\)$',
                     result, re.M), result
    assert 'Frames: none (audio-only file: no video stream).' in result
    assert _frame_times(result) == []
    assert result.rstrip().endswith(
        'Transcript: unavailable (no transcription model is configured)')
    assert _scratch_jobs() == []


@pytest.mark.parametrize('outcome', ['ok', 'unavailable', 'failed'])
def test_audio_transcript_runs_the_existing_transcription_chain(tmp_path, monkeypatch,
                                                                outcome):
    import lib.transcription as stt
    audio = _make_audio(tmp_path / 'podcast.mp3')
    sent = []

    def fake_transcribe(audio_bytes, filename, content_type=None, **kwargs):
        sent.append((len(audio_bytes), filename, kwargs.get('owner_user_id')))
        if outcome == 'failed':
            raise stt.TranscriptionError('provider rejected the audio', status=502)
        return stt.TranscriptionResult(text='hello from the podcast',
                                       model='stt-test', provider_id='p1')

    monkeypatch.setattr(stt, 'transcription_available',
                        lambda **_kw: outcome != 'unavailable')
    monkeypatch.setattr(stt, 'transcribe', fake_transcribe)
    result = _inspect({'path': audio}, task={'_userId': 7})

    assert isinstance(result, str)
    if outcome == 'ok':
        assert 'Transcript (via stt-test):' in result
        assert re.search(r'^\[00:00–00:03 \(0\.00s–3\.\d\ds\)\] hello from the podcast$',
                         result, re.M), result
        (size, filename, owner), = sent
        assert size > 0 and filename in ('track.mp3', 'track.ogg') and owner == 7
    elif outcome == 'unavailable':
        assert 'Transcript: unavailable (no transcription model is configured)' in result
        assert sent == []
    else:
        assert ('Transcript: unavailable (transcription failed: provider rejected '
                'the audio)') in result
        assert len(sent) == 1
    assert _scratch_jobs() == []


def test_audio_window_cuts_the_transcript_span_and_clamps_like_video(tmp_path, monkeypatch):
    import lib.video_analysis as va
    from lib.motion_video._gates import probe_video
    audio = _make_audio(tmp_path / 'talk.wav', codec='pcm_s16le')
    seen = {}

    def fake_transcribe(media_path, scratch_dir, duration_s, **kwargs):
        seen['probe'] = probe_video(media_path, allow_audio_only=True)
        seen['duration_s'] = duration_s
        return {'text': 'words in the window', 'status': 'ok', 'model': 'stt-test'}

    monkeypatch.setattr(va, 'transcribe_track', fake_transcribe)
    windowed = _inspect({'path': audio, 'start': 1.0, 'end': 2.5})

    assert 'Window: 00:01–00:02 (1.00s–2.50s) of 00:03' in windowed
    assert '(whole audio)' not in windowed
    assert seen['duration_s'] == pytest.approx(1.5, abs=0.05)
    assert seen['probe']['codec'] == '' and seen['probe']['has_audio'] is True
    assert seen['probe']['duration'] == pytest.approx(1.5, abs=0.05)
    assert '[00:01–00:02 (1.00s–2.50s)] words in the window' in windowed

    clamped = _inspect({'path': audio, 'start': 2, 'end': 60})
    assert 'Window: 00:02–00:03 (2.00s–3.00s) of 00:03' in clamped
    assert 'end clamped to the audio duration' in clamped

    error = _typed_error(_inspect({'path': audio, 'start': 3.0}))
    assert error.code == 'video_invalid_window'
    assert 'past the end of the audio (3.00s)' in error.message
    assert _scratch_jobs() == []


def test_still_picture_with_audio_keeps_the_video_path(tmp_path):
    """Control: a file with both a (static) video stream and audio is a video."""
    clip = tmp_path / 'still.mp4'
    proc = subprocess.run(
        [_ffmpeg(), '-hide_banner', '-loglevel', 'error', '-y',
         '-f', 'lavfi', '-i', 'color=c=black:size=320x240:duration=2:rate=10',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
         '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'ultrafast',
         '-c:a', 'aac', '-shortest', str(clip)],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-500:]

    result = _inspect({'path': str(clip)})

    frames = _frames(result)
    text = result['_text_content']
    assert frames and len(_frame_times(text)) == len(frames)
    for fact in ('320x240', 'video codec h264', 'audio yes (aac)', '(whole video)'):
        assert fact in text, (fact, text)
    assert 'video no' not in text and 'audio-only' not in text


def test_window_outside_the_video_is_a_typed_error(tmp_path):
    clip = _make_clip(tmp_path / 'clip.mp4', audio=False)

    error = _typed_error(_inspect({'path': clip, 'start': 3.0}))

    assert error.code == 'video_invalid_window'
    assert '3.00s' in error.message


# ── Typed errors that need no media decoding ────────────────────────────

@pytest.mark.parametrize('window', [
    {'start': -1}, {'start': 2, 'end': 1}, {'start': 1, 'end': 1},
    {'start': 'soon'}, {'end': True}, {'end': float('nan')},
])
def test_malformed_window_is_rejected_before_any_work(tmp_path, window):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'not decoded')

    error = _typed_error(_inspect({'path': str(clip), **window}))

    assert error.code == 'video_invalid_window'


def test_missing_file_is_a_typed_error(tmp_path):
    error = _typed_error(_inspect({'path': str(tmp_path / 'absent.mp4')}))
    assert error.code == 'video_file_not_found'


def test_missing_path_argument_is_a_typed_error():
    error = _typed_error(_inspect({}))
    assert error.code == 'video_path_required'


def test_disabled_video_analysis_is_a_typed_error(tmp_path, monkeypatch):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'never read')
    monkeypatch.setenv('TOFU_VIDEO_ANALYSIS', '0')

    error = _typed_error(_inspect({'path': str(clip)}))

    assert error.code == 'video_analysis_disabled'


def test_missing_ffmpeg_is_a_typed_error(tmp_path, monkeypatch):
    import lib.motion_video._env as media_env
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'never decoded')
    monkeypatch.setattr(media_env, 'ensure_ffmpeg', lambda **_kw: '')

    error = _typed_error(_inspect({'path': str(clip)}))

    assert error.code == 'video_ffmpeg_unavailable'
    assert 'TOFU_FFMPEG' in error.next_action


# ── Path forms: project-relative, absolute, chat video attachment ───────

@pytest.fixture
def recorded_inspection(monkeypatch):
    import lib.video_analysis as va
    calls = []

    def fake_inspect_video_file(path, **kwargs):
        calls.append((path, kwargs))
        return 'inspected'

    monkeypatch.setattr(va, 'inspect_video_file', fake_inspect_video_file)
    return calls


def test_project_relative_path_resolves_under_the_project(tmp_path, recorded_inspection):
    (tmp_path / 'media').mkdir()
    clip = tmp_path / 'media' / 'clip.mp4'
    clip.write_bytes(b'x')

    assert _inspect({'path': 'media/clip.mp4', 'start': 1}, base=str(tmp_path)) == 'inspected'

    (path, kwargs), = recorded_inspection
    assert path == str(clip)
    assert kwargs['start'] == 1


def test_absolute_staged_download_works_without_a_project(tmp_path, recorded_inspection):
    staged = tmp_path / 'data' / 'fetched' / 'server-download-abc.mp4'
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b'x')

    assert _inspect({'path': str(staged)}) == 'inspected'

    assert recorded_inspection[0][0] == str(staged)


def test_relative_path_without_project_is_a_typed_error(recorded_inspection):
    error = _typed_error(_inspect({'path': 'clip.mp4'}))
    assert error.code == 'video_path_invalid'
    assert recorded_inspection == []


def test_restricted_caller_cannot_read_outside_registered_roots(tmp_path, recorded_inspection):
    from lib.project_mod.abs_path_guard import reset_restricted, set_restricted
    outside = tmp_path / 'outside.mp4'
    outside.write_bytes(b'x')
    token = set_restricted(True)
    try:
        error = _typed_error(_inspect({'path': str(outside)}))
    finally:
        reset_restricted(token)
    assert error.code == 'video_path_denied'
    assert recorded_inspection == []


def test_chat_video_attachment_ref_resolves_to_its_owned_original(
        tmp_path, monkeypatch, recorded_inspection):
    import lib.attachments as attachments
    original = tmp_path / 'store' / 'original.mp4'
    original.parent.mkdir()
    original.write_bytes(b'x')
    seen = {}

    def fake_resolve(ref, *, messages=None, user_id=None):
        seen.update(ref=ref, user_id=user_id)
        return {'kind': 'video', 'name': 'lecture.mp4', 'path': str(original),
                'metadata': {}, 'attachment_id': 'vid1'}

    monkeypatch.setattr(attachments, 'resolve_attachment', fake_resolve)
    result = _inspect({'path': 'att_media_vid1'}, task={'_userId': 7, 'messages': []})

    assert result == 'inspected'
    assert seen == {'ref': 'att_media_vid1', 'user_id': 7}
    (path, kwargs), = recorded_inspection
    assert path == str(original)
    assert kwargs['source_name'] == 'lecture.mp4'
    assert kwargs['owner_user_id'] == 7


@pytest.mark.parametrize('resolved, code', [
    (None, 'video_attachment_unresolved'),
    ({'kind': 'image', 'raw': b'x', 'mime_type': 'image/png', 'image_b64': ''},
     'video_not_a_video'),
])
def test_unusable_attachment_refs_are_typed_errors(monkeypatch, recorded_inspection,
                                                   resolved, code):
    import lib.attachments as attachments
    monkeypatch.setattr(attachments, 'resolve_attachment',
                        lambda ref, **_kw: resolved)

    error = _typed_error(_inspect({'path': 'att_media_x'}, task={'_userId': 7}))

    assert error.code == code
    assert recorded_inspection == []
