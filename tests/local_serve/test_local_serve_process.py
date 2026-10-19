#!/usr/bin/env python3
"""tests/local_serve/test_local_serve_process.py — managed lifecycle integration tests.

Everything external is faked: the child process (FakeProc), HTTP readiness
(http_get), the engine installer (env_mod), and the ledger path. The tests
pin the supervisor contract: success → running + provider registration is
the api layer's job (covered separately); early exit with an OOM signature
→ next ladder rung; timeout/terminal → failed with a user-facing reason.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

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

    def test_ollama_parallel_oom_restarts_with_reduced_environment(self, sandbox):
        from lib.local_serve._plan import plan_launch
        model = {'format': 'gguf', 'path': '/models/test.gguf',
                 'architecture': 'qwen3', 'weight_bytes': 5 << 30,
                 'max_context': 8192, 'served_name': 'test',
                 'attention_dimensions': {'block_count': 32, 'embedding_length': 4096,
                                          'attention.head_count': 32,
                                          'attention.head_count_kv': 8}}
        hardware = {'gpus': [{'index': 0, 'vram_free_bytes': 24 << 30,
                              'vram_total_bytes': 24 << 30}], 'cpu_count': 8}
        plan = plan_launch(model, hardware, engine='ollama')
        row = _record(engine='ollama', degrade=plan['degrade'])
        store.update_fields(row['id'], env=plan['env'])
        environments = []
        def spawn(argv, **kwargs):
            environments.append(kwargs['env'])
            if len(environments) == 1:
                kwargs['stdout'].write(b'CUDA out of memory')
                return FakeProc([1])
            return FakeProc([None])
        result = proc.start_instance(row['id'], popen=spawn,
                                     http_get=_ready_http, sleep=lambda _: None)
        assert result['ok'] and result['degrade_index'] == 1
        assert int(environments[1]['OLLAMA_NUM_PARALLEL']) == (
            int(environments[0]['OLLAMA_NUM_PARALLEL']) // 2)
        assert environments[1]['OLLAMA_CONTEXT_LENGTH'] == environments[0]['OLLAMA_CONTEXT_LENGTH']

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


def test_port_allocation_is_not_limited_to_one_hundred_plans():
    rows = [{'id': str(port), 'port': port, 'status': 'planned'}
            for port in range(18100, 18200)]
    assert proc.allocate_port(ledger_rows=rows, bind_test=False) == 18200
    selected = proc.allocate_port(ledger_rows=rows)
    assert selected not in {row['port'] for row in rows}
    assert selected not in (8000, 30000, 11434)
    assert 1024 <= selected <= 65535


def test_terminal_history_does_not_reserve_ports():
    rows = [{'port': 18100, 'status': 'stopped'}, {'port': 18101, 'status': 'failed'}]
    assert proc.allocate_port(ledger_rows=rows, bind_test=False) == 18100


def test_bound_socket_and_planned_port_are_both_skipped():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        bound = occupied.getsockname()[1]
        selected = proc.allocate_port(ledger_rows=[{'port': bound, 'status': 'planned'}])
        assert selected != bound


def test_kernel_choice_colliding_with_plan_falls_back(monkeypatch):
    import socket
    class Candidate:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def bind(self, address): self.address = address
        def getsockname(self): return ('127.0.0.1', 18100)
    monkeypatch.setattr(socket, 'socket', lambda *args: Candidate())
    assert proc.allocate_port(ledger_rows=[{'port': 18100, 'status': 'planned'}]) == 18101


def test_real_socket_failure_is_not_reported_as_range_exhaustion(monkeypatch):
    import errno
    import socket
    def no_descriptor(*args): raise OSError(errno.EMFILE, 'descriptor unavailable')
    monkeypatch.setattr(socket, 'socket', no_descriptor)
    with pytest.raises(OSError) as failure:
        proc.allocate_port(ledger_rows=[])
    assert failure.value.errno == errno.EMFILE


@pytest.mark.parametrize('engine', ['vllm', 'sglang', 'llamacpp', 'ollama'])
def test_terminal_restart_updates_endpoint_before_spawn(sandbox, monkeypatch, engine):
    row = _record(engine=engine)
    store.update_fields(row['id'], status='stopped')
    monkeypatch.setattr(proc, 'allocate_port', lambda **kwargs: 18234)
    observed = []
    def spawn(argv, **kwargs):
        current = store.get_instance(row['id'])
        assert current['port'] == 18234
        assert current['base_url'] == 'http://127.0.0.1:18234/v1'
        observed.append((argv, kwargs['env']))
        return FakeProc([None])
    result = proc.start_instance(row['id'], popen=spawn, http_get=_ready_http,
                                 sleep=lambda _: None)
    assert result['ok'] and result['status'] == 'running'
    argv, env = observed[0]
    if engine == 'ollama': assert env['OLLAMA_HOST'] == '127.0.0.1:18234'
    else: assert argv[argv.index('--port') + 1] == '18234'


def test_prepare_preserves_real_port_failure(monkeypatch):
    import errno
    from lib.local_serve import api
    monkeypatch.setattr(api.probe, 'inspect_model_path', lambda _: {'format': 'gguf'})
    monkeypatch.setattr(api.probe, 'probe_hardware', lambda: {})
    def unavailable(): raise OSError(errno.EMFILE, 'descriptor unavailable')
    monkeypatch.setattr(proc, 'allocate_port', unavailable)
    result = api.prepare('/synthetic.gguf')
    assert not result['ok'] and result['stage'] == 'plan'
    assert 'descriptor unavailable' in result['error']


def test_port_search_can_wrap_below_preview_default():
    rows = [{'port': port, 'status': 'planned'} for port in range(18100, 65536)]
    assert proc.allocate_port(ledger_rows=rows, bind_test=False) == 1024


def test_terminal_restart_port_failure_preserves_endpoint_and_does_not_spawn(sandbox, monkeypatch):
    import errno
    row = _record()
    store.update_fields(row['id'], status='stopped')
    def unavailable(**kwargs): raise OSError(errno.EMFILE, 'descriptor unavailable')
    monkeypatch.setattr(proc, 'allocate_port', unavailable)
    result = proc.start_instance(row['id'], popen=lambda *a, **k: pytest.fail('must not spawn'))
    assert not result['ok'] and result['status'] == 'failed'
    assert result['port'] == row['port'] and result['base_url'] == row['base_url']
    assert 'descriptor unavailable' in result['last_error']


@pytest.mark.parametrize('engine,graph_flag', [
    ('vllm', '--enforce-eager'), ('sglang', '--disable-cuda-graph'),
])
def test_estimated_overflow_reaches_spawn_and_recovers_from_real_oom(sandbox, engine, graph_flag):
    from lib.local_serve._plan import plan_launch
    inspection = {'format': 'hf', 'path': '/models/M', 'served_name': 'M',
                  'weight_bytes': 24 << 30, 'max_context': 65536}
    hardware = {'gpus': [{'index': 0, 'vram_total_bytes': 24 << 30,
                          'vram_free_bytes': 4 << 30}], 'ram_available_bytes': 64 << 30}
    plan = plan_launch(inspection, hardware, engine=engine)
    assert plan['ok']
    row = _record(engine=engine, degrade=plan['degrade'])
    store.update_fields(row['id'], argv=plan['argv'], env=plan['env'])
    attempts = []
    def spawn(argv, **kwargs):
        attempts.append(argv)
        if len(attempts) == 1:
            kwargs['stdout'].write(b'CUDA out of memory')
            return FakeProc([1])
        return FakeProc([None])
    result = proc.start_instance(row['id'], popen=spawn, http_get=_ready_http,
                                 sleep=lambda _: None)
    assert result['ok'] and result['degrade_index'] == 1
    assert '--cpu-offload-gb' in attempts[0]
    assert graph_flag not in attempts[0] and graph_flag in attempts[1]
    for argv in attempts:
        assert not {'--max-num-seqs', '--max-running-requests', '--max-total-tokens'}.intersection(argv)
