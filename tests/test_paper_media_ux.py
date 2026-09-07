#!/usr/bin/env python3
"""tests/test_paper_media_ux.py — P-UX1~4 backend suite.

The server half of the progress-perception / anti-stuck contract
(docs/modules/ingest_media.md, epic pt_7e4cc2c898984bde):

  * P-UX1 stall reaping — TaskRuntime.reap_if_stalled: a silent running
    task is declared worker_lost on the poll path (opt-in stall_timeout);
    fresh tasks untouched; disabled by default; NEUTER proves the poll
    hook is load-bearing.
  * P-UX2 heartbeat — emits phase/elapsed beats on the interval and stops
    with the block; NEUTER proves the thread is load-bearing. Phase
    vocabulary: podcast worker emits source→script→audio phase_started;
    motion engine emits the full per-plan sequence + narrate/compose
    progress events (A/B: a narration fake that ignores on_scene_done
    produces NO progress events — the callback wiring is load-bearing).
  * P-UX4 restart resilience — podcast generating row persisted at start,
    terminal rows never linger as 'generating', mark_interrupted_podcasts
    sweep, lookup interrupted surface; video job-manifest disk fallback
    (lookup fragment + file/scenes serving after the task left memory).
  * §2.1 video-abstract dedup + force bypass (NEUTER on claim registration).

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_paper_media_ux.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import uuid
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1

SCRIPT = {
    'title': '测试播客', 'lang': 'zh', 'mode': 'short',
    'segments': [
        {'id': 0, 'section': 'cold_open', 'speaker': 'host',
         'text': '这篇论文把成绩推到了 86.3。' * 6,
         'est_seconds': 60.0, 'figure_ref': None},
        {'id': 1, 'section': 'recap', 'speaker': 'host',
         'text': '三条带走。' * 6, 'est_seconds': 60.0, 'figure_ref': None},
    ],
}
META = {'low_confidence': False, 'issues': [], 'critic_issues': [],
        'revisions': 0, 'source_kind': 'report_zh',
        'usage': {'input': 10, 'output': 100}}


def _tiny_wav(duration_s=0.1, rate=8000) -> bytes:
    pcm = b'\x00\x00' * int(duration_s * rate)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class _FakeSlot:
    def __init__(self, model='unit-tts'):
        self.model = model
        self.key_name = 'k0'
        self.capabilities = frozenset({'tts'})
        self.oauth = False
        self.base_url = 'https://tts.example/v1'
        self.api_key = 'sk-test'
        self.extra_headers = {}
        self.provider_id = 'prov0'

    def score(self):
        return 1.0


@pytest.fixture()
def phash() -> str:
    return uuid.uuid4().hex[:32]


@pytest.fixture()
def storage_env(tmp_path):
    """Own one isolated real Sidecar for tests that persist paper state."""
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test

    runtime = StorageRuntime(StorageSupervisor(
        project_root=tmp_path / 'storage', backend='sqlite'),
        auto_restart=False)
    install_runtime_for_test(runtime)
    runtime.start()
    try:
        yield runtime
    finally:
        install_runtime_for_test(None)


@pytest.fixture()
def podcast_env(tmp_path, monkeypatch, storage_env):
    """Same seams as test_paper_podcast_api: paper dir redirected, script
    LLM + TTS provider stubbed."""
    import lib.paper_identity as hashing
    import lib.paper.podcast_engine.worker as PE
    import lib.paper.podcast_engine._audio as PA
    import lib.model_routing as routing
    import lib.tts as T
    from types import SimpleNamespace
    monkeypatch.setattr(hashing, 'PAPER_DIR', str(tmp_path))
    (tmp_path / 'podcast').mkdir(exist_ok=True)
    monkeypatch.setattr(PE, 'generate_script',
                        lambda **kw: (dict(SCRIPT), dict(META)))
    monkeypatch.setattr(
        'lib.paper.images.figures.load_image_manifest', lambda ph: [])
    monkeypatch.setattr(
        'lib.paper.images.titles.lookup_paper_title',
        lambda ph, *, user_id: '测试论文')
    monkeypatch.setattr(T, '_tts_slots', lambda: [_FakeSlot()])
    monkeypatch.setattr(T, '_post_speech',
                        lambda slot, text, *, voice, fmt, speed: _tiny_wav())
    route = SimpleNamespace(model_id='unit-tts', provider_id='prov0')
    group = SimpleNamespace(pin_id='prov0')
    monkeypatch.setattr(
        routing, 'list_capability_routes',
        lambda *_args, **_kwargs: [route])
    monkeypatch.setattr(
        routing, 'mint_capability_slot_group',
        lambda *_args, **_kwargs: ('unit-tts', group))
    monkeypatch.setattr(
        routing, 'dispose_routed_slot_group', lambda _group: True)
    monkeypatch.setattr(PA, '_transcode_to_mp3', lambda wav: None)
    del storage_env
    return tmp_path


def _insert_report(phash, lang='zh'):
    from lib.storage import get_storage_client
    get_storage_client(write=True).command('paper.report.upsert', {
        'user_id': TEST_OWNER_USER_ID,
        'paper_hash': phash, 'lang': lang,
        'report': '报告:成绩 86.3,上一代 83.1。', 'model': 'm',
        'meta': {}, 'created_at': int(time.time()),
    }, f'paper.podcast.report:{uuid.uuid4().hex}')


def _podcast_row_status(phash, mode='short', lang='zh', voice='alloy'):
    from lib.storage import get_storage_client
    row = get_storage_client().query('paper.podcast.get', {
        'user_id': TEST_OWNER_USER_ID,
        'paper_hash': phash, 'mode': mode, 'lang': lang, 'voice': voice,
    })
    return ((row['status'], json.dumps(row['meta'], ensure_ascii=False))
            if row else (None, None))


# ══════════════════════════════════════════════════════════
#  P-UX1 — stall reaping (TaskRuntime)
# ══════════════════════════════════════════════════════════

def test_stall_reap_declares_worker_lost():
    from lib.agent_core.task_runtime import TaskRuntime
    rt = TaskRuntime('ux-stall', ttl=60, push_channel=None, stall_timeout=0.05)
    task = rt.create(user_id=1, meta={})
    rt.append_event(task['id'], {'type': 'status', 'status': 'running'})
    assert task['status'] == 'running'
    task['updated_at'] = time.time() - 10  # silent for 10s ≫ 0.05s

    resp = rt.poll(task['id'], 0)
    assert resp['done'] is True and resp['status'] == 'error'
    assert resp['error']['kind'] == 'worker_lost', resp['error']
    # terminal event really appended (cursor consumers see it)
    term = [e for e in resp['events'] if e.get('type') == 'error']
    assert term and term[-1]['error']['kind'] == 'worker_lost'
    # reap is idempotent — a second poll stays put
    resp2 = rt.poll(task['id'], resp['next_cursor'])
    assert resp2['status'] == 'error'


def test_stall_reap_fresh_task_untouched():
    from lib.agent_core.task_runtime import TaskRuntime
    rt = TaskRuntime('ux-fresh', ttl=60, push_channel=None, stall_timeout=0.2)
    task = rt.create(user_id=1, meta={})
    rt.append_event(task['id'], {'type': 'status', 'status': 'running'})
    resp = rt.poll(task['id'], 0)
    assert resp['status'] == 'running' and resp['done'] is False


def test_stall_reap_disabled_by_default():
    from lib.agent_core.task_runtime import TaskRuntime
    rt = TaskRuntime('ux-off', ttl=60, push_channel=None)  # stall_timeout=0
    task = rt.create(user_id=1, meta={})
    rt.append_event(task['id'], {'type': 'status', 'status': 'running'})
    task['updated_at'] = time.time() - 99999
    resp = rt.poll(task['id'], 0)
    assert resp['status'] == 'running', 'default runtime must never reap'


def test_stall_reap_NEUTER_poll_hook_loadbearing(monkeypatch):
    """NEUTER: amputate the reap call from poll() (no-op the method) → the
    SAME stale task is never reaped. Proves the poll hook is what declares
    worker_lost, not some background thread."""
    from lib.agent_core.task_runtime import TaskRuntime
    rt = TaskRuntime('ux-neuter', ttl=60, push_channel=None, stall_timeout=0.05)
    task = rt.create(user_id=1, meta={})
    rt.append_event(task['id'], {'type': 'status', 'status': 'running'})
    task['updated_at'] = time.time() - 10
    monkeypatch.setattr(TaskRuntime, 'reap_if_stalled', lambda self, t: False)
    resp = rt.poll(task['id'], 0)
    assert resp['status'] == 'running' and resp['done'] is False


def test_production_runtimes_carry_stall_timeout():
    """Both media runtimes are opted in (120s per the design)."""
    from lib.paper.podcast_runtime import _podcast_runtime
    from lib.motion_video.runtime import _motion_runtime
    assert _podcast_runtime.stall_timeout == 120
    assert _motion_runtime.stall_timeout == 120


# ══════════════════════════════════════════════════════════
#  P-UX2 — heartbeat
# ══════════════════════════════════════════════════════════

def test_heartbeat_emits_and_stops():
    from lib.production.heartbeat import heartbeat
    events = []
    task = {}
    with heartbeat(task, lambda t, ev: events.append(ev), 'script',
                   interval=0.05):
        time.sleep(0.17)
    count_inside = len(events)
    assert count_inside >= 2, events
    assert all(e['type'] == 'heartbeat' and e['phase'] == 'script'
               for e in events)
    assert events[-1]['elapsed_s'] >= events[0]['elapsed_s']
    time.sleep(0.12)
    assert len(events) == count_inside, 'heartbeat must stop with the block'


def test_heartbeat_NEUTER_thread_loadbearing(monkeypatch):
    """NEUTER: the beat thread never started → zero events. Proves the
    background thread (not the block body) produces the beats."""

    class _DeadThread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setattr('lib.production.heartbeat.threading.Thread',
                        _DeadThread)
    from lib.production.heartbeat import heartbeat
    events = []
    with heartbeat({}, lambda t, ev: events.append(ev), 'script',
                   interval=0.02):
        time.sleep(0.1)
    assert events == []


# ══════════════════════════════════════════════════════════
#  P-UX2/4 — podcast worker: phase vocabulary + durable rows
# ══════════════════════════════════════════════════════════

def test_podcast_worker_phase_vocabulary(podcast_env, phash, monkeypatch):
    """phase_started source→script→audio with indexes; script sub-step
    progress events flow through generate_script's on_event."""
    import lib.paper.podcast_engine.worker as PE
    from lib.paper.podcast_runtime import _new_podcast_task

    def _script_with_steps(**kw):
        kw['on_event']({'type': 'progress', 'phase': 'script',
                        'unit': 'pass', 'step': 'draft'})
        kw['on_event']({'type': 'progress', 'phase': 'script',
                        'unit': 'pass', 'step': 'validate'})
        return dict(SCRIPT), dict(META)

    monkeypatch.setattr(PE, 'generate_script', _script_with_steps)
    _insert_report(phash)
    task = _new_podcast_task('podcast_ux01', phash, 'short', 'zh', 'alloy', None, user_id=TEST_OWNER_USER_ID)
    PE.run_podcast_task(task)

    assert task['status'] == 'done', [e for e in task['events']]
    started = [e for e in task['events'] if e['type'] == 'phase_started']
    assert [e['phase'] for e in started] == ['source', 'script', 'audio']
    assert [e['phase_index'] for e in started] == [1, 2, 3]
    assert all(e['phase_total'] == 3 for e in started)
    steps = [e['step'] for e in task['events']
             if e['type'] == 'progress' and e.get('phase') == 'script']
    assert steps == ['draft', 'validate']


def test_podcast_worker_generating_row_then_done(podcast_env, phash, monkeypatch):
    """P-UX4: the row says 'generating' mid-run and 'done' at the end —
    it must never LINGER as generating (that would read as interrupted)."""
    import lib.paper.podcast_engine.worker as PE
    from lib.paper.podcast_runtime import _new_podcast_task

    seen = {}

    def _script_probe(**kw):
        seen['mid_status'], _ = _podcast_row_status(phash)
        return dict(SCRIPT), dict(META)

    monkeypatch.setattr(PE, 'generate_script', _script_probe)
    _insert_report(phash)
    task = _new_podcast_task('podcast_ux02', phash, 'short', 'zh', 'alloy', None, user_id=TEST_OWNER_USER_ID)
    PE.run_podcast_task(task)

    assert seen['mid_status'] == 'generating'
    final_status, _ = _podcast_row_status(phash)
    assert final_status == 'done'


def test_podcast_error_row_never_lingers_generating(podcast_env, phash, monkeypatch):
    import lib.paper.podcast_engine.worker as PE
    from lib.paper.podcast_runtime import _new_podcast_task

    def _boom(**kw):
        raise RuntimeError('script llm dead')

    monkeypatch.setattr(PE, 'generate_script', _boom)
    _insert_report(phash)
    task = _new_podcast_task('podcast_ux03', phash, 'short', 'zh', 'alloy', None, user_id=TEST_OWNER_USER_ID)
    PE.run_podcast_task(task)

    assert task['status'] == 'error'
    status, _ = _podcast_row_status(phash)
    assert status == 'error', f'row must not stay generating, got {status!r}'


def test_podcast_abort_keeps_script_as_script_only(podcast_env, phash, monkeypatch):
    """§3.4F: abort after the script landed → the partial product is kept
    as a script_only row (degrade_reason tells the truth)."""
    import lib.paper.podcast_engine.worker as PE
    from lib.paper.podcast_runtime import _new_podcast_task

    _insert_report(phash)
    task = _new_podcast_task('podcast_ux04', phash, 'short', 'zh', 'alloy', None, user_id=TEST_OWNER_USER_ID)

    def _script_then_abort(**kw):
        task['abort_event'].set()
        return dict(SCRIPT), dict(META)

    monkeypatch.setattr(PE, 'generate_script', _script_then_abort)
    PE.run_podcast_task(task)

    assert task['status'] == 'aborted'
    status, meta_raw = _podcast_row_status(phash)
    assert status == 'script_only'
    assert json.loads(meta_raw).get('degrade_reason') == 'aborted_before_audio'


def test_mark_interrupted_podcasts(podcast_env, phash):
    import lib.paper.podcast_engine.worker as PE

    PE.persist_podcast_row(phash, 'short', 'zh', 'alloy',
                            status='generating', script={}, meta={},
                            user_id=TEST_OWNER_USER_ID)
    phash2 = uuid.uuid4().hex[:32]
    PE.persist_podcast_row(phash2, 'short', 'zh', 'alloy',
                            status='done', script=dict(SCRIPT), meta=dict(META),
                            user_id=TEST_OWNER_USER_ID)

    n = PE.mark_interrupted_podcasts()
    assert n >= 1
    assert _podcast_row_status(phash)[0] == 'interrupted'
    assert _podcast_row_status(phash2)[0] == 'done', 'done rows untouched'
    assert PE.load_interrupted_podcast(
        phash, 'short', 'zh', 'alloy',
        user_id=TEST_OWNER_USER_ID) is True
    assert PE.load_interrupted_podcast(
        phash2, 'short', 'zh', 'alloy',
        user_id=TEST_OWNER_USER_ID) is False


# ══════════════════════════════════════════════════════════
#  P-UX2/3 — motion engine: phase vocabulary + per-scene progress
# ══════════════════════════════════════════════════════════

_SRT = """1
00:00:01,000 --> 00:00:03,000
第一句话。

2
00:00:03,000 --> 00:00:05,000
第二句话。
"""


def _engine_task(tmp_path, monkeypatch, **over):
    from lib.motion_video.runtime import _new_motion_task, _motion_task_id
    monkeypatch.setattr('lib.motion_video._env.motion_root',
                        lambda: str(tmp_path))
    # These engine tests assert phase/progress vocabulary (P-UX3), not
    # composition authoring. Force the zero-LLM template path so a poisoned
    # dispatcher/key_stats state from an earlier test in the same worker can
    # never turn the per-scene author's dispatch into an unbounded slot-wait
    # (the full-suite xdist hang).
    monkeypatch.setenv('TOFU_MOTION_SCENE_AUTHOR', '0')
    srt_path = tmp_path / 't.srt'
    srt_path.write_text(_SRT, encoding='utf-8')
    kw = dict(srt_path=str(srt_path), workdir=str(tmp_path / 'job'),
              voice='', speed=None, alignment='loose', narration=True,
              quality='draft', parallel=2, width=1080, height=1440)
    kw.update(over)
    return _new_motion_task(_motion_task_id(), **kw, user_id=TEST_OWNER_USER_ID)


def _fake_media(monkeypatch):
    import re as _re

    def fake_render(project_dir, output, **kw):
        with open(output, 'wb') as f:
            f.write(b'mp4')
        return {'ok': True, 'output': output, 'elapsed': 0.1}

    def fake_probe(path, **kw):
        idx = os.path.join(os.path.dirname(path), 'index.html')
        dur = 4.0
        if os.path.isfile(idx):
            m = _re.search(r'data-duration="([0-9.]+)"',
                           open(idx, encoding='utf-8').read())
            if m:
                dur = float(m.group(1))
        return {'codec': 'h264', 'width': 1080, 'height': 1440, 'fps': 30.0,
                'duration': dur, 'has_audio': False}

    def fake_concat(inputs, output, **kw):
        with open(output, 'wb') as f:
            f.write(b'mp4')
        return {'ok': True, 'output': output, 'duration': 8.0, 'mode': 'copy'}

    def fake_mux(video, audio, output, **kw):
        with open(output, 'wb') as f:
            f.write(b'mp4')
        return {'ok': True, 'output': output, 'duration': 8.0}

    monkeypatch.setattr('lib.motion_video.render_project', fake_render)
    monkeypatch.setattr('lib.motion_video.probe_video', fake_probe)
    monkeypatch.setattr('lib.motion_video.concat_mp4s', fake_concat)
    monkeypatch.setattr('lib.motion_video.mux_audio_video', fake_mux)


def _fake_tts(monkeypatch):
    import lib.tts as T
    import lib.model_routing as routing
    from types import SimpleNamespace

    monkeypatch.setattr(T, 'tts_available', lambda **_kwargs: True)
    monkeypatch.setattr(T, 'max_input_chars', lambda: 4000)
    monkeypatch.setattr(T, 'synthesize',
                        lambda text, *, voice, fmt, speed:
                        type('R', (), {'audio_bytes': _tiny_wav()})())
    monkeypatch.setattr(T, 'wav_duration', lambda wav: 0.1)
    monkeypatch.setattr(T, 'wav_params', lambda wav: (1, 2, 8000, 800))
    group = SimpleNamespace(pin_id='test-motion-tts')
    monkeypatch.setattr(
        routing, 'mint_capability_slot_group',
        lambda *_args, **_kwargs: ('unit-tts', group))
    monkeypatch.setattr(
        routing, 'dispose_routed_slot_group', lambda _group: True)


def test_engine_phase_vocabulary_and_progress(monkeypatch, tmp_path):
    """phase_started covers the full plan in order; narrate + compose emit
    per-scene progress counts (P-UX3)."""
    from lib.motion_video.engine import run_motion_task
    _fake_media(monkeypatch)
    _fake_tts(monkeypatch)
    task = _engine_task(tmp_path, monkeypatch)
    run_motion_task(task)

    assert task['status'] == 'done', task.get('error')
    started = [e for e in task['events'] if e['type'] == 'phase_started']
    names = [e['phase'] for e in started]
    assert names == ['parse', 'storyboard', 'narrate', 'compose', 'render',
                     'concat', 'mux']
    assert [e['phase_index'] for e in started] == list(range(1, len(names) + 1))
    assert all(e['phase_total'] == len(names) for e in started)

    narrate_prog = [e for e in task['events']
                    if e['type'] == 'progress' and e.get('phase') == 'narrate']
    n_scenes = task['result']['scenes']
    assert [e['done'] for e in narrate_prog] == list(range(1, n_scenes + 1))
    assert all(e['total'] == n_scenes and e['unit'] == 'scene'
               for e in narrate_prog)
    compose_prog = [e for e in task['events']
                    if e['type'] == 'progress' and e.get('phase') == 'compose']
    assert [e['done'] for e in compose_prog] == list(range(1, n_scenes + 1))


def test_narrate_progress_AB_callback_loadbearing(monkeypatch, tmp_path):
    """A/B: a narration fake that IGNORES on_scene_done produces ZERO
    narrate progress events — the callback wiring is what feeds P-UX3."""
    from lib.motion_video.engine import run_motion_task
    _fake_media(monkeypatch)

    def _fake_narration_no_progress(scenes, out_dir, **kw):
        """Settles every scene but NEVER calls on_scene_done."""
        import lib.tts as T
        os.makedirs(out_dir, exist_ok=True)
        out = []
        for sc in scenes:
            wav = os.path.join(out_dir, f"{sc['id']}.wav")
            with open(wav, 'wb') as f:
                f.write(T.silence_wav_bytes(0.1))
            out.append({'scene_id': sc['id'], 'wav': wav, 'text_chars': 1,
                        'audio_duration': 2.0, 'srt_duration': 2.0,
                        'target_duration': 2.0, 'overflow': 0.0})
        return {'ok': True, 'degraded': False, 'alignment': 'loose',
                'overflow_total': 0.0, 'scenes': out}

    monkeypatch.setattr('lib.motion_video.synthesize_scene_narrations',
                        _fake_narration_no_progress)
    task = _engine_task(tmp_path, monkeypatch)
    run_motion_task(task)
    assert task['status'] == 'done', task.get('error')
    assert not [e for e in task['events']
                if e['type'] == 'progress' and e.get('phase') == 'narrate']


def test_narrate_on_scene_done_counts(monkeypatch, tmp_path):
    """Direct: the real synthesize settles every scene exactly once, with a
    strictly increasing 1..N counter (parallel TTS completes out of order, so
    scene arrival order is NOT part of the contract)."""
    from lib.motion_video._audio import synthesize_scene_narrations
    _fake_tts(monkeypatch)
    calls = []
    scenes = [{'id': f'scene-{i:03d}', 'start': (i - 1) * 2.0, 'end': i * 2.0,
               'text': f'第{i}句。'} for i in (1, 2, 3)]
    res = synthesize_scene_narrations(
        scenes, str(tmp_path / 'audio'),
        on_scene_done=lambda i, n, sid: calls.append((i, n, sid)))
    assert res['ok'] is True
    assert [c[0] for c in calls] == [1, 2, 3]
    assert [c[1] for c in calls] == [3, 3, 3]
    assert sorted(c[2] for c in calls) == ['scene-001', 'scene-002',
                                           'scene-003']


def test_narrate_on_scene_done_sequential_order(monkeypatch, tmp_path):
    """Sequential rendering (max_workers=1) settles scenes in scene order."""
    from lib.motion_video._audio import synthesize_scene_narrations
    _fake_tts(monkeypatch)
    calls = []
    scenes = [{'id': f'scene-{i:03d}', 'start': (i - 1) * 2.0, 'end': i * 2.0,
               'text': f'第{i}句。'} for i in (1, 2, 3)]
    res = synthesize_scene_narrations(
        scenes, str(tmp_path / 'audio'), max_workers=1,
        on_scene_done=lambda i, n, sid: calls.append((i, n, sid)))
    assert res['ok'] is True
    assert calls == [(1, 3, 'scene-001'), (2, 3, 'scene-002'),
                     (3, 3, 'scene-003')]


# ══════════════════════════════════════════════════════════
#  §2.1 — video-abstract dedup + force
# ══════════════════════════════════════════════════════════

@pytest.fixture()
def video_env(tmp_path, monkeypatch):
    """Report gate + source + spawn seams faked; task stays pending so a
    second start can join it."""
    monkeypatch.setattr(
        'lib.paper.podcast_engine.worker.has_report',
        lambda ph, *, user_id: user_id == TEST_OWNER_USER_ID)
    monkeypatch.setattr('lib.paper.podcast_engine.worker.load_source_text',
                        lambda ph, lang, *, user_id: (
                            '正文段落。' * 100, 'report_zh'))
    monkeypatch.setattr('lib.motion_video._env.motion_root',
                        lambda: str(tmp_path))
    from lib.motion_video.runtime import _motion_runtime
    monkeypatch.setattr(_motion_runtime, 'spawn', lambda *a, **kw: None)
    return tmp_path


def test_video_abstract_dedup_and_force(video_env, phash, monkeypatch):
    from lib.paper.video_abstract import start_video_abstract

    r1 = start_video_abstract(phash, lang='zh', user_id=TEST_OWNER_USER_ID)
    assert r1['ok'] and not r1.get('deduped')
    r2 = start_video_abstract(phash, lang='zh', user_id=TEST_OWNER_USER_ID)
    assert r2.get('deduped') is True and r2['task_id'] == r1['task_id']
    r3 = start_video_abstract(phash, lang='zh', force=True, user_id=TEST_OWNER_USER_ID)
    assert r3['ok'] and not r3.get('deduped') and r3['task_id'] != r1['task_id']


def test_video_abstract_dedup_NEUTER_register_loadbearing(video_env, phash,
                                                          monkeypatch):
    """NEUTER: amputate claim registration → the second start spawns a NEW
    task instead of joining — the atomic claim's register is load-bearing."""
    import lib.paper.video_abstract as VA
    from lib.motion_video import runtime as motion_runtime
    monkeypatch.setattr(motion_runtime._production, 'index_register',
                        lambda *a, **kw: None)
    r1 = VA.start_video_abstract(phash, lang='zh', user_id=TEST_OWNER_USER_ID)
    r2 = VA.start_video_abstract(phash, lang='zh', user_id=TEST_OWNER_USER_ID)
    assert not r2.get('deduped') and r2['task_id'] != r1['task_id']


# ══════════════════════════════════════════════════════════
#  P-UX4 — video disk fallback (unit fragment)
# ══════════════════════════════════════════════════════════

def _write_disk_job(tmp_path, monkeypatch, phash, *, state='done',
                    with_final=True, user_id=TEST_OWNER_USER_ID):
    monkeypatch.setattr('lib.motion_video._env.motion_root',
                        lambda: str(tmp_path))
    tid = f'motion_{uuid.uuid4().hex[:16]}'
    workdir = tmp_path / 'jobs' / tid
    workdir.mkdir(parents=True)
    manifest = {'task_id': tid, 'kind': 'scenes', 'state': state,
                'paper_hash': phash, 'narration': True,
                'user_id': user_id}
    (workdir / 'job.json').write_text(json.dumps(manifest), encoding='utf-8')
    if with_final:
        (workdir / 'final.mp4').write_bytes(b'mp4-bytes')
    return tid, workdir


def test_video_disk_lookup_done_and_interrupted(tmp_path, monkeypatch, phash):
    from routes.paper_pkg._podcast import _lookup_paper_video_on_disk
    monkeypatch.setattr('lib.motion_video.probe_video',
                        lambda p, **kw: {'duration': 5.0})

    tid, _ = _write_disk_job(tmp_path, monkeypatch, phash, state='done')
    frag = _lookup_paper_video_on_disk(
        phash, user_id=TEST_OWNER_USER_ID)
    assert frag and frag['found'] and frag['status'] == 'done'
    assert frag['task_id'] == tid
    assert frag['result']['duration'] == 5.0
    assert frag['result']['narrated'] is True

    phash2 = uuid.uuid4().hex[:32]
    tid2, _ = _write_disk_job(tmp_path, monkeypatch, phash2, state='running')
    frag2 = _lookup_paper_video_on_disk(
        phash2, user_id=TEST_OWNER_USER_ID)
    assert frag2 and frag2.get('interrupted') is True and frag2['task_id'] == tid2

    assert _lookup_paper_video_on_disk(
        uuid.uuid4().hex[:32], user_id=TEST_OWNER_USER_ID) is None


def test_video_disk_lookup_never_crosses_owner(tmp_path, monkeypatch, phash):
    from routes.paper_pkg._podcast import _lookup_paper_video_on_disk

    _write_disk_job(
        tmp_path, monkeypatch, phash, state='done', user_id=27)

    assert _lookup_paper_video_on_disk(
        phash, user_id=TEST_OWNER_USER_ID) is None
    assert _lookup_paper_video_on_disk(phash, user_id=27) is not None


def test_video_disk_lookup_done_missing_file_is_honest(tmp_path, monkeypatch,
                                                       phash):
    """A done manifest whose final.mp4 vanished → NOT reported as done."""
    from routes.paper_pkg._podcast import _lookup_paper_video_on_disk
    _write_disk_job(tmp_path, monkeypatch, phash, state='done',
                    with_final=False)
    assert _lookup_paper_video_on_disk(
        phash, user_id=TEST_OWNER_USER_ID) is None


# ══════════════════════════════════════════════════════════
#  HTTP layer (real app)
# ══════════════════════════════════════════════════════════

def test_podcast_lookup_surfaces_interrupted(flask_client, podcast_env, phash):
    import lib.paper.podcast_engine.worker as PE
    PE.persist_podcast_row(phash, 'short', 'zh', 'alloy',
                            status='interrupted', script={}, meta={},
                            user_id=TEST_OWNER_USER_ID)
    r = flask_client.post('/api/v1/paper/podcast/lookup',
                          json={'paper_hash': phash, 'mode': 'short',
                                'lang': 'zh', 'voice': 'alloy'})
    body = r.get_json()
    assert body['ok'] and body['found'] is True
    assert body.get('interrupted') is True


def test_podcast_poll_reaps_stalled_task(
        flask_client, podcast_env, phash, monkeypatch):
    """The podcast poll route rides the shared TaskRuntime stall reaper."""
    from lib.paper.podcast_runtime import (
        _new_podcast_task, _podcast_runtime)
    tid = f'podcast_stall_{uuid.uuid4().hex[:8]}'
    _new_podcast_task(
        tid, phash, 'short', 'zh', 'alloy', None,
        user_id=TEST_OWNER_USER_ID)
    _podcast_runtime.mark_running(tid)
    real_now = time.time()
    monkeypatch.setattr(
        'lib.agent_core.task_runtime.time.time',
        lambda: real_now + 9999,
    )
    try:
        r = flask_client.get(f'/api/v1/paper/podcast/poll?task_id={tid}&cursor=0')
        body = r.get_json()
        assert r.status_code == 200
        assert body['done'] is True and body['status'] == 'error'
        assert body['error']['kind'] == 'worker_lost', body['error']
    finally:
        _podcast_runtime.remove_owned(tid, user_id=TEST_OWNER_USER_ID)


def test_video_lookup_and_file_disk_fallback(flask_client, tmp_path,
                                             monkeypatch, phash, storage_env):
    """Restart scenario: task gone from memory; lookup finds the disk job,
    the mp4 + scenes still serve (P-UX4 验收:成品重启后可播放)."""
    _insert_report(phash)
    tid, workdir = _write_disk_job(tmp_path, monkeypatch, phash, state='done')
    (workdir / 'scenes.json').write_text(json.dumps(
        [{'id': 'scene-001', 'start': 0.0, 'end': 4.0, 'text': '第一句'}]),
        encoding='utf-8')
    scene_dir = workdir / 'scenes' / 'scene-001'
    scene_dir.mkdir(parents=True)
    (scene_dir / 'scene-001.mp4').write_bytes(b'scene-bytes')

    r = flask_client.get(f'/api/v1/paper/video/lookup?paper_hash={phash}')
    body = r.get_json()
    assert body['ok'] and body['found'] is True
    assert body['task_id'] == tid and body.get('running') is False
    assert body['result']['final_path'].endswith('final.mp4')

    rf = flask_client.get(f'/api/v1/motion/videos/{tid}/file')
    assert rf.status_code == 200 and rf.data == b'mp4-bytes'

    rs = flask_client.get(f'/api/v1/motion/videos/{tid}/scenes')
    sbody = rs.get_json()
    assert rs.status_code == 200 and sbody['scenes'][0]['has_video'] is True

    rscene = flask_client.get(
        f'/api/v1/motion/videos/{tid}/scenes/scene-001/file')
    assert rscene.status_code == 200 and rscene.data == b'scene-bytes'


def test_video_lookup_disk_interrupted(flask_client, tmp_path, monkeypatch,
                                       phash, storage_env):
    _insert_report(phash)
    tid, _ = _write_disk_job(tmp_path, monkeypatch, phash, state='running')
    r = flask_client.get(f'/api/v1/paper/video/lookup?paper_hash={phash}')
    body = r.get_json()
    assert body['ok'] and body['found'] is True
    assert body.get('interrupted') is True and body['task_id'] == tid


def test_video_start_route_dedup_and_force(
        flask_client, video_env, phash, storage_env):
    _insert_report(phash)
    payload = {'paper_hash': phash, 'lang': 'zh', 'narration': False}
    r1 = flask_client.post('/api/v1/paper/video/start', json=payload).get_json()
    r2 = flask_client.post('/api/v1/paper/video/start', json=payload).get_json()
    assert r1['ok'] and r1.get('deduped') is not True
    assert r2.get('deduped') is True and r2['task_id'] == r1['task_id']
    r3 = flask_client.post('/api/v1/paper/video/start',
                           json={**payload, 'force': True}).get_json()
    assert r3['ok'] and r3['task_id'] != r1['task_id']


if __name__ == '__main__':
    import subprocess
    sys.exit(subprocess.call([
        sys.executable, '-m', 'pytest', __file__, '-q',
        '-p', 'no:cacheprovider']))
