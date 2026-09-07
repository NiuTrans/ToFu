#!/usr/bin/env python3
"""Managed-local env budget, ledger, and model-routing v2 hand-off.

Pins: the 20 GiB serve budget + free-space precheck; the ledger's
upsert/eviction semantics; and the owner-scoped register/unregister round-trip.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

    def test_eviction_prefers_terminal_rows(self, ledger):
        for i in range(40):
            store.upsert_instance({'id': 'ls_%d' % i,
                                   'status': 'failed' if i % 2 else 'running'})
        rows = store.list_instances()
        assert len(rows) <= 32
        assert all(r['status'] == 'running' for r in rows[:0] or [])
        running = [r for r in rows if r['status'] == 'running']
        assert len(running) == 20       # running rows are never evicted


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


class TestDiskBudget:
    def test_ok(self, envroot):
        r = env_mod.check_disk_budget('llamacpp',
                                      disk_free=100 * (1 << 30))
        assert r['ok'] and r['budget_bytes'] == 20 * (1 << 30)

    def test_insufficient_free(self, envroot):
        r = env_mod.check_disk_budget('vllm', disk_free=1 * (1 << 30))
        assert not r['ok'] and '磁盘余量不足' in r['error']

    def test_budget_overflow(self, envroot, monkeypatch):
        monkeypatch.setattr(env_mod, '_tree_bytes',
                            lambda p: 19 * (1 << 30))
        r = env_mod.check_disk_budget('sglang', disk_free=500 * (1 << 30))
        assert not r['ok'] and '预算' in r['error']
        assert 'TOFU_LOCAL_SERVE_BUDGET_GB' in r['error']

    def test_unknown_engine(self, envroot):
        assert not env_mod.check_disk_budget('nope')['ok']


class TestEnsureEngine:
    def test_short_circuit_when_installed(self, envroot, monkeypatch):
        monkeypatch.setattr(env_mod, 'engine_status',
                            lambda e: {'installed': True, 'binary': '/b',
                                       'system': False})
        r = env_mod.ensure_engine('vllm')
        assert r['ok'] and r['installed'] is False  # no work was needed

    def test_budget_refusal_precedes_install(self, envroot, monkeypatch):
        called = []
        monkeypatch.setattr(env_mod, 'engine_status',
                            lambda e: {'installed': False})
        monkeypatch.setattr(env_mod, 'check_disk_budget',
                            lambda e: {'ok': False, 'error': '预算不足'})
        monkeypatch.setattr(env_mod, '_install_uv_env',
                            lambda *a, **k: called.append(1) or {'ok': True})
        r = env_mod.ensure_engine('vllm')
        assert not r['ok'] and not called

    def test_uv_install_flow(self, envroot, monkeypatch):
        states = iter([{'installed': False},
                       {'installed': True, 'binary': '/env/bin/vllm',
                        'system': False}])
        monkeypatch.setattr(env_mod, 'engine_status', lambda e: next(states))
        monkeypatch.setattr(env_mod, 'check_disk_budget',
                            lambda e: {'ok': True})
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
