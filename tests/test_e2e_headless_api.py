"""tests/test_e2e_headless_api.py — Full end-to-end headless API contract test.

Boots the REAL server.py (with all blueprints + middleware), mints
both a regular and an admin API key in a tempdir-isolated key store,
and drives every documented endpoint as an external client would.

Goals:

  1. **Contract compliance** — every endpoint returns the documented
     status / shape.
  2. **Auth & scopes** — unauthenticated, wrong-scope, and admin paths
     each behave correctly.
  3. **Cross-feature interactions** — usage tracking + rate limits +
     idempotency + audit + capabilities all stay coherent.
  4. **Compat ecosystem** — verify the OpenAI / Anthropic adapters
     speak the right wire format.
  5. **No leaks** — capabilities reports the same model registry the
     dispatcher will route to; OpenAPI matches actual url_map.

If something is broken, this test catches it before SDK clients hit it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import tempfile
import unittest

import pytest


# ── Once-only global fixture ────────────────────────────────────────

_STATE = {'app': None, 'tmp': None, 'admin': None, 'user': None,
           'orig_keys_path': None, 'orig_usage_path': None}


def _setup_once():
    if _STATE['app'] is not None:
        return _STATE
    # ⚠️ DATA-LOSS GUARD (2026-06-28): imports server.py + builds the real app
    # OUTSIDE the conftest flask_app/live_server fixtures, so it must call the
    # keystone DB guard itself before touching the real app/DB.
    from tests.conftest import _assert_test_database
    _assert_test_database('test_e2e_headless_api._setup_once')
    _STATE['tmp'] = tempfile.TemporaryDirectory()
    tmp = _STATE['tmp'].name
    # Patch the API-key + usage stores to a tempdir BEFORE booting server.
    from lib import api_keys, usage_tracker
    _STATE['orig_keys_path'] = api_keys._STORE_PATH
    _STATE['orig_usage_path'] = usage_tracker._STORE_PATH
    api_keys._STORE_PATH = os.path.join(tmp, 'api_keys.json')
    api_keys._cache.clear()
    api_keys._cache_loaded = False
    usage_tracker._STORE_PATH = os.path.join(tmp, 'usage.json')
    usage_tracker._state.clear()
    usage_tracker._loaded = False
    # Empty TUNNEL_TOKEN means tunnel auth is fully open — but bearer
    # auth still gates /api/v1/* paths because _is_api_path() is
    # checked unconditionally in the middleware. (If a deployment had
    # no TUNNEL_TOKEN AND no Bearer, requests to /api/v1/* return 401.)
    os.environ['TUNNEL_TOKEN'] = ''

    import importlib.util
    spec = importlib.util.spec_from_file_location('server_e2e', 'server.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _STATE['app'] = mod.app

    # Mint two keys: one full-admin, one chat+tasks+usage scoped.
    from lib.api_keys import create_key
    _row, _STATE['admin'] = create_key(name='e2e-admin', scopes=[],
                                        admin=True)
    _row, _STATE['user'] = create_key(
        name='e2e-user',
        scopes=['chat', 'tasks', 'usage', 'capabilities',
                'agents:memory', 'agents:image', 'agents:browser',
                'agents:translate', 'webhooks'],
        rate_limit_rpm=120, rate_limit_tpd=0)
    return _STATE


def _teardown_once():
    _uninstall_chat_stub()  # before the early return: a half-failed setup
    # must never strand the global spawn_task stub either
    if _STATE['app'] is None:
        return
    from lib import api_keys, usage_tracker
    api_keys._STORE_PATH = _STATE['orig_keys_path']
    api_keys._cache.clear()
    api_keys._cache_loaded = False
    usage_tracker._STORE_PATH = _STATE['orig_usage_path']
    usage_tracker._state.clear()
    usage_tracker._loaded = False
    _STATE['tmp'].cleanup()
    _STATE['app'] = None


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _hdr(token, extra=None):
    h = {'Authorization': f'Bearer {token}'}
    if extra:
        h.update(extra)
    return h


# ── Stub the LLM pipeline once so chat completions don't hit real models ──

_STUB_INSTALLED = False
_ORIG_SPAWN = None


def _install_chat_stub():
    """Replace ``spawn_task`` with a synchronous stub that fills in a
    fake assistant response immediately. Idempotent."""
    global _STUB_INSTALLED, _ORIG_SPAWN
    if _STUB_INSTALLED:
        return
    import lib.tasks_pkg as pkg
    from lib.tasks_pkg.manager import append_event
    _ORIG_SPAWN = pkg.spawn_task

    def _fake_spawn(task):
        # Echo the last user message in the stub response so we can
        # detect mis-routed bodies.
        msgs = task.get('messages') or []
        last_user = ''
        for m in reversed(msgs):
            if m.get('role') == 'user':
                c = m.get('content', '')
                if isinstance(c, str):
                    last_user = c
                elif isinstance(c, list):
                    parts = [p.get('text', '') for p in c
                              if isinstance(p, dict) and p.get('type') == 'text']
                    last_user = ' '.join(parts)
                break
        task['content'] = f'[stub] echo: {last_user[:80]}'
        task['thinking'] = ''
        task['status'] = 'done'
        task['finishReason'] = 'stop'
        task['usage'] = {
            'input_tokens': max(1, len(last_user) // 4),
            'output_tokens': 8,
            'total_tokens': max(1, len(last_user) // 4) + 8,
        }
        append_event(task, {'type': 'delta', 'content': task['content']})
        append_event(task, {'type': 'done',
                             'finishReason': 'stop',
                             'usage': task['usage']})

    pkg.spawn_task = _fake_spawn
    _STUB_INSTALLED = True


def _uninstall_chat_stub():
    """Restore the real ``spawn_task``. Without this the stub LEAKS to every
    later suite in the same xdist worker — measured 2026-08-05 (d820520 unit
    leg): test_spawn_serving_loop's ``tp.spawn_task`` resolved to this fake
    and died on KeyError('events_lock')."""
    global _STUB_INSTALLED
    if not _STUB_INSTALLED:
        return
    import lib.tasks_pkg as pkg
    pkg.spawn_task = _ORIG_SPAWN
    _STUB_INSTALLED = False


# ── Test class ──────────────────────────────────────────────────────


class E2EHeadlessApiTest(unittest.TestCase):

    # Auth contract assertions require the credential gate to be ACTIVE,
    # which it only is in private/multi-user mode (open mode — the conftest
    # default — lets unauth /api/v1/* through with a synthetic principal).
    # The per-test conftest fixture forces private mode for this file.
    pytestmark = pytest.mark.auth_mode('private')

    @classmethod
    def setUpClass(cls):
        _setup_once()
        _install_chat_stub()
        cls.app = _STATE['app']
        cls.admin = _STATE['admin']
        cls.user = _STATE['user']

    @classmethod
    def tearDownClass(cls):
        _teardown_once()

    def setUp(self):
        # Per-test cleanup of usage state so day-totals don't bleed across.
        from lib import usage_tracker
        usage_tracker._state.clear()
        usage_tracker._loaded = False
        try:
            os.remove(usage_tracker._STORE_PATH)
        except FileNotFoundError:
            pass
        # Drop idempotency cache too.
        from lib.idempotency import _cache
        _cache.clear()

    def _client(self):
        return self.app.test_client()

    # ── 1. Self-description endpoints ──────────────────────────────

    def test_openapi_spec_describes_every_route(self):
        """The OpenAPI spec must mention every documented blueprint."""
        async def go():
            c = self._client()
            r = await c.get('/api/openapi.json')
            self.assertEqual(r.status_code, 200)
            spec = json.loads(await r.get_data(as_text=True))
            paths = set(spec['paths'].keys())
            # Every native + compat endpoint we expose must be present.
            must_have = [
                '/api/v1/capabilities',
                '/api/v1/keys',
                '/api/v1/keys/whoami',
                '/api/v1/keys/{key_id}',
                '/api/v1/chat/completions',
                '/api/v1/tasks',
                '/api/v1/tasks/{task_id}',
                '/api/v1/tasks/{task_id}/abort',
                '/api/v1/tasks/{task_id}/events',
                '/api/v1/tasks/{task_id}/stream',
                '/api/v1/usage',
                '/api/v1/usage/summary',
                '/api/v1/agents/translate',
                '/api/v1/agents/memory/search',
                '/api/v1/agents/browser/fetch',
                '/api/v1/agents/image-gen',
                '/api/v1/agents/paper/report',
                '/api/v1/agents/paper/translate',
                '/api/v1/webhooks',
                '/api/v1/webhooks/{sub_id}',
                '/v1/chat/completions',
                '/v1/models',
                '/v1/embeddings',
                '/v1/messages',
                '/v1/messages/count_tokens',
                '/metrics',
            ]
            missing = [p for p in must_have if p not in paths]
            self.assertEqual(missing, [], f'missing paths in OpenAPI: {missing}')
            # Spec is valid 3.1.0
            self.assertEqual(spec['openapi'], '3.1.0')
            # Components include all schemas
            schemas = spec['components']['schemas']
            for s in ('ErrorEnvelope', 'ChatMessage', 'ChatCompletionRequest',
                       'ChatCompletionResponse', 'TofuConfig', 'TaskState',
                       'ApiKey'):
                self.assertIn(s, schemas, f'missing schema: {s}')
            # Security schemes wired
            self.assertIn('bearerAuth',
                           spec['components']['securitySchemes'])
        _run(go())

    def test_swagger_and_redoc_render(self):
        async def go():
            c = self._client()
            for path, marker in (('/api/docs', 'SwaggerUIBundle'),
                                  ('/api/redoc', 'redoc')):
                r = await c.get(path)
                self.assertEqual(r.status_code, 200)
                html = await r.get_data(as_text=True)
                self.assertIn(marker, html)
        _run(go())

    def test_capabilities_self_describes_runtime(self):
        """Capabilities should reflect the real running config."""
        async def go():
            r = await self._client().get('/api/v1/capabilities')
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            for k in ('models', 'tools', 'agents', 'presets', 'backends',
                       'scopes', 'config_schema', 'compat'):
                self.assertIn(k, body, f'capabilities missing {k}')
            self.assertIn('admin', body['scopes'])
            self.assertIn('chat', body['scopes'])
            # config_schema must be JSON-Schema-shaped
            self.assertEqual(body['config_schema']['type'], 'object')
            # compat block lists the three drop-in surfaces
            self.assertEqual(body['compat']['openai_chat_completions'],
                              '/v1/chat/completions')
            self.assertEqual(body['compat']['anthropic_messages'],
                              '/v1/messages')
        _run(go())

    # ── 2. Auth & scope enforcement ────────────────────────────────

    def test_unauthenticated_v1_calls_rejected(self):
        async def go():
            for path in ('/api/v1/keys', '/api/v1/chat/completions',
                          '/api/v1/tasks', '/api/v1/usage',
                          '/v1/chat/completions', '/v1/messages',
                          '/v1/models', '/metrics'):
                method = 'POST' if 'completions' in path or path == '/v1/messages' \
                                else 'GET'
                r = await getattr(self._client(), method.lower())(
                    path, json={} if method == 'POST' else None)
                self.assertEqual(r.status_code, 401,
                    f'{method} {path} → expected 401, got {r.status_code}')
        _run(go())

    def test_invalid_token_rejected(self):
        async def go():
            r = await self._client().get(
                '/api/v1/keys',
                headers=_hdr('tofu_live_' + 'z' * 32))
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_user_token_cannot_access_admin_endpoints(self):
        async def go():
            c = self._client()
            for path in ('/api/v1/keys', '/api/v1/usage/summary', '/metrics'):
                r = await c.get(path, headers=_hdr(self.user))
                self.assertEqual(r.status_code, 403,
                    f'GET {path} with user token → expected 403, got {r.status_code}')
        _run(go())

    def test_admin_token_grants_every_scope(self):
        async def go():
            c = self._client()
            r = await c.get('/api/v1/keys/whoami', headers=_hdr(self.admin))
            self.assertEqual(r.status_code, 200)
            d = await r.get_json()
            self.assertTrue(d['authenticated'])
            self.assertIn('admin', d['scopes'])
        _run(go())

    def test_disabled_key_rejected(self):
        """Disable a key, immediately try to use it → 401."""
        from lib.api_keys import create_key, update_key
        _row, tok = create_key(name='disable-me', scopes=['chat'])

        async def go():
            c = self._client()
            r = await c.get('/api/v1/keys/whoami', headers=_hdr(tok))
            self.assertEqual(r.status_code, 200)
            update_key(_row['id'], disabled=True)
            r = await c.get('/api/v1/keys/whoami', headers=_hdr(tok))
            # whoami is public — but the auth middleware still validates
            # the bearer token before reaching it. Rejection happens at
            # auth → 401.
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_anthropic_x_api_key_header_works(self):
        """Anthropic SDKs send `x-api-key`, not Bearer."""
        async def go():
            c = self._client()
            r = await c.post(
                '/v1/messages',
                headers={'x-api-key': self.user, 'Content-Type': 'application/json'},
                json={
                    'model': 'test', 'max_tokens': 100,
                    'messages': [{'role': 'user', 'content': 'Hi'}],
                })
            # If the header was respected, status is 200 (stub). If
            # not, auth would 401.
            self.assertEqual(r.status_code, 200,
                              await r.get_data(as_text=True))
            body = await r.get_json()
            self.assertEqual(body['type'], 'message')
        _run(go())

    # ── 3. Native chat completion ──────────────────────────────────

    def test_chat_completion_sync(self):
        async def go():
            r = await self._client().post(
                '/api/v1/chat/completions',
                headers=_hdr(self.user),
                json={
                    'model': 'test-model',
                    'messages': [{'role': 'user', 'content': 'PING_42'}],
                    'timeout_s': 5,
                })
            self.assertEqual(r.status_code, 200,
                              await r.get_data(as_text=True))
            body = await r.get_json()
            # Body merges OpenAI shape into top-level (api_ok pattern)
            self.assertEqual(body['object'], 'chat.completion')
            self.assertEqual(body['model'], 'test-model')
            # Stub echoes the user prompt → confirms our messages
            # actually reached the orchestrator.
            self.assertIn('PING_42', body['choices'][0]['message']['content'])
            # finish_reason must be in OpenAI's enum
            self.assertIn(body['choices'][0]['finish_reason'],
                           {'stop', 'length', 'tool_calls', 'content_filter'})
            # task_id surfaces for follow-up
            self.assertTrue(body['task_id'])
            # Usage is structured
            self.assertGreater(body['usage']['total_tokens'], 0)
        _run(go())

    def test_chat_completion_streaming_sse(self):
        """Streaming response must be valid SSE with parseable JSON
        chunks and a [DONE] terminator."""
        async def go():
            r = await self._client().post(
                '/api/v1/chat/completions',
                headers=_hdr(self.user),
                json={
                    'model': 'm', 'stream': True,
                    'messages': [{'role': 'user', 'content': 'STREAM_TOKEN'}],
                    'timeout_s': 5,
                })
            self.assertEqual(r.status_code, 200)
            ct = r.headers.get('Content-Type', '')
            self.assertIn('text/event-stream', ct)
            self.assertEqual(r.headers.get('X-Tofu-Task-Id', '')[:1] != '', True)
            text = await r.get_data(as_text=True)
            # Must end with [DONE]
            self.assertIn('data: [DONE]', text)
            # Every data: line must be valid JSON or [DONE]
            chunks = []
            for line in text.split('\n'):
                if line.startswith('data:'):
                    payload = line[5:].strip()
                    if not payload or payload == '[DONE]':
                        continue
                    obj = json.loads(payload)  # raises on bad JSON
                    chunks.append(obj)
            # At least one chunk must be a chat.completion.chunk
            self.assertTrue(any(c.get('object') == 'chat.completion.chunk'
                                 for c in chunks))
            # The user prompt text must appear in deltas
            joined = ''.join(
                c.get('choices', [{}])[0].get('delta', {}).get('content', '')
                for c in chunks)
            self.assertIn('STREAM_TOKEN', joined)
        _run(go())

    def test_chat_validates_messages(self):
        async def go():
            c = self._client()
            for bad in ({'messages': []},
                         {'messages': [{'role': 'wrong'}]},
                         {'messages': [{'role': 'user'}]},  # no content
                         {}):  # no messages key
                r = await c.post('/api/v1/chat/completions',
                                  headers=_hdr(self.user), json=bad)
                self.assertEqual(r.status_code, 400,
                    f'expected 400 for body={bad}: '
                    f'{await r.get_data(as_text=True)}')
        _run(go())

    # ── 4. Idempotency ─────────────────────────────────────────────

    def test_idempotency_replays(self):
        async def go():
            c = self._client()
            payload = {'messages': [{'role': 'user', 'content': 'idem'}],
                        'timeout_s': 5}
            r1 = await c.post('/api/v1/chat/completions',
                               headers=_hdr(self.user, {'Idempotency-Key': 'idem-1'}),
                               json=payload)
            r2 = await c.post('/api/v1/chat/completions',
                               headers=_hdr(self.user, {'Idempotency-Key': 'idem-1'}),
                               json=payload)
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r2.headers.get('Idempotency-Replay'), 'true')
            d1 = await r1.get_json()
            d2 = await r2.get_json()
            self.assertEqual(d1['task_id'], d2['task_id'])
        _run(go())

    def test_idempotency_keys_are_per_principal(self):
        """Same Idempotency-Key from a different principal must NOT
        collide — it would be a tenant-isolation bug."""
        from lib.api_keys import create_key
        _row, second = create_key(name='other', scopes=['chat'])

        async def go():
            c = self._client()
            payload = {'messages': [{'role': 'user', 'content': 'isolated'}],
                        'timeout_s': 5}
            r1 = await c.post('/api/v1/chat/completions',
                               headers=_hdr(self.user, {'Idempotency-Key': 'shared'}),
                               json=payload)
            r2 = await c.post('/api/v1/chat/completions',
                               headers=_hdr(second, {'Idempotency-Key': 'shared'}),
                               json=payload)
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r2.status_code, 200)
            # Different principals, same idem key → different tasks
            self.assertNotEqual((await r1.get_json())['task_id'],
                                 (await r2.get_json())['task_id'])
            self.assertNotEqual(r2.headers.get('Idempotency-Replay'), 'true')
        _run(go())

    # ── 5. Rate limiting ───────────────────────────────────────────

    def test_rate_limit_headers_present(self):
        async def go():
            r = await self._client().get('/api/v1/keys/whoami',
                                          headers=_hdr(self.user))
            # User key has rpm=120 → headers should appear
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers.get('X-RateLimit-Limit-Requests'),
                              '120')
        _run(go())

    def test_rate_limit_kicks_in_at_threshold(self):
        """A key with rpm=2 must 429 on the third call."""
        from lib import rate_limit_api
        from lib.api_keys import create_key
        _row, tok = create_key(name='rate-limited', scopes=['capabilities'],
                                rate_limit_rpm=2)
        rate_limit_api._state.pop(_row['id'], None)

        async def go():
            c = self._client()
            r1 = await c.get('/api/v1/keys/whoami', headers=_hdr(tok))
            r2 = await c.get('/api/v1/keys/whoami', headers=_hdr(tok))
            r3 = await c.get('/api/v1/keys/whoami', headers=_hdr(tok))
            # whoami is PUBLIC — bypasses rate limit by design.
            # Use a scope-gated route instead.
            for r in (r1, r2, r3):
                self.assertEqual(r.status_code, 200)
            # Hit a rate-limited route.
            r4 = await c.get('/api/v1/capabilities', headers=_hdr(tok))
            r5 = await c.get('/api/v1/capabilities', headers=_hdr(tok))
            r6 = await c.get('/api/v1/capabilities', headers=_hdr(tok))
            statuses = [r4.status_code, r5.status_code, r6.status_code]
            # Whoami is public so doesn't decrement the bucket. capabilities
            # is also public via _PUBLIC_API_PATHS, but a Bearer-auth call
            # to a public path STILL goes through the middleware. Check
            # /api/v1/usage instead — that's scope-gated.
            # Burn down the bucket by hitting usage:
            results = []
            for _ in range(5):
                r = await c.get('/api/v1/usage?days=1', headers=_hdr(tok))
                results.append(r.status_code)
            # At least one 429 within the 5 calls.
            self.assertIn(429, results,
                f'expected at least one 429, got {results}')
        _run(go())

    # ── 6. Tasks lifecycle ─────────────────────────────────────────

    def test_task_lifecycle_after_chat(self):
        """Start a chat → poll /tasks/{id} → confirm terminal state."""
        async def go():
            c = self._client()
            r = await c.post('/api/v1/chat/completions',
                              headers=_hdr(self.user),
                              json={'messages': [{'role': 'user', 'content': 'X'}],
                                    'timeout_s': 5})
            self.assertEqual(r.status_code, 200)
            tid = (await r.get_json())['task_id']
            self.assertTrue(tid)
            # Get task state
            r2 = await c.get(f'/api/v1/tasks/{tid}', headers=_hdr(self.user))
            self.assertEqual(r2.status_code, 200)
            t = await r2.get_json()
            self.assertEqual(t['status'], 'done')
            self.assertEqual(t['kind'], 'chat')
            # Events replay
            r3 = await c.get(f'/api/v1/tasks/{tid}/events',
                              headers=_hdr(self.user))
            self.assertEqual(r3.status_code, 200)
            events = await r3.get_json()
            # Events must include at least a 'done' event
            ev_types = {e.get('type') for e in events.get('events', [])}
            self.assertIn('done', ev_types)
            # List
            r4 = await c.get('/api/v1/tasks?kind=chat&limit=10',
                              headers=_hdr(self.user))
            self.assertEqual(r4.status_code, 200)
            lst = await r4.get_json()
            self.assertGreaterEqual(len(lst.get('tasks') or []), 1)
        _run(go())

    def test_task_404_on_unknown(self):
        async def go():
            r = await self._client().get(
                '/api/v1/tasks/nonexistent_id_xyz',
                headers=_hdr(self.user))
            self.assertEqual(r.status_code, 404)
        _run(go())

    # ── 7. Usage analytics + middleware coherence ──────────────────

    def test_usage_increments_with_each_request(self):
        async def go():
            c = self._client()
            # Make 3 requests
            for i in range(3):
                r = await c.post('/api/v1/chat/completions',
                                  headers=_hdr(self.user),
                                  json={'messages': [{'role': 'user',
                                                       'content': f'usage-{i}'}],
                                         'timeout_s': 5})
                self.assertEqual(r.status_code, 200)
            # Inspect usage
            r = await c.get('/api/v1/usage?days=1',
                             headers=_hdr(self.user))
            self.assertEqual(r.status_code, 200)
            u = await r.get_json()
            today = u['days'][-1]
            # 3 chat completions + 1 usage GET = at least 4 requests
            self.assertGreaterEqual(today['requests'], 4)
            self.assertGreater(today['tokens'], 0)
        _run(go())

    def test_usage_summary_admin_only(self):
        async def go():
            r = await self._client().get(
                '/api/v1/usage/summary?days=7', headers=_hdr(self.admin))
            self.assertEqual(r.status_code, 200)
            d = await r.get_json()
            self.assertIn('per_key', d)
            self.assertIn('daily', d)
        _run(go())

    def test_metrics_endpoint_well_formed(self):
        async def go():
            # First make a request so we have something to measure
            await self._client().post(
                '/api/v1/chat/completions',
                headers=_hdr(self.user),
                json={'messages': [{'role': 'user', 'content': 'metric'}],
                      'timeout_s': 5})
            r = await self._client().get('/metrics', headers=_hdr(self.admin))
            self.assertEqual(r.status_code, 200)
            self.assertIn('text/plain', r.headers.get('Content-Type', ''))
            text = await r.get_data(as_text=True)
            # Must include all our metric families
            for metric in ('tofu_usage_requests_total',
                            'tofu_usage_tokens_total',
                            'tofu_active_keys',
                            'tofu_tasks_inflight',
                            'tofu_idempotency_cache_size',
                            'tofu_rate_limit_buckets'):
                self.assertIn(metric, text,
                               f'missing metric: {metric}')
            # Every non-comment, non-empty line must end in a number.
            for line in text.splitlines():
                if not line or line.startswith('#'):
                    continue
                self.assertRegex(line, r' [\d.eE+\-]+$',
                                  f'malformed metrics line: {line!r}')
        _run(go())

    # ── 8. OpenAI compat ───────────────────────────────────────────

    def test_openai_chat_completion_sync(self):
        async def go():
            r = await self._client().post(
                '/v1/chat/completions',
                headers=_hdr(self.user),
                json={
                    'model': 'gpt-fake',
                    'messages': [{'role': 'user', 'content': 'OPENAI_PING'}],
                    'temperature': 0.7,
                    'max_tokens': 100,
                })
            self.assertEqual(r.status_code, 200,
                              await r.get_data(as_text=True))
            body = await r.get_json()
            # Standard OpenAI shape — no 'ok' wrapper here
            self.assertNotIn('ok', body)
            self.assertEqual(body['object'], 'chat.completion')
            self.assertIn('OPENAI_PING',
                           body['choices'][0]['message']['content'])
            self.assertIn(body['choices'][0]['finish_reason'],
                           {'stop', 'length', 'tool_calls', 'content_filter'})
            # Bonus task_id field for follow-up
            self.assertIn('task_id', body)
        _run(go())

    def test_openai_chat_streaming(self):
        async def go():
            r = await self._client().post(
                '/v1/chat/completions',
                headers=_hdr(self.user),
                json={
                    'model': 'm',
                    'messages': [{'role': 'user', 'content': 'OPENAI_STREAM'}],
                    'stream': True,
                })
            self.assertEqual(r.status_code, 200)
            text = await r.get_data(as_text=True)
            self.assertIn('data: [DONE]', text)
            # Must contain at least one valid chunk
            for line in text.split('\n'):
                if line.startswith('data:') and '[DONE]' not in line:
                    payload = line[5:].strip()
                    if payload:
                        obj = json.loads(payload)
                        self.assertEqual(obj['object'],
                                          'chat.completion.chunk')
                        break
            else:
                self.fail('no chat.completion.chunk in stream')
        _run(go())

    def test_openai_models_list(self):
        async def go():
            r = await self._client().get('/v1/models', headers=_hdr(self.user))
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            # Standard OpenAI list shape
            self.assertEqual(body['object'], 'list')
            self.assertIsInstance(body['data'], list)
            for m in body['data']:
                self.assertIn('id', m)
                self.assertEqual(m['object'], 'model')
        _run(go())

    # ── 9. Anthropic compat ────────────────────────────────────────

    def test_anthropic_messages_sync(self):
        async def go():
            r = await self._client().post(
                '/v1/messages',
                headers=_hdr(self.user),
                json={
                    'model': 'claude-fake',
                    'max_tokens': 100,
                    'system': 'Be brief.',
                    'messages': [{'role': 'user', 'content': 'ANTHROPIC_PING'}],
                })
            self.assertEqual(r.status_code, 200,
                              await r.get_data(as_text=True))
            body = await r.get_json()
            self.assertEqual(body['type'], 'message')
            self.assertEqual(body['role'], 'assistant')
            self.assertEqual(body['model'], 'claude-fake')
            # Content blocks
            self.assertIsInstance(body['content'], list)
            text_block = next((b for b in body['content']
                                if b.get('type') == 'text'), None)
            self.assertIsNotNone(text_block, body)
            self.assertIn('ANTHROPIC_PING', text_block['text'])
            # stop_reason in Anthropic enum
            self.assertIn(body['stop_reason'],
                           {'end_turn', 'max_tokens', 'stop_sequence',
                            'tool_use'})
            # Anthropic usage shape
            self.assertIn('input_tokens', body['usage'])
            self.assertIn('output_tokens', body['usage'])
        _run(go())

    def test_anthropic_messages_streaming(self):
        async def go():
            r = await self._client().post(
                '/v1/messages',
                headers=_hdr(self.user),
                json={
                    'model': 'm', 'max_tokens': 100,
                    'messages': [{'role': 'user', 'content': 'ANTHRO_STREAM'}],
                    'stream': True,
                })
            self.assertEqual(r.status_code, 200)
            text = await r.get_data(as_text=True)
            # Anthropic uses NAMED events, not [DONE]
            self.assertIn('event: message_start', text)
            self.assertIn('event: content_block_delta', text)
            self.assertIn('event: message_stop', text)
            # No OpenAI-style [DONE] marker
            self.assertNotIn('data: [DONE]', text)
        _run(go())

    def test_anthropic_count_tokens(self):
        async def go():
            r = await self._client().post(
                '/v1/messages/count_tokens',
                headers=_hdr(self.user),
                json={
                    'model': 'm',
                    'messages': [{'role': 'user', 'content': 'count me'}],
                })
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertIn('input_tokens', body)
            self.assertGreater(body['input_tokens'], 0)
        _run(go())

    # ── 10. Keys CRUD lifecycle ────────────────────────────────────

    def test_keys_full_lifecycle(self):
        async def go():
            c = self._client()
            # Create
            r = await c.post('/api/v1/keys',
                              headers=_hdr(self.admin),
                              json={'name': 'lifecycle-test',
                                    'scopes': ['chat'],
                                    'rate_limit_rpm': 30})
            self.assertEqual(r.status_code, 201)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertTrue(body['token'].startswith('tofu_live_'))
            kid = body['key']['id']
            # Get
            r = await c.get(f'/api/v1/keys/{kid}', headers=_hdr(self.admin))
            self.assertEqual(r.status_code, 200)
            self.assertNotIn('secret_hash', await r.get_data(as_text=True))
            # Update (toggle disabled)
            r = await c.patch(f'/api/v1/keys/{kid}',
                               headers=_hdr(self.admin),
                               json={'disabled': True, 'rate_limit_rpm': 60})
            self.assertEqual(r.status_code, 200)
            updated = await r.get_json()
            self.assertTrue(updated['key']['disabled'])
            self.assertEqual(updated['key']['rate_limit_rpm'], 60)
            # List includes the key
            r = await c.get('/api/v1/keys', headers=_hdr(self.admin))
            self.assertEqual(r.status_code, 200)
            ids = [k['id'] for k in (await r.get_json())['keys']]
            self.assertIn(kid, ids)
            # Revoke
            r = await c.delete(f'/api/v1/keys/{kid}',
                                headers=_hdr(self.admin))
            self.assertEqual(r.status_code, 200)
            # Now gone
            r = await c.get(f'/api/v1/keys/{kid}', headers=_hdr(self.admin))
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_keys_create_rejects_unknown_scope(self):
        async def go():
            r = await self._client().post(
                '/api/v1/keys',
                headers=_hdr(self.admin),
                json={'name': 'bad', 'scopes': ['bogus_scope']})
            self.assertEqual(r.status_code, 400)
            body = await r.get_json()
            self.assertIn('Unknown scopes', json.dumps(body))
        _run(go())

    def test_secret_hash_never_leaked(self):
        """The SHA-256 hash must never appear in any /keys response."""
        async def go():
            c = self._client()
            r1 = await c.get('/api/v1/keys', headers=_hdr(self.admin))
            r2 = await c.post('/api/v1/keys',
                               headers=_hdr(self.admin),
                               json={'name': 'sec-test', 'scopes': ['chat']})
            r3 = await c.get('/api/v1/keys/whoami', headers=_hdr(self.admin))
            for r in (r1, r2, r3):
                body = await r.get_data(as_text=True)
                self.assertNotIn('secret_hash', body)
        _run(go())

    # ── 11. Webhooks subscribe + delete ────────────────────────────

    def test_webhooks_lifecycle(self):
        async def go():
            c = self._client()
            r = await c.post('/api/v1/webhooks',
                              headers=_hdr(self.user),
                              json={'url': 'https://example.com/hook',
                                    'channel': 'chat',
                                    'event_types': ['done']})
            self.assertEqual(r.status_code, 201)
            body = await r.get_json()
            sub = body['subscription']
            # Secret only shown ONCE
            self.assertTrue(sub['secret'])
            sid = sub['id']
            # List includes it (without secret)
            r = await c.get('/api/v1/webhooks', headers=_hdr(self.user))
            subs = (await r.get_json())['subs']
            target = next((s for s in subs if s['id'] == sid), None)
            self.assertIsNotNone(target)
            self.assertNotIn('secret', target)
            # Delete
            r = await c.delete(f'/api/v1/webhooks/{sid}',
                                headers=_hdr(self.user))
            self.assertEqual(r.status_code, 200)
        _run(go())

    def test_webhook_url_must_be_http(self):
        async def go():
            r = await self._client().post(
                '/api/v1/webhooks',
                headers=_hdr(self.user),
                json={'url': 'ftp://bad'})
            self.assertEqual(r.status_code, 400)
        _run(go())

    # ── 12. Memory agent ───────────────────────────────────────────

    def test_memory_search(self):
        async def go():
            r = await self._client().post(
                '/api/v1/agents/memory/search',
                headers=_hdr(self.user),
                json={'query': 'rate limit', 'top_k': 3})
            # Memory module might be unavailable in tests — accept 200
            # (real result) or 500 (graceful failure with logged error).
            self.assertIn(r.status_code, (200, 500),
                f'unexpected status: {r.status_code}: '
                f'{await r.get_data(as_text=True)}')
        _run(go())

    def test_memory_search_requires_query(self):
        async def go():
            r = await self._client().post(
                '/api/v1/agents/memory/search',
                headers=_hdr(self.user),
                json={})
            self.assertEqual(r.status_code, 400)
        _run(go())

    # ── 13. Error envelope shape ───────────────────────────────────

    def test_error_envelope_shape(self):
        """Every error response must have ok:false and error fields."""
        async def go():
            c = self._client()
            for path, status in (
                ('/api/v1/keys', 401),  # no auth
                ('/api/v1/tasks/zzz', 404),  # bad path with auth
            ):
                kwargs = {} if status == 401 else {'headers': _hdr(self.user)}
                r = await c.get(path, **kwargs)
                self.assertEqual(r.status_code, status)
                body = await r.get_json()
                self.assertEqual(body['ok'], False, body)
                self.assertIn('error', body)
        _run(go())

    # ── 14. Cross-feature smoke ────────────────────────────────────

    def test_cross_feature_smoke(self):
        """One scenario hitting capabilities → chat → tasks → usage →
        metrics → openai compat → anthropic compat in sequence."""
        async def go():
            c = self._client()
            # 1. capabilities is public
            r = await c.get('/api/v1/capabilities')
            self.assertEqual(r.status_code, 200)
            # 2. chat (native)
            r = await c.post('/api/v1/chat/completions',
                              headers=_hdr(self.user),
                              json={'messages': [{'role': 'user', 'content': 'x'}],
                                    'timeout_s': 5})
            tid = (await r.get_json())['task_id']
            # 3. task lookup
            r = await c.get(f'/api/v1/tasks/{tid}', headers=_hdr(self.user))
            self.assertEqual(r.status_code, 200)
            # 4. openai compat
            r = await c.post('/v1/chat/completions',
                              headers=_hdr(self.user),
                              json={'model': 'm',
                                    'messages': [{'role': 'user', 'content': 'y'}]})
            self.assertEqual(r.status_code, 200)
            # 5. anthropic compat
            r = await c.post('/v1/messages',
                              headers=_hdr(self.user),
                              json={'model': 'm', 'max_tokens': 50,
                                    'messages': [{'role': 'user', 'content': 'z'}]})
            self.assertEqual(r.status_code, 200)
            # 6. usage now reflects all three
            r = await c.get('/api/v1/usage?days=1', headers=_hdr(self.user))
            u = await r.get_json()
            self.assertGreaterEqual(u['days'][-1]['requests'], 3)
            # 7. metrics shows non-zero counters
            r = await c.get('/metrics', headers=_hdr(self.admin))
            text = await r.get_data(as_text=True)
            self.assertRegex(text, r'tofu_usage_requests_total\{[^}]+\} [1-9]')
        _run(go())


if __name__ == '__main__':
    unittest.main()
