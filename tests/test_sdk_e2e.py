"""tests/test_sdk_e2e.py — Python SDK driving the live server.

The SDK uses ``requests`` over real HTTP. We can't easily start a real
HTTPS listener inside pytest, so this test boots the Quart app, mounts
it on a real ephemeral port via ``hypercorn``, and points the SDK at
``http://127.0.0.1:<port>``.

This is the highest-fidelity confirmation that the SDK contract holds:
real network, real HTTP/1.1, real bytes-on-the-wire SSE.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import threading
import time
import unittest


def _free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


_STATE = {'app': None, 'admin_token': None, 'user_token': None,
           'port': None, 'thread': None, 'shutdown': None,
           'tmp': None}


def _boot_real_server():
    if _STATE['app'] is not None:
        return _STATE
    _STATE['tmp'] = tempfile.TemporaryDirectory()
    tmp = _STATE['tmp'].name

    from lib import api_keys, usage_tracker
    api_keys._STORE_PATH = os.path.join(tmp, 'api_keys.json')
    api_keys._cache.clear()
    api_keys._cache_loaded = False
    usage_tracker._STORE_PATH = os.path.join(tmp, 'usage.json')
    usage_tracker._state.clear()
    usage_tracker._loaded = False
    os.environ['TUNNEL_TOKEN'] = ''

    import importlib.util
    spec = importlib.util.spec_from_file_location('server_sdk_e2e', 'server.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _STATE['app'] = mod.app

    # Stub spawn_task (no real LLM).
    import lib.tasks_pkg as pkg
    from lib.tasks_pkg.manager import append_event

    def _fake_spawn(task):
        msgs = task.get('messages') or []
        last = ''
        for m in reversed(msgs):
            if m.get('role') == 'user':
                c = m.get('content', '')
                last = c if isinstance(c, str) else str(c)
                break
        task['content'] = f'sdk-stub: {last[:60]}'
        task['status'] = 'done'
        task['finishReason'] = 'stop'
        task['usage'] = {'input_tokens': 5, 'output_tokens': 5,
                         'total_tokens': 10}
        append_event(task, {'type': 'delta', 'content': task['content']})
        append_event(task, {'type': 'done', 'finishReason': 'stop',
                             'usage': task['usage']})

    pkg.spawn_task = _fake_spawn

    from lib.api_keys import create_key
    _row, _STATE['admin_token'] = create_key(
        name='sdk-admin', scopes=[], admin=True)
    _row, _STATE['user_token'] = create_key(
        name='sdk-user',
        scopes=['chat', 'tasks', 'capabilities', 'usage'],
        rate_limit_rpm=120)

    # Boot Hypercorn on a free port.
    port = _free_port()
    _STATE['port'] = port

    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    cfg = Config()
    cfg.bind = [f'127.0.0.1:{port}']
    cfg.accesslog = None
    cfg.errorlog = None

    shutdown_evt = asyncio.Event()
    _STATE['shutdown'] = shutdown_evt

    def _runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # Re-bind shutdown event to this loop.
        evt = asyncio.Event()
        _STATE['shutdown'] = evt
        try:
            loop.run_until_complete(
                serve(_STATE['app'], cfg,
                      shutdown_trigger=evt.wait))
        finally:
            loop.close()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    _STATE['thread'] = t
    # Wait for the port to accept connections (max ~5s).
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return _STATE


def _shutdown_real_server():
    if _STATE['app'] is None:
        return
    evt = _STATE.get('shutdown')
    if evt is not None:
        try:
            # Set the event from the worker thread's loop.
            import asyncio as _a
            for loop in [t for t in threading.enumerate()]:
                pass
            # Best-effort: fire from a fresh loop via call_soon_threadsafe.
            evt._loop.call_soon_threadsafe(evt.set)  # type: ignore
        except Exception:
            pass
    _STATE['thread'].join(timeout=3)
    _STATE['app'] = None
    if _STATE['tmp'] is not None:
        _STATE['tmp'].cleanup()
        _STATE['tmp'] = None


@unittest.skipIf(
    os.environ.get('TOFU_SKIP_NETWORK_E2E') == '1',
    'TOFU_SKIP_NETWORK_E2E=1 set — skipping real-network SDK test')
class SDKE2ETest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Make the SDK importable as a top-level package.
        sdk_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               '..', 'clients', 'python')
        sdk_dir = os.path.abspath(sdk_dir)
        if sdk_dir not in sys.path:
            sys.path.insert(0, sdk_dir)
        _boot_real_server()
        cls.base = f'http://127.0.0.1:{_STATE["port"]}'
        cls.user_token = _STATE['user_token']
        cls.admin_token = _STATE['admin_token']

    @classmethod
    def tearDownClass(cls):
        _shutdown_real_server()

    def _client(self, token=None):
        from tofu_sdk import Tofu
        return Tofu(base_url=self.base, api_key=token or self.user_token,
                     timeout=15)

    # ── Tests ──────────────────────────────────────────────────────

    def test_capabilities(self):
        caps = self._client().capabilities()
        self.assertIn('models', caps)
        self.assertIn('scopes', caps)

    def test_whoami(self):
        ctx = self._client().keys.whoami()
        self.assertTrue(ctx['authenticated'])
        self.assertEqual(ctx['name'], 'sdk-user')

    def test_chat_sync_via_sdk(self):
        client = self._client()
        resp = client.chat(
            messages=[{'role': 'user', 'content': 'SDK_PING'}],
            model='m', timeout_s=5)
        self.assertEqual(resp['object'], 'chat.completion')
        self.assertIn('SDK_PING', resp['choices'][0]['message']['content'])

    def test_chat_streaming_via_sdk(self):
        client = self._client()
        chunks = list(client.stream(
            messages=[{'role': 'user', 'content': 'SDK_STREAM_TOK'}],
            model='m'))
        self.assertGreater(len(chunks), 0)
        # Some chunk should carry the user prompt back via the stub
        joined = ''.join(
            c.get('choices', [{}])[0].get('delta', {}).get('content', '')
            for c in chunks)
        self.assertIn('SDK_STREAM_TOK', joined)

    def test_tasks_get_after_chat(self):
        client = self._client()
        resp = client.chat(
            messages=[{'role': 'user', 'content': 'task-lookup'}],
            timeout_s=5)
        tid = resp['task_id']
        t = client.tasks.get(tid)
        self.assertEqual(t['status'], 'done')

    def test_invalid_token_raises(self):
        from tofu_sdk import TofuError
        from tofu_sdk import Tofu
        bad = Tofu(base_url=self.base, api_key='tofu_live_' + 'z' * 32,
                    timeout=5)
        with self.assertRaises(TofuError) as cm:
            bad.capabilities()
        self.assertEqual(cm.exception.status, 401)

    def test_admin_keys_list(self):
        client = self._client(self.admin_token)
        keys = client.keys.list()['keys']
        names = [k['name'] for k in keys]
        self.assertIn('sdk-admin', names)
        self.assertIn('sdk-user', names)

    def test_clean_log_via_sdk(self):
        """SDK can drive the log-noise detector that powers the UI banner."""
        client = self._client()
        text = '\n'.join([
            'INFO 2026-01-01 10:00:00,000 mod.foo Working',
        ] * 30)
        result = client.agents.clean_log(text=text)
        self.assertTrue(result['ok'])
        self.assertNotIn('no_noise', result,
            'expected real cleaning result, got no_noise=true')
        self.assertGreater(result.get('savedChars', 0), 0)

    def test_kind_routes_introspectable(self):
        """tasks.KIND_ROUTES is the public list of supported start kinds."""
        from tofu_sdk import Tofu
        kinds = Tofu(base_url=self.base, api_key=self.user_token,
                      timeout=5).tasks.KIND_ROUTES
        # All v1 agent endpoints we expose:
        for expected in ('paper-report', 'paper-translate',
                          'translate', 'image-gen', 'memory-search',
                          'search'):
            self.assertIn(expected, kinds)

    def test_run_unknown_kind_raises(self):
        client = self._client()
        with self.assertRaises(ValueError) as cm:
            client.tasks.run(kind='no-such-kind', params={})
        self.assertIn('no-such-kind', str(cm.exception))

    def test_extract_file_changes_via_sdk(self):
        """SDK can drive the file-change extractor that powers the UI bar."""
        client = self._client()
        rounds = [{
            'toolName': 'write_file',
            'toolArgs': '{"path": "src/foo.py"}',
            'results': [{'badge': 'Created', 'writeOk': True}],
        }, {
            'toolName': 'apply_diff',
            'toolArgs': '{"path": "src/bar.py"}',
            'results': [{'writeOk': True}],
        }]
        result = client.agents.extract_file_changes(tool_rounds=rounds)
        self.assertTrue(result['ok'])
        files = result['files']
        self.assertEqual(len(files), 2)
        by_path = {f['path']: f for f in files}
        self.assertEqual(by_path['src/foo.py']['action'], 'created')
        self.assertEqual(by_path['src/bar.py']['action'], 'patched')


if __name__ == '__main__':
    unittest.main()
