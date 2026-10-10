#!/usr/bin/env python3
"""tests/providers/test_provider_catalog_sync.py — remote provider catalog sync.

The catalog-sync worker (lib/llm_dispatch/provider_catalog_sync.py)
periodically re-lists every configured remote provider's OpenAI-compatible
/models with the stored credential and folds NEW wire identities into the
owner-scoped model-routing v2 aggregate as pending offerings. This is what
keeps a configured provider current when the vendor ships a new model (the
"Gemini 3.8 Flash is already out" gap) without a template edit.

Owner-ratified semantics:

  * Add-only: existing rows (enabled state, confirmed identities) are never
    modified; nothing is ever deleted.
  * Idempotent: ids already served in ANY form (confirmed, pending, wire)
    are skipped; a second pass adds nothing.
  * No zombies: a synced model whose offering the user deletes moves to the
    dismissed ledger and is never re-added. Identity CONFIRMATION
    (pending → confirmed) is not a deletion.
  * Bounded: aggregator-scale catalogs are skipped wholesale; one pass adds
    at most _MAX_ADDITIONS_PER_PROVIDER models per access.
  * Fail-closed: one failing endpoint never masks the others.
  * Local endpoints and non-api_key credentials are never targeted (the
    local sweep owns loopback; oauth rows have no Bearer key).
  * Opt-out: TOFU_MODEL_CATALOG_SYNC=0 → sweep is a no-op.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.llm_dispatch import provider_catalog_sync as cs
from lib.model_routing import (
    InMemoryModelRoutingRepository,
    OwnerBoundary,
)

from tests.support.standalone_model_routing import (
    standalone_model_routing_envelope,
)

pytestmark = pytest.mark.unit

BASE_URL = 'https://models.example/v1'


def _discover_rows(*ids):
    return [{'model_id': mid, 'capabilities': ['text']} for mid in ids]


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Owner authority with ONE remote api_key access; stubbed slot rebuild."""
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(1)
    envelope = standalone_model_routing_envelope(base_url=BASE_URL)
    for reference, plaintext in envelope['credential_secrets'].items():
        repository.put_secret(boundary, plaintext, secret_reference=reference)
    repository.compare_and_swap(
        boundary, envelope['model_routing'], expected_revision=0)
    monkeypatch.setattr(cs, '_runtime_context', lambda: (boundary, repository))
    monkeypatch.setattr(cs, '_STATE_PATH', str(tmp_path / 'catalog_sync.json'))
    # conftest setdefault-disables the worker for the suite; opt back in.
    monkeypatch.delenv('TOFU_MODEL_CATALOG_SYNC', raising=False)

    import lib.llm_dispatch.health_local as health_local
    rebuilds = []
    monkeypatch.setattr(
        health_local, '_rebuild_dispatcher_slots',
        lambda: rebuilds.append(True))
    return {
        'repository': repository,
        'boundary': boundary,
        'state': tmp_path / 'catalog_sync.json',
        'rebuilds': rebuilds,
    }


def _document(sandbox):
    return sandbox['repository'].get(sandbox['boundary']).document


def _access_rows(sandbox):
    document = _document(sandbox)
    access = document['provider_accesses'][0]
    access_id = access['provider_access_id']
    offerings = [
        row for row in document['offerings']
        if row['provider_access_id'] == access_id]
    deployments = [
        row for row in document['deployments']
        if row['offering_id'] in {o['offering_id'] for o in offerings}]
    return document, access, offerings, deployments


def _add_second_access(sandbox, *, provider_id='provider-b',
                       base_url='https://other.example/v1'):
    """Append a second remote access with its own credential + secret."""
    repository = sandbox['repository']
    boundary = sandbox['boundary']

    def _mutate(working):
        access_id = f'{provider_id}-access'
        connection_id = f'{provider_id}-connection'
        working['providers'].append({
            'provider_id': provider_id, 'name': provider_id, 'scope': 'owner',
        })
        working['provider_accesses'].append({
            'provider_access_id': access_id,
            'provider_id': provider_id,
            'enabled': True,
            'quota_policy': {},
        })
        working['connections'].append({
            'connection_id': connection_id,
            'provider_access_id': access_id,
            'base_url': base_url,
            'protocol': 'openai',
            'enabled': True,
            'priority': 0,
            'extra_headers': {},
        })
        working['credentials'].append({
            'credential_id': f'{provider_id}-credential',
            'provider_access_id': access_id,
            'kind': 'api_key',
            'secret_reference': f'{provider_id}-secret',
            'key_hint': 'configured',
            'enabled': True,
            'authorization': {'connection_ids': [connection_id], 'models': []},
            'quota_policy': {},
        })
        return working

    repository.mutate(boundary, _mutate)
    repository.put_secret(
        boundary, 'sk-second', secret_reference=f'{provider_id}-secret')


@pytest.mark.unit
class TestCatalogSync:

    def test_new_remote_model_added_as_pending_offering(self, sandbox):
        stats = cs.sweep_once(
            discover=lambda _url, _key: _discover_rows('model-a', 'model-b'))
        assert len(stats['added']) == 1
        assert stats['added'][0]['models'] == ['model-b']
        _doc, _access, offerings, deployments = _access_rows(sandbox)
        by_id = {
            row.get('pending_model_id')
            or (row.get('model') or {}).get('model_id'): row
            for row in offerings
        }
        added = by_id['model-b']
        assert added['identity_state'] == 'pending_identity'
        assert added['enabled'] is True
        assert added['offering_id'].startswith('csync-offering-')
        wire = next(
            row for row in deployments
            if row['offering_id'] == added['offering_id'])
        assert wire['wire_model_id'] == 'model-b'
        assert wire['identity_confidence'] == 'pending'
        assert wire['probe_status'] == 'unprobed'
        assert 'enabled' not in wire
        # The pre-existing confirmed offering is untouched.
        assert by_id['model-a']['identity_state'] == 'confirmed'
        assert sandbox['rebuilds'] == [True]

    def test_disabled_access_still_synced(self, sandbox):
        # The access switch gates routing, not catalog freshness: a
        # re-enabled provider must never reveal a stale model list.
        def _disable(working):
            working['provider_accesses'][0]['enabled'] = False
            return working
        sandbox['repository'].mutate(sandbox['boundary'], _disable)
        stats = cs.sweep_once(
            discover=lambda _url, _key: _discover_rows('model-a', 'model-b'))
        assert len(stats['added']) == 1
        assert stats['added'][0]['models'] == ['model-b']
        _doc, access, _offerings, _dep = _access_rows(sandbox)
        assert access['enabled'] is False

    def test_known_ids_in_any_form_never_duplicated(self, sandbox):
        # 'model-a' is confirmed, 'wire/model-a' is its wire id: both known.
        first = cs.sweep_once(
            discover=lambda _url, _key: _discover_rows(
                'model-a', 'wire/model-a', 'model-b'))
        assert first['added'][0]['models'] == ['model-b']
        second = cs.sweep_once(
            discover=lambda _url, _key: _discover_rows(
                'model-a', 'wire/model-a', 'model-b'))
        assert second['added'] == []
        assert len(second['unchanged']) == 1
        _doc, _access, offerings, _dep = _access_rows(sandbox)
        assert len(offerings) == 2

    def test_deleted_synced_model_is_dismissed_never_resurrects(self, sandbox):
        cs.sweep_once(
            discover=lambda _url, _key: _discover_rows('model-b'))
        # User deletes the synced offering in Settings.
        def _delete(working):
            doomed = {
                row['offering_id'] for row in working['offerings']
                if row.get('pending_model_id') == 'model-b'}
            working['offerings'] = [
                row for row in working['offerings']
                if row['offering_id'] not in doomed]
            working['deployments'] = [
                row for row in working['deployments']
                if row['offering_id'] not in doomed]
            return working
        sandbox['repository'].mutate(sandbox['boundary'], _delete)

        stats = cs.sweep_once(
            discover=lambda _url, _key: _discover_rows('model-b'))
        assert stats['added'] == []
        _doc, _access, offerings, _dep = _access_rows(sandbox)
        assert [row.get('pending_model_id') for row in offerings] == [None]
        state = json.loads(sandbox['state'].read_text())
        owner_state = state['owners'][':1']
        assert 'model-b' in owner_state['dismissed']['provider-a-access']
        assert owner_state['added'].get('provider-a-access') in (None, [])

    def test_identity_confirmation_is_not_a_deletion(self, sandbox):
        cs.sweep_once(
            discover=lambda _url, _key: _discover_rows('model-b'))
        # User confirms the pending identity (pending → confirmed).
        def _confirm(working):
            for row in working['offerings']:
                if row.get('pending_model_id') == 'model-b':
                    row['identity_state'] = 'confirmed'
                    row['model'] = {
                        'creator_id': 'test-creator', 'model_id': 'model-b'}
                    del row['pending_model_id']
            confirmed = {
                row['offering_id'] for row in working['offerings']
                if row.get('identity_state') == 'confirmed'}
            for row in working['deployments']:
                if row['offering_id'] in confirmed:
                    row['identity_confidence'] = 'high'
            working['models'].append({
                'creator_id': 'test-creator',
                'model_id': 'model-b',
                'display_name': 'model-b',
                'capabilities': ['text'],
                'context_window': 32_768,
                'quality_rank': 10,
            })
            return working
        sandbox['repository'].mutate(sandbox['boundary'], _confirm)

        stats = cs.sweep_once(
            discover=lambda _url, _key: _discover_rows('model-b'))
        assert stats['added'] == []
        state = json.loads(sandbox['state'].read_text())
        owner_state = state['owners'][':1']
        assert owner_state['dismissed'].get('provider-a-access') in (None, [])
        assert 'model-b' in owner_state['added']['provider-a-access']

    def test_disabled_offering_never_modified(self, sandbox):
        def _disable(working):
            working['offerings'][0]['enabled'] = False
            return working
        sandbox['repository'].mutate(sandbox['boundary'], _disable)
        cs.sweep_once(
            discover=lambda _url, _key: _discover_rows('model-b'))
        _doc, _access, offerings, _dep = _access_rows(sandbox)
        original = next(
            row for row in offerings
            if (row.get('model') or {}).get('model_id') == 'model-a')
        assert original['enabled'] is False

    def test_aggregator_scale_catalog_skipped(self, sandbox, monkeypatch):
        monkeypatch.setattr(cs, '_MAX_CATALOG_MODELS', 3)
        stats = cs.sweep_once(
            discover=lambda _url, _key: _discover_rows(
                'm1', 'm2', 'm3', 'm4'))
        assert stats['added'] == []
        assert len(stats['skipped_aggregator']) == 1
        _doc, _access, offerings, _dep = _access_rows(sandbox)
        assert len(offerings) == 1

    def test_additions_capped_per_pass(self, sandbox, monkeypatch):
        monkeypatch.setattr(cs, '_MAX_ADDITIONS_PER_PROVIDER', 2)
        stats = cs.sweep_once(
            discover=lambda _url, _key: _discover_rows(
                'm1', 'm2', 'm3', 'm4'))
        assert stats['added'][0]['models'] == ['m1', 'm2']
        assert stats['truncated']
        # The remainder arrives on the next pass.
        stats = cs.sweep_once(
            discover=lambda _url, _key: _discover_rows(
                'm1', 'm2', 'm3', 'm4'))
        assert stats['added'][0]['models'] == ['m3', 'm4']

    def test_local_endpoints_belong_to_the_local_sweep(self, sandbox):
        called = []
        _add_second_access(sandbox, base_url='http://127.0.0.1:8000/v1')

        def discover(url, _key):
            called.append(url)
            return _discover_rows('remote-new')

        stats = cs.sweep_once(discover=discover)
        assert called == [BASE_URL]
        assert stats['added'][0]['models'] == ['remote-new']

    def test_oauth_credentials_are_not_used_for_listing(self, sandbox):
        def _to_oauth(working):
            working['credentials'][0]['kind'] = 'oauth'
            return working
        sandbox['repository'].mutate(sandbox['boundary'], _to_oauth)
        stats = cs.sweep_once(
            discover=lambda _url, _key: _discover_rows('model-b'))
        assert stats['scanned'] == 0
        assert stats['added'] == []

    def test_missing_secret_skips_provider_without_failure(self, sandbox):
        def _break_ref(working):
            working['credentials'][0]['secret_reference'] = 'no-such-ref'
            return working
        sandbox['repository'].mutate(sandbox['boundary'], _break_ref)
        stats = cs.sweep_once(
            discover=lambda _url, _key: _discover_rows('model-b'))
        assert stats['skipped_no_key'] == ['provider-a-access']
        assert stats['added'] == [] and stats['failed'] == []

    def test_one_failing_provider_does_not_mask_others(self, sandbox):
        _add_second_access(sandbox)

        def discover(url, _key):
            if 'other' in url:
                raise RuntimeError('boom')
            return _discover_rows('model-b')

        stats = cs.sweep_once(discover=discover)
        assert stats['failed'] == ['provider-b-access']
        assert stats['added'][0]['access_id'] == 'provider-a-access'

    def test_opt_out_env_makes_sweep_noop(self, sandbox, monkeypatch):
        monkeypatch.setenv('TOFU_MODEL_CATALOG_SYNC', '0')
        stats = cs.sweep_once(
            discover=lambda _url, _key: _discover_rows('model-b'))
        assert stats.get('disabled') is True
        _doc, _access, offerings, _dep = _access_rows(sandbox)
        assert len(offerings) == 1


@pytest.fixture()
def poll_sandbox(sandbox, monkeypatch):
    """Deterministic clock + reset scheduler for polling tests."""
    monkeypatch.setattr(cs, '_BOOT_DELAY', 0.0)
    monkeypatch.setattr(cs, '_SYNC_INTERVAL', 100.0)
    monkeypatch.setattr(cs, '_runtime_enabled', True)
    monkeypatch.setattr(cs, '_next_sync_at', None)
    monkeypatch.setattr(cs, '_force_sync', False)
    monkeypatch.setattr(cs, '_schedule_generation', 0)
    monkeypatch.setattr(cs, '_failure_counts', {})
    return sandbox


@pytest.mark.unit
def test_poll_runs_on_interval_and_trigger_forces(poll_sandbox, monkeypatch):
    import lib.llm_dispatch.health_local as health_local

    calls = []

    def discover(_url, _key):
        calls.append(True)
        return _discover_rows('model-b')

    at_0 = cs.poll_if_due(now=0, discover=discover)
    assert at_0['scheduled'] is True and len(calls) == 1
    early = cs.poll_if_due(now=1, discover=discover)
    assert early['scheduled'] is False and len(calls) == 1

    wakes = []
    monkeypatch.setattr(
        health_local, 'wake_local_health_checker',
        lambda: wakes.append(True) or True)
    assert cs.trigger_provider_catalog_sync() is True
    forced = cs.poll_if_due(now=2, discover=discover)
    assert forced['scheduled'] is True and len(calls) == 2
    assert wakes == [True]

    later = cs.poll_if_due(now=102, discover=discover)
    assert later['scheduled'] is True and len(calls) == 3


@pytest.mark.unit
def test_poll_opt_out_and_stopped_are_inactive(poll_sandbox, monkeypatch):
    monkeypatch.setenv('TOFU_MODEL_CATALOG_SYNC', '0')
    assert cs.poll_if_due(now=0).get('disabled') is True
    monkeypatch.delenv('TOFU_MODEL_CATALOG_SYNC')
    monkeypatch.setattr(cs, '_runtime_enabled', False)
    assert cs.poll_if_due(now=0).get('stopped') is True


@pytest.mark.unit
def test_health_monitor_executes_cooperative_catalog_sync(monkeypatch):
    import threading
    import lib.llm_dispatch.health_local as health_local

    called = threading.Event()
    monkeypatch.setattr(
        cs, 'poll_if_due',
        lambda **_kwargs: called.set() or {
            'scheduled': False, 'next_poll_s': 60.0})

    assert health_local.start_local_health_checker() is True
    try:
        assert called.wait(1.0), 'shared monitor never scheduled catalog sync'
        assert health_local._thread.name == 'local-endpoint-monitor'
    finally:
        assert health_local.stop_local_health_checker(timeout=1.0) is True
