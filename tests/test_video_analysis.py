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
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_video_analysis.py -v
"""

import io
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]


@pytest.fixture
def client(flask_app):
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

def test_frame_tiers():
    from lib.video_analysis import frame_target_for_duration, FRAME_CEILING
    assert frame_target_for_duration(1) == 16
    assert frame_target_for_duration(60) == 16
    assert frame_target_for_duration(61) == 32
    assert frame_target_for_duration(600) == 32
    assert frame_target_for_duration(601) == 64
    assert frame_target_for_duration(900) == FRAME_CEILING == 64


def test_frame_ceiling_parity():
    """model_info._video duplicates FRAME_CEILING BY VALUE (one-way import);
    this pin fails if either side moves without the other."""
    from lib.model_info._video import _EXTRACTION_CEILING
    from lib.video_analysis import FRAME_CEILING
    assert _EXTRACTION_CEILING == FRAME_CEILING


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
    out = _build_user_message(_video_msg(n_frames=10), model='gpt-4o')
    blocks = out['content']
    imgs = [b for b in blocks if b.get('type') == 'image_url']
    assert len(imgs) == 4
    assert '4 frames sampled (of 10 extracted)' in blocks[0]['text']
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
    # 3s → tier 16 → uniform layer = 16 - 16//3 = 11; a flat color clip has
    # no scene cuts AND any cut would dedupe against the dense uniform layer,
    # so the total IS the uniform layer (see _merge_scene_extras docstring).
    assert len(frames) == 11
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


def test_merge_scene_extras_dedupe_and_budget():
    """Cuts within ±1s of a uniform sample (or each other) are redundant;
    the budget caps how many extras ride."""
    from lib.video_analysis._frames import _merge_scene_extras
    uniform = [10.0, 20.0, 30.0, 40.0]
    cuts = [10.4, 25.0, 25.5, 33.0, 50.0]
    picked = _merge_scene_extras(uniform, cuts, budget=5)
    assert picked == [25.0, 33.0, 50.0]
    assert _merge_scene_extras(uniform, cuts, budget=1) == [25.0]
    assert _merge_scene_extras(uniform, [], 5) == []


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


def test_pipeline_end_to_end(tmp_path, monkeypatch):
    """Full pipeline on a real synthetic clip: probe → persist → frames →
    (stubbed) transcript → registry record ready with durable frame URLs."""
    import lib.video_analysis._pipeline as pipe
    import lib.video_analysis._store as store

    monkeypatch.setattr(store, '_registry_path',
                        lambda: str(tmp_path / 'registry.json'))
    monkeypatch.setattr(pipe, 'uploads_root', lambda: str(tmp_path / 'uploads'))
    monkeypatch.setattr(pipe, 'transcribe_track',
                        lambda *a, **k: {'text': 'fake words', 'status': 'ok',
                                         'model': 'whisper-1'})
    import lib.video_analysis._caption as cap
    monkeypatch.setattr(cap, 'storyboard_for_frames',
                        lambda *a, **k: {'text': '- [00:00] red screen\nOverall: demo',
                                         'status': 'ok', 'model': 'vl-test'})

    video = _make_video(tmp_path / 'job' / 'upload.mp4', seconds=2.0,
                        with_audio=True)
    store.create_record('v_test_e2e', name='clip.mp4', size_bytes=os.path.getsize(video))
    pipe._process('v_test_e2e', video, 'clip.mp4')

    rec = store.get_record('v_test_e2e')
    assert rec['status'] == 'ready', rec.get('error')
    assert rec['phase'] == 'done'
    assert rec['duration_s'] == pytest.approx(2.0, abs=0.3)
    assert rec['frame_count'] >= 8
    assert rec['transcript'] == 'fake words'
    assert rec['transcript_status'] == 'ok'
    assert rec['storyboard_status'] == 'ok'
    assert 'Overall: demo' in rec['storyboard']
    assert rec['storyboard_model'] == 'vl-test'
    assert rec['video_url'].startswith('/api/videos/')
    # Frames persisted under the (tmp) uploads store with durable URLs.
    for fr in rec['frames']:
        assert fr['url'].startswith('/api/images/')
        fname = fr['url'].rsplit('/', 1)[-1]
        assert os.path.isfile(tmp_path / 'uploads' / 'images' / fname)
    # The original was copied into the durable videos dir.
    stored = rec['video_url'].rsplit('/', 1)[-1]
    assert os.path.isfile(tmp_path / 'uploads' / 'videos' / stored)


def test_pipeline_rejects_too_long(tmp_path, monkeypatch):
    import lib.video_analysis._pipeline as pipe
    import lib.video_analysis._store as store

    monkeypatch.setattr(store, '_registry_path',
                        lambda: str(tmp_path / 'registry.json'))
    monkeypatch.setattr(pipe, 'video_max_duration_s', lambda: 1.0)
    video = _make_video(tmp_path / 'job' / 'upload.mp4', seconds=3.0)
    store.create_record('v_long', name='long.mp4', size_bytes=1)
    pipe._process('v_long', video, 'long.mp4')
    rec = store.get_record('v_long')
    assert rec['status'] == 'failed'
    assert 'too long' in rec['error']


def test_stale_processing_record_swept(tmp_path, monkeypatch):
    import lib.video_analysis._store as store
    monkeypatch.setattr(store, '_registry_path',
                        lambda: str(tmp_path / 'registry.json'))
    store.create_record('v_stale', name='x.mp4', size_bytes=1)
    # Age the record directly so the stale-processing sweep sees it.
    from lib.json_store import update_json_atomic
    def _age(reg):
        reg['v_stale']['updated_at'] = 0
        return reg
    update_json_atomic(str(tmp_path / 'registry.json'), _age, default={})
    got = store.get_record('v_stale')
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

    def _fake_start(video_id, scratch_path, name):
        started['video_id'] = video_id
        import shutil
        shutil.rmtree(os.path.dirname(scratch_path), ignore_errors=True)
    monkeypatch.setattr(va, 'start_processing', _fake_start)

    async def go():
        form, files = _multipart_video(_fake_mp4_bytes())
        r = await client.post('/api/v1/videos/upload', form=form, files=files)
        assert r.status_code == 200, r.status_code
        data = await r.get_json()
        assert data['ok'] is True
        vid = data['video_id']
        assert data['status'] == 'processing'

        r2 = await client.get(f'/api/v1/videos/{vid}')
        assert r2.status_code == 200
        rec = await r2.get_json()
        assert rec['ok'] is True
        assert rec['status'] == 'processing'
        assert rec['name'] == 'clip.mp4'
        assert rec['size_bytes'] == len(_fake_mp4_bytes())
        return vid
    vid = _run_async(go())
    assert started['video_id'] == vid


def test_upload_route_rejects_bad_extension(client):
    async def go():
        form, files = _multipart_video(b'x' * 2048, filename='notes.txt',
                                       content_type='text/plain')
        r = await client.post('/api/v1/videos/upload', form=form, files=files)
        assert r.status_code == 400
        data = await r.get_json()
        assert data['ok'] is False
    _run_async(go())


def test_upload_route_rejects_magic_mismatch(client, tmp_path, monkeypatch):
    import lib.video_analysis as va
    monkeypatch.setattr(va, 'scratch_root', lambda: str(tmp_path / 'scratch'))
    os.makedirs(tmp_path / 'scratch', exist_ok=True)

    async def go():
        form, files = _multipart_video(b'PK\x03\x04' + b'\x00' * 2048)
        r = await client.post('/api/v1/videos/upload', form=form, files=files)
        assert r.status_code == 400
        data = await r.get_json()
        assert data['ok'] is False
    _run_async(go())


def test_status_route_404(client, tmp_path, monkeypatch):
    import lib.video_analysis._store as store
    monkeypatch.setattr(store, '_registry_path',
                        lambda: str(tmp_path / 'registry.json'))

    async def go():
        r = await client.get('/api/v1/videos/v_nonexistent')
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
    """One batched vision call: per-frame image+timestamp pairs, instruction
    LAST (media-before-question convention), frame set thinned to budget."""
    import lib.video_analysis._caption as cap
    monkeypatch.setattr(cap, '_vision_slot_models', lambda: ['vl-test'])
    monkeypatch.setattr(cap, 'video_frame_budget', lambda m, **k: 2)
    captured = {}

    def _fake_dispatch(messages, *, capability, temperature, max_tokens, log_prefix):
        captured['messages'] = messages
        captured['capability'] = capability
        return ('- [00:00] a red screen\nOverall: a demo', {'_dispatch': {'model': 'vl-test'}})
    import lib.llm_dispatch as disp
    monkeypatch.setattr(disp, 'dispatch_chat', _fake_dispatch)

    frames = [{'url': f'/api/images/f{i}.jpg', 't': float(i) * 3, 'bytes': 100}
              for i in range(6)]
    res = cap.storyboard_for_frames(frames, name='demo.mp4', duration_s=15.0)
    assert res['status'] == 'ok'
    assert res['model'] == 'vl-test'
    assert 'Overall: a demo' in res['text']
    assert captured['capability'] == 'vision'
    blocks = captured['messages'][0]['content']
    imgs = [b for b in blocks if b.get('type') == 'image_url']
    assert len(imgs) == 2  # thinned 6 → 2
    assert blocks[-1]['type'] == 'text' and 'visual storyboard' in blocks[-1]['text']
    assert any(b.get('text') == '[frame at 00:00]' for b in blocks)


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
    msg = build_user_msg_from_payload(payload, {'autoTranslate': False})
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
    msg = build_user_msg_from_payload(payload, {'autoTranslate': False})
    v = msg['videos'][0]
    assert v['storyboard'] == 'arc text' and v['storyboard_model'] == 'vl-test'
    assert 'storyboard_status' not in v  # status is registry metadata, not payload


def test_turn_builder_no_videos_key_when_empty():
    from lib.chat.turn_builder import build_user_msg_from_payload
    msg = build_user_msg_from_payload({'text': 'hi', 'videos': []},
                                      {'autoTranslate': False})
    assert 'videos' not in msg


def test_frontend_video_wiring_pins():
    """Static pins on the frontend glue (the repo's ratchet idiom): the send
    pipeline, upload chips, Api namespace and file-picker accept must ALL stay
    wired for videos — a silent de-wire turns uploads into no-ops."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    send = (root / 'static/js/main/main_send_pipeline.js').read_text()
    assert 'msgPayload.videos' in send
    assert 'userMsg.videos = msgPayload.videos' in send
    assert 'pendingVideos = [];' in send
    assert '_waitForPendingVideos' in send
    upload = (root / 'static/js/upload.js').read_text()
    assert '_looksLikeVideo' in upload and 'Api.videos.upload' in upload
    assert 'video-chip' in upload
    api = (root / 'static/js/api.js').read_text()
    assert '/api/v1/videos/upload' in api and '/api/v1/videos/' in api
    html = (root / 'index.html').read_text()
    assert 'video/*' in html


def test_body_cap_guard_matrix():
    """The central body-cap table: only the video upload path gets 512 MiB;
    everything else stays at the legacy 50 MiB (owner ruling). Source-pinned
    so a future refactor can't silently widen the default."""
    import inspect
    import server as _srv
    src = inspect.getsource(_srv)
    assert "'/api/v1/videos/upload', 512 * 1024 * 1024" in src
    assert '_DEFAULT_BODY_CAP = 50 * 1024 * 1024' in src
    assert "app.config['MAX_CONTENT_LENGTH'] = 520 * 1024 * 1024" in src


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
