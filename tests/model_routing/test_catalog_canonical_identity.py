"""Maintained catalog lookups resolve a spelled Model ID by canonical identity.

Relay recipes spell versions with dots (``claude-opus-5.5``) while developer
templates spell them with dashes (``claude-opus-5-5``). Every maintained
lookup matches the exact identity first; otherwise only the single entry of
the same creator sharing its spelling key (case and dot/dash/underscore/space
separators) supplies developer facts. Entries sharing the key with identical
borrowed facts are one entry; entries whose facts differ (ambiguity), other
creators, other versions, snapshot dates and quantization suffixes never
match, and the relay spelling keeps its own Model identity.
"""
import json

import pytest

from lib.model_catalog._creator_identity import canonical_key
from lib.model_routing import model_defaults, plan_legacy_migration
from lib.provider.provider_template_recipes import compile_provider_template_bundle

pytestmark = pytest.mark.unit

IMPORT_DEFAULT = 128_000


@pytest.mark.parametrize('model_id', ['claude-opus-5.5', 'claude-opus-4.7', 'claude-opus-4.8'])
def test_relay_dot_spelling_inherits_the_official_dash_spelling_context(model_id):
    bundle = compile_provider_template_bundle('yourprovider', selected_model_ids=[model_id])
    offering, = bundle['offerings']
    model, = bundle['models']
    assert offering['model'] == {'creator_id': 'anthropic', 'model_id': model_id}
    assert model['context_window'] == offering['context_window'] == 1_000_000
    assert offering['inherited_model_fields'] == ['context_window']


@pytest.mark.parametrize('model_id,window', [
    ('claude-opus-5', 1_000_000), ('claude-opus-4.6', 1_000_000), ('claude-sonnet-4.5', 200_000)])
def test_exact_spellings_keep_their_exact_directory_facts(model_id, window):
    bundle = compile_provider_template_bundle('yourprovider', selected_model_ids=[model_id])
    offering, = bundle['offerings']
    assert offering['model'] == {'creator_id': 'anthropic', 'model_id': model_id}
    assert offering['context_window'] == bundle['models'][0]['context_window'] == window


def _official(model_id, window, creator='anthropic'):
    return {'creator_id': creator, 'model_id': model_id, 'display_name': model_id,
            'capabilities': ['text', 'thinking', 'vision'], 'context_window': window,
            'quality_rank': 70.0}


def _plan(seed, directory):
    return plan_legacy_migration({'providers': [{
        'id': 'relay', 'base_url': 'https://relay.invalid/v1', 'api_keys': ['placeholder'],
        'models': [seed]}]}, official_directory=directory).document


def test_unique_same_creator_respelling_supplies_bounded_developer_facts():
    document = _plan({'model_id': 'claude-test-9.1', 'capabilities': ['text', 'video'],
                      'context_window': 2_000_000}, [_official('claude-test-9-1', 1_000_000)])
    model, = document['models']
    offering, = document['offerings']
    assert (model['creator_id'], model['model_id']) == ('anthropic', 'claude-test-9.1')
    assert (model['context_window'], model['capabilities'], model['quality_rank']) == (
        1_000_000, ['text', 'thinking', 'vision'], 70.0)
    # A relay row cannot expand the official baseline it resolved to.
    assert offering['context_window'] == 1_000_000
    assert offering['capabilities'] == ['text']
    assert document['deployments'][0]['wire_model_id'] == 'claude-test-9.1'


def test_spellings_of_one_entry_with_identical_facts_are_not_ambiguous():
    document = _plan({'model_id': 'Claude-Test-9.1'}, [
        _official('claude-test-9-1', 1_000_000), _official('claude_test_9_1', 1_000_000)])
    model = next(row for row in document['models'] if row['model_id'] == 'Claude-Test-9.1')
    assert (model['context_window'], model['capabilities'], model['quality_rank']) == (
        1_000_000, ['text', 'thinking', 'vision'], 70.0)


@pytest.mark.parametrize('directory', [
    # Two entries share the key with different facts: refused, never guessed.
    [_official('claude-test-9-1', 1_000_000), _official('claude_test_9_1', 800_000)],
    # Same spelling key under another creator is not this creator's Model.
    [_official('claude-test-9-1', 1_000_000, creator='other')],
    # A different version is a different Model.
    [_official('claude-test-9-2', 1_000_000)],
], ids=['ambiguous', 'other-creator', 'other-version'])
def test_respelling_without_one_same_creator_entry_keeps_import_defaults(directory):
    document = _plan({'model_id': 'claude-test-9.1'}, directory)
    model = next(row for row in document['models'] if row['model_id'] == 'claude-test-9.1')
    assert model['context_window'] == IMPORT_DEFAULT
    assert model['capabilities'] == ['text']


@pytest.mark.parametrize('model_id', ['claude-test-9.1-20270101', 'claude-test-9.1-fp8'],
                         ids=['dated-snapshot', 'quantized-variant'])
def test_snapshot_or_quantized_variant_never_borrows_its_alias_facts(model_id):
    # The registry's alias merge still treats them as one trained model ...
    assert canonical_key(model_id) == canonical_key('claude-test-9-1')
    alias = {**_official('claude-test-9-1', 1_000_000), 'list_pricing': {
        'input': 5, 'output': 25, 'currency': 'USD', 'unit': 'per_million_tokens'}}
    document = _plan({'model_id': model_id}, [alias])
    model, = document['models']
    # ... but the moving alias's price and limits are not this Model's facts.
    assert (model['creator_id'], model['model_id']) == ('anthropic', model_id)
    assert (model['context_window'], model['capabilities']) == (IMPORT_DEFAULT, ['text'])
    assert 'list_pricing' not in model and model['quality_rank'] != 70.0


def test_exact_identity_wins_over_a_canonical_twin():
    document = _plan({'model_id': 'claude-test-9.1'}, [
        _official('claude-test-9.1', 500_000), _official('claude-test-9-1', 1_000_000)])
    offering, = document['offerings']
    assert offering['model'] == {'creator_id': 'anthropic', 'model_id': 'claude-test-9.1'}
    assert offering['context_window'] == 500_000


@pytest.fixture
def declared_defaults(tmp_path, monkeypatch):
    shipped = json.loads(model_defaults._PATH.read_text(encoding='utf-8'))
    profile, other = (next(group['inference_profile'] for group in shipped['groups']
                           if model_id in group['model_ids'])
                      for model_id in ('claude-opus-5-5', 'claude-haiku-5-5'))
    assert profile != other
    path = tmp_path / 'model_inference_defaults.json'
    path.write_text(json.dumps({
        'contract_version': shipped['contract_version'],
        'groups': [
            {'creator_id': 'anthropic', 'model_ids': ['claude-test-7-1'], 'inference_profile': profile},
            # One Model declared in both spellings, like the shipped catalogue.
            {'creator_id': 'anthropic', 'model_ids': ['claude-test-8-1', 'claude-test-8.1'],
             'inference_profile': profile},
            # Two Models sharing one spelling key with different facts.
            {'creator_id': 'anthropic', 'model_ids': ['claude-test-6-1'], 'inference_profile': profile},
            {'creator_id': 'anthropic', 'model_ids': ['claude-test-6.1'], 'inference_profile': other},
        ]}), encoding='utf-8')
    monkeypatch.setattr(model_defaults, '_PATH', path)
    return profile


def test_model_defaults_resolve_the_unique_same_creator_spelling(declared_defaults):
    declared = {'creator_id': 'anthropic', 'model_id': 'claude-test-7-1'}
    assert model_defaults.apply_model_defaults(declared)
    respelled = {'creator_id': 'anthropic', 'model_id': 'claude-test-7.1'}
    assert model_defaults.has_exact_model_defaults(respelled)
    assert model_defaults.apply_model_defaults(respelled)
    assert respelled['inference_profile'] == declared['inference_profile']
    assert not model_defaults.apply_model_defaults(respelled)
    snapshot = {'creator_id': 'anthropic', 'model_id': 'claude-test-7.1'}
    assert model_defaults.apply_model_defaults(
        snapshot, profiles=model_defaults.current_model_defaults())
    assert snapshot == respelled


@pytest.mark.parametrize('creator,model_id', [
    ('other', 'claude-test-7.1'),
    ('anthropic', 'claude-test-7.2'),
    ('anthropic', 'claude-test-7-1-20270101'),
    ('anthropic', 'claude-test-7.1-fp8'),
    ('anthropic', 'claude-test-6_1'),
], ids=['other-creator', 'other-version', 'dated-snapshot', 'quantized-variant', 'ambiguous'])
def test_model_defaults_never_guess_across_creators_versions_or_ambiguity(
        declared_defaults, creator, model_id):
    model = {'creator_id': creator, 'model_id': model_id}
    assert not model_defaults.has_exact_model_defaults(model)
    assert not model_defaults.apply_model_defaults(model)
    assert 'inference_profile' not in model


def test_both_declared_spellings_still_resolve_exactly(declared_defaults):
    for model_id in ('claude-test-8-1', 'claude-test-8.1', 'claude-test-6-1', 'claude-test-6.1'):
        assert model_defaults.has_exact_model_defaults(
            {'creator_id': 'anthropic', 'model_id': model_id})


def test_a_third_spelling_of_identically_declared_spellings_resolves(declared_defaults):
    declared = {'creator_id': 'anthropic', 'model_id': 'claude-test-8-1'}
    third = {'creator_id': 'anthropic', 'model_id': 'Claude_Test_8_1'}
    assert model_defaults.has_exact_model_defaults(third)
    assert model_defaults.apply_model_defaults(declared)
    assert model_defaults.apply_model_defaults(third)
    assert third['inference_profile'] == declared['inference_profile']
