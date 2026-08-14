#!/usr/bin/env python3
"""E2E tests for the paper-podcast API layer (Layer 3).

Drives the REAL app (flask_client fixture — per-session isolated SQLite) and
the REAL worker, with only three seams stubbed: the script LLM call
(podcast_engine.generate_script), the TTS provider POST (lib.tts._post_speech)
and the ffmpeg transcode. Everything else runs for real: report gate, dedup
index, worker thread, event log, paper_podcasts upsert, atomic file write,
Range streaming, cache hit, script_only degrade.

Isolation: the flask_client DB is per-SESSION, so every test gets a UNIQUE
paper hash (uuid) — a stale report/podcast row from a sibling test can never
leak into another test's cache keys.

NEUTERs:
  * report gate — amputating has_report flips start from report_required to
    proceeding (the gate is what enforces report-first UX);
  * degrade-vs-error contrast — tts_available=True with zero slots must
    produce a tts_unavailable ERROR, NOT a script_only row (proves the
    script_only outcome comes from the degrade branch, not the error path).

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_paper_podcast_api.py
"""

import io
import os
import sys
import time
import uuid
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = pytest.mark.unit

SCRIPT = {
    'title': '测试播客', 'lang': 'zh', 'mode': 'short',
    'segments': [
        {'id': 0, 'section': 'cold_open', 'speaker': 'host',
         'text': '这篇论文把成绩推到了 86.3,比上一代高了 3.2 个百分点。' * 6,
         'est_seconds': 60.0, 'figure_ref': None},
        {'id': 1, 'section': 'method', 'speaker': 'host',
         'text': '方法的核心是一个稀疏路由,每个词只和少数键做交互。' * 6,
         'est_seconds': 60.0, 'figure_ref': None},
        {'id': 2, 'section': 'recap', 'speaker': 'host',
         'text': '三条带走:第一,路由稀疏;第二,成绩 86.3;第三,好复现。' * 6,
         'est_seconds': 60.0, 'figure_ref': None},
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
    """A unique 32-hex paper hash per test (session-DB isolation)."""
    return uuid.uuid4().hex[:32]


@pytest.fixture()
def podcast_env(tmp_path, monkeypatch):
    """Redirect the paper dir to tmp; stub script LLM + TTS provider seams."""
    import lib.paper.hashing as hashing
    import lib.paper.podcast_engine as PE
    import lib.paper.podcast_engine._audio as PA
    import lib.tts as T
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test

    runtime = StorageRuntime(StorageSupervisor(
        project_root=tmp_path / 'storage', backend='sqlite'),
        auto_restart=False)
    install_runtime_for_test(runtime)
    runtime.start()

    monkeypatch.setattr(hashing, 'PAPER_DIR', str(tmp_path))
    (tmp_path / 'podcast').mkdir(exist_ok=True)

    monkeypatch.setattr(PE, 'generate_script',
                        lambda **kw: (dict(SCRIPT), dict(META)))
    monkeypatch.setattr('lib.paper._load_image_manifest', lambda ph: [])
    monkeypatch.setattr('lib.paper._lookup_paper_title', lambda ph: '测试论文')

    monkeypatch.setattr(T, '_tts_slots', lambda: [_FakeSlot()])
    monkeypatch.setattr(T, '_post_speech',
                        lambda slot, text, *, voice, fmt, speed: _tiny_wav())
    monkeypatch.setattr(PA, '_transcode_to_mp3', lambda wav: None)  # WAV path
    try:
        yield tmp_path
    finally:
        install_runtime_for_test(None)


def _insert_report(phash, lang='zh'):
    from lib.storage import get_storage_client
    get_storage_client(write=True).command('paper.report.upsert', {
        'paper_hash': phash, 'lang': lang,
        'report': '报告:成绩 86.3,上一代 83.1,规模 130 亿参数。',
        'model': 'm', 'meta': {}, 'created_at': int(time.time()),
    }, f'paper.podcast.report:{uuid.uuid4().hex}')


def _wait_done(client, task_id, timeout=15.0):
    deadline = time.time() + timeout
    cursor = 0
    while time.time() < deadline:
        r = client.get(f'/api/v1/paper/podcast/poll?task_id={task_id}&cursor={cursor}')
        assert r.status_code == 200
        body = r.get_json()
        cursor = body['cursor']
        if body['done']:
            return body
        time.sleep(0.05)
    raise AssertionError(f'task {task_id} did not finish within {timeout}s')


# ═══ Worker + persistence (driven directly) ═══

def test_worker_full_chain(podcast_env, phash):
    """Direct worker drive: events, DB row, atomic file, WAV master."""
    import lib.paper.podcast_engine as PE
    from lib.paper.podcast_runtime import _new_podcast_task

    _insert_report(phash)
    task = _new_podcast_task('podcast_test01', phash, 'short', 'zh', 'alloy', None)
    PE._run_podcast_task(task)

    assert task['status'] == 'done', [e for e in task['events']]
    types = [e['type'] for e in task['events']]
    assert 'phase' in types and 'script' in types and 'audio_ready' in types
    assert types[-1] == 'done'
    seg_events = [e for e in task['events'] if e['type'] == 'segment_done']
    assert len(seg_events) == len(SCRIPT['segments'])

    row = PE.load_cached_podcast(phash, 'short', 'zh', 'alloy')
    assert row and row['status'] == 'done'
    assert row['script_json']['segments'][0]['section'] == 'cold_open'
    assert row['tts_model'] == 'unit-tts'
    assert row['duration_sec'] > 0
    fpath = row['file_path']
    assert os.path.exists(fpath) and fpath.endswith('.wav')
    import lib.tts as T
    assert abs(T.wav_duration(open(fpath, 'rb').read()) - row['duration_sec']) < 0.05
    # atomic write: no tmp residue
    outdir = os.path.dirname(fpath)
    assert not [f for f in os.listdir(outdir) if '.tmp.' in f]


def test_worker_script_only_degrade(podcast_env, phash, monkeypatch):
    """No TTS slot configured → script_only row + honest reason (owner rule)."""
    import lib.paper.podcast_engine as PE
    import lib.tts as T
    from lib.paper.podcast_runtime import _new_podcast_task

    monkeypatch.setattr(T, '_tts_slots', lambda: [])
    _insert_report(phash)
    task = _new_podcast_task('podcast_test02', phash, 'short', 'zh', 'alloy', None)
    PE._run_podcast_task(task)

    assert task['status'] == 'done'
    done = [e for e in task['events'] if e['type'] == 'done'][-1]
    assert done['scriptOnly'] is True and done['reason'] == 'no_tts_slot'
    row = PE.load_cached_podcast(phash, 'short', 'zh', 'alloy')
    assert row and row['status'] == 'script_only'
    assert row['meta']['degrade_reason'] == 'no_tts_slot'
    assert not row['file_path']


def test_worker_degrade_vs_error_contrast(podcast_env, phash, monkeypatch):
    """NEUTER-contrast: tts_available=True but ZERO slots → synthesize raises
    503 → the task must go ERROR(tts_unavailable), NOT script_only. Proves
    script_only comes from the degrade branch, not from any TTS failure."""
    import lib.paper.podcast_engine as PE
    import lib.tts as T
    from lib.paper.podcast_runtime import _new_podcast_task

    monkeypatch.setattr(T, 'tts_available', lambda: True)   # gate says yes…
    monkeypatch.setattr(T, '_tts_slots', lambda: [])        # …but no slot
    _insert_report(phash)
    task = _new_podcast_task('podcast_test03', phash, 'short', 'zh', 'alloy', None)
    PE._run_podcast_task(task)

    assert task['status'] == 'error'
    err = [e for e in task['events'] if e['type'] == 'error'][-1]
    assert err['reason'] == 'tts_unavailable', err
    assert PE.load_cached_podcast(phash, 'short', 'zh', 'alloy') is None


def test_worker_abort(podcast_env, phash, monkeypatch):
    import lib.paper.podcast_engine as PE
    from lib.paper.podcast_runtime import _new_podcast_task

    _insert_report(phash)
    task = _new_podcast_task('podcast_test04', phash, 'short', 'zh', 'alloy', None)

    def _script_then_abort(**kw):
        task['abort_event'].set()
        return dict(SCRIPT), dict(META)

    monkeypatch.setattr(PE, 'generate_script', _script_then_abort)
    PE._run_podcast_task(task)
    assert task['status'] == 'aborted'
    assert [e for e in task['events'] if e['type'] == 'aborted']


def test_worker_source_gate(podcast_env, phash):
    """No report/translation/parsed text → error with report_required reason."""
    import lib.paper.podcast_engine as PE
    from lib.paper.podcast_runtime import _new_podcast_task

    task = _new_podcast_task('podcast_test05', phash, 'short', 'zh', 'alloy', None)
    PE._run_podcast_task(task)
    assert task['status'] == 'error'
    err = [e for e in task['events'] if e['type'] == 'error'][-1]
    assert err['reason'] == 'report_required'


# ═══ HTTP layer (real app + real worker thread) ═══

def test_start_requires_report(flask_client, podcast_env, phash):
    r = flask_client.post('/api/v1/paper/podcast/start',
                          json={'paper_hash': phash, 'mode': 'short', 'lang': 'zh'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] is False and body['report_required'] is True


def test_start_gate_NEUTER(flask_client, podcast_env, phash, monkeypatch):
    """NEUTER: amputate has_report → the SAME no-report start proceeds —
    the gate is load-bearing, not incidental."""
    monkeypatch.setattr('routes.paper.has_report', lambda ph: True)
    r = flask_client.post('/api/v1/paper/podcast/start',
                          json={'paper_hash': phash, 'mode': 'short', 'lang': 'zh'})
    body = r.get_json()
    assert body.get('ok') is True and body.get('task_id'), body


def test_start_poll_done_and_cache_hit(flask_client, podcast_env, phash):
    _insert_report(phash)
    r = flask_client.post('/api/v1/paper/podcast/start',
                          json={'paper_hash': phash, 'mode': 'short', 'lang': 'zh'})
    body = r.get_json()
    assert body['ok'] and body['task_id']
    final = _wait_done(flask_client, body['task_id'])
    assert final['status'] == 'done'
    assert final['scriptOnly'] is False
    assert final['audioUrl'].startswith('/api/v1/paper/podcast/audio/')
    assert final['durationSec'] > 0
    assert final['script']['segments'][0]['section'] == 'cold_open'

    # second start for the same key → cache hit, NO new task
    r2 = flask_client.post('/api/v1/paper/podcast/start',
                           json={'paper_hash': phash, 'mode': 'short', 'lang': 'zh'})
    body2 = r2.get_json()
    assert body2.get('cached') is True and 'task_id' not in body2
    assert body2['audioUrl'] == final['audioUrl']


def test_lookup(flask_client, podcast_env, phash):
    _insert_report(phash)
    r = flask_client.post('/api/v1/paper/podcast/lookup',
                          json={'paper_hash': phash, 'mode': 'short', 'lang': 'zh'})
    body = r.get_json()
    assert body['ok'] and body['found'] is False
    assert body['report_available'] is True

    r = flask_client.post('/api/v1/paper/podcast/start',
                          json={'paper_hash': phash, 'mode': 'short', 'lang': 'zh'})
    task_id = r.get_json()['task_id']
    _wait_done(flask_client, task_id)
    r = flask_client.post('/api/v1/paper/podcast/lookup',
                          json={'paper_hash': phash, 'mode': 'short', 'lang': 'zh'})
    body = r.get_json()
    assert body['found'] is True and body.get('cached') is True


def test_cache_identity_includes_model(flask_client, podcast_env, phash):
    """Cache-key-skew guard: a cached podcast made with model A must NOT be
    served for a request naming model B (the row is ONE slot per
    (hash, mode, lang, voice) — a model mismatch regenerates + overwrites).
    The same model as the row IS a cache hit, and the lookup surfaces the
    making-model for the panel's badge."""
    from lib import tts as _tts
    eff_voice = _tts.default_voice()   # resolved exactly like the route does
    _insert_report(phash)
    r = flask_client.post('/api/v1/paper/podcast/start',
                          json={'paper_hash': phash, 'mode': 'short',
                                'lang': 'zh', 'model': 'm-alpha'})
    body = r.get_json()
    assert body['ok'] and body['task_id']
    final = _wait_done(flask_client, body['task_id'])
    assert final['status'] == 'done'
    assert final.get('model') == 'm-alpha'

    # same key incl. model → cache hit, NO new task
    r2 = flask_client.post('/api/v1/paper/podcast/start',
                           json={'paper_hash': phash, 'mode': 'short',
                                 'lang': 'zh', 'model': 'm-alpha'})
    body2 = r2.get_json()
    assert body2.get('cached') is True and 'task_id' not in body2
    assert body2.get('model') == 'm-alpha'

    # a DIFFERENT model → a NEW task; the old audio is not served as if it
    # were model-B's work
    r3 = flask_client.post('/api/v1/paper/podcast/start',
                           json={'paper_hash': phash, 'mode': 'short',
                                 'lang': 'zh', 'model': 'm-beta'})
    body3 = r3.get_json()
    assert body3.get('ok') and body3.get('task_id') and not body3.get('cached')
    final3 = _wait_done(flask_client, body3['task_id'])
    assert final3['status'] == 'done'

    # the one-slot row now belongs to m-beta: m-beta hits, and the lookup
    # carries the making-model
    import lib.paper.podcast_engine as PE
    row = PE.load_cached_podcast(phash, 'short', 'zh', eff_voice)
    assert row and (row.get('model') or '') == 'm-beta'
    r4 = flask_client.post('/api/v1/paper/podcast/start',
                           json={'paper_hash': phash, 'mode': 'short',
                                 'lang': 'zh', 'model': 'm-beta'})
    assert r4.get_json().get('cached') is True
    r5 = flask_client.post('/api/v1/paper/podcast/lookup',
                           json={'paper_hash': phash, 'mode': 'short',
                                 'lang': 'zh'})
    body5 = r5.get_json()
    assert body5.get('found') and body5.get('cached')
    assert body5.get('model') == 'm-beta'


def test_lookup_reattach_ignores_model_and_voice(flask_client, podcast_env,
                                                 phash, monkeypatch):
    """Re-attach regression: the panel's lookup sends only (paper_hash, mode,
    lang) — never the model/voice the RUNNING task was started with (that is
    what re-attach is supposed to DISCOVER). An exact dedup-key miss must
    fall back to the newest live task for (paper_hash, mode, lang); without
    it a podcast started with any concrete model is invisible to the lookup
    and the tab regresses to the idle/generate card mid-run."""
    import lib.paper.podcast_engine as PE

    _insert_report(phash)
    gate = {}

    def _slow_script(**kw):
        gate['ready'] = True
        deadline = time.time() + 10
        while not gate.get('released') and time.time() < deadline:
            time.sleep(0.02)
        return dict(SCRIPT), dict(META)

    monkeypatch.setattr(PE, 'generate_script', _slow_script)
    r = flask_client.post('/api/v1/paper/podcast/start',
                          json={'paper_hash': phash, 'mode': 'short',
                                'lang': 'zh', 'model': 'm-alpha',
                                'voice': 'alloy'})
    body = r.get_json()
    assert body['ok'] and body['task_id']
    deadline = time.time() + 5
    while not gate.get('ready') and time.time() < deadline:
        time.sleep(0.02)
    try:
        # The panel's real lookup shape: no model, no voice.
        r2 = flask_client.post('/api/v1/paper/podcast/lookup',
                               json={'paper_hash': phash, 'mode': 'short',
                                     'lang': 'zh'})
        body2 = r2.get_json()
        assert body2.get('found') is True and body2.get('running') is True
        assert body2.get('task_id') == body['task_id']
        assert body2.get('model') == 'm-alpha'
        assert body2.get('createdAt')
    finally:
        gate['released'] = True


def test_dedup_index_separates_models():
    """A request for model B must never JOIN an in-flight task made with
    model A — the join would silently hand back A's audio."""
    import uuid
    from lib.paper.podcast_runtime import (
        _new_podcast_task,
        _podcast_index_get,
        _podcast_index_register,
    )
    tid = 'podcast_idx_' + uuid.uuid4().hex[:8]
    task = _new_podcast_task(tid, 'ph_dedup', 'short', 'zh', 'alloy', 'm-alpha')
    _podcast_index_register('ph_dedup', 'short', 'zh', 'alloy', 'm-alpha',
                            task['task_id'])
    assert _podcast_index_get('ph_dedup', 'short', 'zh', 'alloy',
                              'm-alpha') == task['task_id']
    assert _podcast_index_get('ph_dedup', 'short', 'zh', 'alloy',
                              'm-beta') is None


def test_audio_range(flask_client, podcast_env, phash):
    _insert_report(phash)
    r = flask_client.post('/api/v1/paper/podcast/start',
                          json={'paper_hash': phash, 'mode': 'short', 'lang': 'zh'})
    final = _wait_done(flask_client, r.get_json()['task_id'])
    url = final['audioUrl']

    r_full = flask_client.get(url)
    assert r_full.status_code == 200
    assert r_full.headers['Content-Type'] == 'audio/wav'
    total = len(r_full.data)
    assert total > 100

    r_part = flask_client.get(url, headers={'Range': 'bytes=0-99'})
    assert r_part.status_code == 206
    assert len(r_part.data) == 100
    assert r_part.headers['Content-Range'] == f'bytes 0-99/{total}'
    # the tail range a phone player issues when seeking to the end
    r_tail = flask_client.get(url, headers={'Range': f'bytes={total-50}-'})
    assert r_tail.status_code == 206 and len(r_tail.data) == 50


def test_script_endpoint(flask_client, podcast_env, phash):
    _insert_report(phash)
    r = flask_client.post('/api/v1/paper/podcast/start',
                          json={'paper_hash': phash, 'mode': 'short', 'lang': 'zh'})
    _wait_done(flask_client, r.get_json()['task_id'])
    r = flask_client.get(f'/api/v1/paper/podcast/script?paper_hash={phash}'
                         '&mode=short&lang=zh')
    body = r.get_json()
    assert body['ok'] and body['script']['title'] == '测试播客'
    assert body['scriptOnly'] is False
    r404 = flask_client.get('/api/v1/paper/podcast/script?paper_hash='
                            + uuid.uuid4().hex[:32] + '&mode=short&lang=zh')
    assert r404.status_code == 404


def test_abort_endpoint(flask_client, podcast_env, phash, monkeypatch):
    import lib.paper.podcast_engine as PE

    _insert_report(phash)
    gate = {}

    def _slow_script(**kw):
        # hold the worker in the script phase until the abort lands
        gate['ready'] = True
        deadline = time.time() + 10
        while not gate.get('released') and time.time() < deadline:
            time.sleep(0.02)
        return dict(SCRIPT), dict(META)

    monkeypatch.setattr(PE, 'generate_script', _slow_script)
    r = flask_client.post('/api/v1/paper/podcast/start',
                          json={'paper_hash': phash, 'mode': 'short', 'lang': 'zh'})
    task_id = r.get_json()['task_id']
    deadline = time.time() + 5
    while not gate.get('ready') and time.time() < deadline:
        time.sleep(0.02)
    r = flask_client.post(f'/api/v1/paper/podcast/abort/{task_id}')
    assert r.status_code == 200
    gate['released'] = True
    final = _wait_done(flask_client, task_id)
    assert final['status'] == 'aborted'


def test_bad_mode_rejected(flask_client, podcast_env, phash):
    r = flask_client.post('/api/v1/paper/podcast/start',
                          json={'paper_hash': phash, 'mode': 'epic', 'lang': 'zh'})
    assert r.status_code == 400
    r = flask_client.post('/api/v1/paper/podcast/start',
                          json={'paper_hash': phash, 'mode': 'short', 'lang': 'fr'})
    assert r.status_code == 400
    r = flask_client.get('/api/v1/paper/podcast/audio/'
                         + 'zz' * 16 + '/short/zh/alloy')
    assert r.status_code in (400, 404)


def test_status_endpoint(flask_client, podcast_env):
    r = flask_client.get('/api/v1/paper/podcast/status')
    body = r.get_json()
    assert body['ok'] is True
    assert body['tts_available'] is True
    assert body['models'][0]['model'] == 'unit-tts'
    assert body['default_voice']
    assert set(body['modes']) == {'short', 'full'}
    assert body['modes']['short']['target'] == 300
