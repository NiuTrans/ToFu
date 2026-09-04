#!/usr/bin/env python3
"""tests/test_local_serve_process.py — managed lifecycle integration tests.

Everything external is faked: the child process (FakeProc), HTTP readiness
(http_get), the engine installer (env_mod), and the ledger path. The tests
pin the supervisor contract: success → running + provider registration is
the api layer's job (covered separately); early exit with an OOM signature
→ next ladder rung; timeout/terminal → failed with a user-facing reason.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib.local_serve import _env as env_mod
from lib.local_serve import _process as proc
from lib.local_serve import _store as store

pytestmark = pytest.mark.unit


class FakeProc:
    _next_pid = [40000]

    def __init__(self, rc_sequence):
        FakeProc._next_pid[0] += 1
        self.pid = FakeProc._next_pid[0]
        self._rcs = list(rc_sequence)

    def poll(self):
        if not self._rcs:
            return None
        rc = self._rcs[0]
        if len(self._rcs) > 1:
            self._rcs.pop(0)
        return rc


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolate the ledger + logs/env root into tmp_path."""
    monkeypatch.setattr(store, 'LEDGER_PATH', str(tmp_path / 'ledger.json'))
    monkeypatch.setattr(env_mod, 'serve_root',
                        lambda: str(tmp_path / 'serve'))
    os.makedirs(tmp_path / 'serve', exist_ok=True)
    monkeypatch.setattr(env_mod, 'ensure_engine',
                        lambda engine, log=None: {'ok': True, 'binary': '/fake/bin'})
    monkeypatch.setattr(env_mod, 'resolve_launcher',
                        lambda engine, argv: ['/fake/bin'] + list(argv[1:]))
    monkeypatch.setattr(env_mod, 'launcher_env',
                        lambda engine, env: dict(env))
    return tmp_path


def _record(instance_id='ls_vllm_test', engine='vllm', degrade=()):
    return store.upsert_instance({
        'id': instance_id, 'engine': engine,
        'model_path': '/models/M', 'served_name': 'M',
        'port': 18100, 'base_url': 'http://127.0.0.1:18100/v1',
        'argv': ['vllm', 'serve', '/models/M', '--host', '127.0.0.1',
                 '--port', '18100', '--max-model-len', '16384'],
        'env': {'CUDA_VISIBLE_DEVICES': '0'},
        'tier': 'tight', 'notes': [], 'degrade': list(degrade),
        'setup_steps': [], 'degrade_index': 0,
        'status': 'planned', 'pid': None, 'provider_id': None,
        'last_error': None,
    })


def _ready_http(url, timeout=2.0):
    if url.endswith('/models'):
        return 200, '{"data": [{"id": "M"}]}'
    return 200, '{}'


class TestStartSuccess:
    def test_running_on_first_try(self, sandbox):
        _record()
        spawns = []

        def fake_popen(argv, **kw):
            spawns.append(argv)
            return FakeProc([None])          # never exits

        r = proc.start_instance('ls_vllm_test', popen=fake_popen,
                                http_get=_ready_http, sleep=lambda s: None)
        assert r['ok'] and r['status'] == 'running'
        assert r['pid'] is not None
        assert spawns[0][0] == '/fake/bin'   # launcher resolution applied

    def test_install_failure_stops_early(self, sandbox, monkeypatch):
        _record()
        monkeypatch.setattr(env_mod, 'ensure_engine',
                            lambda engine, log=None:
                            {'ok': False, 'error': '磁盘余量不足'})
        r = proc.start_instance('ls_vllm_test', popen=lambda a, **k: FakeProc([]),
                                http_get=_ready_http, sleep=lambda s: None)
        assert not r['ok'] and r['status'] == 'failed'
        assert '磁盘' in r['last_error']


class TestOomLadder:
    def test_oom_triggers_next_rung(self, sandbox):
        _record(degrade=[
            {'note': 'OOM 降级：上下文降到 4k',
             'replace': {'--max-model-len': '4096'}},
            {'note': '仍 OOM：换量化', 'terminal': True},
        ])
        attempts = []

        def fake_popen(argv, **kw):
            attempts.append(list(argv))
            if len(attempts) == 1:
                # First child dies with an OOM signature in its log.
                kw['stdout'].write(b'RuntimeError: CUDA out of memory')
                return FakeProc([1])
            return FakeProc([None])

        r = proc.start_instance('ls_vllm_test', popen=fake_popen,
                                http_get=_ready_http, sleep=lambda s: None)
        assert r['ok'] and r['status'] == 'running'
        assert r['degrade_index'] == 1
        i = attempts[1].index('--max-model-len')
        assert attempts[1][i + 1] == '4096'

    def test_terminal_rung_is_never_executed(self, sandbox):
        _record(degrade=[{'note': '换更小的量化', 'terminal': True}])

        def fake_popen(argv, **kw):
            kw['stdout'].write(b'CUDA out of memory')
            return FakeProc([1])

        r = proc.start_instance('ls_vllm_test', popen=fake_popen,
                                http_get=_ready_http, sleep=lambda s: None)
        assert not r['ok'] and r['status'] == 'failed'
        assert '量化' in (r['last_error'] or '')

    def test_non_oom_death_does_not_climb(self, sandbox):
        _record(degrade=[{'note': 'x', 'append': ['--enforce-eager']}])
        spawns = []

        def fake_popen(argv, **kw):
            spawns.append(argv)
            kw['stdout'].write(b'tokenizer file missing')
            return FakeProc([3])

        r = proc.start_instance('ls_vllm_test', popen=fake_popen,
                                http_get=_ready_http, sleep=lambda s: None)
        assert not r['ok']
        assert len(spawns) == 1          # no pointless retry without OOM
        assert '提前退出' in r['last_error']


class TestTimeout:
    def test_readiness_timeout_fails(self, sandbox):
        _record()
        r = proc.start_instance(
            'ls_vllm_test',
            popen=lambda a, **k: FakeProc([None]),
            http_get=lambda url, timeout=2.0: (0, 'refused'),
            sleep=lambda s: None, ready_timeout=0.05)
        assert not r['ok'] and r['status'] == 'failed'
        assert '超时' in r['last_error']


class TestStopAndStatus:
    def test_status_reconciles_dead_pid(self, sandbox, monkeypatch):
        _record()
        store.update_fields('ls_vllm_test', status='running', pid=999999)
        monkeypatch.setattr(proc, '_pid_alive', lambda pid: False)
        r = proc.status_instance('ls_vllm_test', http_get=_ready_http)
        assert r['status'] == 'stopped' and r['pid_alive'] is False

    def test_serving_probe(self, sandbox, monkeypatch):
        _record()
        store.update_fields('ls_vllm_test', status='running', pid=12345)
        monkeypatch.setattr(proc, '_pid_alive', lambda pid: True)
        r = proc.status_instance('ls_vllm_test', http_get=_ready_http)
        assert r['serving'] is True


class TestPortAllocation:
    def test_skips_ledger_and_busy(self, sandbox):
        _record('ls_a')
        store.update_fields('ls_a', port=18100)
        _record('ls_b')
        store.update_fields('ls_b', port=18101)
        p = proc.allocate_port(bind_test=False)
        assert p == 18102
