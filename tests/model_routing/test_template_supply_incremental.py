#!/usr/bin/env python3
"""tests/model_routing/test_template_supply_incremental.py — one card, converged on demand.

WHY
===
``sync_template_supply`` is probe-gated: it adds a recipe only when the
recipe's identity is ABSENT from the card. A card that already owns
``gpt-6.1-sol`` is therefore never inspected again, so a recipe that later
gains a face-pinned wire id (``request_faces`` -> aigc's kunlun alias) never
reaches the install. The matrix then shows only the pre-existing cells, all of
which fail on the public id's RPM wall, and the operator reasonably concludes
the alias "does not work" — while the alias is the one wire that does.

Two levers are pinned here:

  * ``backfill_only`` — the deterministic convergence (face Connection +
    alias Deployment) without consuming a probe;
  * ``provider_access_ids`` — scoped to ONE card, so pressing Detect on a row
    cannot rewrite or re-probe the owner's other providers.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/model_routing/test_template_supply_incremental.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from lib.model_routing import OwnerBoundary, empty_document
from lib.model_routing.repository import InMemoryModelRoutingRepository
from lib.model_routing.template_supply import sync_template_supply
from lib.provider.provider_template_recipes import (
    ProviderTemplateRecipeError,
    compile_provider_template_bundle,
)

try:
    compile_provider_template_bundle('meituan',
                                     selected_model_ids=['gpt-6.1-sol'])
except ProviderTemplateRecipeError:
    pytest.skip('meituan provider template is not shipped in this build',
                allow_module_level=True)

pytestmark = pytest.mark.unit

_OWNER = 246813579
_ALIAS = 'kunlunbedrock.gpt-6.1-sol-m17'


def _seed_card_without_the_new_face(repository, boundary):
    """An OLD aigc card: owns gpt-6.1-sol, predates the kunlun face."""
    bundle = compile_provider_template_bundle(
        'meituan', selected_model_ids=['gpt-6.1-sol'])
    document = empty_document(revision=0)
    document['providers'].append(bundle['provider'])
    document['provider_accesses'].append(bundle['provider_access'])
    alias_connections = {
        row['connection_id'] for row in bundle['connections']
        if row.get('gateway_namespace') == 'kunlun'}
    assert alias_connections, 'template must declare the kunlun face'
    document['connections'].extend(
        row for row in bundle['connections']
        if row['connection_id'] not in alias_connections)
    credential = dict(bundle['credentials'][0])
    secret = repository.put_secret(boundary, 'sk-test')
    credential['secret_reference'] = secret['secret_reference']
    credential['key_hint'] = secret['key_hint']
    credential['authorization']['connection_ids'] = [
        value for value in credential['authorization']['connection_ids']
        if value not in alias_connections]
    document['credentials'].append(credential)
    document['offerings'].extend(bundle['offerings'])
    document['deployments'].extend(
        row for row in bundle['deployments']
        if row['wire_model_id'] != _ALIAS)
    document['creators'].extend(bundle['creators'])
    document['models'].extend(bundle['models'])
    repository.compare_and_swap(boundary, document, expected_revision=0)
    return bundle


def _never_probe(**kwargs):
    raise AssertionError(
        'backfill_only must not consume a live probe: %r' % (kwargs,))


def test_backfill_only_adds_the_face_and_alias_without_probing():
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(_OWNER)
    bundle = _seed_card_without_the_new_face(repository, boundary)
    access_id = bundle['provider_access']['provider_access_id']

    stats = sync_template_supply(
        boundary=boundary, repository=repository, prober=_never_probe,
        provider_access_ids=[access_id], backfill_only=True)

    assert stats['added_connections'] == 1, stats
    assert stats['added_deployments'] == 1, stats

    document = repository.get(boundary).document
    connections = {row['connection_id']: row
                   for row in document['connections']}
    kunlun = [row for row in connections.values()
              if row.get('gateway_namespace') == 'kunlun']
    assert len(kunlun) == 1, connections
    assert kunlun[0]['extra_headers'] == {'x-context-provider': 'kunlun'}
    assert kunlun[0]['protocol'] == 'openai_responses'

    deployments = [row for row in document['deployments']
                   if row['wire_model_id'] == _ALIAS]
    assert len(deployments) == 1, deployments
    assert deployments[0]['connection_id'] == kunlun[0]['connection_id']

    credential = next(row for row in document['credentials']
                      if row['provider_access_id'] == access_id)
    assert kunlun[0]['connection_id'] in credential['authorization'][
        'connection_ids']


def test_backfill_only_is_idempotent():
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(_OWNER)
    bundle = _seed_card_without_the_new_face(repository, boundary)
    access_id = bundle['provider_access']['provider_access_id']

    first = sync_template_supply(
        boundary=boundary, repository=repository, prober=_never_probe,
        provider_access_ids=[access_id], backfill_only=True)
    revision = repository.get(boundary).revision
    second = sync_template_supply(
        boundary=boundary, repository=repository, prober=_never_probe,
        provider_access_ids=[access_id], backfill_only=True)

    assert first['added_deployments'] == 1
    assert second['added_deployments'] == 0, second
    assert second['added_connections'] == 0, second
    assert repository.get(boundary).revision == revision


def test_a_face_pinned_alias_on_the_wrong_connection_is_retargeted():
    """Adding the request name by hand must not be enough to break it.

    The panel used to bind a new request name to the card's first enabled
    Connection. For a face-pinned wire (aigc kunlun) that Connection answers
    400 ``配置不存在``, so the operator added the same name repeatedly and it
    never worked. Convergence must move that row onto the face — and grant
    the face to the credentials, or dispatch still refuses it.
    """
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(_OWNER)
    bundle = _seed_card_without_the_new_face(repository, boundary)
    access_id = bundle['provider_access']['provider_access_id']
    default_connection = next(
        row['connection_id'] for row in bundle['connections']
        if row.get('gateway_namespace') != 'kunlun')

    def _misroute(document):
        offering = next(row for row in document['offerings']
                        if (row.get('model') or {}).get('model_id') == 'gpt-6.1-sol')
        document['deployments'].append({
            'deployment_id': 'dep-wrong-face',
            'offering_id': offering['offering_id'],
            'connection_id': default_connection,
            'wire_model_id': _ALIAS,
            'identity_confidence': 'high',
            'probe_status': 'failed',
            'priority': 100,
        })
        return document

    repository.mutate(boundary, _misroute)
    stats = sync_template_supply(
        boundary=boundary, repository=repository, prober=_never_probe,
        provider_access_ids=[access_id], backfill_only=True)

    assert stats['repaired_deployments'] == 1, stats
    assert stats['added_deployments'] == 0, stats
    document = repository.get(boundary).document
    face = next(row for row in document['connections']
                if row.get('gateway_namespace') == 'kunlun')
    aliases = [row for row in document['deployments']
               if row['wire_model_id'] == _ALIAS]
    assert len(aliases) == 1, aliases
    assert aliases[0]['connection_id'] == face['connection_id']
    # Stale evidence from the wrong endpoint must not survive the correction.
    assert aliases[0]['probe_status'] == 'unprobed'
    credential = next(row for row in document['credentials']
                      if row['provider_access_id'] == access_id)
    assert face['connection_id'] in credential['authorization'][
        'connection_ids']

    rerun = sync_template_supply(
        boundary=boundary, repository=repository, prober=_never_probe,
        provider_access_ids=[access_id], backfill_only=True)
    assert rerun['repaired_deployments'] == 0, rerun


def test_an_unrelated_card_scope_leaves_the_card_untouched():
    """Pressing Detect on one provider must not rewrite another."""
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(_OWNER)
    _seed_card_without_the_new_face(repository, boundary)

    stats = sync_template_supply(
        boundary=boundary, repository=repository, prober=_never_probe,
        provider_access_ids=['provider-access-not-installed'],
        backfill_only=True)

    assert stats['added_connections'] == 0, stats
    assert stats['added_deployments'] == 0, stats
    document = repository.get(boundary).document
    assert not [row for row in document['deployments']
                if row['wire_model_id'] == _ALIAS]


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))


@pytest.mark.parametrize('restriction', ['face_revoked', 'model_revoked', 'wire_excluded'])
def test_fresh_install_owner_restrictions_survive_backfill(restriction):
    from tests.model_routing.test_template_supply_sync import _seed_mini_card

    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(_OWNER)
    bundle = _seed_mini_card(repository, boundary, selected=('gpt-6.1-sol',))
    access_id = bundle['provider_access']['provider_access_id']
    face_id = next(row['connection_id'] for row in bundle['connections']
                   if row.get('gateway_namespace') == 'kunlun')

    def restrict(document):
        auth = document['credentials'][0]['authorization']
        if restriction == 'face_revoked':
            auth['connection_ids'].remove(face_id)
        elif restriction == 'model_revoked':
            auth['models'] = []
        else:
            auth['models'][0]['excluded_wire_model_ids'] = [_ALIAS]
        return document

    repository.mutate(boundary, restrict)
    before = repository.get(boundary)
    sync_template_supply(boundary=boundary, repository=repository,
                         prober=_never_probe, provider_access_ids=[access_id],
                         backfill_only=True)
    after = repository.get(boundary)
    assert after.document['credentials'] == before.document['credentials']
    assert after.revision == before.revision


@pytest.mark.parametrize('restriction', ['no_connections', 'no_models', 'excluded_wire'])
def test_new_face_does_not_expand_restricted_credentials(restriction):
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(_OWNER)
    bundle = _seed_card_without_the_new_face(repository, boundary)
    access_id = bundle['provider_access']['provider_access_id']

    def restrict(document):
        auth = document['credentials'][0]['authorization']
        if restriction == 'no_connections':
            auth['connection_ids'] = []
        elif restriction == 'no_models':
            auth['models'] = []
        else:
            auth['models'][0]['excluded_wire_model_ids'] = [_ALIAS]
        return document

    repository.mutate(boundary, restrict)
    before = repository.get(boundary).document['credentials']
    stats = sync_template_supply(boundary=boundary, repository=repository,
                                prober=_never_probe, provider_access_ids=[access_id],
                                backfill_only=True)
    assert stats['added_connections'] == 1
    assert repository.get(boundary).document['credentials'] == before


@pytest.mark.parametrize('grant_face', [False, True])
def test_new_credential_existing_face_grant_is_explicit(grant_face):
    from copy import deepcopy
    from tests.model_routing.test_template_supply_sync import _seed_mini_card

    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(_OWNER)
    bundle = _seed_mini_card(repository, boundary, selected=('gpt-6.1-sol',))
    access_id = bundle['provider_access']['provider_access_id']
    face_id = next(row['connection_id'] for row in bundle['connections']
                   if row.get('gateway_namespace') == 'kunlun')

    def add_credential(document):
        credential = deepcopy(document['credentials'][0])
        credential['credential_id'] = 'credential-added-later'
        if not grant_face:
            credential['authorization']['connection_ids'].remove(face_id)
        document['credentials'].append(credential)
        return document

    repository.mutate(boundary, add_credential)
    before = repository.get(boundary)
    sync_template_supply(boundary=boundary, repository=repository,
                         prober=_never_probe, provider_access_ids=[access_id],
                         backfill_only=True)
    after = repository.get(boundary)
    assert after.document['credentials'] == before.document['credentials']
    assert after.revision == before.revision
