"""Owner-scoped learned route health (credential-source outage memory).

``owner_health_registry`` gives each explicit (owner, tenant) its own bounded,
process-local ``RouteHealthRegistry``. A credential-source outage is a
credential-scoped fact; one owner's learned outage never steers another
owner's or tenant's routing.
"""
from types import SimpleNamespace

import pytest

from lib.model_routing import health as route_health
from lib.model_routing.health import (
    CREDENTIAL_SOURCE_UNAVAILABLE,
    HealthTarget,
    RouteHealthRegistry,
    classify_failure,
    existing_owner_health_registry,
    owner_health_learned,
    owner_health_registry,
)

pytestmark = pytest.mark.unit


def test_credential_source_outage_is_credential_scoped():
    assert classify_failure(kind=CREDENTIAL_SOURCE_UNAVAILABLE) == 'credential'
    candidate = SimpleNamespace(
        credential={'credential_id': 'cred-a'},
        deployment={'deployment_id': 'dep-a'},
        connection={'connection_id': 'conn-a'})
    assert RouteHealthRegistry.target_for_failure(
        candidate, kind=CREDENTIAL_SOURCE_UNAVAILABLE) == HealthTarget('credential', 'cred-a')


def test_owner_tables_are_isolated_by_owner_and_tenant():
    assert existing_owner_health_registry(1, 'tenant-a') is None
    assert not owner_health_learned(), 'read-only probes must not allocate tables'
    first = owner_health_registry(1, 'tenant-a')
    assert owner_health_registry(1, ' tenant-a ') is first
    target = HealthTarget('credential', 'cred-same-id')
    first.record_failure(target, reason=CREDENTIAL_SOURCE_UNAVAILABLE)
    assert first.target_unavailable(target)
    for owner, tenant in ((2, 'tenant-a'), (1, 'tenant-b'), (1, '')):
        assert not owner_health_registry(owner, tenant).target_unavailable(target)
    assert existing_owner_health_registry(1, 'tenant-a') is first


@pytest.mark.parametrize('owner', [0, -3, None, True, 'owner'])
def test_owner_identity_is_explicit_and_positive(owner):
    with pytest.raises(ValueError):
        owner_health_registry(owner, '')


def test_owner_count_is_lru_bounded(monkeypatch):
    monkeypatch.setattr(route_health, 'MAX_OWNER_HEALTH_REGISTRIES', 2)
    first = owner_health_registry(1)
    owner_health_registry(2)
    assert owner_health_registry(1) is first
    owner_health_registry(3)
    assert existing_owner_health_registry(2) is None, 'least recently used owner is dropped'
    assert existing_owner_health_registry(1) is first


def test_observations_inside_one_cooldown_are_one_outage():
    """Retries, re-admitted demoted routes and concurrent requests observe one
    outage; only a failure after its cooldown backs off longer."""
    now = [100.0]
    registry = RouteHealthRegistry(clock=lambda: now[0])
    target = HealthTarget('credential', 'cred-a')
    registry.record_failure(target, reason=CREDENTIAL_SOURCE_UNAVAILABLE)
    now[0] += 200
    registry.record_failure(target, reason=CREDENTIAL_SOURCE_UNAVAILABLE)
    [entry] = registry.snapshot()['entries']
    assert entry['failures'] == 1
    assert entry['cooldown_remaining'] == pytest.approx(100.0), 'neither escalated nor extended'

    now[0] += 101
    registry.record_failure(target, reason=CREDENTIAL_SOURCE_UNAVAILABLE)
    [entry] = registry.snapshot()['entries']
    assert entry['failures'] == 2
    assert entry['cooldown_remaining'] == pytest.approx(600.0)


def test_credential_cooldown_expires_backs_off_and_clears():
    now = [100.0]
    registry = RouteHealthRegistry(clock=lambda: now[0])
    target = HealthTarget('credential', 'cred-a')
    registry.record_failure(target, reason=CREDENTIAL_SOURCE_UNAVAILABLE)
    assert registry.target_unavailable(target)
    now[0] += 301
    assert not registry.target_unavailable(target), 'the cooldown is bounded'
    registry.record_failure(target, reason=CREDENTIAL_SOURCE_UNAVAILABLE)
    now[0] += 301
    assert registry.target_unavailable(target), 'a repeated outage backs off longer'
    registry.clear_target(target)
    assert not registry.target_unavailable(target)
