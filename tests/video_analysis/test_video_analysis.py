#!/usr/bin/env python3
"""Tests for the video-upload + analysis pipeline (epic pt_6aca988757cb4019).

Layers:
  * pure unit     — duration tiers, frame budget clamps, container sniff,
                    frame thinning, timestamp formatting
  * transform     — videos[] → frame blocks + transcript expansion, model-aware
                    budget thinning, aggregate image ceiling, cache-friendly order
  * integration   — REAL ffmpeg on synthetic clips (testsrc / color / sine are
                    synthesized on the fly — no binary fixtures), frame files,
                    audio extraction, the full pipeline into a tmp uploads store
  * routes        — POST /api/v1/videos/upload + GET status (processing stubbed
                    synchronous-free; no provider, no keys)

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/video_analysis/test_video_analysis.py -v
"""

import io
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from tests.support.envelope import v1_get_json

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]


@pytest.fixture
def client(flask_app):
    from lib.storage import start_storage
    start_storage()
    return flask_app.test_client()


def _run_async(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ─────────────────────────────────────────────────────────────────────
#  Synthetic video helpers (real ffmpeg, tiny clips — no fixtures)
# ─────────────────────────────────────────────────────────────────────

def _ffmpeg():
    from lib.motion_video._env import ffmpeg_bin
    ff = ffmpeg_bin()
    if not ff:
        pytest.skip('ffmpeg unavailable on this host')
    return ff


def _make_video(path, *, seconds=2.0, with_audio=False, with_cut=False,
                size='64x64'):
    """Synthesize a tiny mp4. with_cut inserts a hard red→blue cut mid-clip
    (a scene-score spike the detector must catch); with_audio adds a sine."""
    ff = _ffmpeg()
    parent = os.path.dirname(str(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if with_cut:
        half = seconds / 2
        cmd = [
            ff, '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'lavfi', '-i', f'color=red:size={size}:duration={half}:rate=10',
            '-f', 'lavfi', '-i', f'color=blue:size={size}:duration={half}:rate=10',
            '-filter_complex', '[0:v][1:v]concat=n=2:v=1',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'ultrafast',
            str(path),
        ]
    elif with_audio:
        cmd = [
            ff, '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'lavfi', '-i', f'testsrc=size={size}:duration={seconds}:rate=10',
            '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'ultrafast',
            '-c:a', 'aac', '-shortest', str(path),
        ]
    else:
        cmd = [
            ff, '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'lavfi', '-i', f'color=red:size={size}:duration={seconds}:rate=10',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'ultrafast',
            str(path),
        ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-500:]
    return str(path)


# ─────────────────────────────────────────────────────────────────────
#  Pure unit: config / budget / sniff / thin / ts
# ─────────────────────────────────────────────────────────────────────

def test_frame_sampling_scales_with_complete_duration():
    from lib.video_analysis import frame_target_for_duration
    assert frame_target_for_duration(1) == 16
    assert frame_target_for_duration(60) == 16
    assert frame_target_for_duration(600) == 60
    assert frame_target_for_duration(900) == 90
    assert frame_target_for_duration(7200) == 720


def test_large_model_context_is_not_clamped_to_old_extraction_count(monkeypatch):
    import lib.model_info._video as video
    monkeypatch.setattr(video, 'model_supports_vision', lambda model: True)
    monkeypatch.setattr(video, 'is_claude', lambda model: False)
    monkeypatch.setattr('lib.context_limits.lookup_learned_context_limit', lambda *args: 1_000_000)
    assert video.video_frame_budget('large-context-vision') == 200


def test_processing_handoff_has_no_capacity_gate_and_cannot_be_reused():
    import lib.video_analysis._pipeline as pipe
    reservations = [pipe.reserve_processing_slot() for _ in range(256)]
    assert all(reservation is not None for reservation in reservations)
    releases = [reservation.handoff() for reservation in reservations]
    for reservation, release in zip(reservations, releases):
        reservation.release()
        with pytest.raises(RuntimeError, match='not active'):
            reservation.handoff()
        release()
        release()


def test_stale_video_scratch_is_reclaimed_in_a_bounded_namespace(
        tmp_path, monkeypatch):
    from lib.video_analysis import _config

    monkeypatch.setenv('TOFU_VIDEO_SCRATCH_TTL_S', '3600')
    root = tmp_path / 'tofu-video-analysis'
    stale = root / 'job_stale'
    fresh = root / 'job_fresh'
    unrelated = root / 'operator-file'
    stale.mkdir(parents=True)
    fresh.mkdir()
    unrelated.mkdir()
    now = time.time()
    os.utime(stale, (now - 7200, now - 7200))

    assert _config.reclaim_stale_scratch(str(root), now_s=now) == 1
    assert not stale.exists()
    assert fresh.is_dir()
    assert unrelated.is_dir()


def test_video_frame_budget_vision_gate(monkeypatch):
    from lib.model_info import _video as v
    monkeypatch.setattr(v, 'model_supports_vision', lambda m: False)
    assert v.video_frame_budget('text-only-model') == 0


def test_video_frame_budget_claude_family_cap(monkeypatch):
    """With a huge learned window, the Claude per-video family cap (40) binds
    below the 64 extraction ceiling."""
    from lib.model_info import _video as v
    monkeypatch.setattr(v, 'model_supports_vision', lambda m: True)
    monkeypatch.setattr(v, 'is_claude', lambda m: True)
    import lib.context_limits as cl
    monkeypatch.setattr(cl, 'lookup_learned_context_limit',
                        lambda p, m: 1_000_000)
    assert v.video_frame_budget('claude-x') == 40


def test_video_frame_budget_context_clamp(monkeypatch):
    """A small learned window clamps below the family cap: 128k → 25 frames."""
    from lib.model_info import _video as v
    monkeypatch.setattr(v, 'model_supports_vision', lambda m: True)
    monkeypatch.setattr(v, 'is_claude', lambda m: False)
    import lib.context_limits as cl
    monkeypatch.setattr(cl, 'lookup_learned_context_limit',
                        lambda p, m: 128_000)
    assert v.video_frame_budget('generic-vl') == 25


def test_video_frame_budget_wire_clamp_and_floor(monkeypatch):
    from lib.model_info import _video as v
    monkeypatch.setattr(v, 'model_supports_vision', lambda m: True)
    monkeypatch.setattr(v, 'is_claude', lambda m: False)
    import lib.context_limits as cl
    monkeypatch.setattr(cl, 'lookup_learned_context_limit',
                        lambda p, m: 1_000_000)
    # 8 MiB / 2 MiB per frame = 4 → the FLOOR (4) keeps minimal coverage.
    assert v.video_frame_budget('generic-vl', avg_frame_bytes=2 * 1024 * 1024) == 4
    # 8 MiB / 200 KB = 40 frames
    assert v.video_frame_budget('generic-vl', avg_frame_bytes=200 * 1024) == 40


def test_aggregate_image_cap():
    from lib.model_info import aggregate_image_cap
    assert aggregate_image_cap('claude-sonnet-4-5') == 90
    assert aggregate_image_cap('gpt-4o') is None


def test_sniff_video_container():
    from routes.api_v1.videos import _sniff_video_container
    assert _sniff_video_container(b'\x00\x00\x00\x18ftypisom' + b'\x00' * 8) == 'mp4'
    assert _sniff_video_container(b'\x1a\x45\xdf\xa3' + b'\x00' * 16) == 'webm'
    assert _sniff_video_container(b'RIFF\x10\x00\x00\x00AVI ' + b'\x00' * 4) == 'avi'
    assert _sniff_video_container(b'\x89PNG\r\n\x1a\n' + b'\x00' * 8) is None
    assert _sniff_video_container(b'') is None


def test_thin_frames():
    from lib.tasks_pkg.conv_message_builder._transform import _thin_frames
    frames = [{'t': float(i)} for i in range(64)]
    out = _thin_frames(frames, 40)
    assert len(out) <= 40
    assert out[0]['t'] == 0.0 and out[-1]['t'] == 63.0  # endpoints kept
    ts = [f['t'] for f in out]
    assert ts == sorted(ts)                              # order preserved
    assert _thin_frames(frames, 0) == []
    assert _thin_frames(frames, 64) is frames            # no thinning
    assert _thin_frames(frames, 100) is frames


def test_fmt_video_ts():
    from lib.tasks_pkg.conv_message_builder._transform import _fmt_video_ts
    assert _fmt_video_ts(0) == '00:00'
    assert _fmt_video_ts(65.4) == '01:05'
    assert _fmt_video_ts(599.9) == '09:59'
    assert _fmt_video_ts(3600) == '1:00:00'


# ─────────────────────────────────────────────────────────────────────
#  Transform: videos[] expansion
# ─────────────────────────────────────────────────────────────────────

def _video_msg(n_frames=6, transcript='hello transcript', with_text=True):
    return {
        'role': 'user',
        'content': 'what happens here?' if with_text else '',
        'videos': [{
            'video_id': 'v_test', 'name': 'clip.mp4', 'duration_s': 12.0,
            'frames': [{'url': f'/api/images/f{i}.jpg', 't': float(i) * 2,
                        'bytes': 1000} for i in range(n_frames)],
            'avg_frame_bytes': 1000,
            'transcript': transcript,
            'transcript_status': 'ok' if transcript else 'no_audio',
        }],
    }


def test_transform_expands_video_blocks():
    from lib.tasks_pkg.conv_message_builder._transform import _build_user_message
    out = _build_user_message(_video_msg(), model='gpt-4o')
    blocks = out['content']
    assert isinstance(blocks, list)
    # Header first, then frame/label pairs, transcript, user text LAST
    # (stable cache prefix: media before the question).
    assert blocks[0]['text'].startswith('[Video 1: "clip.mp4" — 12s')
    assert '6 frames sampled' in blocks[0]['text']
    assert 'audio transcript: ok' in blocks[0]['text']
    imgs = [b for b in blocks if b.get('type') == 'image_url']
    assert len(imgs) == 6
    labels = [b['text'] for b in blocks
              if b.get('type') == 'text' and 'frame at' in b.get('text', '')]
    assert '[Video 1 frame at 00:02]' in labels
    assert '[Video 1 frame at 00:10]' in labels
    tr = [b for b in blocks if b.get('type') == 'text'
          and b['text'].startswith('[Video 1 audio transcript]')]
    assert tr and 'hello transcript' in tr[0]['text']
    assert blocks[-1] == {'type': 'text', 'text': 'what happens here?'}
    # Video frames must NOT carry per-frame inspect_image ref hints (noise).
    assert not any('image ref' in b.get('text', '') for b in blocks
                   if b.get('type') == 'text')


def test_transform_video_budget_thins(monkeypatch):
    import lib.model_info as mi
    monkeypatch.setattr(mi, 'video_frame_budget', lambda m, **k: 4)
    from lib.tasks_pkg.conv_message_builder._transform import _build_user_message
    message = _video_msg(n_frames=10)
    message['videos'][0]['storyboard'] = 'Full visual evidence, including omitted frames'
    out = _build_user_message(message, model='gpt-4o')
    blocks = out['content']
    imgs = [b for b in blocks if b.get('type') == 'image_url']
    assert len(imgs) == 4
    assert '4 frames sampled (of 10 extracted)' in blocks[0]['text']
    assert any('Full visual evidence, including omitted frames' in block.get('text', '')
               for block in blocks)
    # Uniform thinning keeps the FIRST and LAST frame.
    assert imgs[0]['image_url']['url'] == '/api/images/f0.jpg'
    assert imgs[-1]['image_url']['url'] == '/api/images/f9.jpg'


def test_transform_video_non_vision_keeps_transcript(monkeypatch):
    import lib.model_info as mi
    monkeypatch.setattr(mi, 'video_frame_budget', lambda m, **k: 0)
    from lib.tasks_pkg.conv_message_builder._transform import _build_user_message
    out = _build_user_message(_video_msg(), model='text-only')
    blocks = out['content']
    assert not any(b.get('type') == 'image_url' for b in blocks)
    assert 'frames omitted (model has no vision capability)' in blocks[0]['text']
    assert any('hello transcript' in b.get('text', '') for b in blocks)


def test_transform_aggregate_image_ceiling(monkeypatch):
    """Claude's 90-image aggregate ceiling is accounted ACROSS videos:
    40 + 40 + 10, not 40 + 40 + 40."""
    import lib.model_info as mi
    monkeypatch.setattr(mi, 'video_frame_budget', lambda m, **k: 40)
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages
    msgs = [
        {'role': 'user', 'content': 'one', 'videos': [
            {'name': 'a.mp4', 'duration_s': 900,
             'frames': [{'url': f'/api/images/a{i}.jpg', 't': float(i)}
                        for i in range(50)]}]},
        {'role': 'assistant', 'content': 'ok'},
        {'role': 'user', 'content': 'two', 'videos': [
            {'name': 'b.mp4', 'duration_s': 900,
             'frames': [{'url': f'/api/images/b{i}.jpg', 't': float(i)}
                        for i in range(50)]}]},
        {'role': 'assistant', 'content': 'ok'},
        {'role': 'user', 'content': 'three', 'videos': [
            {'name': 'c.mp4', 'duration_s': 900,
             'frames': [{'url': f'/api/images/c{i}.jpg', 't': float(i)}
                        for i in range(50)]}]},
    ]
    out = _transform_messages(msgs, {'model': 'claude-sonnet-4-5'})
    total_imgs = 0
    per_msg = []
    for m in out:
        if m.get('role') != 'user' or not isinstance(m.get('content'), list):
            continue
        n = sum(1 for b in m['content'] if b.get('type') == 'image_url')
        per_msg.append(n)
        total_imgs += n
    assert per_msg == [40, 40, 10], per_msg
    assert total_imgs == 90


def test_transform_plain_message_unchanged():
    """Regression: a text-only user message still builds a plain string body."""
    from lib.tasks_pkg.conv_message_builder._transform import _build_user_message
    out = _build_user_message({'role': 'user', 'content': 'hello'}, model='gpt-4o')
    assert out == {'role': 'user', 'content': 'hello'}


# ─────────────────────────────────────────────────────────────────────
#  Integration: real ffmpeg on synthetic clips
# ─────────────────────────────────────────────────────────────────────

def test_extract_frames_real(tmp_path):
    from lib.video_analysis._frames import extract_frames
    video = _make_video(tmp_path / 'plain.mp4', seconds=3.0)
    frames = extract_frames(video, 3.0, str(tmp_path))
    # A short flat clip retains its 16 uniform samples.
    assert len(frames) == 16
    ts = [f['t'] for f in frames]
    assert ts == sorted(ts) and 0 <= ts[0] < ts[-1] <= 3.0
    for fr in frames:
        with open(fr['path'], 'rb') as f:
            assert f.read(3) == b'\xff\xd8\xff'  # JPEG magic


def test_scene_cut_detected(tmp_path):
    """The scene pass itself finds the hard red→blue cut (t≈2.0)."""
    from lib.video_analysis._frames import _scene_cut_times
    video = _make_video(tmp_path / 'cut.mp4', seconds=4.0, with_cut=True)
    cuts = _scene_cut_times(_ffmpeg(), video, 4.0)
    assert any(abs(t - 2.0) < 0.4 for t in cuts), cuts


def test_merge_scene_extras_deduplicates_without_count_loss():
    """Cuts within ±1s of a uniform sample (or each other) are redundant;
    every nonredundant cut remains."""
    from lib.video_analysis._frames import _merge_scene_extras
    uniform = [10.0, 20.0, 30.0, 40.0]
    cuts = [10.4, 25.0, 25.5, 33.0, 50.0]
    picked = _merge_scene_extras(uniform, cuts)
    assert picked == [25.0, 33.0, 50.0]
    assert _merge_scene_extras(uniform, []) == []


def test_extract_single_frame_real(tmp_path):
    """The seek-extract path used for scene frames yields a real JPEG."""
    from lib.video_analysis._frames import _extract_single
    video = _make_video(tmp_path / 'single.mp4', seconds=3.0)
    one = _extract_single(_ffmpeg(), video, 1.5, str(tmp_path), 't0')
    assert one is not None and abs(one['t'] - 1.5) < 0.01
    with open(one['path'], 'rb') as f:
        assert f.read(3) == b'\xff\xd8\xff'


def test_audio_extraction_real(tmp_path):
    from lib.video_analysis._audio import _extract_audio
    video = _make_video(tmp_path / 'aud.mp4', seconds=2.0, with_audio=True)
    out = _extract_audio(video, str(tmp_path), 2.0)
    assert out is not None
    data, ext = out
    assert ext in ('mp3', 'ogg')
    assert len(data) > 1000
    # Extracted format must be in the transcription allow-list.
    from lib.transcription import allowed_audio_upload
    assert allowed_audio_upload(f'track.{ext}') is not None


def test_transcribe_track_degrades_without_slot(tmp_path, monkeypatch):
    import lib.transcription as stt
    monkeypatch.setattr(stt, '_transcription_slots', lambda: [])
    from lib.video_analysis._audio import transcribe_track
    video = _make_video(tmp_path / 'aud2.mp4', seconds=2.0, with_audio=True)
    res = transcribe_track(video, str(tmp_path), 2.0)
    assert res['status'] == 'unavailable' and res['text'] == ''


def test_transcribe_track_happy(tmp_path, monkeypatch):
    import lib.transcription as stt
    from lib.transcription import TranscriptionResult
    monkeypatch.setattr(stt, 'transcription_available', lambda: True)
    captured = {}

    def _fake(audio_bytes, filename, content_type):
        captured['filename'] = filename
        captured['bytes'] = len(audio_bytes)
        return TranscriptionResult(text='the transcript', model='whisper-1',
                                   provider_id='test')
    monkeypatch.setattr(stt, 'transcribe', _fake)
    from lib.video_analysis._audio import transcribe_track
    video = _make_video(tmp_path / 'aud3.mp4', seconds=2.0, with_audio=True)
    res = transcribe_track(video, str(tmp_path), 2.0)
    assert res == {'text': 'the transcript', 'status': 'ok', 'model': 'whisper-1'}
    assert captured['filename'].endswith(('.mp3', '.ogg'))


def test_transcribe_track_forwards_owner_to_v2_stt(monkeypatch):
    import lib.transcription as stt
    import lib.video_analysis._audio as audio
    from lib.transcription import TranscriptionResult

    captured = {}
    monkeypatch.setattr(
        audio, '_extract_audio', lambda *_args: (b'audio-bytes', 'mp3'))

    def _available(**kwargs):
        captured['available'] = kwargs
        return True

    def _transcribe(_data, _filename, _content_type, **kwargs):
        captured['transcribe'] = kwargs
        return TranscriptionResult(
            text='owner transcript', model='owner-stt', provider_id='p1')

    monkeypatch.setattr(stt, 'transcription_available', _available)
    monkeypatch.setattr(stt, 'transcribe', _transcribe)

    result = audio.transcribe_track(
        '/video.mp4', '/scratch', 2.0,
        owner_user_id=73, tenant_id='tenant-a')

    assert result['text'] == 'owner transcript'
    assert captured['available'] == {
        'owner_user_id': 73, 'tenant_id': 'tenant-a'}
    assert captured['transcribe'] == {
        'owner_user_id': 73, 'tenant_id': 'tenant-a'}


def test_pipeline_end_to_end(tmp_path, monkeypatch):
    """Full pipeline extracts once and commits only a durable attachment ref."""
    import lib.video_analysis._pipeline as pipe
    import lib.video_analysis._store as store

    monkeypatch.setattr(store, '_registry_path',
                        lambda: str(tmp_path / 'registry.json'))
    monkeypatch.setattr(pipe, 'transcribe_track',
                        lambda *a, **k: {'text': 'fake words', 'status': 'ok',
                                         'model': 'whisper-1'})
    import lib.video_analysis._caption as cap
    monkeypatch.setattr(cap, 'storyboard_for_frames',
                        lambda *a, **k: {'text': '- [00:00] red screen\nOverall: demo',
                                         'status': 'ok', 'model': 'vl-test'})
    import lib.media_attachments as media
    committed = {}
    monkeypatch.setattr(media, 'set_phase', lambda *a, **k: None)
    monkeypatch.setattr(media, 'mark_failed', lambda *a, **k: None)

    def _complete(attachment_id, **payload):
        committed.update(payload)
        return {
            'attachmentId': attachment_id,
            'kind': 'video', 'name': 'clip.mp4', 'status': 'ready',
            'sourceUrl': f'/api/v4/media/attachments/{attachment_id}/source',
            'durationSeconds': float(payload['probe']['duration']),
            'frameCount': len(payload['frames']),
            'transcriptStatus': payload['transcript_status'],
        }

    monkeypatch.setattr(media, 'complete_video', _complete)

    video = _make_video(tmp_path / 'job' / 'upload.mp4', seconds=2.0,
                        with_audio=True)
    store.create_record(
        'v_test_e2e',
        name='clip.mp4',
        size_bytes=os.path.getsize(video),
        user_id=1,
    )
    pipe._process('v_test_e2e', video, 'clip.mp4', user_id=1)

    rec = store.get_record('v_test_e2e', user_id=1)
    assert store.get_record('v_test_e2e', user_id=2) is None
    assert rec['status'] == 'ready', rec.get('error')
    assert rec['phase'] == 'done'
    assert rec['durationSeconds'] == pytest.approx(2.0, abs=0.3)
    assert rec['frameCount'] >= 8
    assert rec['attachmentId'] == 'v_test_e2e'
    assert rec['attachment']['attachmentId'] == 'v_test_e2e'
    assert 'frames' not in rec and 'transcript' not in rec
    assert committed['transcript'] == 'fake words'
    assert committed['transcript_status'] == 'ok'
    assert committed['storyboard_status'] == 'ok'
    assert 'Overall: demo' in committed['storyboard']
    assert committed['storyboard_model'] == 'vl-test'


def test_pipeline_accepts_long_video_and_preserves_complete_text(tmp_path, monkeypatch):
    import lib.video_analysis._pipeline as pipe
    import lib.video_analysis._store as store
    import lib.video_analysis._caption as cap
    import lib.motion_video._gates as gates
    import lib.media_attachments as media
    monkeypatch.setattr(store, '_registry_path', lambda: str(tmp_path / 'registry.json'))
    monkeypatch.setattr(gates, 'probe_video', lambda path: {
        'codec': 'h264', 'duration': 7200.0, 'has_audio': True})
    frames = [{'url': '/api/images/frame.jpg', 'ts': 7100.0}]
    transcript = 'spoken evidence ' * 5000 + 'TRANSCRIPT_END'
    storyboard = 'visual evidence ' * 5000 + 'STORYBOARD_END'
    monkeypatch.setattr(pipe, 'extract_frames', lambda *args: frames)
    monkeypatch.setattr(cap, 'storyboard_for_frames', lambda *args, **kwargs: {
        'text': storyboard, 'status': 'ok', 'model': 'vision'})
    monkeypatch.setattr(pipe, 'transcribe_track', lambda *args, **kwargs: {
        'text': transcript, 'status': 'ok', 'model': 'stt'})
    committed = {}
    def complete(video_id, **payload):
        committed.update(payload)
        return {'attachmentId': video_id, 'status': 'ready'}
    monkeypatch.setattr(media, 'complete_video', complete)
    monkeypatch.setattr(media, 'set_phase', lambda *args, **kwargs: None)
    def unexpected_failure(*args, **kwargs):
        pytest.fail('Valid complete video was rejected')
    monkeypatch.setattr(media, 'mark_failed', unexpected_failure)
    store.create_record('v_long', name='long.mp4', size_bytes=1, user_id=1)
    pipe._process('v_long', str(tmp_path / 'source.mp4'), 'long.mp4', user_id=1)
    assert store.get_record('v_long', user_id=1)['status'] == 'ready'
    assert committed['transcript'] == transcript
    assert committed['storyboard'] == storyboard
    assert committed['probe']['duration'] == 7200.0
    assert committed['frames'] == frames



def test_stale_processing_record_swept(tmp_path, monkeypatch):
    import lib.video_analysis._store as store
    monkeypatch.setattr(store, '_registry_path',
                        lambda: str(tmp_path / 'registry.json'))
    store.create_record(
        'v_stale', name='x.mp4', size_bytes=1, user_id=1)
    # Age the record directly so the stale-processing sweep sees it.
    from lib.json_store import update_json_atomic
    def _age(reg):
        reg['v_stale']['updated_at'] = 0
        return reg
    update_json_atomic(str(tmp_path / 'registry.json'), _age, default={})
    got = store.get_record('v_stale', user_id=1)
    assert got['status'] == 'processing'
    def exited_owner(reg):
        reg['v_stale']['process_owner']['started'] = '0'
        return reg
    update_json_atomic(str(tmp_path / 'registry.json'), exited_owner, default={})
    got = store.get_record('v_stale', user_id=1)
    assert got['status'] == 'failed'
    assert 'interrupted' in got['error']


# ─────────────────────────────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────────────────────────────

def _multipart_video(data, filename='clip.mp4', content_type='video/mp4'):
    from werkzeug.datastructures import FileStorage
    fs = FileStorage(stream=io.BytesIO(data), filename=filename,
                     content_type=content_type)
    return {}, {'file': fs}


def _fake_mp4_bytes():
    # ftyp magic + padding to clear the 1KB minimum — NOT a playable video,
    # which is fine because start_processing is stubbed in route tests.
    return b'\x00\x00\x00\x18ftypisom' + b'\x00' * 2048


def test_upload_route_happy_and_status(client, tmp_path, monkeypatch):
    import lib.video_analysis as va
    import lib.video_analysis._store as store

    monkeypatch.setattr(store, '_registry_path',
                        lambda: str(tmp_path / 'registry.json'))
    monkeypatch.setattr(va, 'scratch_root', lambda: str(tmp_path / 'scratch'))
    os.makedirs(tmp_path / 'scratch', exist_ok=True)

    started = {}

    def _fake_start(
        video_id, scratch_path, name, *, user_id, reservation=None,
    ):
        started['video_id'] = video_id
        started['user_id'] = user_id
        import shutil
        shutil.rmtree(os.path.dirname(scratch_path), ignore_errors=True)
    monkeypatch.setattr(va, 'start_processing', _fake_start)
    import lib.media_attachments as media
    canonical = {
        'attachmentId': 'media-video-1', 'kind': 'video',
        'name': 'clip.mp4', 'status': 'processing',
        'sourceUrl': '/api/v4/media/attachments/media-video-1/source',
        'sizeBytes': len(_fake_mp4_bytes()),
    }
    monkeypatch.setattr(
        media, 'create_video', lambda *a, **k: (dict(canonical), False))
    monkeypatch.setattr(
        media, 'get_attachment',
        lambda _id, *, user_id: dict(canonical) if user_id == 1 else None)

    async def go():
        form, files = _multipart_video(_fake_mp4_bytes())
        r = await client.post('/api/v4/videos/upload', form=form, files=files)
        assert r.status_code == 200, r.status_code
        data = await v1_get_json(r)
        assert data['ok'] is True
        vid = data['video_id']
        assert data['status'] == 'processing'

        r2 = await client.get(f'/api/v4/videos/{vid}')
        assert r2.status_code == 200
        rec = await v1_get_json(r2)
        assert rec['ok'] is True
        assert rec['status'] == 'processing'
        assert 'process_owner' not in rec
        assert rec['name'] == 'clip.mp4'
        assert rec['size_bytes'] == len(_fake_mp4_bytes())
        return vid
    vid = _run_async(go())
    assert started['video_id'] == vid
    assert started['user_id'] == 1

    import routes.api_v1.videos as video_routes
    monkeypatch.setattr(video_routes, 'request_user_id', lambda: 2)

    async def get_as_foreign_owner():
        return await client.get(f'/api/v4/videos/{vid}')

    assert _run_async(get_as_foreign_owner()).status_code == 404


def test_upload_route_reports_actual_worker_start_failure(client, monkeypatch):
    import lib.video_analysis as va
    def fail(*args, **kwargs):
        raise RuntimeError('injected thread creation failure')
    monkeypatch.setattr(va, 'start_processing', fail)
    async def go():
        form, files = _multipart_video(_fake_mp4_bytes())
        response = await client.post('/api/v4/videos/upload', form=form, files=files)
        assert response.status_code == 500
        payload = await v1_get_json(response)
        assert payload['ok'] is False
    _run_async(go())


@pytest.mark.auth_mode('private')
def test_video_upload_status_and_legacy_source_require_auth(client):
    async def go():
        upload = await client.post('/api/v4/videos/upload')
        status = await client.get('/api/v4/videos/not-owned')
        source = await client.get('/api/videos/not-owned.mp4')
        assert upload.status_code == 401
        assert status.status_code == 401
        assert source.status_code == 401

    _run_async(go())


def test_video_file_route_requires_durable_asset_owner(
        client, tmp_path, monkeypatch):
    import lib.video_analysis._pipeline as pipe
    import lib.video_analysis._store as store
    import routes.api_v1.videos as video_routes

    monkeypatch.setattr(pipe, 'uploads_root', lambda: str(tmp_path / 'uploads'))
    filename = 'owned-video.mp4'
    video_dir = tmp_path / 'uploads' / 'videos'
    video_dir.mkdir(parents=True)
    (video_dir / filename).write_bytes(b'video-bytes')
    store.register_video_asset(
        filename,
        video_id='v_owned',
        user_id=1,
    )

    async def get_file():
        return await client.get(f'/api/videos/{filename}')

    assert _run_async(get_file()).status_code == 200
    monkeypatch.setattr(video_routes, 'request_user_id', lambda: 2)
    assert _run_async(get_file()).status_code == 404


def test_upload_route_rejects_bad_extension(client):
    async def go():
        form, files = _multipart_video(b'x' * 2048, filename='notes.txt',
                                       content_type='text/plain')
        r = await client.post('/api/v4/videos/upload', form=form, files=files)
        assert r.status_code == 400
        data = await v1_get_json(r)
        assert data['ok'] is False
    _run_async(go())


def test_upload_route_rejects_magic_mismatch(client, tmp_path, monkeypatch):
    import lib.video_analysis as va
    monkeypatch.setattr(va, 'scratch_root', lambda: str(tmp_path / 'scratch'))
    os.makedirs(tmp_path / 'scratch', exist_ok=True)

    async def go():
        form, files = _multipart_video(b'PK\x03\x04' + b'\x00' * 2048)
        r = await client.post('/api/v4/videos/upload', form=form, files=files)
        assert r.status_code == 400
        data = await v1_get_json(r)
        assert data['ok'] is False
    _run_async(go())


# Saturation-bound: red 3/3 in the full -n 4 unit lane (503 instead of 404
# under whole-host storage-sidecar contention), always green solo/small-lane;
# see test_server_async.py for the shared note.
@pytest.mark.serial
def test_status_route_404(client, tmp_path, monkeypatch):
    import lib.video_analysis._store as store
    monkeypatch.setattr(store, '_registry_path',
                        lambda: str(tmp_path / 'registry.json'))

    async def go():
        r = await client.get('/api/v4/videos/v_nonexistent')
        assert r.status_code == 404
    _run_async(go())


# ──────────────────────────────────────────────────────────────
#  Visual storyboard (model-agnostic vision relay)
# ──────────────────────────────────────────────────────────────

def test_storyboard_disabled_by_env(monkeypatch):
    monkeypatch.setenv('TOFU_VIDEO_STORYBOARD', '0')
    from lib.video_analysis._caption import storyboard_for_frames
    res = storyboard_for_frames([{'url': '/api/images/a.jpg', 't': 0, 'bytes': 1}])
    assert res['status'] == 'disabled' and res['text'] == ''


def test_storyboard_no_frames():
    from lib.video_analysis._caption import storyboard_for_frames
    assert storyboard_for_frames([])['status'] == 'no_frames'


def test_storyboard_no_vision_slot(monkeypatch):
    import lib.video_analysis._caption as cap
    monkeypatch.setattr(cap, '_vision_slot_models', lambda: [])
    res = cap.storyboard_for_frames([{'url': '/api/images/a.jpg', 't': 0, 'bytes': 1}])
    assert res['status'] == 'no_vision_slot'


def test_storyboard_happy_message_shape(monkeypatch):
    """Every frame reaches a bounded request batch in chronological order."""
    import lib.video_analysis._caption as cap
    monkeypatch.setattr(cap, '_vision_slot_models', lambda: ['vl-test'])
    monkeypatch.setattr(cap, 'video_frame_budget', lambda m, **k: 2)
    captured = []

    def _fake_dispatch(messages, *, capability, temperature, max_tokens, log_prefix):
        captured.append({'messages': messages, 'capability': capability})
        return ('- [00:00] a red screen\nOverall: a demo', {'_dispatch': {'model': 'vl-test'}})
    import lib.llm_dispatch as disp
    monkeypatch.setattr(disp, 'dispatch_chat', _fake_dispatch)

    frames = [{'url': f'/api/images/f{i}.jpg', 't': float(i) * 3, 'bytes': 100}
              for i in range(6)]
    res = cap.storyboard_for_frames(frames, name='demo.mp4', duration_s=15.0)
    assert res['status'] == 'ok'
    assert res['model'] == 'vl-test'
    assert 'Overall: a demo' in res['text']
    assert len(captured) == 3
    assert all(call['capability'] == 'vision' for call in captured)
    all_images = [block['image_url']['url'] for call in captured
                  for block in call['messages'][0]['content'] if block['type'] == 'image_url']
    assert all_images == [frame['url'] for frame in frames]
    blocks = captured[0]['messages'][0]['content']
    imgs = [b for b in blocks if b.get('type') == 'image_url']
    assert len(imgs) == 2
    assert blocks[-1]['type'] == 'text' and 'visual storyboard' in blocks[-1]['text']
    assert any(b.get('text') == '[frame at 00:00]' for b in blocks)


def test_storyboard_owner_route_is_pinned_and_disposed(monkeypatch):
    from types import SimpleNamespace

    import lib.llm_dispatch as dispatch
    import lib.model_routing as routing
    import lib.video_analysis._caption as cap
    from lib.llm_dispatch.provider_pin import get_pinned_provider

    group = SimpleNamespace(pin_id='owner-vision-group')
    observed = {'pins': [], 'disposed': [], 'mint': []}

    def _mint(*_args, **kwargs):
        observed['mint'].append(kwargs)
        return 'owner-vision', group

    def _dispatch(*_args, **_kwargs):
        observed['pins'].append(get_pinned_provider())
        return ('- [00:00] frame\nOverall: demo', {})

    monkeypatch.setattr(routing, 'mint_capability_slot_group', _mint)
    monkeypatch.setattr(
        routing, 'dispose_routed_slot_group', observed['disposed'].append)
    monkeypatch.setattr(cap, '_vision_slot_models', lambda: ['owner-vision'])
    monkeypatch.setattr(dispatch, 'dispatch_chat', _dispatch)

    result = cap.storyboard_for_frames(
        [{'url': '/api/images/a.jpg', 't': 0, 'bytes': 1}],
        owner_user_id=73,
    )

    assert result['status'] == 'ok'
    assert observed['pins'] == ['owner-vision-group']
    assert observed['disposed'] == [group]
    assert observed['mint'][0]['max_candidates'] == 8


def test_storyboard_dispatch_failure_degrades(monkeypatch):
    import lib.video_analysis._caption as cap
    monkeypatch.setattr(cap, '_vision_slot_models', lambda: ['vl-test'])
    import lib.llm_dispatch as disp
    def _boom(*a, **k):
        raise RuntimeError('all slots down')
    monkeypatch.setattr(disp, 'dispatch_chat', _boom)
    res = cap.storyboard_for_frames([{'url': '/api/images/a.jpg', 't': 0, 'bytes': 1}])
    assert res['status'] == 'failed' and res['text'] == ''


def test_transform_storyboard_serves_text_only_model(monkeypatch):
    """Chat model without vision: no image blocks, the storyboard carries the
    visual channel (plus transcript); header says who narrated."""
    import lib.model_info as mi
    monkeypatch.setattr(mi, 'video_frame_budget', lambda m, **k: 0)
    from lib.tasks_pkg.conv_message_builder._transform import _build_user_message
    msg = _video_msg()
    msg['videos'][0]['storyboard'] = '- [00:00] red\nOverall: demo arc'
    msg['videos'][0]['storyboard_model'] = 'vl-test'
    out = _build_user_message(msg, model='text-only-model')
    blocks = out['content']
    assert not any(b.get('type') == 'image_url' for b in blocks)
    assert 'visual storyboard' in blocks[0]['text'] and 'vl-test' in blocks[0]['text']
    sb = [b for b in blocks if b.get('type') == 'text'
          and b['text'].startswith('[Video 1 visual storyboard]')]
    assert sb and 'Overall: demo arc' in sb[0]['text']
    assert any('hello transcript' in b.get('text', '') for b in blocks)


def test_transform_storyboard_unused_when_model_has_vision(monkeypatch):
    """NEUTER (reverse): a vision chat model gets raw frames and the storyboard
    must NOT ride — proves the suppression branch is load-bearing."""
    import lib.model_info as mi
    monkeypatch.setattr(mi, 'video_frame_budget', lambda m, **k: 6)
    from lib.tasks_pkg.conv_message_builder._transform import _build_user_message
    msg = _video_msg()
    msg['videos'][0]['storyboard'] = '- [00:00] red\nOverall: demo arc'
    out = _build_user_message(msg, model='gpt-4o')
    blocks = out['content']
    assert sum(1 for b in blocks if b.get('type') == 'image_url') == 6
    assert not any(b.get('text', '').startswith('[Video 1 visual storyboard]')
                   for b in blocks if b.get('type') == 'text')


def test_turn_builder_videos_sanitize_and_passthrough():
    """build_user_msg_from_payload carries videos[] with a strict whitelist:
    unknown keys dropped, frame URLs must be local durable /api/images/ URLs."""
    from lib.chat.turn_builder import build_user_msg_from_payload
    payload = {'text': 'hi', 'videos': [
        {'video_id': 'v1', 'name': 'a.mp4', 'duration_s': 12,
         'frames': [{'url': '/api/images/x.jpg', 't': 1.0, 'bytes': 100},
                    {'url': 'https://evil.example/x.jpg', 't': 2.0},
                    {'url': 42}],
         'transcript': 'words', 'evil_extra': 'drop me'},
    ]}
    msg = build_user_msg_from_payload(
        payload, {'autoTranslate': False}, user_id=1)
    vids = msg['videos']
    assert len(vids) == 1
    assert vids[0]['video_id'] == 'v1'
    assert 'evil_extra' not in vids[0]
    assert vids[0]['frames'] == [{'url': '/api/images/x.jpg', 't': 1.0, 'bytes': 100}]


def test_turn_builder_storyboard_keys_survive():
    from lib.chat.turn_builder import build_user_msg_from_payload
    payload = {'text': 'hi', 'videos': [
        {'name': 'a.mp4', 'storyboard': 'arc text', 'storyboard_model': 'vl-test',
         'storyboard_status': 'internal-only'},
    ]}
    msg = build_user_msg_from_payload(
        payload, {'autoTranslate': False}, user_id=1)
    v = msg['videos'][0]
    assert v['storyboard'] == 'arc text' and v['storyboard_model'] == 'vl-test'
    assert 'storyboard_status' not in v  # status is registry metadata, not payload


def test_turn_builder_no_videos_key_when_empty():
    from lib.chat.turn_builder import build_user_msg_from_payload
    msg = build_user_msg_from_payload({'text': 'hi', 'videos': []},
                                      {'autoTranslate': False}, user_id=1)
    assert 'videos' not in msg


def test_video_transport_has_no_default_byte_quota_and_other_policy_is_explicit():
    from lib.http_transport.body_policy import build_http_body_policy
    from lib.app.app_assembly import DEFAULT_MAX_CONTENT_LENGTH
    policy = build_http_body_policy()
    caps = dict(policy.route_caps)
    assert caps['/api/v4/videos/upload'] is None
    assert caps['/api/v1/videos/upload'] is None
    assert DEFAULT_MAX_CONTENT_LENGTH is None
    assert policy.default_cap == 50*1024*1024


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))


@pytest.mark.slow
def test_large_multipart_video_reaches_durable_source_and_processing_owner(client,tmp_path,monkeypatch):
    import asyncio
    import lib.video_analysis as va
    import lib.video_analysis._store as store
    from lib.media_attachments import get_attachment
    scratch = tmp_path / 'large-scratch'
    scratch.mkdir()
    monkeypatch.setattr(va,'scratch_root',lambda:str(scratch))
    monkeypatch.setattr(store,'_registry_path',lambda:str(tmp_path/'registry.json'))
    started = []
    def finish_handoff(video_id,path,name,*,user_id,reservation=None):
        started.append((video_id,os.path.getsize(path),user_id))
        import shutil
        shutil.rmtree(os.path.dirname(path))
    monkeypatch.setattr(va,'start_processing',finish_handoff)
    boundary = 'tofu-large-video-demand'
    header = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="large.mp4"\r\n'
              'Content-Type: video/mp4\r\n\r\n').encode()
    tail = f'\r\n--{boundary}--\r\n'.encode()
    chunk = b'\x00\x00\x00\x18ftypisom' + b'x' * (1024*1024-12)
    count = 513
    async def exercise():
        async with client.request('/api/v4/videos/upload',method='POST',headers={
            'Content-Type':f'multipart/form-data; boundary={boundary}',
            'Content-Length':str(len(header)+count*len(chunk)+len(tail)),
        }) as connection:
            await connection.send(header)
            for _ in range(count): await connection.send(chunk)
            await connection.send(tail)
            await connection.send_complete()
        response = await connection.as_response()
        assert response.status_code == 200, await response.get_json()
        result = await v1_get_json(response)
        assert result['ok'] is True and result['status'] == 'processing'
        assert started == [(result['video_id'],count*len(chunk),1)]
        attachment = get_attachment(result['video_id'],user_id=1)
        assert attachment['sizeBytes'] == count*len(chunk)
        from lib.storage.errors import StorageError
        try:
            foreign = get_attachment(result['video_id'],user_id=2)
        except StorageError as error:
            assert error.code == 'database_forbidden'
            assert error.message == 'authority_scope_mismatch'
        else:
            assert foreign is None
        assert not list(scratch.iterdir())
    _run_async(exercise())


def test_scratch_reclaim_respects_live_and_unknown_process_owners(tmp_path, monkeypatch):
    import json
    from lib.video_analysis import _config
    monkeypatch.setenv('TOFU_VIDEO_SCRATCH_TTL_S', '3600')
    now = time.time()
    live = tmp_path / 'job_live'
    unknown = tmp_path / 'job_unknown'
    dead = tmp_path / 'job_dead'
    for path in (live, unknown, dead):
        path.mkdir()
        _config.mark_scratch_owner(str(path))
        (path / 'source.mp4').write_bytes(b'owned bytes')
    (unknown / '.process-owner.json').write_text('null')
    owner = json.loads((dead / '.process-owner.json').read_text())
    owner['started'] = '0'
    (dead / '.process-owner.json').write_text(json.dumps(owner))
    for path in (live, unknown, dead):
        os.utime(path, (now - 7200, now - 7200))
    assert _config.reclaim_stale_scratch(str(tmp_path), now_s=now) == 1
    assert (live / 'source.mp4').read_bytes() == b'owned bytes'
    assert (unknown / 'source.mp4').read_bytes() == b'owned bytes'
    assert not dead.exists()


def test_processing_records_do_not_expire_by_age_or_unknown_owner(tmp_path, monkeypatch):
    import lib.video_analysis._store as store
    from lib.json_store import update_json_atomic
    path = tmp_path / 'registry.json'
    monkeypatch.setattr(store, '_registry_path', lambda: str(path))
    store.create_record('v_unknown', name='long.mp4', size_bytes=1, user_id=1)
    def age(reg):
        reg['v_unknown']['updated_at'] = 0
        reg['v_unknown']['process_owner'] = None
        return reg
    update_json_atomic(str(path), age, default={})
    store.create_record('v_new', name='new.mp4', size_bytes=1, user_id=1)
    assert store.get_record('v_unknown', user_id=1)['status'] == 'processing'
    assert store.get_record('v_unknown', user_id=2) is None


@pytest.mark.parametrize('duration', [float('nan'), float('inf'), -1.0])
def test_unusable_video_duration_still_settles_failure(tmp_path, monkeypatch, duration):
    import lib.video_analysis._pipeline as pipe
    import lib.video_analysis._store as store
    import lib.motion_video._gates as gates
    import lib.media_attachments as media
    monkeypatch.setattr(store, '_registry_path', lambda: str(tmp_path / 'registry.json'))
    monkeypatch.setattr(gates, 'probe_video', lambda path: {'codec': 'h264', 'duration': duration})
    monkeypatch.setattr(media, 'set_phase', lambda *args, **kwargs: None)
    failures = []
    monkeypatch.setattr(media, 'mark_failed', lambda video_id, error, **kwargs: failures.append(error))
    store.create_record('v_invalid', name='bad.mp4', size_bytes=1, user_id=1)
    pipe._process('v_invalid', '/not-decoded.mp4', 'bad.mp4', user_id=1)
    assert failures == ['could not determine video duration']
    assert store.get_record('v_invalid', user_id=1)['status'] == 'failed'


def test_real_long_clip_preserves_more_than_64_frames(tmp_path):
    from lib.video_analysis._frames import extract_frames
    video = _make_video(tmp_path / 'long.mp4', seconds=700.0, size='32x32')
    frames = extract_frames(video, 700.0, str(tmp_path))
    assert len(frames) == 70
    assert frames[-1]['t'] > 690
    assert all(os.path.getsize(frame['path']) > 0 for frame in frames)
    print(f'video_frame_demand: {len(frames)} frames, '
          f'{sum(os.path.getsize(frame["path"]) for frame in frames)} JPEG bytes, 700s source')


def test_scene_evidence_beyond_old_count_ceiling_is_retained():
    from lib.video_analysis._frames import _merge_scene_extras
    cuts = [float(index * 3) for index in range(200)]
    assert _merge_scene_extras([], cuts) == cuts


def test_late_storyboard_batch_failure_cannot_publish_partial_success(monkeypatch):
    import lib.video_analysis._caption as caption
    import lib.llm_dispatch as dispatch
    monkeypatch.setattr(caption, '_vision_slot_models', lambda: ['vision'])
    monkeypatch.setattr(caption, 'video_frame_budget', lambda *args, **kwargs: 2)
    calls = []
    def narrate(messages, **kwargs):
        calls.append(messages)
        if len(calls) == 3:
            raise RuntimeError('late provider failure')
        return ('partial narration', {})
    monkeypatch.setattr(dispatch, 'dispatch_chat', narrate)
    frames = [{'url': f'/api/images/{index}.jpg', 't': index, 'bytes': 10} for index in range(6)]
    result = caption.storyboard_for_frames(frames)
    assert len(calls) == 3
    assert result == {'text': '', 'status': 'failed', 'model': ''}


def test_storyboard_visits_every_frame_beyond_old_ceiling(monkeypatch):
    import lib.video_analysis._caption as caption
    import lib.llm_dispatch as dispatch
    monkeypatch.setattr(caption, '_vision_slot_models', lambda: ['vision'])
    monkeypatch.setattr(caption, 'video_frame_budget', lambda *args, **kwargs: 200)
    seen = []
    batches = []
    def narrate(messages, **kwargs):
        images = [block['image_url']['url'] for block in messages[0]['content']
                  if block['type'] == 'image_url']
        batches.append(len(images))
        seen.extend(images)
        return ('narrated ' + images[-1], {'model': 'vision'})
    monkeypatch.setattr(dispatch, 'dispatch_chat', narrate)
    frames = [{'url': f'/api/images/{index}.jpg', 't': index, 'bytes': 10} for index in range(193)]
    result = caption.storyboard_for_frames(frames)
    assert result['status'] == 'ok'
    assert seen == [frame['url'] for frame in frames]
    assert len(batches) > 1
    assert '/api/images/192.jpg' in result['text']


def test_detected_scene_frame_failure_cannot_be_hidden_as_complete(monkeypatch):
    import lib.video_analysis._frames as frames
    monkeypatch.setattr(frames, '_ffmpeg', lambda: 'ffmpeg')
    monkeypatch.setattr(frames, '_extract_uniform', lambda *args: [{'path': 'uniform.jpg', 't': 0.0}])
    monkeypatch.setattr(frames, '_scene_cut_times', lambda *args: [5.0])
    monkeypatch.setattr(frames, '_extract_single', lambda *args: None)
    with pytest.raises(RuntimeError, match='Scene evidence extraction failed'):
        frames.extract_frames('video.mp4', 60.0, '/scratch')
