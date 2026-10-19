"""Registered delivery surface stays complete and release-current."""
from __future__ import annotations

import json

import pytest

from scripts.gen_api_endpoint_index import OUTPUT, ROOT, generate, route_app

pytestmark = pytest.mark.unit


def test_index_matches_every_registered_operation_and_owner():
    app = route_app()
    document = generate(app)
    assert document == json.loads(OUTPUT.read_text())
    assert document['contract'] == 'tofu.api-endpoint-index/v1'
    assert document['apiMajor'] == 4
    assert not document['unresolved']
    actual = [(e['method'], e['path'], e['transport']) for e in document['endpoints']]
    expected = [(method, rule.rule, 'websocket' if rule.websocket else 'http')
                for rule in app.url_map.iter_rules()
                for method in set(rule.methods or ()) - {'HEAD', 'OPTIONS'}]
    assert sorted(actual) == sorted(expected)
    assert len(actual) == len(set(actual))
    for endpoint in document['endpoints']:
        assert (ROOT / endpoint['owner']).is_file()
        assert endpoint['handler']
        assert not endpoint['path'].startswith(('/api/v1/', '/api/v3/'))
    assert any(e['responseContract'] == 'tofu.conversation-sync/v3' for e in document['endpoints'])
    assert any(e['transport'] == 'websocket' for e in document['endpoints'])
    assert any(e['path'] == '/api/v4/paper/qa/abort/<task_id>' for e in document['endpoints'])


def test_index_keeps_missing_metadata_explicit():
    from quart import Blueprint
    from lib.app.app_factory import create_base_app

    app = create_base_app('index-metadata')
    bp = Blueprint('index_example', __name__)

    @bp.get('/api/v4/undocumented')
    async def example():
        return 'example'

    app.register_blueprint(bp)
    endpoint, = generate(app)['endpoints']
    assert endpoint['documented'] is False
    assert endpoint['public'] is None
    assert endpoint['scope'] is None
    assert endpoint['responseContract'] is None


def test_index_reports_unresolvable_source_owners():
    from lib.app.app_factory import create_base_app

    app = create_base_app('index-unresolved')
    # A foreign callable must not disappear without an explicit diagnostic.
    app.add_url_rule('/foreign', 'foreign', len)
    document = generate(app)
    assert not document['endpoints']
    assert document['unresolved'] == [{'path': '/foreign', 'endpoint': 'foreign'}]
