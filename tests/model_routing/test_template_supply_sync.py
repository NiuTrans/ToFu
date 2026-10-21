"""tests/model_routing/test_template_supply_sync.py — 已安装卡片向模板供给收敛.

Pins the 2026-09-06 ruling: provider templates seed NEW installs only, so a
YourProvider gateway card added last month never sees newly listed models
(gemini-3.8-flash), and your-llm-gateway.example.com's empty ``/models`` listing gives
provider_catalog_sync nothing to discover. The template-supply sync
converges every installed access that fingerprint-matches a template
toward a fresh install:

* fingerprint match — every template base URL hosted + brand/recipe overlap;
* probe-gated — a missing recipe is added only when a live probe on the
  access's own credential proves the gateway serves it (ok / rate_limited);
* insert-only — existing offerings/deployments/credentials are never
  modified or removed, and a second run is a no-op.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/model_routing/test_template_supply_sync.py -v
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from lib.llm.responses_outbound import openai_body_to_responses_checked
from lib.model_routing import OwnerBoundary, empty_document
from lib.model_routing.inference import (
    profile_from_metadata,
    resolve_inference_profile,
)
from lib.model_routing.repository import InMemoryModelRoutingRepository
from lib.model_routing.template_supply import sync_template_supply
from lib.provider.provider_template_recipes import (
    ProviderTemplateRecipeError,
    compile_provider_template_bundle,
)

# The YourProvider template ships only in internal builds (bundles/ is stripped
# from the opensource export); this suite is about that template's sync, so
# the whole module skips when the template is absent.
try:
    compile_provider_template_bundle('yourprovider',
                                     selected_model_ids=['gemini-2.5-flash-lite'])
except ProviderTemplateRecipeError:
    pytest.skip('yourprovider provider template is not shipped in this build',
                allow_module_level=True)

pytestmark = pytest.mark.unit

_OWNER = 987654321
_SEED_MODEL = 'gemini-2.5-flash-lite'


def _boundary() -> OwnerBoundary:
    return OwnerBoundary.create(_OWNER)


def _prober_ok(**kwargs):
    return {row['model_id']: 'ok' for row in kwargs['recipes']}


def _template() -> dict:
    with open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            'bundles', 'yourprovider', 'provider_templates', 'yourprovider.json'),
            encoding='utf-8') as handle:
        return json.load(handle)


def _seed_mini_card(repository, boundary, selected=(_SEED_MODEL,)):
    """A real YourProvider-card-in-miniature: same template, one installed model."""
    bundle = compile_provider_template_bundle(
        'yourprovider', selected_model_ids=list(selected))
    document = empty_document(revision=0)
    document['providers'].append(bundle['provider'])
    document['provider_accesses'].append(bundle['provider_access'])
    document['connections'].extend(bundle['connections'])
    credential = dict(bundle['credentials'][0])
    secret = repository.put_secret(boundary, 'sk-test')
    credential['secret_reference'] = secret['secret_reference']
    credential['key_hint'] = secret['key_hint']
    document['credentials'].append(credential)
    document['offerings'].extend(bundle['offerings'])
    document['deployments'].extend(bundle['deployments'])
    document['creators'].extend(bundle['creators'])
    document['models'].extend(bundle['models'])
    repository.compare_and_swap(boundary, document, expected_revision=0)
    return bundle


def _offerings(document, access_id):
    return [row for row in document['offerings']
            if row['provider_access_id'] == access_id]


def _model_ids(offerings):
    return {
        (row.get('model') or {}).get('model_id') or row.get('pending_model_id')
        for row in offerings
    }


def test_missing_served_recipes_converge():
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    bundle = _seed_mini_card(repository, boundary)
    access_id = bundle['provider_access']['provider_access_id']

    stats = sync_template_supply(
        boundary=boundary, repository=repository, prober=_prober_ok)

    document = repository.get(boundary).document
    ids = _model_ids(_offerings(document, access_id))
    assert 'gemini-3.8-flash' in ids
    assert 'glm-5.3-flash' in ids
    assert 'kimi-0902-preview' in ids
    # Every recipe lands exactly one offering, minus the seeded one, minus
    # gpt-4o-2024-{08-06,11-20}: those dated snapshots merge-key to the
    # gpt-4o-2024-05-13 recipe added in the same pass, and one identity may
    # never grow a second offering. Derived from the template so a newly
    # shipped recipe does not need this number edited.
    recipes = [row for row in _template()['offering_recipes']
               if row['model_id'] != _SEED_MODEL]
    assert stats['added_offerings'] == len(recipes) - 2
    from lib.model_catalog._creator_identity import merge_keys
    gpt4o = merge_keys('gpt-4o')
    assert len([row for row in _offerings(document, access_id)
                if merge_keys((row.get('model') or {}).get('model_id')
                              or row.get('pending_model_id'))
                & gpt4o]) == 1

    # Claude recipes deploy on the access's Anthropic-face connection,
    # Gemini on the OpenAI one — the face the dispatcher would route to.
    connections = {row['connection_id']: row
                   for row in document['connections']}
    offerings = {row['offering_id']: row
                 for row in _offerings(document, access_id)}
    by_model = {_model_ids([row]).pop(): row['offering_id']
                for row in offerings.values()}
    claude_deps = [row for row in document['deployments']
                   if row['offering_id'] == by_model['claude-fable-5']]
    assert claude_deps
    assert all(connections[row['connection_id']]['protocol'] == 'anthropic'
               for row in claude_deps)
    assert all(row['probe_status'] == 'passed' for row in claude_deps)

    assert all('enabled' not in row for row in claude_deps)
    gemini_deps = [row for row in document['deployments']
                   if row['offering_id'] == by_model['gemini-3.8-flash']]
    assert gemini_deps
    assert all(connections[row['connection_id']]['protocol'] == 'openai'
               for row in gemini_deps)

    # The credential gained dispatch grants for the new confirmed models.
    credential = next(row for row in document['credentials']
                      if row['provider_access_id'] == access_id)
    granted = {(row['creator_id'], row['model_id'])
               for row in credential['authorization']['models']}
    assert ('google', 'gemini-3.8-flash') in granted
    assert ('anthropic', 'claude-fable-5') in granted

    # The registry gained the new model rows.
    registry = {row['model_id'] for row in document['models']}
    assert 'gemini-3.8-flash' in registry


def test_probe_dead_recipes_are_never_added():
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    bundle = _seed_mini_card(repository, boundary)
    access_id = bundle['provider_access']['provider_access_id']
    before = repository.get(boundary)

    stats = sync_template_supply(
        boundary=boundary, repository=repository,
        prober=lambda **kw: {row['model_id']: 'not_found'
                             for row in kw['recipes']})

    assert stats['added_offerings'] == 0
    after = repository.get(boundary)
    assert after.revision == before.revision
    assert len(_offerings(after.document, access_id)) == 1


def test_only_served_recipes_are_added():
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    bundle = _seed_mini_card(repository, boundary)
    access_id = bundle['provider_access']['provider_access_id']

    stats = sync_template_supply(
        boundary=boundary, repository=repository,
        prober=lambda **kw: {
            row['model_id']: ('ok' if row['model_id'] == 'gemini-3.8-flash'
                              else 'not_found')
            for row in kw['recipes']
        })

    assert stats['added_offerings'] == 1
    document = repository.get(boundary).document
    ids = _model_ids(_offerings(document, access_id))
    assert ids == {_SEED_MODEL, 'gemini-3.8-flash'}


def test_rate_limited_recipe_stays_reprobeable():
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    bundle = _seed_mini_card(repository, boundary)
    access_id = bundle['provider_access']['provider_access_id']

    sync_template_supply(
        boundary=boundary, repository=repository,
        prober=lambda **kw: {
            row['model_id']: ('rate_limited'
                              if row['model_id'] == 'glm-5.3-flash'
                              else 'not_found')
            for row in kw['recipes']
        })

    document = repository.get(boundary).document
    offerings = _offerings(document, access_id)
    target = next(row for row in offerings
                  if (row.get('model') or {}).get('model_id')
                  == 'glm-5.3-flash')
    deps = [row for row in document['deployments']
            if row['offering_id'] == target['offering_id']]
    assert deps
    assert all(row['probe_status'] == 'unprobed' for row in deps)
    assert all('enabled' not in row for row in deps)


def test_second_run_is_a_no_op():
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    _seed_mini_card(repository, boundary)

    sync_template_supply(
        boundary=boundary, repository=repository, prober=_prober_ok)
    stats = sync_template_supply(
        boundary=boundary, repository=repository, prober=_prober_ok)

    assert stats['added_offerings'] == 0
    assert stats['added_deployments'] == 0


def test_face_pinned_alias_is_backfilled_onto_existing_offering():
    """A recipe-declared alias that an installed card lacks must be added as
    a Deployment on the correct face Connection — even when the Offering
    itself already exists (insert-only never revisits it).

    Mirrors 2026-10-20: a card seeded from the OLD template (no kunlun face)
    already serves ``gpt-6.1-sol`` on the public wire. The new recipe adds
    ``kunlunbedrock.gpt-6.1-sol-m17`` pinned to the kunlun face. Convergence
    must backfill BOTH the missing kunlun Connection and the alias
    Deployment, idempotently, without touching the existing public-wire
    Deployment or the credential grant."""
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    # Seed from the CURRENT bundle (which already includes the alias), then
    # strip everything kunlun to reproduce a pre-kunlun installed card.
    bundle = _seed_mini_card(repository, boundary, selected=[_SEED_MODEL,
                                                             'gpt-6.1-sol'])
    access_id = bundle['provider_access']['provider_access_id']
    document = repository.get(boundary).document
    kunlun_conns = {row['connection_id'] for row in document['connections']
                    if row.get('gateway_namespace') == 'kunlun'}
    assert kunlun_conns, 'seed must start from a bundle that has the face'
    document['connections'] = [row for row in document['connections']
                               if row['connection_id'] not in kunlun_conns]
    document['deployments'] = [row for row in document['deployments']
                               if row['connection_id'] not in kunlun_conns]
    for credential in document['credentials']:
        credential['authorization']['connection_ids'] = [
            row for row in credential['authorization']['connection_ids']
            if row not in kunlun_conns]
    repository.compare_and_swap(
        boundary, document, expected_revision=repository.get(boundary).revision)

    # Sanity: the installed card now serves only the public wire.
    before = repository.get(boundary).document
    assert {row['wire_model_id'] for row in before['deployments']
            if (row.get('offering_id'))} >= {'gpt-6.1-sol'}
    assert not any(row['wire_model_id'] == 'kunlunbedrock.gpt-6.1-sol-m17'
                   for row in before['deployments'])

    stats = sync_template_supply(
        boundary=boundary, repository=repository, prober=_prober_ok)

    document = repository.get(boundary).document
    connections = {row['connection_id']: row for row in document['connections']}
    sol_offering = next(row for row in _offerings(document, access_id)
                        if (row.get('model') or {}).get('model_id')
                        == 'gpt-6.1-sol')
    deps = [row for row in document['deployments']
            if row['offering_id'] == sol_offering['offering_id']]
    wires = {row['wire_model_id'] for row in deps}
    assert 'kunlunbedrock.gpt-6.1-sol-m17' in wires, wires
    alias_dep = next(row for row in deps
                     if row['wire_model_id'] == 'kunlunbedrock.gpt-6.1-sol-m17')
    alias_conn = connections[alias_dep['connection_id']]
    # The alias rides the kunlun face Connection, not the public default.
    assert alias_conn.get('gateway_namespace') == 'kunlun', alias_conn
    assert alias_conn.get('extra_headers') == {'x-context-provider': 'kunlun'}
    assert alias_conn.get('protocol') == 'openai_responses'
    # The existing public-wire deployment is untouched.
    public_dep = next(row for row in deps
                      if row['wire_model_id'] == 'gpt-6.1-sol')
    assert connections[public_dep['connection_id']].get(
        'gateway_namespace') in (None, 'default')
    assert stats['added_deployments'] >= 1

    # Idempotent: a second pass adds nothing and never duplicates the wire.
    again = sync_template_supply(
        boundary=boundary, repository=repository, prober=_prober_ok)
    document = repository.get(boundary).document
    deps = [row for row in document['deployments']
            if row['offering_id'] == sol_offering['offering_id']]
    assert [row['wire_model_id'] for row in deps].count(
        'kunlunbedrock.gpt-6.1-sol-m17') == 1
    assert again['added_deployments'] == 0


def test_declared_face_capability_converges_onto_installed_face_connection():
    """A face the recipe later declares MORE capable must reach the card.

    Mirrors 2026-10-21: a card installed from the previous template already
    rides the kunlun face Connection, but that Connection carries only
    ``sampling.temperature='omit'``. The recipe now also declares
    ``features.responses_reasoning_replay='supported'`` because the endpoint
    streams opaque ``reasoning.encrypted_content``. Insert-only seeding never
    revisits the row, so without convergence every tool round after the first
    opaque reasoning item dies in request-prepare as
    ``protocol_conversion:mandatory_continuity_unrepresentable``.
    """
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    bundle = _seed_mini_card(repository, boundary, selected=['gpt-6.1-sol'])
    access_id = bundle['provider_access']['provider_access_id']
    document = repository.get(boundary).document
    kunlun = next(row for row in document['connections']
                  if row.get('gateway_namespace') == 'kunlun')
    # Reproduce the card measured before the replay fact was known.
    kunlun['inference_profile'] = {'sampling': {'temperature': 'omit'}}
    kunlun['inference_profile']['features'] = {
        'an_owner_declared_feature': 'supported'}
    repository.compare_and_swap(
        boundary, document, expected_revision=repository.get(boundary).revision)

    stats = sync_template_supply(
        boundary=boundary, repository=repository, backfill_only=True)

    document = repository.get(boundary).document
    kunlun = next(row for row in document['connections']
                  if row.get('gateway_namespace') == 'kunlun')
    profile = kunlun['inference_profile']
    assert profile['features']['responses_reasoning_replay'] == 'supported'
    # Only the declared keys converge; sampling and owner features survive.
    assert profile['sampling'] == {'temperature': 'omit'}
    assert profile['features']['an_owner_declared_feature'] == 'supported'
    assert kunlun['extra_headers'] == {'x-context-provider': 'kunlun'}
    assert kunlun['responses_profile'] == 'compatible'
    assert stats['repaired_connections'] == 1

    # The acceptance check: the exact history that failed must now compile.
    deployment = next(row for row in document['deployments']
                      if row['connection_id'] == kunlun['connection_id']
                      and row['wire_model_id']
                      == 'kunlunbedrock.gpt-6.1-sol-m17')
    resolved = resolve_inference_profile(
        connection={'protocol': 'openai_responses',
                    'inference_profile': profile_from_metadata(kunlun)},
        deployment={'inference_profile': profile_from_metadata(deployment)})
    result = openai_body_to_responses_checked({
        'model': 'kunlunbedrock.gpt-6.1-sol-m17',
        '_responses_feature_profile': 'compatible',
        '_route_inference_profile': resolved,
        'messages': [
            {'role': 'user', 'content': 'trace the header'},
            {'role': 'assistant', 'content': 'working',
             'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {
                 'name': 'repo_map', 'arguments': '{}'}}],
             '_responses_items': [{'type': 'reasoning',
                                   'encrypted_content': 'gAAAA-opaque'}]},
            {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'ok'}]})
    assert not result.validation_issues, result.validation_issues
    payload = result.require_payload()
    assert 'reasoning.encrypted_content' in payload['include']
    assert any(item.get('type') == 'reasoning' for item in payload['input'])
    # Idempotent: a second pass rewrites nothing.
    again = sync_template_supply(
        boundary=boundary, repository=repository, backfill_only=True)
    assert again['repaired_connections'] == 0


def test_declared_face_convergence_never_touches_an_owner_connection():
    """A namespace-less Connection is the owner's endpoint, not the template's."""
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    bundle = _seed_mini_card(repository, boundary, selected=['gpt-6.1-sol'])
    document = repository.get(boundary).document
    kunlun = next(row for row in document['connections']
                  if row.get('gateway_namespace') == 'kunlun')
    del kunlun['gateway_namespace']
    kunlun['inference_profile'] = {'sampling': {'temperature': 'omit'}}
    repository.compare_and_swap(
        boundary, document, expected_revision=repository.get(boundary).revision)

    sync_template_supply(
        boundary=boundary, repository=repository, backfill_only=True)

    document = repository.get(boundary).document
    kunlun = next(row for row in document['connections']
                  if row['base_url'].endswith('/v1')
                  and row['base_url'].count('/') <= 4)
    assert 'features' not in (kunlun.get('inference_profile') or {})

def _install_deepseek_pair(repository, boundary):
    """Scaffold plus the pre-rename DeepSeek supply: V4.1 live, V4 leftover.

    Mirrors the aggregate a rename leaves behind — the template now authors
    the gateway mirrors under ``deepseek-v4.1-flash`` while the earlier
    ``deepseek-v4-flash`` offering still deploys them.
    """
    bundle = _seed_mini_card(repository, boundary)
    access_id = bundle['provider_access']['provider_access_id']
    connection_id = bundle['connections'][0]['connection_id']
    document = repository.get(boundary).document
    for model_id in ('deepseek-v4.1-flash', 'deepseek-v4-flash'):
        document['models'].append({
            'creator_id': 'deepseek',
            'model_id': model_id,
            'display_name': model_id,
            'capabilities': ['text', 'vision', 'thinking'],
            'context_window': 1000000,
            'quality_rank': 1.0,
        })
    document['creators'].append({'creator_id': 'deepseek', 'name': 'DeepSeek'})
    for index, (model_id, wires) in enumerate((
            ('deepseek-v4.1-flash', ('deepseek-v4.1-flash',
                                     'deepseek-v4.1-flash-huawei')),
            ('deepseek-v4-flash', ('deepseek-v4-flash-huawei',
                                   'deepseek-v4-flash-tencent')))):
        offering_id = f'off_rename_{index}'
        document['offerings'].append({
            'offering_id': offering_id,
            'provider_access_id': access_id,
            'model': {'creator_id': 'deepseek', 'model_id': model_id},
            'identity_state': 'confirmed',
            'capabilities': ['text', 'vision', 'thinking'],
            'context_window': 1000000,
            'enabled': True,
            'stale': False,
        })
        for wire in wires:
            document['deployments'].append({
                'deployment_id': f'dep_rename_{index}_{wire}',
                'offering_id': offering_id,
                'connection_id': connection_id,
                'wire_model_id': wire,
                'probe_status': 'passed',
                'identity_confidence': 'high',
            })
    repository.compare_and_swap(
        boundary, document, expected_revision=repository.get(boundary).revision)
    return access_id


def _offering(document, offering_id):
    return next(row for row in document['offerings']
                if row['offering_id'] == offering_id)


def test_recipe_rename_supersedes_the_previous_offering_only():
    """The same gateway wires re-authored under a new logical id must retire
    the previous offering — otherwise one supply is offered (and dispatched)
    twice under two Model identities."""
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    access_id = _install_deepseek_pair(repository, boundary)

    stats = sync_template_supply(
        boundary=boundary, repository=repository, prober=_prober_ok)

    document = repository.get(boundary).document
    assert _offering(document, 'off_rename_1')['stale'] is True
    assert _offering(document, 'off_rename_0')['stale'] is False
    assert stats['superseded_offerings'] == 1
    # Superseding never deletes the retired row or its wire evidence.
    assert [row for row in document['deployments']
            if row['offering_id'] == 'off_rename_1']
    # The Model itself stays in the world registry; only YourProvider's supply of
    # it is retired.
    assert any(row['model_id'] == 'deepseek-v4-flash'
               for row in document['models'])
    # Idempotent: a second pass retires nothing new.
    again = sync_template_supply(
        boundary=boundary, repository=repository, prober=_prober_ok)
    assert again['superseded_offerings'] == 0
    assert _offering(repository.get(boundary).document,
                     'off_rename_1')['stale'] is True
    assert _offering(repository.get(boundary).document,
                     'off_rename_0')['stale'] is False


def test_partial_wire_overlap_is_not_superseded():
    """Only a FULL wire-pool takeover retires an offering; shared capacity
    under two identities may be genuine and stays dispatchable."""
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    _install_deepseek_pair(repository, boundary)
    document = repository.get(boundary).document
    # Give the legacy offering one wire the renamed recipe does not author.
    legacy = [row for row in document['deployments']
              if row['offering_id'] == 'off_rename_1']
    legacy[0]['wire_model_id'] = 'deepseek-v4-flash-legacy-only'
    repository.compare_and_swap(
        boundary, document, expected_revision=repository.get(boundary).revision)

    sync_template_supply(
        boundary=boundary, repository=repository, prober=_prober_ok)

    assert _offering(repository.get(boundary).document,
                     'off_rename_1')['stale'] is False


def test_owner_disabled_offering_is_never_resurrected():
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    bundle = _seed_mini_card(
        repository, boundary,
        selected=[_SEED_MODEL, 'gemini-3.8-flash'])
    access_id = bundle['provider_access']['provider_access_id']
    # The owner disabled that offering; the sync must leave it untouched.
    document = repository.get(boundary).document
    target = next(row for row in document['offerings']
                  if (row.get('model') or {}).get('model_id')
                  == 'gemini-3.8-flash')
    target['enabled'] = False
    repository.compare_and_swap(
        boundary, document, expected_revision=repository.get(boundary).revision)

    sync_template_supply(
        boundary=boundary, repository=repository, prober=_prober_ok)

    rows = [row for row in _offerings(
        repository.get(boundary).document, access_id)
        if (row.get('model') or {}).get('model_id') == 'gemini-3.8-flash']
    assert len(rows) == 1
    assert rows[0]['enabled'] is False


def test_access_without_template_urls_is_never_flooded():
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    _seed_mini_card(repository, boundary)
    # Point every connection at an unrelated host: no template's URL set is
    # hosted anymore, so no template may match — not even via brand.
    document = repository.get(boundary).document
    for row in document['connections']:
        row['base_url'] = 'https://api.example.com/v1'
    repository.compare_and_swap(
        boundary, document, expected_revision=repository.get(boundary).revision)

    def _explode(**_kwargs):
        raise AssertionError('prober must not run without a template match')

    stats = sync_template_supply(
        boundary=boundary, repository=repository, prober=_explode)
    assert stats['added_offerings'] == 0


def test_partial_url_coverage_does_not_match():
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    bundle = _seed_mini_card(repository, boundary)
    access_id = bundle['provider_access']['provider_access_id']
    # Drop the Anthropic-face connection: the access no longer hosts every
    # template URL, so the template must not match at all.
    document = repository.get(boundary).document
    anthropic = [row for row in document['connections']
                 if row['protocol'] == 'anthropic']
    assert anthropic
    victim = anthropic[0]['connection_id']
    document['connections'] = [row for row in document['connections']
                               if row['connection_id'] != victim]
    document['deployments'] = [row for row in document['deployments']
                               if row['connection_id'] != victim]
    for credential in document['credentials']:
        credential['authorization']['connection_ids'] = [
            row for row in credential['authorization']['connection_ids']
            if row != victim]
    repository.compare_and_swap(
        boundary, document, expected_revision=repository.get(boundary).revision)

    def _explode(**_kwargs):
        raise AssertionError('prober must not run without full URL coverage')

    stats = sync_template_supply(
        boundary=boundary, repository=repository, prober=_explode)
    assert stats['added_offerings'] == 0
    assert len(_offerings(repository.get(boundary).document, access_id)) == 1


def test_recipe_respelling_a_registered_model_converges():
    # The o3-2025-04-16 recipe merge-collides with an (openai, o3) registry
    # row the access does not offer (left over from another access). The
    # bundle's dated row must still be appended so the domain alias merge
    # canonicalizes the new offering/credential refs onto the survivor —
    # skipping it stranded those refs on a nonexistent row and the whole
    # pass was rejected as unknown_model.
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    bundle = _seed_mini_card(repository, boundary)
    access_id = bundle['provider_access']['provider_access_id']

    document = repository.get(boundary).document
    if not any(row['creator_id'] == 'openai' for row in document['creators']):
        document['creators'].append({'creator_id': 'openai', 'name': 'OpenAI'})
    document['models'].append({
        'creator_id': 'openai',
        'model_id': 'o3',
        'display_name': 'o3',
        'capabilities': ['chat'],
        'context_window': 200000,
        'quality_rank': 1.0,
    })
    # The exact recipe id may also already exist with a smaller window and
    # fewer capabilities: the current owner registry remains authoritative;
    # a provider recipe cannot promote its larger limits into the Model.
    document['models'].append({
        'creator_id': 'google',
        'model_id': 'gemini-3.8-flash',
        'display_name': 'gemini-3.8-flash',
        'capabilities': ['chat'],
        'context_window': 8192,
        'quality_rank': 1.0,
    })
    repository.compare_and_swap(
        boundary, document,
        expected_revision=repository.get(boundary).revision)

    stats = sync_template_supply(
        boundary=boundary, repository=repository, prober=_prober_ok)

    recipes = [row for row in _template()['offering_recipes']
               if row['model_id'] != _SEED_MODEL]
    assert stats['added_offerings'] == len(recipes) - 2
    document = repository.get(boundary).document
    o3 = [row for row in _offerings(document, access_id)
          if (row.get('model') or {}).get('creator_id') == 'openai'
          and (row.get('model') or {}).get('model_id') == 'o3']
    assert len(o3) == 1
    credential = next(row for row in document['credentials']
                      if row['provider_access_id'] == access_id)
    granted = {(row['creator_id'], row['model_id'])
               for row in credential['authorization']['models']}
    assert ('openai', 'o3') in granted
    assert ('openai', 'o3-2025-04-16') not in granted
    dated = [row for row in document['models']
             if row['model_id'] == 'o3-2025-04-16']
    assert dated == []
    survivors = [row for row in document['models']
                 if row['creator_id'] == 'openai' and row['model_id'] == 'o3']
    assert len(survivors) == 1

    gemini = next(row for row in document['models']
                  if row['creator_id'] == 'google'
                  and row['model_id'] == 'gemini-3.8-flash')
    offering = next(row for row in _offerings(document, access_id)
                    if (row.get('model') or {}).get('model_id')
                    == 'gemini-3.8-flash')
    assert gemini['context_window'] == offering['context_window'] == 8192
    assert set(offering['capabilities']) - {'cheap'} <= set(gemini['capabilities'])


@pytest.mark.parametrize('fail_start', [False, True])
def test_template_probe_cells_start_without_window_and_preserve_evidence(monkeypatch, fail_start):
    import threading
    import time
    import lib.provider.provider_probe as probes
    from lib.model_routing import template_supply

    count = 4 if fail_start else 32
    barrier = threading.Barrier(count - int(fail_start), timeout=10)
    original = threading.Thread.start
    starts = 0
    called = []
    lock = threading.Lock()
    work = [(i, 'private-key', f'm{i}', f'wire{i}', [], 'https://example.invalid', 'openai')
            for i in range(count)]

    def start(thread):
        nonlocal starts
        if thread.name.startswith('template-supply'):
            starts += 1
            if fail_start and starts == 2:
                raise RuntimeError('unstarted template probe')
        return original(thread)

    def probe(url, key, wire, *args, **kwargs):
        with lock:
            called.append(wire)
        barrier.wait()
        return 'ok', ''

    monkeypatch.setattr(probes, 'build_probe_work', lambda *args, **kwargs: work)
    monkeypatch.setattr(probes, 'probe_one_cell', probe)
    monkeypatch.setattr(template_supply, '_PROBE_WORKERS', 1)
    monkeypatch.setattr(threading.Thread, 'start', start)
    started = time.monotonic()
    result = template_supply._live_prober(base_url='https://example.invalid',
        protocol='openai', faces={}, api_key='private-key', extra_headers={}, recipes=[])
    assert len(result) == count
    for i in range(count):
        assert result[f'm{i}'] == ('unknown' if fail_start and i == 1 else 'ok')
    assert len(called) == count - int(fail_start)
    assert not any(t.name.startswith('template-supply') for t in threading.enumerate())
    if not fail_start:
        print(f'32 template cells: {(time.monotonic() - started) * 1000:.2f} ms; 0 workers')


@pytest.mark.parametrize('cell_headers', [None, {}, {
    'x-context-provider': 'kunlun', 'Authorization': 'face-token',
    'X-Collision': 'face',
}])
def test_template_probe_merges_face_and_credential_headers(monkeypatch, cell_headers):
    from lib.model_routing import template_supply
    from lib.provider import provider_probe

    envelope = {'Authorization': 'synthetic-envelope-token',
                'X-Collision': 'credential', 'X-Owner': 'owner'}
    work = (0, 'synthetic-key', 'model', 'wire', [],
            'https://example.invalid/v1', 'openai')
    if cell_headers is not None:
        work += ({'extra_headers': cell_headers},)
    monkeypatch.setattr(provider_probe, 'build_probe_work', lambda *a, **k: [work])
    received = []

    def probe(url, key, wire, headers, timeout, **kwargs):
        received.append(dict(headers))
        return 'ok', ''

    monkeypatch.setattr(provider_probe, 'probe_one_cell', probe)
    before = dict(envelope)
    assert template_supply._live_prober(
        base_url='https://example.invalid/v1', protocol='openai', faces={},
        api_key='synthetic-key', extra_headers=envelope, recipes=[]) == {'model': 'ok'}
    assert received == [{**(cell_headers or {}), **envelope}]
    assert envelope == before
