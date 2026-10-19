"""Composer choices are a projection of authorized route contracts, not names."""
from __future__ import annotations

import copy

import pytest

from lib.model_routing.controls import reasoning_controls
from lib.model_routing.domain import ModelRef, NativeModelSelection
from lib.model_routing.routing import RouteCandidateCompiler
from tests.model_routing.test_model_routing_contract import _full_document

pytestmark = pytest.mark.unit


def _profile(*, disable=True, efforts=('none', 'low', 'medium', 'high')):
    return {'reasoning': {
        'support': 'supported', 'wire_format': 'reasoning_effort',
        'can_disable': disable, 'supported_efforts': list(efforts),
        **({'disabled_effort': 'none'} if disable else {}),
    }}


def _document():
    document = _full_document()
    document['deployments'][0]['inference_profile'] = _profile()
    document['deployments'][1]['inference_profile'] = _profile(
        disable=False, efforts=('low', 'high'))
    return document


def _alpha(rows):
    return {row['provider_id']: row for row in rows
            if row['model_id'] == 'alpha' and not row['offering_id']}


def test_auto_controls_intersect_routes_without_inventing_off_or_effort():
    rows = _alpha(reasoning_controls(_document()))
    assert rows['']['depths'] == ['default', 'low', 'high']
    assert rows['']['can_disable'] is None
    assert rows['provider-a']['depths'] == ['default', 'off', 'low', 'medium', 'high']
    assert rows['provider-b']['depths'] == ['default', 'low', 'high']
    assert all(row['creator_id'] == 'creator' and row['offering_id'] == '' for row in rows.values())


def test_restoration_uses_final_intersection_and_every_target_compiles():
    from lib.llm.reasoning import ReasoningContractError, capture_intent, compile_reasoning
    document = _document()
    row = _alpha(reasoning_controls(document))['']
    assert row['restoration']['fallback'] == 'default'
    replacements = row['restoration']['replacements']
    assert replacements['off'] == 'low'
    assert replacements['medium'] == 'low'
    assert replacements['ultra'] == 'high'
    # At most eleven legacy choices; no per-conversation cache or round trip.
    assert len(replacements) <= 11
    assert len(__import__('json').dumps(row['restoration']).encode()) < 2048
    for replacement in replacements.values():
        assert replacement in row['depths']
        for deployment in document['deployments'][:2]:
            compile_reasoning({'_reasoning_intent': capture_intent(thinking_depth=replacement)},
                              'opaque', inference_profile=deployment['inference_profile'])
    with pytest.raises(ReasoningContractError, match='unsupported_effort'):
        compile_reasoning({'_reasoning_intent': capture_intent(effort='ultra')},
                          'opaque', inference_profile=document['deployments'][1]['inference_profile'])


def test_unknown_and_unroutable_controls_never_rewrite_saved_preferences():
    assert all('restoration' not in row for row in reasoning_controls(_full_document()))
    document = _document()
    for credential in document['credentials']:
        credential['authorization']['models'] = []
    assert all('restoration' not in row for row in reasoning_controls(document))


@pytest.mark.parametrize('depths,saved,expected', [
    (['default', 'low', 'max', 'ultra'], 'ultra', 'ultra'),
    (['default', 'low', 'high'], 'max', 'high'),
    (['default', 'minimal', 'high'], 'low', 'minimal'),
    (['default', 'careful', 'deepest'], 'off', 'careful'),
    (['default', 'careful', 'deepest'], 'obsolete', 'default'),
    (['default', 'high'], 'low', 'default'),
])
def test_saved_depth_restoration_respects_native_order_and_never_invents_rank(depths, saved, expected):
    from lib.llm.reasoning import clamp_reasoning_depth
    assert clamp_reasoning_depth(depths, saved) == expected


def test_unknown_reasoning_is_not_unsupported_or_off():
    rows = reasoning_controls(_full_document())
    assert rows
    assert all(row['depths'] == ['default'] for row in rows)
    assert all(row['support'] == 'unknown' and row['can_disable'] is None for row in rows)


def test_declared_model_controls_survive_missing_grants_without_authorizing_routes():
    document = _full_document()
    document['models'][0]['inference_profile'] = _profile()
    for credential in document['credentials']:
        credential['authorization']['models'] = []
    before = copy.deepcopy(document)
    rows = _alpha(reasoning_controls(document))
    assert rows['']['support'] == 'supported'
    assert rows['']['availability'] == 'no_route'
    assert rows['']['depths'] == ['default', 'off', 'low', 'medium', 'high']
    assert not RouteCandidateCompiler(document).compile(
        NativeModelSelection(ModelRef('creator', 'alpha'), None))
    assert document == before


def test_binary_reasoning_never_invents_effort_rungs():
    from lib.model_routing.controls import _choices
    from lib.llm.reasoning import capture_intent, compile_reasoning
    profile = {'reasoning': {'support': 'supported', 'wire_format': 'thinking_type', 'can_disable': True}}
    assert _choices(profile)[2] == ('default', 'off', 'enabled')
    body = {'_reasoning_intent': capture_intent(thinking_depth='enabled')}
    compile_reasoning(body, 'opaque-wire', inference_profile=profile)
    assert body['thinking'] == {'type': 'enabled'}
    assert 'reasoning_effort' not in body


def test_native_efforts_not_aliases_drive_three_rung_and_ultra_controls():
    document = _document()
    profile = document['deployments'][0]['inference_profile']
    profile['reasoning'] = {'support': 'supported', 'wire_format': 'reasoning_effort',
        'can_disable': False, 'supported_efforts': ['low', 'high', 'max'],
        'default_effort': 'max', 'effort_map': {'medium': 'high', 'xhigh': 'max', 'ultra': 'max'}}
    row = _alpha(reasoning_controls(document))['provider-a']
    assert row['depths'] == ['default', 'low', 'high', 'max']
    assert row['default_depth'] == 'max'
    assert row['can_disable'] is False
    profile['reasoning']['supported_efforts'] = ['low', 'high', 'max', 'ultra', 'future_depth']
    row = _alpha(reasoning_controls(document))['provider-a']
    assert row['depths'] == ['default', 'low', 'high', 'max', 'ultra', 'future_depth']
    from lib.llm.reasoning import capture_intent, compile_reasoning
    body = {'_reasoning_intent': capture_intent(thinking_depth='ultra')}
    compile_reasoning(body, 'opaque-name', inference_profile=profile)
    assert body['reasoning_effort'] == 'ultra'


@pytest.mark.parametrize('block', ['credential', 'grant', 'connection', 'access', 'offering', 'protocol'])
def test_ineligible_routes_cannot_narrow_or_widen_controls(block):
    document = _document()
    if block == 'credential':
        document['credentials'][1]['enabled'] = False
    elif block == 'grant':
        document['credentials'][1]['authorization']['models'] = []
    elif block == 'connection':
        document['connections'][1]['enabled'] = False
    elif block == 'access':
        document['provider_accesses'][1]['enabled'] = False
    elif block == 'offering':
        document['offerings'][1]['enabled'] = False
    else:
        document['connections'][1]['protocol'] = 'custom'
    rows = _alpha(reasoning_controls(document))
    if block == 'offering':
        assert 'provider-b' not in rows
    else:
        assert rows['provider-b']['availability'] == 'no_route'
    assert rows['']['depths'] == rows['provider-a']['depths']


def test_duplicate_credentials_do_not_duplicate_controls_or_resolver_work(monkeypatch):
    import lib.model_routing.inference as inference
    document = _document()
    for index in range(32):
        credential = copy.deepcopy(document['credentials'][0])
        credential['credential_id'] = f'extra-{index}'
        document['credentials'].append(credential)
    observed = []
    resolve = inference.resolve_inference_profile

    def counted(**kwargs):
        observed.append(kwargs['deployment']['deployment_id'])
        return resolve(**kwargs)

    monkeypatch.setattr(inference, 'resolve_inference_profile', counted)
    before = copy.deepcopy(document)
    rows = _alpha(reasoning_controls(document))
    assert len(rows) == 3
    assert len(observed) == len(set(observed)) == 3
    assert document == before


def test_projection_materializes_one_authorization_witness_per_deployment(monkeypatch):
    import lib.model_routing.routing as routing
    document = _document()
    for index in range(256):
        credential = copy.deepcopy(document['credentials'][0])
        credential['credential_id'] = f'extra-{index}'
        document['credentials'].append(credential)
    checks = []
    authorized = routing._credential_rejection

    def counted(credential, **kwargs):
        checks.append(credential['credential_id'])
        return authorized(credential, **kwargs)

    monkeypatch.setattr(routing, '_credential_rejection', counted)
    compiler = RouteCandidateCompiler(document)
    candidates = compiler.compile_deployments(NativeModelSelection(ModelRef('creator', 'alpha'), None))
    assert len(candidates) == 2
    assert len(checks) == 2


def test_pending_identity_controls_stay_provider_and_offering_scoped():
    document = _document()
    offering = document['offerings'][0]
    offering['identity_state'] = 'pending_identity'
    offering['pending_model_id'] = 'never-before-seen-release'
    offering.pop('model')
    rows = [row for row in reasoning_controls(document) if row['model_id'] == 'never-before-seen-release']
    assert len(rows) == 1
    assert rows[0]['creator_id'] == ''
    assert rows[0]['provider_id'] == 'provider-a'
    assert rows[0]['offering_id'] == 'alpha-a'
    assert rows[0]['depths'] == ['default']  # pending identity cannot advertise executable controls


@pytest.mark.parametrize('reasoning', [
    {'support': 'supported', 'wire_format': 'reasoning_effort', 'can_disable': True,
     'supported_efforts': ['low', 'high']},
    {'support': 'supported', 'wire_format': 'none', 'can_disable': True,
     'supported_efforts': ['low', 'high']},
    {'support': 'supported', 'wire_format': 'uninstalled-test-plugin', 'can_disable': True,
     'supported_efforts': ['none', 'low', 'high']},
    {'support': 'supported', 'wire_format': 'thinking_type', 'can_disable': True},
    {'support': 'supported', 'wire_format': 'reasoning_split', 'can_disable': True},
    {'support': 'unsupported'}, {'support': 'unknown'},
])
def test_every_advertised_choice_has_the_same_executable_runtime_binding(reasoning):
    from lib.llm.reasoning import ReasoningContractError, capture_intent, compile_reasoning
    document = _document()
    document['deployments'][0]['inference_profile'] = {'reasoning': reasoning}
    before = copy.deepcopy(document)
    row = _alpha(reasoning_controls(document))['provider-a']
    for depth in row['depths']:
        body = {'_reasoning_intent': capture_intent(thinking_depth=depth)}
        compile_reasoning(body, 'unknown-future-name', inference_profile={'reasoning': reasoning})
    if reasoning.get('wire_format') in ('none', 'uninstalled-test-plugin'):
        assert row['depths'] == ['default']
    if reasoning.get('wire_format') == 'reasoning_effort':
        assert 'off' not in row['depths']
        assert row['can_disable'] is None
    assert document == before


def test_declared_none_token_provides_disable_without_duplicate_alias_metadata():
    from lib.llm.reasoning import ReasoningContractError, capture_intent, compile_reasoning
    document = _document()
    p = document['deployments'][0]['inference_profile']
    p['reasoning'].pop('disabled_effort')
    row = _alpha(reasoning_controls(document))['provider-a']
    assert 'off' in row['depths']
    body = {'_reasoning_intent': capture_intent(thinking_depth='off')}
    compile_reasoning(body, 'unknown-future-name', inference_profile=p)
    assert body['reasoning_effort'] == 'none'



def test_reasoning_controls_preserve_every_configured_offering_beyond_old_tail_limit():
    document = _document()
    prototype = document['offerings'][0]
    document['deployments'] = []
    document['offerings'] = [{**copy.deepcopy(prototype), 'offering_id': f'visible-{i:05}'}
                              for i in range(8193)]
    controls = reasoning_controls(document)
    visible = {row['offering_id'] for row in controls if row['offering_id']}
    assert visible == {f'visible-{i:05}' for i in range(8193)}
    assert all(row['availability'] == 'no_route' for row in controls)
