"""Generic failure cards preserve authoritative evidence without JSON headers."""
import json

import pytest

from tests.frontend.test_artifact_continuation_chip import _render, _summary, _text

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('source', ['text', 'object', 'results', 'reload'])
def test_historical_mcp_failure_disclosure(tmp_path, source):
    reason = 'experiment has not run yet'
    envelope = {'status': 'error', 'summary': 'MCP Error: ' + json.dumps({
        'ok': False, 'error': reason, 'error_detail': {'message': reason},
        'nextCursor': 'trq1.private.cursor', 'html': '<img src=x onerror=alert(1)>',
    }), 'artifactRef': 'tool-result:private'}
    raw = json.dumps(envelope)
    value = {'status': 'error', 'toolName': 'experiment_report', 'roundNum': 9,
             'query': 'Read experiment report', 'results': []}
    if source == 'reload':
        value['_snapshotToolResult'] = {'preview': raw, 'chars': len(raw) + 2000}
    elif source == 'results':
        value['results'] = [{'content': raw}]
    else:
        value['toolContent'] = envelope if source == 'object' else raw
    before = json.dumps(value)
    (markup,) = _render([value], tmp_path)
    summary = _summary(markup)
    assert reason in _text(summary)
    assert 'trq1.private' not in summary and 'tool-result:private' not in summary
    assert 'MCP Error:' not in summary and 'onerror' not in summary
    assert 'ptool-badge-err' in summary
    assert '<img src=x' not in markup
    assert markup.count('<details ') == 1 and '<details open' not in markup
    assert 'trq1.private.cursor' in _text(markup)
    assert json.dumps(value) == before
    if source == 'reload':
        assert 'data-lazy-tool-result' in markup


@pytest.mark.parametrize('raw', ['MCP Error: {broken', '{"error":"' + 'x' * 40000,
                                'Error artifactRef=tool-result:private trq1.signed'],
                         ids=['malformed MCP', 'oversized JSON', 'private metadata'])
def test_unparseable_private_failure_has_safe_generic_header(tmp_path, raw):
    (markup,) = _render([{'status': 'error', 'toolName': 'experiment_report',
        'roundNum': 9, 'query': 'Read experiment report', 'toolContent': raw}], tmp_path)
    summary = _summary(markup)
    # This isolated harness translates known chip keys and echoes other keys.
    assert 'tool.failedTip' in _text(summary)
    assert 'trq1.signed' not in summary and '{' not in _text(summary)
    assert 'ptool-badge-err' in summary
    assert markup.count('<details ') == 1
