"""Model ownership, discovery provenance and refresh are one routing contract."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary, normalize_document
from lib.model_routing.catalog_merge import merge_missing_catalog_entities
from lib.model_routing.metadata_migration import reconcile_offering_metadata
from tests.model_routing.test_model_routing_contract import _full_document

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('entrypoint', ['provider', 'oauth', 'recovery'])
def test_provider_imports_cannot_promote_scoped_facts(entrypoint):
    document = _full_document()
    original = deepcopy(document['models'])
    imported = deepcopy(document)
    for model in imported['models']:
        model.update(context_window=9000000, capabilities=['text', 'video'],
                     quality_rank=99, display_name='Provider alias',
                     inference_profile={'reasoning': {'support': 'unsupported'}})
    if entrypoint == 'provider':
        merge_missing_catalog_entities(document, imported)
    elif entrypoint == 'oauth':
        from lib.oauth.outbound import _merge_v2_catalog_entities
        _merge_v2_catalog_entities(document, imported)
    else:
        from lib.model_routing.bootstrap import _merge_missing_legacy_plan
        from lib.model_routing.migration import MigrationPlan
        for key in ('providers', 'provider_accesses', 'connections', 'credentials', 'offerings', 'deployments'):
            imported[key] = []
        plan = MigrationPlan(document=imported, issues=[], source_digest='test',
                             redacted_backup={}, _secrets=[])
        document = _merge_missing_legacy_plan(document, plan).document
    assert document['models'] == original


def test_local_exact_model_inherits_without_name_guesses_and_refresh_keeps_grants():
    from lib.model_routing.local_provider import upsert_local_provider
    document = _full_document()
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(984)
    repo.compare_and_swap(owner, document, expected_revision=0)
    args = dict(provider_id='local-test', display_name='Local',
                base_url='http://127.0.0.1:19001/v1', models=[{'model_id': 'alpha'}])
    installed = upsert_local_provider(repo, owner, **args).authority
    row = next(o for o in installed.document['offerings'] if o['offering_id'].startswith('local-'))
    assert row['identity_state'] == 'confirmed'
    assert row['context_window'] == document['models'][0]['context_window']
    assert row['inherited_model_fields'] == ['capabilities', 'context_window']
    access = row['provider_access_id']
    credential = next(c for c in installed.document['credentials'] if c['provider_access_id'] == access)
    assert credential['authorization']['models'] == [row['model']]
    row.update(context_window=16384, inherited_model_fields=['capabilities'], enabled=False)
    credential['authorization']['models'][0]['excluded_wire_model_ids'] = ['alpha']
    repo.compare_and_swap(owner, installed.document, expected_revision=installed.revision)
    refreshed = upsert_local_provider(repo, owner, **args).authority
    after = next(o for o in refreshed.document['offerings'] if o['offering_id'] == row['offering_id'])
    assert after == row
    after_credential = next(c for c in refreshed.document['credentials'] if c['provider_access_id'] == access)
    assert after_credential['authorization'] == credential['authorization']


@pytest.mark.parametrize('context', [None, 0, True, '32000'])
def test_invalid_or_missing_discovery_context_uses_public_model(context):
    from lib.model_routing.catalog_identity import inherit_discovered_metadata
    model = _full_document()['models'][0]
    row = inherit_discovered_metadata([{'model_id': model['model_id'], 'context_window': context}], [model])[0]
    assert row['context_window'] == model['context_window']
    assert row['inherited_model_fields'] == ['capabilities', 'context_window']


def test_catalog_context_alias_is_explicit_provider_evidence():
    from lib.model_routing.catalog_identity import inherit_discovered_metadata
    model = _full_document()['models'][0]
    row = inherit_discovered_metadata([{'model_id': model['model_id'], 'context_length': 16384}], [model])[0]
    assert row['context_window'] == 16384
    assert row['inherited_model_fields'] == ['capabilities']


def test_legacy_catalog_fingerprint_repairs_defaults_once_and_keeps_other_state():
    from lib.llm_dispatch.provider_catalog_sync import _stable_id
    document = _full_document()
    row = document['offerings'][0]
    old_id = row['offering_id']
    row.update(offering_id=_stable_id('offering', row['provider_access_id'], 'alpha'),
               capabilities=['text'], context_window=32768, enabled=False)
    for deployment in document['deployments']:
        if deployment['offering_id'] == old_id:
            deployment.update(offering_id=row['offering_id'], wire_model_id='alpha')
    preserved = deepcopy({k: document[k] for k in ('deployments', 'credentials', 'models')})
    changes = reconcile_offering_metadata(document)
    assert changes
    assert row['context_window'] == document['models'][0]['context_window']
    assert row['capabilities'] == document['models'][0]['capabilities']
    assert row['enabled'] is False
    assert all(document[k] == v for k, v in preserved.items())
    normalize_document(document)
    assert reconcile_offering_metadata(document) == []


def test_legacy_migration_keeps_explicit_pins_and_nondefault_limits():
    document = _full_document()
    document['offerings'][0].update(context_window=32768, inherited_model_fields=[])
    document['offerings'][1]['context_window'] = 16384
    before = deepcopy(document)
    reconcile_offering_metadata(document)
    assert document['offerings'][0] == before['offerings'][0]
    assert document['offerings'][1]['context_window'] == 16384
    assert 'context_window' not in document['offerings'][1]['inherited_model_fields']


@pytest.mark.parametrize('declared', [False, True])
def test_migration_receipt_distinguishes_an_override_from_the_same_default(declared):
    from lib.model_routing.migration import plan_legacy_migration
    seed = {'model_id': 'alpha'}
    if declared:
        seed['context_window'] = 128000
    plan = plan_legacy_migration({'providers': [{'id': 'receipt-provider',
        'base_url': 'https://example.invalid', 'models': [seed]}]},
        official_directory=_full_document()['models'])
    document = plan.document
    offering = document['offerings'][0]
    offering.pop('inherited_model_fields')
    offering['context_window'] = 128000
    reconcile_offering_metadata(document,
        legacy_receipt={'receipt': {'redacted_backup': plan.redacted_backup}})
    assert offering['context_window'] == (128000 if declared else 200000)
    assert ('context_window' in offering['inherited_model_fields']) is not declared


def test_explicit_provider_catalog_limit_is_preserved_even_when_equal_to_model():
    document = _full_document()
    offering = document['offerings'][0]
    connection = next(c for c in document['connections']
                      if c['provider_access_id'] == offering['provider_access_id'])
    window = offering['context_window']
    templates = [{'base_url': connection['base_url'], 'offering_recipes': [{
        'model_id': offering['model']['model_id'], 'context_window': window,
        'context_window_source': 'provider_documentation'}]}]
    reconcile_offering_metadata(document, provider_templates=templates)
    assert 'context_window' not in offering['inherited_model_fields']
    document['models'][0]['context_window'] *= 2
    after = normalize_document(document)
    assert next(o for o in after['offerings'] if o['offering_id'] == offering['offering_id'])['context_window'] == window


def test_read_only_audit_reports_unknown_and_keeps_input_unchanged():
    from scripts.audit_model_configuration import audit_configuration
    document = _full_document()
    before = deepcopy(document)
    result = audit_configuration(document, plan_migration=True)
    assert result['failures'] == []
    assert result['reasoning']['unknown'] == len(document['deployments'])
    assert result['migration_plan']
    assert document == before


def test_metadata_migration_has_bounded_durable_growth():
    import json
    document = _full_document()
    seed = document['offerings'][0]
    document['offerings'] = [dict(deepcopy(seed), offering_id=f'budget-{i}') for i in range(1000)]
    before = len(json.dumps(document, separators=(',', ':')))
    changes = reconcile_offering_metadata(document)
    assert len(changes) == 1000
    assert len(json.dumps(document, separators=(',', ':'))) - before < 100_000
    assert reconcile_offering_metadata(document) == []


def test_oauth_refresh_inherits_missing_context_and_cannot_rewrite_model():
    from lib.oauth.outbound import _upsert_oauth_provider
    document = _full_document()
    model = deepcopy(document['models'][0])
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(985)
    document = repo.compare_and_swap(owner, document, expected_revision=0).document
    entry = {'id': 'oauth-example', 'name': 'Subscription', 'brand': 'oauth', 'oauth': 'codex',
             'base_url': 'https://example.invalid',
             'models': [{'model_id': model['model_id'], 'capabilities': ['text']}]}
    assert _upsert_oauth_provider(entry, owner_user_id=985, tenant_id=None, repository=repo)
    after = repo.get(owner).document
    assert after['models'] == document['models']
    offering = next(o for o in after['offerings'] if o['offering_id'] not in {r['offering_id'] for r in document['offerings']})
    assert offering['context_window'] == model['context_window']
    assert offering['inherited_model_fields'] == ['context_window']
    assert 'catalog_capabilities' not in offering
    # A custom Codex OAuth endpoint is not the managed oauth_codex catalog.
    # Repeated refreshes must not stamp a managed-only baseline onto it.
    _upsert_oauth_provider(entry, owner_user_id=985, tenant_id=None, repository=repo)
    refreshed = next(o for o in repo.get(owner).document['offerings']
                     if o['offering_id'] == offering['offering_id'])
    assert 'catalog_capabilities' not in refreshed
    assert refreshed['inherited_model_fields'] == ['context_window']


def test_every_template_reasoning_choice_compiles_without_legacy_names(monkeypatch):
    from lib.llm.reasoning import compile_reasoning
    from lib.model_routing.inference import candidate_inference_metadata
    from lib.model_routing.controls import _choices
    from lib.model_routing.model_defaults import official_model_directory
    from lib.provider.provider_template_recipes import compile_provider_template_bundle, load_provider_templates
    def forbidden(*args, **kwargs):
        pytest.fail('a declared route reached the legacy name compiler')
    monkeypatch.setattr('lib.llm._reasoning_legacy.apply_legacy', forbidden)
    templates = load_provider_templates()
    directory = official_model_directory(templates)
    seen = 0
    for template in templates:
        if template['category'] == 'local':
            continue
        bundle = compile_provider_template_bundle(template['key'], official_directory=directory, _templates=templates)
        models = {(m['creator_id'], m['model_id']): m for m in bundle['models']}
        offerings = {o['offering_id']: o for o in bundle['offerings']}
        connections = {c['connection_id']: c for c in bundle['connections']}
        for deployment in bundle['deployments']:
            offering = offerings[deployment['offering_id']]
            ref = offering.get('model', {})
            candidate = SimpleNamespace(model=models.get((ref.get('creator_id'), ref.get('model_id')), {}),
                offering=offering, connection=connections[deployment['connection_id']], deployment=deployment)
            profile = candidate_inference_metadata(candidate)['inference_profile']
            for depth in _choices(profile)[2]:
                mode = depth if depth in {'default', 'off', 'lowest', 'enabled'} else 'preset'
                results = []
                for name in ('opaque-provider-wire', 'claude-unknown-future', 'gpt-unknown-future'):
                    body = {'_reasoning_intent': {'version': 1, 'mode': mode, 'value': depth, 'strict': True}}
                    compile_reasoning(body, name, inference_profile=profile)
                    results.append(body)
                assert results[0] == results[1] == results[2], (template['key'], ref, depth)
                seen += 1
    assert seen > 100
