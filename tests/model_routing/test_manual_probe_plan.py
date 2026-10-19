"""Manual matrix plans are transient selections, never routing-authority edits."""
from copy import deepcopy

import pytest

from lib.model_routing import ModelRoutingError
from lib.model_routing.probe_plan import (
    manual_probe_bundle,
    matrix_probe_plan,
    provider_bundle,
)
from lib.provider.provider_probe import build_probe_work
from tests.model_routing.test_model_routing_contract import _full_document

pytestmark = pytest.mark.unit


def manual_document():
    """One disabled saved row/connection beside an enabled sibling."""
    document = _full_document()
    document['connections'].append({
        **deepcopy(document['connections'][0]),
        'connection_id': 'connection-off',
        'base_url': 'https://disabled.example/v1',
        'enabled': False,
        'extra_headers': {'X-Transport': 'disabled'},
    })
    document['offerings'][0]['enabled'] = False
    document['deployments'][0]['connection_id'] = 'connection-off'
    document['credentials'][0]['authorization']['models'] = []
    document['credentials'][0]['authorization']['connection_ids'] = []
    return document


def target(wire='alpha-wire-a', *, offering='alpha-a', connection='connection-off'):
    return {'offering_id': offering, 'connection_id': connection,
            'wire_model_id': wire}


def plan_work(bundle, **kwargs):
    plan = matrix_probe_plan(None, None, bundle, **kwargs)
    return build_probe_work(
        plan['provider'], plan['models'], plan['api_keys'],
        request_contexts=plan['request_contexts'],
        credential_contexts=plan['credential_contexts'])


def test_target_projection_is_exact_trimmed_and_deeply_read_only():
    document = manual_document()
    before_document = deepcopy(document)
    bundle = provider_bundle(document, 'provider-a')
    before_bundle = deepcopy(bundle)
    targets = [target('  unsaved/provider-name  ')]
    before_targets = deepcopy(targets)

    projected = manual_probe_bundle(bundle, targets)
    assert [(row['offering_id'], row['connection_id'], row['wire_model_id'])
            for row in projected['deployments']] == [
                ('alpha-a', 'connection-off', 'unsaved/provider-name')]
    work = plan_work(projected, manual=True)
    assert [(row[2], row[3], row[5]) for row in work] == [
        ('alpha', 'unsaved/provider-name', 'https://disabled.example/v1')]
    assert work[0][7]['extra_headers'] == {'X-Transport': 'disabled'}
    assert projected['offerings'][0]['enabled'] is False
    assert projected['connections'][-1]['enabled'] is False
    assert projected['credentials'][0]['authorization']['models'] == []
    projected['credentials'][0]['authorization']['models'].append({'model_id': 'changed'})
    projected['connections'][-1]['extra_headers']['X-Transport'] = 'changed'
    assert document == before_document
    assert bundle == before_bundle
    assert targets == before_targets


def test_target_projection_keeps_multiple_unsaved_names_in_selection_order():
    bundle = provider_bundle(manual_document(), 'provider-a')
    projected = manual_probe_bundle(bundle, [target('new-b'), target('new-a')])
    assert [row[3] for row in plan_work(projected, manual=True)] == ['new-b', 'new-a']
    assert 'beta-wire-a' not in [row['wire_model_id'] for row in projected['deployments']]


@pytest.mark.parametrize('targets', [
    None, {}, 'alpha-wire-a', [], [None], ['alpha-wire-a'],
    [{}], [{'offering_id': 'alpha-a', 'connection_id': 'connection-off'}],
    [target(offering='unknown')], [target(connection='unknown')],
    [target(offering='alpha-b')], [target(connection='connection-b')],
    [target(offering='')], [target(connection='')],
    [target(offering=[])], [target(connection={})], [target(123)],
    [target('')], [target('   ')], [target('x' * 257)],
    [target('x\x00y')], [target('x\ny')], [target('x\ry')],
    [target('\twire')], [target('wire\n')], [target('x\x7fy')],
    [{**target(), 'base_url': 'https://untrusted.example/v1'}],
    [target()] * 1025,
])
def test_invalid_target_never_falls_back_to_saved_or_unselected_rows(targets):
    bundle = provider_bundle(manual_document(), 'provider-a')
    before = deepcopy(bundle)
    before_targets = deepcopy(targets)
    with pytest.raises(ModelRoutingError) as error:
        manual_probe_bundle(bundle, targets)
    assert error.value.kind == 'provider_probe_invalid_target'
    assert bundle == before
    assert targets == before_targets


def test_wire_length_limit_applies_after_trimming():
    bundle = provider_bundle(manual_document(), 'provider-a')
    projected = manual_probe_bundle(bundle, [target('  ' + 'x' * 256 + '  ')])
    assert projected['deployments'][0]['wire_model_id'] == 'x' * 256


@pytest.mark.parametrize('connection_access', ['access-b', '', None])
def test_target_requires_same_nonempty_provider_access(connection_access):
    bundle = provider_bundle(manual_document(), 'provider-a')
    bundle['connections'][-1]['provider_access_id'] = connection_access
    before = deepcopy(bundle)
    with pytest.raises(ModelRoutingError) as error:
        manual_probe_bundle(bundle, [target()])
    assert error.value.kind == 'provider_probe_invalid_target'
    assert bundle == before


def test_second_access_of_same_provider_cannot_borrow_first_access_offering():
    document = manual_document()
    document['provider_accesses'].append({
        'provider_access_id': 'access-a-second', 'provider_id': 'provider-a',
        'enabled': False,
    })
    document['connections'][-1]['provider_access_id'] = 'access-a-second'
    bundle = provider_bundle(document, 'provider-a')
    assert len(bundle['connections']) == 2
    with pytest.raises(ModelRoutingError) as error:
        manual_probe_bundle(bundle, [target()])
    assert error.value.kind == 'provider_probe_invalid_target'


@pytest.mark.parametrize('conflicting', [
    target('same-wire', offering='beta-a'),
    target('same-wire', connection='connection-a'),
])
def test_duplicate_wire_cannot_select_conflicting_associations(conflicting):
    bundle = provider_bundle(manual_document(), 'provider-a')
    with pytest.raises(ModelRoutingError) as error:
        manual_probe_bundle(bundle, [target(' same-wire '), conflicting])
    assert error.value.kind == 'provider_probe_invalid_target'


def test_duplicate_wire_with_same_association_is_one_cell():
    bundle = provider_bundle(manual_document(), 'provider-a')
    projected = manual_probe_bundle(bundle, [target('same'), target(' same ')])
    assert [row[3] for row in plan_work(projected, manual=True)] == ['same']


@pytest.mark.parametrize('offering_enabled,connection_enabled', [
    (False, True), (True, False), (False, False), (True, True),
])
def test_manual_includes_disabled_while_default_automatic_scope_is_unchanged(
        offering_enabled, connection_enabled):
    document = manual_document()
    document['offerings'][0]['enabled'] = offering_enabled
    document['connections'][-1]['enabled'] = connection_enabled
    bundle = provider_bundle(document, 'provider-a')
    before = deepcopy(bundle)
    automatic = plan_work(bundle)
    explicit_automatic = plan_work(bundle, manual=False)
    manual = plan_work(bundle, manual=True)
    expected = {'beta-wire-a'}
    if offering_enabled and connection_enabled:
        expected.add('alpha-wire-a')
    assert {row[3] for row in automatic} == expected
    assert automatic == explicit_automatic
    assert {row[3] for row in manual} == {'alpha-wire-a', 'beta-wire-a'}
    assert next(row[5] for row in manual if row[3] == 'alpha-wire-a') == (
        'https://disabled.example/v1')
    assert bundle == before


def test_manual_all_off_is_probeable_without_enabling_access_or_grants():
    document = manual_document()
    for key in ('provider_accesses', 'offerings', 'connections', 'credentials'):
        for row in document[key]:
            row['enabled'] = False
    bundle = provider_bundle(document, 'provider-a')
    before = deepcopy(bundle)
    assert {row[3] for row in plan_work(bundle, manual=True)} == {
        'alpha-wire-a', 'beta-wire-a'}
    with pytest.raises(ModelRoutingError) as error:
        plan_work(bundle)
    assert error.value.kind == 'provider_probe_no_connection'
    assert bundle == before


def test_automatic_all_offerings_off_still_has_no_enabled_scope():
    bundle = provider_bundle(manual_document(), 'provider-a')
    for row in bundle['offerings']:
        row['enabled'] = False
    with pytest.raises(ModelRoutingError) as error:
        plan_work(bundle)
    assert error.value.kind == 'provider_probe_no_offerings'
    assert {row[3] for row in plan_work(bundle, manual=True)} == {
        'alpha-wire-a', 'beta-wire-a'}
