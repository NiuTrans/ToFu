"""Manifest admission and advertised inputs own step execution unchanged."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _document():
    return {
        'pack': 'tofu.site-pack/v1', 'id': 'input-fixture', 'name': 'Input fixture',
        'domains': ['example.com'],
        'operations': [{
            'name': 'inspect', 'description': 'Inspect the requested page',
            'input_schema': {'type': 'object', 'properties': {
                'pages': {'type': 'integer'}, 'chars': {'type': 'integer'}}},
            'output_schema': {'type': 'object'},
            'steps': [
                {'do': 'research', 'url': 'https://example.com', 'max_scrolls': 0,
                 'max_pages_from_param': 'pages', 'max_chars_from_param': 'chars'},
                {'do': 'normalize', 'kind': 'json_payload'},
            ],
        }],
    }


def _parse(raw):
    from lib.site_packs._manifest import parse_manifest
    return parse_manifest(raw, source='input-fixture.json')


@pytest.mark.parametrize('reference', [
    {'max_pages_from_param': 'missing'},
    {'max_chars_from_param': 'missing'},
    {'url': 'https://example.com/page/{missing|floor:1}'},
])
def test_every_research_parameter_reference_is_checked_before_registration(reference):
    from lib.site_packs._manifest import ManifestError
    raw = _document()
    raw['operations'][0]['steps'][0].update(reference)
    with pytest.raises(ManifestError, match='missing'):
        _parse(raw)


@pytest.mark.parametrize('reference', ['max_pages_from_param', 'max_chars_from_param'])
def test_empty_parameter_reference_is_not_an_unvalidated_input_port(reference):
    from lib.site_packs._manifest import ManifestError
    raw = _document()
    raw['operations'][0]['steps'][0][reference] = ''
    with pytest.raises(ManifestError):
        _parse(raw)


@pytest.mark.parametrize('side', ['input_schema', 'output_schema'])
def test_invalid_inline_schema_is_rejected_during_manifest_admission(side):
    from lib.site_packs._manifest import ManifestError
    raw = _document()
    raw['operations'][0][side]['properties'] = {'pages': {'minimum': 'invalid'}}
    with pytest.raises(ManifestError, match=side):
        _parse(raw)


@pytest.mark.parametrize(('side', 'schema'), [
    ('input_schema', {'type': 'array'}),
    ('input_schema', {'type': ['object', 'null']}),
    ('output_schema', {'description': 'missing root type'}),
    ('output_schema', {'type': 'null'}),
    ('output_schema', {'type': ['object', 'null']}),
])
def test_inline_schema_must_be_usable_by_the_shared_adapter(side, schema):
    from lib.site_packs._manifest import ManifestError
    raw = _document()
    if side == 'input_schema':
        schema = {**raw['operations'][0][side], **schema}
    raw['operations'][0][side] = schema
    with pytest.raises(ManifestError):
        _parse(raw)


def test_validated_manifest_owns_nested_input_and_step_snapshots():
    raw = _document()
    operation = raw['operations'][0]
    operation['steps'] = [{'do': 'try', 'steps': operation['steps']}]
    parsed = _parse(raw).operations[0]
    schema, steps = deepcopy(parsed.input_schema), deepcopy(parsed.steps)
    operation['input_schema']['properties']['pages']['type'] = 'string'
    operation['steps'][0]['steps'][0]['max_scrolls'] = 8
    assert parsed.input_schema == schema
    assert parsed.steps == steps


def _adapter(monkeypatch, raw):
    from lib.browser import adapters, protocol
    from lib.site_packs._projection import pack_to_adapter
    adapter = pack_to_adapter(_parse(raw))
    monkeypatch.setattr(adapters, 'get_adapter', lambda identifier: adapter)
    monkeypatch.setattr(protocol, 'require_capabilities', lambda *a, **kw: None)
    monkeypatch.setattr(adapters, 'audit_log', lambda *a, **kw: None)
    return adapters, adapter


def test_zero_scroll_request_survives_public_adapter_execution(monkeypatch):
    from lib.browser.page import BrowserPage
    from lib.browser.sessions import BrowserSessionLease
    adapters, adapter = _adapter(monkeypatch, _document())
    calls = []

    def research(url, **kwargs):
        calls.append((url, kwargs))
        return {'page': {'url': url}, 'result': {'ok': True, 'status': 200, 'data': [],
            'source': {'document_url': url, 'request_url': url, 'response_url': url}}}

    page = BrowserPage(BrowserSessionLease('inputs', '41', 'device', profile='Original'))
    monkeypatch.setattr(page, 'research', research)
    assert adapters.invoke_adapter(
        adapter.id, 'inspect', {'pages': 2, 'chars': 1200}, owner_user_id='41',
        client_id='device', _page=page)['ok']
    assert calls == [('https://example.com', {
        'max_chars': 1200, 'max_scrolls': 0, 'max_pages': 2, 'pagination': 'auto'})]


@pytest.mark.parametrize('params', [
    {'pages': 9}, {'pages': 0}, {'pages': True}, {'pages': 1.5}, {'pages': '2'},
    {'pages': None}, {'chars': 999}, {'chars': 200001},
])
def test_step_budget_refusal_precedes_browser_lease_and_matches_public_schema(monkeypatch, params):
    from jsonschema import Draft202012Validator
    from lib.browser.adapter_errors import AdapterValidationError
    adapters, adapter = _adapter(monkeypatch, _document())
    monkeypatch.setattr(adapters, 'acquire_browser_lease',
                        lambda **kw: pytest.fail('invalid inputs acquired a browser page'))
    with pytest.raises(AdapterValidationError):
        adapters.invoke_adapter(adapter.id, 'inspect', params,
                                owner_user_id='41', client_id='device')
    public_schema = adapter.command('inspect').public_dict()['input_schema']
    assert not Draft202012Validator(public_schema).is_valid(params)


@pytest.mark.parametrize('access', ['read', 'write'])
def test_nested_steps_intersect_bounds_before_any_browser_effect_or_grant(monkeypatch, access):
    from jsonschema import Draft202012Validator
    from lib.browser.adapter_errors import AdapterValidationError
    raw = _document()
    op = raw['operations'][0]
    op['access'] = access
    op['steps'].insert(0, {'do': 'paginate', 'mode': 'scroll',
                           'times_from_param': 'pages', 'max_times': 2})
    op['steps'] = [{'do': 'try', 'steps': op['steps']}]
    if access == 'write':
        op['steps'].insert(0, {'do': 'click', 'selector': '#save'})
    adapters, adapter = _adapter(monkeypatch, raw)
    monkeypatch.setattr(adapters, 'acquire_browser_lease',
                        lambda **kw: pytest.fail('invalid inputs acquired a page'))
    monkeypatch.setattr(adapters, 'require_access',
                        lambda *a, **kw: pytest.fail('invalid inputs entered the grant boundary'))
    with pytest.raises(AdapterValidationError):
        adapters.invoke_adapter(adapter.id, 'inspect', {'pages': 4},
                                owner_user_id='41', client_id='device')
    validator = Draft202012Validator(adapter.command('inspect').input_schema)
    assert validator.is_valid({'pages': 3})
    assert not validator.is_valid({'pages': 4})


def test_authored_constraints_are_not_weakened_by_step_projection(monkeypatch):
    from jsonschema import Draft202012Validator
    raw = _document()
    raw['operations'][0]['input_schema'].update(
        additionalProperties=False, allOf=[{'properties': {'pages': {'maximum': 2}}}])
    _adapters, adapter = _adapter(monkeypatch, raw)
    validator = Draft202012Validator(adapter.command('inspect').input_schema)
    assert validator.is_valid({'pages': 2})
    assert not validator.is_valid({'pages': 3})
    assert not validator.is_valid({'surprise': 2})


def test_detail_budget_is_visible_and_refused_before_navigation(monkeypatch):
    from lib.browser.adapter_errors import AdapterValidationError
    raw = _document()
    raw['operations'][0]['steps'] = [
        {'do': 'navigate', 'url': 'https://example.com'},
        {'do': 'normalize', 'kind': 'detail', 'max_chars_from_param': 'chars'},
    ]
    adapters, adapter = _adapter(monkeypatch, raw)
    monkeypatch.setattr(adapters, 'acquire_browser_lease',
                        lambda **kw: pytest.fail('invalid detail budget navigated'))
    with pytest.raises(AdapterValidationError):
        adapters.invoke_adapter(adapter.id, 'inspect', {'chars': 200001},
                                owner_user_id='41', client_id='device')


def test_explicit_floor_transform_keeps_its_existing_meaning():
    from lib.site_packs._templates import render_template
    raw = _document()
    raw['operations'][0]['steps'][0]['url'] = 'https://example.com/page/{pages|floor:2}'
    _parse(raw)
    assert render_template(raw['operations'][0]['steps'][0]['url'], {'pages': 1}) == (
        'https://example.com/page/2')
