"""Guard for contracts/site_pack_v1.schema.json (docs/SITE_PACKS.md).

Pins the schema as valid draft 2020-12, proves every shipped core pack
validates against it, and rejects the failure shapes the loader must skip:
unknown steps, budget violations, and cross-field references the semantic
layer owns.
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / 'contracts' / 'site_pack_v1.schema.json'
CORE_PACKS = ROOT / 'lib' / 'site_packs' / 'data'


@pytest.fixture(scope='module')
def schema():
    return json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))


def test_schema_is_valid_draft_2020_12(schema):
    Draft202012Validator.check_schema(schema)


def test_shipped_core_packs_validate(schema):
    from lib.site_packs import load_packs
    from lib.site_packs._manifest import parse_manifest

    paths = sorted(CORE_PACKS.glob('*.json'))
    assert paths, 'core pack directory must not be empty'
    validator = Draft202012Validator(schema)
    for path in paths:
        raw = json.loads(path.read_text(encoding='utf-8'))
        errors = list(validator.iter_errors(raw))
        assert not errors, f'{path.name}: {errors[0].message}'
        parse_manifest(raw, source=path.name)
    registered = load_packs()
    assert {p['id'] for p in (json.loads(p.read_text()) for p in paths)} \
        <= set(registered)


def _pack(**overrides):
    base = {
        'pack': 'tofu.site-pack/v1',
        'id': 'demo-site',
        'name': 'Demo',
        'domains': ['example.com'],
        'operations': [{
            'name': 'search',
            'description': 'demo search',
            'steps': [
                {'do': 'navigate', 'url': 'https://example.com/?q={query}'},
                {'do': 'normalize', 'kind': 'search_records',
                 'query_from_param': 'query', 'limit_from_param': 'limit',
                 'source_name': 'Demo'},
            ],
            'input_schema': {
                'type': 'object',
                'properties': {'query': {'type': 'string'},
                               'limit': {'type': 'integer'}},
            },
        }],
    }
    base.update(overrides)
    return base


def test_unknown_step_type_rejected(schema):
    from lib.site_packs._manifest import ManifestError, parse_manifest
    bad = _pack()
    bad['operations'][0]['steps'].insert(1, {'do': 'teleport'})
    with pytest.raises(ManifestError):
        parse_manifest(bad, source='test')


def test_step_budgets_enforced(schema):
    from lib.site_packs._manifest import ManifestError, parse_manifest
    bad = _pack()
    bad['operations'][0]['steps'].insert(
        1, {'do': 'wait', 'selector': 'a', 'timeout': 600})
    with pytest.raises(ManifestError):
        parse_manifest(bad, source='test')


def test_step_param_reference_must_exist_in_input_schema():
    from lib.site_packs._manifest import ManifestError, parse_manifest
    bad = _pack()
    bad['operations'][0]['steps'][0] = {
        'do': 'navigate', 'url': 'https://example.com/?q={missing}'}
    with pytest.raises(ManifestError, match='missing'):
        parse_manifest(bad, source='test')


def test_click_requires_write_access():
    from lib.site_packs._manifest import ManifestError, parse_manifest
    bad = _pack()
    bad['operations'][0]['steps'].insert(
        1, {'do': 'click', 'selector': '#go'})
    with pytest.raises(ManifestError, match='click'):
        parse_manifest(bad, source='test')
    good = _pack()
    good['operations'][0]['access'] = 'write'
    good['operations'][0]['steps'].insert(1, {'do': 'click', 'selector': '#go'})
    parse_manifest(good, source='test')


def test_ensure_auth_requires_auth_section():
    from lib.site_packs._manifest import ManifestError, parse_manifest
    bad = _pack()
    bad['operations'][0]['steps'].insert(1, {'do': 'ensure_auth'})
    with pytest.raises(ManifestError, match='auth'):
        parse_manifest(bad, source='test')
