#!/usr/bin/env python3
"""tests/test_probe_nonchat_skip.py — access-matrix probe must NOT chat-probe
non-chat models (image_gen / embedding / transcription).

WHY
---
The matrix's "Probe & Recommend" sends a tiny ``/chat/completions`` request
(``max_tokens: 1``) to every (key × model-id) cell. For models with no chat
surface the gateway has NO chat binding to route to — the Meituan AIGC
gateway (a Java service) then picks a random upstream from an EMPTY candidate
list: ``Random.nextInt(0)`` → ``IllegalArgumentException: bound must be
positive`` → HTTP 500. Confirmed against the live persisted snapshot
(data/config/probe_cache): every ``bound must be positive`` 500 belonged to
an image/embedding model (gemini-3-pro-image-preview, gemini-2.5-flash-image,
gemini-3.1-flash-image-preview, text-embedding-v4) while every chat model got
a real verdict. The probe then flagged those cells ``unavailable`` with
``recommend_disable=True`` — one click on "Apply recommendations" would have
disabled WORKING image models.

THE FIX (all asserted here)
---------------------------
  * BACKEND — ``lib/provider_probe.run_cell_probe_task`` routes cells whose
    capabilities fail ``is_chat_model`` to a PER-MODALITY probe that
    exercises the same surface the app itself uses:
      image_gen      → POST /images/generations (openai_image slots) or
                       POST /chat/completions with modalities TEXT+IMAGE
                       (gemini-style); ONE attempt only (each attempt bills
                       a real image) and a 60s timeout floor;
      transcription  → multipart POST /audio/transcriptions with a 0.3s
                       silence WAV (no real speech leaves the box);
      embedding      → POST /embeddings with a one-word input.
    Only capabilities with no implemented surface keep the ``skipped``
    verdict (zero network, ``recommend_disable=False``).
  * ROUTE — ``routes/config.probe_provider_cells_start`` carries each model's
    ``capabilities`` into the work tuples so the backend can pick the probe.
  * FRONTEND — ``access_matrix.js`` sends capabilities, renders the
    ``skipped`` pip, reconciles STALE persisted snapshots (downgrades
    non-chat false positives so old disk snapshots heal without a retest),
    and refuses to apply a disable recommendation to a non-chat model.

Layers (A)-(D) are backend; (E)-(G) run the REAL shipped access_matrix.js
under node. (C)/(G) are NEUTER proofs that the guards are load-bearing.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = pytest.mark.unit

import lib.provider_probe as pp  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
ACCESS_MATRIX_JS = os.path.join(ROOT, 'static', 'js', 'settings', 'providers', 'access_matrix.js')
MODEL_CAPS_JS = os.path.join(ROOT, 'static', 'js', 'core', 'model_caps.js')

_NONCHAT_CAPS = (['image_gen'], ['embedding'], ['transcription'])


def _task() -> dict:
    return {
        'provider_id': 't-nonchat',
        'status': 'running',
        'started_at': 0, 'finished_at': None,
        'total': 0, 'done_count': 0,
        'cells': {}, 'summary': {'ok': 0, 'disable': 0},
        'error': None, 'attempts': 1, '_abort': False,
        '_base_url': 'https://gw.example.com/v1',
        '_extra_headers': {},
    }


class _ProbeRedirect(unittest.TestCase):
    """Redirect the probe cache into a temp dir for every test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = pp.probe_cache_path
        pp.probe_cache_path = lambda pid: os.path.join(self._tmp.name, pid + '.json')

    def tearDown(self):
        pp.probe_cache_path = self._orig
        self._tmp.cleanup()


class BackendSkipTest(_ProbeRedirect):

    def test_known_nonchat_caps_use_modality_probe(self):
        """image_gen / embedding / transcription cells must be driven through
        their OWN probe function — never the chat probe — with the image
        cell capped at ONE attempt (billed generation) and a 60s timeout
        floor; aliases inherit the root caps."""
        calls = []

        def fake_multi(base_url, api_key, model_id, extra_headers, timeout,
                       attempts=3, retry_delay=0.8, protocol='openai', probe_fn=None,
                       oauth=''):
            calls.append({'mid': model_id, 'attempts': attempts,
                          'timeout': timeout, 'probe_fn': probe_fn})
            return 'ok', 'HTTP 200'

        work = [
            (0, 'sk-a', 'chat-x', 'chat-x', ['text']),
            (0, 'sk-a', 'img-1', 'img-1', ['image_gen']),
            (0, 'sk-a', 'img-1', 'img-1-alias', ['image_gen']),  # alias inherits caps
            (0, 'sk-a', 'emb-1', 'emb-1', ['embedding']),
            (0, 'sk-a', 'asr-1', 'asr-1', ['transcription']),
        ]
        task = _task()
        task['total'] = len(work)
        task['attempts'] = 3
        with mock.patch.object(pp, 'probe_cell_multi', side_effect=fake_multi):
            pp.run_cell_probe_task(task, work, timeout=10)

        by_mid = {c['mid']: c for c in calls}
        self.assertIsNone(by_mid['chat-x']['probe_fn'],
                          'chat model must keep the default chat probe')
        self.assertEqual(by_mid['chat-x']['attempts'], 3)
        for mid in ('img-1', 'img-1-alias'):
            self.assertIs(by_mid[mid]['probe_fn'], pp.probe_image_cell, mid)
            self.assertEqual(by_mid[mid]['attempts'], 1,
                             mid + ': image probes bill one generation per '
                             'attempt — must be single-shot')
            self.assertEqual(by_mid[mid]['timeout'], pp._IMAGE_PROBE_MIN_TIMEOUT,
                             mid + ': generation outlives the chat timeout')
        self.assertIs(by_mid['emb-1']['probe_fn'], pp.probe_embedding_cell)
        self.assertEqual(by_mid['emb-1']['attempts'], 3,
                         'cheap embedding probes keep multi-attempt filtering')
        self.assertEqual(by_mid['emb-1']['timeout'], 10)
        self.assertIs(by_mid['asr-1']['probe_fn'], pp.probe_transcription_cell)
        self.assertEqual(by_mid['asr-1']['attempts'], 3)

        for mid in ('img-1', 'img-1-alias', 'emb-1', 'asr-1'):
            cell = task['cells'][pp.probe_cell_key(0, mid)]
            self.assertEqual(cell['status'], 'ok', mid)
        self.assertEqual(task['summary']['skipped'], 0)

        # Every cell is stamped with the surface that produced its verdict —
        # the frontend's fresh-vs-stale discrimination depends on it.
        self.assertEqual(
            {mid: task['cells'][pp.probe_cell_key(0, mid)]['probe_surface']
             for mid in ('chat-x', 'img-1', 'img-1-alias', 'emb-1', 'asr-1')},
            {'chat-x': 'chat', 'img-1': 'image', 'img-1-alias': 'image',
             'emb-1': 'embedding', 'asr-1': 'transcription'})

    def test_unknown_nonchat_caps_still_skipped(self):
        """A FUTURE non-chat capability with no implemented probe surface
        keeps the 'skipped' verdict with ZERO network.

        Today's taxonomy exclusion set is exactly {image_gen, embedding,
        transcription} and all three have probes, so the skipped branch is
        unreachable through real caps — this test simulates the taxonomy
        gaining 'hologram_gen' tomorrow (is_chat_model → False) before the
        probe engine grows a surface for it (nonchat_probe_fn → None), and
        pins the safe behaviour: skip, never chat-probe."""
        def fail_multi(*a, **k):
            raise AssertionError('network must not be touched for unknown caps')

        work = [(0, 'sk-a', 'holo-1', 'holo-1', ['hologram_gen'])]
        task = _task()
        task['total'] = 1
        with mock.patch.object(pp, '_is_chat_model', return_value=False), \
                mock.patch.object(pp, 'probe_cell_multi', side_effect=fail_multi):
            pp.run_cell_probe_task(task, work, timeout=5)

        cell = task['cells'][pp.probe_cell_key(0, 'holo-1')]
        self.assertEqual(cell['status'], 'skipped')
        self.assertEqual(cell['probe_surface'], 'none',
                         'skipped cells stamp none — never a fresh verdict')
        self.assertFalse(cell['recommend_disable'])
        self.assertIn('no probe surface', cell['detail'])
        self.assertEqual(task['summary'],
                         {'ok': 0, 'disable': 0, 'skipped': 1},
                         'skipped cells must NOT inflate the ok count')

    def test_chat_caps_and_legacy_tuples_still_probed(self):
        """caps=['text'], caps=['audio_chat'], caps=[] and legacy 4-tuples
        (no caps element at all) are all chat → probed as before."""
        probed = []

        def fake_multi(base_url, api_key, model_id, extra_headers, timeout,
                       attempts=3, retry_delay=0.8, protocol='openai', probe_fn=None,
                       oauth=''):
            probed.append((model_id, probe_fn))
            return 'ok', 'HTTP 200'

        work = [
            (0, 'sk-a', 'm-text', 'm-text', ['text']),
            (0, 'sk-a', 'm-audio', 'm-audio', ['audio_chat']),
            (0, 'sk-a', 'm-omni', 'm-omni', ['text', 'audio_chat', 'vision']),
            (0, 'sk-a', 'm-empty', 'm-empty', []),
            (0, 'sk-a', 'm-legacy', 'm-legacy'),               # 4-tuple, no caps
        ]
        task = _task()
        task['total'] = len(work)
        with mock.patch.object(pp, 'probe_cell_multi', side_effect=fake_multi):
            pp.run_cell_probe_task(task, work, timeout=5)

        self.assertEqual(sorted(m for m, _ in probed),
                         ['m-audio', 'm-empty', 'm-legacy', 'm-omni', 'm-text'])
        self.assertTrue(all(fn is None for _, fn in probed),
                        'chat cells must use the default chat probe')
        self.assertTrue(all(c['status'] == 'ok' for c in task['cells'].values()))
        self.assertTrue(all(c['probe_surface'] == 'chat'
                            for c in task['cells'].values()),
                        'chat-verdict cells stamp chat — the frontend treats '
                        'a chat stamp on a non-chat model as stale')
        self.assertEqual(task['summary']['skipped'], 0)

    def test_skipped_is_never_a_disable_verdict(self):
        """'skipped' must never enter the disable set — even a future edit to
        _PROBE_DISABLE_STATUSES must not sweep it in."""
        self.assertNotIn('skipped', pp._PROBE_DISABLE_STATUSES)


class _FakeResp:
    def __init__(self, code, payload=None, text=None):
        self.status_code = code
        self._payload = payload
        self.text = text if text is not None else (
            '' if payload is None else str(payload))

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


class ModalityProbeTest(unittest.TestCase):
    """Each modality probe must exercise the SAME surface the app itself
    uses, with a payload the gateway actually accepts."""

    def _post(self, capture, resp):
        def fake(url, **kw):
            capture.update(kw)
            capture['url'] = url
            return resp
        return mock.patch('lib.http_client.http_post', side_effect=fake)

    def test_embedding_probe_shape(self):
        cap = {}
        resp = _FakeResp(200, {'data': [{'embedding': [0.1, 0.2], 'index': 0}]})
        with self._post(cap, resp):
            status, detail = pp.probe_embedding_cell(
                'https://gw.example.com/v1', 'sk-a', 'emb-1', {'X-H': '1'}, 10)
        self.assertEqual((status,), ('ok',), detail)
        self.assertIn('/embeddings', detail)
        self.assertEqual(cap['url'], 'https://gw.example.com/v1/embeddings')
        self.assertEqual(cap['json'], {'model': 'emb-1', 'input': 'ping'})
        self.assertEqual(cap['headers']['Authorization'], 'Bearer sk-a')
        self.assertEqual(cap['headers']['X-H'], '1')

    def test_embedding_probe_rejects_bad_shape(self):
        cap = {}
        with self._post(cap, _FakeResp(200, {'data': []})):
            status, detail = pp.probe_embedding_cell(
                'https://gw.example.com/v1', 'sk-a', 'emb-1', {}, 10)
        self.assertEqual(status, 'error')
        self.assertIn('invalid shape', detail)

    def test_transcription_probe_sends_silence_wav(self):
        cap = {}
        with self._post(cap, _FakeResp(200, {'text': ''})):
            status, detail = pp.probe_transcription_cell(
                'https://gw.example.com/v1', 'sk-a', 'asr-1', {}, 10)
        self.assertEqual((status,), ('ok',), detail)
        self.assertIn('/audio/transcriptions', detail)
        self.assertEqual(cap['url'],
                         'https://gw.example.com/v1/audio/transcriptions')
        self.assertEqual(cap['data']['model'], 'asr-1')
        fname, stream, mime = cap['files']['file']
        self.assertEqual(mime, 'audio/wav')
        wav = stream.read()
        self.assertEqual(wav[:4], b'RIFF', 'multipart payload must be a real WAV')
        self.assertEqual(wav[8:12], b'WAVE')
        self.assertLess(len(wav), 20_000, 'silence clip must stay tiny')
        # Digital silence: every PCM sample is zero — no speech leaves the box.
        self.assertEqual(set(wav[44:]), {0})

    def test_image_probe_chat_surface_carries_modalities(self):
        """protocol='openai' mirrors the app's gemini-style image path."""
        cap = {}
        resp = _FakeResp(200, {'choices': [{'message': {'content': [
            {'type': 'image', 'b64_json': 'aGk='}]}}]})
        with self._post(cap, resp):
            status, detail = pp.probe_image_cell(
                'https://gw.example.com/v1', 'sk-a', 'img-1', {}, 10,
                protocol='openai')
        self.assertEqual((status,), ('ok',), detail)
        self.assertEqual(cap['url'],
                         'https://gw.example.com/v1/chat/completions')
        self.assertEqual(cap['json']['modalities'], ['TEXT', 'IMAGE'],
                         'the gateway routes to the image binding via '
                         'modalities — the field the failing probe omitted')
        self.assertFalse(cap['json'].get('stream'))

    def test_image_probe_images_api_surface(self):
        """protocol='openai_image' mirrors the OpenAI-native slot path."""
        cap = {}
        resp = _FakeResp(200, {'data': [{'b64_json': 'aGk='}]})
        with self._post(cap, resp):
            status, detail = pp.probe_image_cell(
                'https://gw.example.com/v1', 'sk-a', 'img-1', {}, 10,
                protocol='openai_image')
        self.assertEqual((status,), ('ok',), detail)
        self.assertEqual(cap['url'],
                         'https://gw.example.com/v1/images/generations')
        self.assertEqual(cap['json']['n'], 1)

    def test_image_probe_404_recommends_disable(self):
        """A model-not-found on the IMAGE surface is now a MEANINGFUL
        disable recommendation (unlike the old chat-probe 500)."""
        cap = {}
        resp = _FakeResp(404, None, text='model_not_found: img-1')
        with self._post(cap, resp):
            status, _ = pp.probe_image_cell(
                'https://gw.example.com/v1', 'sk-a', 'img-1', {}, 10)
        self.assertEqual(status, 'not_found')

    def test_nonchat_probe_fn_dispatch(self):
        self.assertIs(pp.nonchat_probe_fn(['image_gen']), pp.probe_image_cell)
        self.assertIs(pp.nonchat_probe_fn(['transcription']),
                      pp.probe_transcription_cell)
        self.assertIs(pp.nonchat_probe_fn(['embedding']), pp.probe_embedding_cell)
        self.assertIs(pp.nonchat_probe_fn(['image_gen', 'embedding']),
                      pp.probe_image_cell, 'image wins on multi-cap models')
        self.assertIsNone(pp.nonchat_probe_fn(['video_gen']))
        self.assertIsNone(pp.nonchat_probe_fn([]))
        self.assertIsNone(pp.nonchat_probe_fn(None))


class BackendNeuterCostGuardTest(_ProbeRedirect):
    """NEUTER: relax the single-attempt image guard in a COPY → the image
    cell gets attempts=3 → proves the cost guard is load-bearing."""

    def test_neuter_image_attempts_guard(self):
        with open(pp.__file__, encoding='utf-8') as f:
            src = f.read()
        anchor = '                cell_attempts = 1\n'
        self.assertIn(anchor, src, 'cost-guard anchor drifted — update the neuter')
        neutered = src.replace(anchor, '                cell_attempts = attempts\n', 1)
        self.assertNotEqual(neutered, src)

        tmp_mod = os.path.join(self._tmp.name, 'provider_probe_costguard_neutered.py')
        with open(tmp_mod, 'w', encoding='utf-8') as f:
            f.write(neutered)
        spec = importlib.util.spec_from_file_location(
            'provider_probe_costguard_neutered', tmp_mod)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.probe_cache_path = lambda pid: os.path.join(self._tmp.name, pid + '.json')

        calls = []

        def fake_multi(base_url, api_key, model_id, extra_headers, timeout,
                       attempts=3, retry_delay=0.8, protocol='openai', probe_fn=None,
                       oauth=''):
            calls.append(attempts)
            return 'ok', 'HTTP 200'

        task = _task()
        task['attempts'] = 3
        work = [(0, 'sk-a', 'img-1', 'img-1', ['image_gen'])]
        task['total'] = 1
        with mock.patch.object(mod, 'probe_cell_multi', side_effect=fake_multi):
            mod.run_cell_probe_task(task, work, timeout=5)

        self.assertEqual(calls, [3],
                         'NEUTER did not bite: without the guard the image '
                         'cell must fan out to 3 billed generations')

        with open(pp.__file__, encoding='utf-8') as f:
            self.assertEqual(f.read(), src, 'harness mutated the shipped provider_probe.py')


class BackendNeuterTest(_ProbeRedirect):
    """NEUTER: strip the non-chat guard from a COPY of provider_probe.py →
    the image model IS probed and flagged → proves the guard is load-bearing.
    The shipped module is left byte-identical."""

    def test_neuter_guard_restores_false_positive(self):
        with open(pp.__file__, encoding='utf-8') as f:
            src = f.read()
        anchor = 'if caps and not _is_chat_model(caps):'
        self.assertIn(anchor, src, 'guard anchor drifted — update the neuter')
        neutered = src.replace(anchor, 'if False and caps and not _is_chat_model(caps):', 1)
        self.assertNotEqual(neutered, src)

        tmp_mod = os.path.join(self._tmp.name, 'provider_probe_neutered.py')
        with open(tmp_mod, 'w', encoding='utf-8') as f:
            f.write(neutered)
        spec = importlib.util.spec_from_file_location('provider_probe_neutered', tmp_mod)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.probe_cache_path = lambda pid: os.path.join(self._tmp.name, pid + '.json')

        task = _task()
        work = [(0, 'sk-a', 'img-1', 'img-1', ['image_gen'])]
        task['total'] = 1
        with mock.patch.object(mod, 'probe_one_cell',
                               return_value=('unavailable', 'HTTP 500 bound must be positive')):
            mod.run_cell_probe_task(task, work, timeout=5)

        cell = task['cells'][mod.probe_cell_key(0, 'img-1')]
        self.assertEqual(cell['status'], 'unavailable',
                         'NEUTER did not bite: guard removed but cell still skipped')
        self.assertTrue(cell['recommend_disable'],
                        'NEUTER did not bite: without the guard the false positive '
                        'must again recommend disabling a working image model')

        with open(pp.__file__, encoding='utf-8') as f:
            self.assertEqual(f.read(), src, 'harness mutated the shipped provider_probe.py')


class RouteCapsFlowTest(unittest.TestCase):
    """``probe_provider_cells_start`` must carry model capabilities into the
    work tuples (otherwise the backend skip never sees them)."""

    def test_capabilities_reach_work_tuples(self):
        try:
            import routes.config as cfg
        except ImportError as e:
            pytest.skip(
                'routes.config unimportable under active sibling memory '
                'refactor (pt_229606ca deleted lib/memory/installer.py with a '
                'stale package facade) — auto-activates once the facade lands: %s' % e)
        from quart import Quart

        app = Quart('probe-caps-flow')
        app.register_blueprint(cfg.config_bp)

        captured = {}
        done = threading.Event()

        def stub(task, work, timeout):
            captured['work'] = list(work)
            done.set()

        body = {
            'provider_id': 't-caps-flow',
            'base_url': 'https://gw.example.com/v1',
            'api_keys': ['sk-a', 'sk-b'],
            'models': [
                {'model_id': 'chat-x', 'capabilities': ['text', 'thinking']},
                {'model_id': 'img-1', 'aliases': ['img-1-a'], 'capabilities': ['image_gen']},
            ],
            'attempts': 1,
            'force': True,
        }

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(cfg, '_run_cell_probe_task', side_effect=stub), \
                mock.patch.object(cfg, '_probe_cache_path',
                                  lambda pid: os.path.join(tmp.name, pid + '.json')):
            async def _post():
                async with app.test_client() as client:
                    return await client.post('/api/v1/providers/probe-cells/start', json=body)
            resp = asyncio.run(_post())
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(done.wait(5), 'probe thread stub never ran')

        work = captured['work']
        # 2 keys × (1 root + (root + 1 alias)) = 6 cells.
        self.assertEqual(len(work), 6)
        by_mid = {}
        for item in work:
            # 7-tuple since the per-cell face feature: (key_idx, api_key,
            # root, wire_id, caps, base_url, protocol).
            self.assertEqual(len(item), 7, 'work item must be a 7-tuple with caps + face')
            by_mid.setdefault(item[3], item[4])
        self.assertEqual(by_mid['chat-x'], ['text', 'thinking'])
        self.assertEqual(by_mid['img-1'], ['image_gen'])
        self.assertEqual(by_mid['img-1-a'], ['image_gen'],
                         'aliases must inherit the root model capabilities')

        with pp.CELL_PROBE_LOCK:
            pp.CELL_PROBE_TASKS.pop('t-caps-flow', None)


# ── Frontend: run the REAL access_matrix.js under node ──────────────────

def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.document = { querySelector: function () { return null; } };
global.t = function (k) { return k; };
global.escapeHtml = function (s) { return String(s); };
global.showToast = function () {};
global._renderProvidersTab = function () {};
// Declared in settings/local_endpoints.js in production (shared window scope);
// the harness supplies it so we only eval the file under test.
global._stgProviders = [];

eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL core/model_caps.js
eval(fs.readFileSync(process.argv[3], 'utf8'));  // REAL access_matrix.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Provider with one image model + one chat model.
_stgProviders[0] = {
  id: 'p',
  models: [
    { model_id: 'img-m', capabilities: ['image_gen'] },
    { model_id: 'chat-m', capabilities: ['text'] },
  ],
};

// ── (E) Stale persisted snapshot (pre-fix false positives) heals on ingest ──
const stale = {
  status: 'done',
  cells: {
    '0::img-m':  { key_idx: 0, model_id: 'img-m',  root_model_id: 'img-m',
                   status: 'unavailable',
                   detail: 'HTTP 500 {"data":"bound must be positive"} (3/3 attempts failed)',
                   recommend_disable: true },
    '0::chat-m': { key_idx: 0, model_id: 'chat-m', root_model_id: 'chat-m',
                   status: 'unavailable', detail: 'HTTP 500 x', recommend_disable: true },
  },
  summary: { ok: 0, disable: 2 },
  total: 2, done_count: 2,
};
check('ingest_returns_true', _ingestProbeSnapshot(0, stale) === true);
const probe = _stgMatrixProbe[0];
check('nonchat_downgraded_to_skipped', probe.cells['0::img-m'].status === 'skipped');
check('nonchat_no_longer_recommended', probe.cells['0::img-m'].recommend_disable === false);
check('original_verdict_kept_in_tooltip',
      probe.cells['0::img-m'].detail.indexOf('was unavailable') >= 0 &&
      probe.cells['0::img-m'].detail.indexOf('bound must be positive') >= 0);
check('chat_cell_untouched',
      probe.cells['0::chat-m'].status === 'unavailable' &&
      probe.cells['0::chat-m'].recommend_disable === true);
check('summary_recomputed',
      probe.summary.skipped === 1 && probe.summary.disable === 1 && probe.summary.ok === 0);

// ── (F) Apply guard: even a hand-crafted recommend_disable on a non-chat
//        model (bypassing reconcile entirely) must NOT be applied ──
_stgMatrixProbe[0] = {
  status: 'done',
  cells: {
    '0::img-m':  { key_idx: 0, model_id: 'img-m',  root_model_id: 'img-m',
                   status: 'unavailable', detail: 'x', recommend_disable: true },
    '0::chat-m': { key_idx: 0, model_id: 'chat-m', root_model_id: 'chat-m',
                   status: 'unavailable', detail: 'x', recommend_disable: true },
  },
  summary: { ok: 0, disable: 2 },
};
_applyMatrixRecommendations(0);
const imgModel = _stgProviders[0].models[0];
const chatModel = _stgProviders[0].models[1];
const imgDisabled = imgModel.key_access && imgModel.key_access['0'] &&
  (imgModel.key_access['0'].disabled_ids || []).indexOf('img-m') >= 0;
const chatDisabled = chatModel.key_access && chatModel.key_access['0'] &&
  (chatModel.key_access['0'].disabled_ids || []).indexOf('chat-m') >= 0;
check('apply_never_disables_nonchat', !imgDisabled);
check('apply_still_disables_chat', !!chatDisabled);

// ── (H) FRESH modality verdict (stamped probe_surface) SURVIVES reconcile
//        and its not_found IS applied — exposing dead models is the whole
//        point of the per-modality probe. Reset the fixture first. ──
_stgProviders[0] = {
  id: 'p',
  models: [
    { model_id: 'img-m', capabilities: ['image_gen'] },
    { model_id: 'chat-m', capabilities: ['text'] },
  ],
};
const fresh = {
  status: 'done',
  cells: {
    '0::img-m':  { key_idx: 0, model_id: 'img-m',  root_model_id: 'img-m',
                   status: 'not_found',
                   detail: 'HTTP 404 via /images/generations',
                   recommend_disable: true, probe_surface: 'image' },
    '0::chat-m': { key_idx: 0, model_id: 'chat-m', root_model_id: 'chat-m',
                   status: 'not_found', detail: 'HTTP 404',
                   recommend_disable: true, probe_surface: 'chat' },
  },
  summary: { ok: 0, disable: 2 },
  total: 2, done_count: 2,
};
_ingestProbeSnapshot(0, fresh);
const fp = _stgMatrixProbe[0];
check('fresh_modality_verdict_survives_reconcile',
      fp.cells['0::img-m'].status === 'not_found' &&
      fp.cells['0::img-m'].recommend_disable === true &&
      fp.cells['0::img-m'].detail.indexOf('stale chat-probe') < 0);
check('fresh_chat_verdict_untouched',
      fp.cells['0::chat-m'].status === 'not_found' &&
      fp.cells['0::chat-m'].recommend_disable === true);
_applyMatrixRecommendations(0);
const imgModel2 = _stgProviders[0].models[0];
const chatModel2 = _stgProviders[0].models[1];
const imgDisabled2 = imgModel2.key_access && imgModel2.key_access['0'] &&
  (imgModel2.key_access['0'].disabled_ids || []).indexOf('img-m') >= 0;
const chatDisabled2 = chatModel2.key_access && chatModel2.key_access['0'] &&
  (chatModel2.key_access['0'].disabled_ids || []).indexOf('chat-m') >= 0;
check('apply_executes_fresh_modality_not_found', !!imgDisabled2);
check('apply_still_executes_chat_not_found', !!chatDisabled2);

console.log(out.join('\n'));
process.exit(0);
"""


# Cross-stack NEUTER harness: ingest a backend-produced snapshot JSON
# (argv[4]) through the REAL, unmodified access_matrix.js and report what
# the image cell became. Used to prove that without the backend's
# probe_surface stamp, a FRESH modality not_found is wrongly swallowed.
_SURFACE_NEUTER_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.document = { querySelector: function () { return null; } };
global.t = function (k) { return k; };
global.escapeHtml = function (s) { return String(s); };
global.showToast = function () {};
global._renderProvidersTab = function () {};
global._stgProviders = [];

eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL core/model_caps.js
eval(fs.readFileSync(process.argv[3], 'utf8'));  // REAL access_matrix.js

const snap = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
_stgProviders[0] = {
  id: 'p',
  models: [{ model_id: 'img-m', capabilities: ['image_gen'] }],
};
_ingestProbeSnapshot(0, snap);
const cell = _stgMatrixProbe[0].cells['0::img-m'];
console.log('CELLSTATUS ' + cell.status + ' recommend_disable=' + cell.recommend_disable);
process.exit(0);
"""


def _run_surface_neuter_harness(snapshot_path: str) -> str:
    harness = os.path.join(HERE, '_probe_surface_neuter_harness.js')
    with open(harness, 'w') as f:
        f.write(_SURFACE_NEUTER_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, MODEL_CAPS_JS, ACCESS_MATRIX_JS, snapshot_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


def _run_node_harness(matrix_js: str) -> str:
    harness = os.path.join(HERE, '_probe_nonchat_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, MODEL_CAPS_JS, matrix_js],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
class FrontendReconcileTest(unittest.TestCase):

    def test_stale_snapshot_reconciled_and_apply_guarded(self):
        output = _run_node_harness(ACCESS_MATRIX_JS)
        fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
        self.assertEqual(fails, [], 'frontend reconcile/apply-guard failures:\n' + output)
        self.assertGreaterEqual(output.count('PASS'), 12,
                                'expected >=12 PASS lines, got:\n' + output)

    def test_surface_stamp_neuter_fresh_verdict_swallowed(self):
        """NEUTER (cross-stack): strip the probe_surface stamp from a COPY of
        provider_probe.py → a FRESH image-surface not_found comes off the
        backend UNSTAMPED → the REAL, unmodified frontend reconcile swallows
        it into 'skipped'. Proves the stamp is what lets the gates tell a
        fresh modality verdict from a stale chat false positive."""
        import json
        with open(pp.__file__, encoding='utf-8') as f:
            src = f.read()
        anchor1 = "            'probe_surface': surface,\n"
        anchor2 = "                    'probe_surface': 'none',\n"
        self.assertIn(anchor1, src, 'stamp anchor drifted — update the neuter')
        self.assertIn(anchor2, src, 'none-stamp anchor drifted — update the neuter')
        neutered = src.replace(anchor1, '', 1).replace(anchor2, '', 1)
        self.assertNotEqual(neutered, src)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_mod = os.path.join(tmp, 'provider_probe_nostamp.py')
            with open(tmp_mod, 'w', encoding='utf-8') as f:
                f.write(neutered)
            spec = importlib.util.spec_from_file_location('provider_probe_nostamp', tmp_mod)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.probe_cache_path = lambda pid: os.path.join(tmp, pid + '.json')

            task = {
                'provider_id': 't-nostamp', 'status': 'running',
                'started_at': 0, 'finished_at': None, 'total': 1,
                'done_count': 0, 'cells': {},
                'summary': {'ok': 0, 'disable': 0},
                'error': None, 'attempts': 1, '_abort': False,
                '_base_url': 'https://gw.example.com/v1', '_extra_headers': {},
            }
            work = [(0, 'sk-a', 'img-m', 'img-m', ['image_gen'])]
            with mock.patch.object(mod, 'probe_cell_multi',
                                   return_value=('not_found', 'HTTP 404 via /images/generations')):
                mod.run_cell_probe_task(task, work, timeout=5)

            snap = mod.public_probe_snapshot(task)
            cell = snap['cells']['0::img-m']
            self.assertEqual(cell['status'], 'not_found',
                             'neutered backend must still produce the fresh verdict')
            self.assertNotIn('probe_surface', cell,
                             'NEUTER did not bite: the stamp survived stripping')

            snap_path = os.path.join(tmp, 'snap.json')
            with open(snap_path, 'w', encoding='utf-8') as f:
                json.dump(snap, f)
            output = _run_surface_neuter_harness(snap_path)
            self.assertIn('CELLSTATUS skipped recommend_disable=false', output,
                          'NEUTER did not bite: without the stamp the fresh '
                          'modality not_found must be swallowed by the REAL '
                          'frontend reconcile — got: ' + output)

        with open(pp.__file__, encoding='utf-8') as f:
            self.assertEqual(f.read(), src, 'harness mutated the shipped provider_probe.py')

    def test_frontend_neuter_reconcile_is_load_bearing(self):
        """NEUTER: drop the _reconcileProbeNonChat call from a COPY of
        access_matrix.js → the stale false positive is NOT healed → proves
        the reconcile call is load-bearing. Shipped file byte-identical."""
        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            src = f.read()
        anchor = '  _reconcileProbeNonChat(provIdx);\n'
        self.assertIn(anchor, src, 'reconcile-call anchor drifted — update the neuter')
        neutered = src.replace(anchor, '', 1)
        self.assertNotEqual(neutered, src)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        copy = os.path.join(tmp.name, 'access_matrix_neutered.js')
        with open(copy, 'w', encoding='utf-8') as f:
            f.write(neutered)

        output = _run_node_harness(copy)
        self.assertIn('FAIL nonchat_downgraded_to_skipped', output,
                      'NEUTER did not bite: stale false positive healed even without '
                      'the reconcile call.\n' + output)

        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            self.assertEqual(f.read(), src, 'harness mutated the shipped access_matrix.js')


if __name__ == '__main__':
    unittest.main()
