#!/usr/bin/env python3
"""Managed-local installation, ledger, and model-routing v2 hand-off.

Pins: installation without estimated-space admission; the ledger's
complete retention and protected atomic mutations; and the owner-scoped register/unregister round-trip.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from lib.local_serve import _env as env_mod
from lib.local_serve import api as serve_api
from lib.local_serve import _register as register
from lib.local_serve import _store as store
from lib.model_routing import (
    InMemoryModelRoutingRepository,
    OwnerBoundary,
    empty_document,
    upsert_local_provider,
)

pytestmark = pytest.mark.unit


# ─────────────────────────── ledger ───────────────────────────

@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'LEDGER_PATH', str(tmp_path / 'ledger.json'))
    return tmp_path


class TestLedger:
    def test_roundtrip(self, ledger):
        store.upsert_instance({'id': 'ls_a', 'engine': 'vllm',
                               'status': 'planned'})
        row = store.get_instance('ls_a')
        assert row['engine'] == 'vllm'
        store.update_fields('ls_a', status='running', pid=123)
        row = store.get_instance('ls_a')
        assert row['status'] == 'running' and row['pid'] == 123
        assert store.remove_instance('ls_a') is True
        assert store.get_instance('ls_a') is None

    def test_update_preserves_created_at(self, ledger):
        store.upsert_instance({'id': 'ls_a', 'status': 'planned'})
        first = store.get_instance('ls_a')['created_at']
        store.update_fields('ls_a', status='stopped')
        assert store.get_instance('ls_a')['created_at'] == first

    def test_running_and_terminal_records_survive_count_churn(self, ledger):
        for i in range(129):
            store.upsert_instance({'id':f'ls_{i}', 'owner_user_id':1 if i%2 else 2,
                                   'status':('running','stopped','failed')[i%3], 'pid':10000+i})
        rows=store.list_instances()
        assert len(rows)==129
        assert rows[0]['id']=='ls_0' and rows[0]['pid']==10000
        store.update_fields('ls_0',status='stopped')
        assert len(store.list_instances())==129
        assert store.remove_instance('ls_1')
        assert len(store.list_instances())==128
        assert store.get_instance('ls_128')['pid']==10128

    def test_field_patch_preserves_concurrent_status_update(self, ledger, monkeypatch):
        store.upsert_instance({'id':'owned','status':'starting','pid':10})
        original=store.update_json_atomic
        def interleaved(path, mutator, **kwargs):
            def other_write(state):
                state['instances'][0]['status']='running'
                return state
            original(path,other_write,default={'instances':[]},strict=True)
            return original(path,mutator,**kwargs)
        monkeypatch.setattr(store,'update_json_atomic',interleaved)
        patched=store.update_fields('owned',pid=20)
        assert patched['status']=='running' and patched['pid']==20
        assert store.get_instance('owned')['status']=='running'
        assert store.update_fields('missing',pid=30) is None

    @pytest.mark.parametrize('raw',['not-json','[]','{"instances":{}}','{"instances":[{}]}'])
    def test_corrupt_ledger_cannot_be_overwritten(self, ledger, raw):
        from pathlib import Path
        from lib.json_store import JsonStoreReadError
        path=Path(store.LEDGER_PATH);path.write_text(raw)
        with pytest.raises(JsonStoreReadError):
            store.upsert_instance({'id':'new','status':'planned'})
        assert path.read_text()==raw
        with pytest.raises(JsonStoreReadError):
            store.update_fields('new',status='running')
        assert path.read_text()==raw
        with pytest.raises(JsonStoreReadError):
            store.remove_instance('new')
        assert path.read_text()==raw


class TestOwnerBoundary:
    def test_distributed_mode_refuses_host_global_managed_serving(
            self, ledger, monkeypatch):
        monkeypatch.setattr(
            'runtime_guards.load_deployment_configuration',
            lambda: SimpleNamespace(mode='distributed'))
        monkeypatch.setattr(
            serve_api, 'prepare',
            lambda *_args, **_kwargs: pytest.fail(
                'owner refusal must precede model or hardware probing'))

        result = serve_api.create_deployment('/models/m', owner_user_id=1)

        assert result['ok'] is False
        assert result['stage'] == 'owner'
        assert 'personal' in result['error']

    def test_personal_mode_refuses_a_different_owner(
            self, ledger, monkeypatch):
        monkeypatch.setattr(
            'runtime_guards.load_deployment_configuration',
            lambda: SimpleNamespace(mode='personal'))

        result = serve_api.list_deployments(owner_user_id=2)

        assert result['ok'] is False
        assert result['instances'] == []

    def test_personal_mode_stamps_legacy_row_once(self, ledger, monkeypatch):
        monkeypatch.setattr(
            'runtime_guards.load_deployment_configuration',
            lambda: SimpleNamespace(mode='personal'))
        store.upsert_instance({
            'id': 'ls_legacy', 'engine': 'vllm', 'status': 'stopped'})

        result = serve_api.list_deployments(owner_user_id=1)

        assert result['ok'] is True
        assert [row['id'] for row in result['instances']] == ['ls_legacy']
        assert store.get_instance('ls_legacy')['owner_user_id'] == 1

    def test_malformed_ledger_owner_fails_closed(self, ledger, monkeypatch):
        monkeypatch.setattr(
            'runtime_guards.load_deployment_configuration',
            lambda: SimpleNamespace(mode='personal'))
        store.upsert_instance({
            'id': 'ls_bad', 'owner_user_id': 'not-an-id',
            'engine': 'vllm', 'status': 'stopped'})

        result = serve_api.list_deployments(owner_user_id=1)

        assert result['ok'] is True
        assert result['instances'] == []


# ─────────────────────────── env budget ───────────────────────────

@pytest.fixture
def envroot(tmp_path, monkeypatch):
    root = tmp_path / 'serve'
    root.mkdir()
    monkeypatch.setattr(env_mod, 'serve_root', lambda: str(root))
    return root


class TestDiskObservations:
    @pytest.mark.parametrize('free_bytes', [0, 1 << 30, 100 << 30])
    def test_space_estimates_do_not_refuse_installation(self, envroot, free_bytes):
        result = env_mod.check_disk_budget('vllm', disk_free=free_bytes)
        assert result['ok'] and result['free_bytes'] == free_bytes

    def test_retired_budget_and_directory_size_do_not_gate(self, envroot, monkeypatch):
        monkeypatch.setenv('TOFU_LOCAL_SERVE_BUDGET_GB', '1')
        with open(envroot / 'existing', 'wb') as output:
            output.truncate(21 << 30)
        assert env_mod.check_disk_budget('sglang', disk_free=0)['ok']

    def test_failed_probe_is_diagnostic(self, envroot, monkeypatch):
        def unavailable(*args):
            raise OSError('probe unavailable')
        monkeypatch.setattr(env_mod.shutil, 'disk_usage', unavailable)
        result = env_mod.check_disk_budget('vllm')
        assert result['ok'] and result['free_bytes'] is None
        assert result['disk_probe_error'] == 'probe unavailable'

    def test_unknown_engine(self, envroot):
        assert not env_mod.check_disk_budget('nope')['ok']


class TestStreamingDownload:
    @pytest.mark.parametrize('failure', [None, 'read', 'write'])
    def test_large_transfer_and_real_io_failure(self, monkeypatch, failure):
        from contextlib import nullcontext
        chunk = b'x' * (1 << 20)
        read_count = 0
        written = 0
        def read(size):
            nonlocal read_count
            assert size == len(chunk)
            read_count += 1
            if failure == 'read' and read_count == 3:
                raise OSError('network read failed')
            return chunk if read_count <= 4097 else b''
        def write(data):
            nonlocal written
            if failure == 'write' and written == 2 * len(chunk):
                raise OSError('No space left on device')
            written += len(data)
        monkeypatch.setattr(env_mod.urllib.request, 'urlopen',
                            lambda *a, **k: nullcontext(SimpleNamespace(read=read)))
        monkeypatch.setattr(env_mod, 'open',
                            lambda *a, **k: nullcontext(SimpleNamespace(write=write)),
                            raising=False)
        if failure:
            with pytest.raises(OSError):
                env_mod._fetch('https://example.invalid/engine', '/unused')
            assert written == 2 * len(chunk)
        else:
            env_mod._fetch('https://example.invalid/engine', '/unused')
            assert written == 4097 * len(chunk)


class TestEnsureEngine:
    def test_short_circuit_when_installed(self, envroot, monkeypatch):
        monkeypatch.setattr(env_mod, 'engine_status',
                            lambda e: {'installed': True, 'binary': '/b',
                                       'system': False})
        r = env_mod.ensure_engine('vllm')
        assert r['ok'] and r['installed'] is False  # no work was needed

    def test_unknown_engine_precedes_install(self, envroot, monkeypatch):
        monkeypatch.setattr(env_mod, 'engine_status',
                            lambda e: pytest.fail('invalid engine must not install'))
        assert not env_mod.ensure_engine('unknown')['ok']

    def test_actual_install_failure_is_reported(self, envroot, monkeypatch):
        monkeypatch.setattr(env_mod, 'engine_status', lambda e: {'installed': False})
        def full(*args):
            raise OSError('No space left on device')
        monkeypatch.setattr(env_mod, '_install_uv_env', full)
        result = env_mod.ensure_engine('vllm')
        assert not result['ok'] and 'No space left on device' in result['error']

    def test_uv_install_flow(self, envroot, monkeypatch):
        states = iter([{'installed': False},
                       {'installed': True, 'binary': '/env/bin/vllm',
                        'system': False}])
        monkeypatch.setattr(env_mod, 'engine_status', lambda e: next(states))
        monkeypatch.setattr(env_mod, 'check_disk_budget',
                            lambda e: pytest.fail('observations must not gate installation'))
        seen = []

        class R:
            returncode = 0
            stderr = ''

        monkeypatch.setattr(env_mod, '_ensure_uv', lambda runner: '/usr/bin/uv')
        monkeypatch.setattr(env_mod, '_install_uv_env',
                            lambda engine, runner, log:
                            (seen.append(engine), {'ok': True})[1])
        r = env_mod.ensure_engine('vllm', runner=lambda *a, **k: R())
        assert r['ok'] and r['binary'] == '/env/bin/vllm'
        assert seen == ['vllm']


# ─────────────────────────── register ───────────────────────────

@pytest.fixture
def routing():
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(1)
    repository.compare_and_swap(
        boundary, empty_document(), expected_revision=0)
    upsert_local_provider(
        repository,
        boundary,
        provider_id='user_made',
        display_name='User made',
        base_url='http://127.0.0.1:9000/v1',
        models=[{'model_id': 'user-model', 'capabilities': ['text']}],
    )
    return repository, boundary


def _record():
    return {'id': 'ls_vllm_m', 'engine': 'vllm', 'served_name': 'M',
            'port': 18100, 'base_url': 'http://127.0.0.1:18100/v1',
            'owner_user_id': 1}


class TestRegister:
    def test_register_persists_managed_provider(self, routing):
        repository, boundary = routing
        r = register.register_instance(
            _record(),
            discover=lambda url: ([{'model_id': 'M'}], url),
            rebuild=lambda: None,
            repository=repository)
        assert r['ok'] and r['provider_id'] == 'managed_vllm_18100'
        document = repository.get(boundary).document
        provs = {p['provider_id']: p for p in document['providers']}
        assert 'managed_vllm_18100' in provs
        assert provs['managed_vllm_18100']['scope'] == 'owner'
        offering = next(
            row for row in document['offerings']
            if row.get('pending_model_id') == 'M')
        assert offering['identity_state'] == 'pending_identity'
        assert 'user_made' in provs  # untouched

    def test_register_replaces_not_duplicates(self, routing):
        repository, boundary = routing
        for _ in range(2):
            register.register_instance(
                _record(),
                discover=lambda url: ([{'model_id': 'M'}], url),
                rebuild=lambda: None,
                repository=repository)
        ids = [
            p['provider_id']
            for p in repository.get(boundary).document['providers']]
        assert ids.count('managed_vllm_18100') == 1

    def test_empty_model_list_refused(self, routing):
        repository, _boundary = routing
        r = register.register_instance(
            _record(), discover=lambda url: ([], url), rebuild=lambda: None,
            repository=repository)
        assert not r['ok']

    def test_unregister_only_managed_rows(self, routing):
        repository, boundary = routing
        register.register_instance(
            _record(),
            discover=lambda url: ([{'model_id': 'M'}], url),
            rebuild=lambda: None,
            repository=repository)
        r = register.unregister_instance(
            _record(), rebuild=lambda: None, repository=repository)
        assert r['ok'] and r['provider_id'] == 'managed_vllm_18100'
        ids = [
            p['provider_id']
            for p in repository.get(boundary).document['providers']]
        assert 'managed_vllm_18100' not in ids
        assert 'user_made' in ids
