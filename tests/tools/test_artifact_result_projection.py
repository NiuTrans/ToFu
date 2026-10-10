"""Only declared native artifact defaults may disappear from a model view."""
import json
import pytest
from lib.tools.artifact_result_projection import project_artifact_result
from lib.tools.result_surfaces_generated import CONTRACT
from lib.tools.result_envelope import split_tool_result_delivery
from lib.tasks_pkg.compaction._budget import budget_tool_result_v2

pytestmark = pytest.mark.unit
REF = 'tool-result:' + 'a' * 64
OTHER = 'tool-result:' + 'b' * 64


def page(name='search_tool_artifact', **extra):
    value = {'status': 'ok', 'artifactRef': REF, 'coverage': 'complete',
             'scope': 'text_only', 'nextCursor': None, 'truncated': False,
             'resumeCursor': '0', 'unavailable': [], 'outputTruncated': False}
    value.update({'content': 'Exact evidence\n世界', 'offset': 0, 'sizeBytes': 44}
                 if name == 'read_tool_artifact' else {'query': '"depth"', 'items': []})
    return {**value, **extra}


def raw(value):
    return json.dumps(value, ensure_ascii=False)


def restore_defaults(value):
    return {**CONTRACT['artifactProjectionDefaults'], **value}


@pytest.mark.parametrize('name', ['read_tool_artifact', 'search_tool_artifact'])
def test_declared_defaults_are_recoverable_and_nothing_else_is_removed(name):
    value = page(name, unknown={'nextCursor': None, 'truncated': False, 'value': ''})
    result = json.loads(project_artifact_result(name, raw(value)))
    assert restore_defaults(result) == value
    assert result['artifactRef'] == REF
    assert result['coverage'] == 'complete' and result['scope'] == 'text_only'
    assert result['unknown'] == value['unknown']
    assert len(raw(result).encode()) < len(raw(value).encode())


def test_zero_matches_mixed_errors_indexes_and_source_coverage_are_not_promoted():
    first = page(index=0, coverage='incomplete', sourceCoverage='unknown')
    failed = {'index': 1, 'status': 'error', 'artifactRef': OTHER,
              'error': {'code': 'artifact_unavailable', 'retryable': False}, 'future': 123}
    value = {'status': 'partial_failure', 'items': [first, failed], 'unknown': {'count': 2}}
    projected = json.loads(project_artifact_result('search_tool_artifact', raw(value)))
    assert projected['status'] == 'partial_failure'
    assert projected['items'][1] == failed
    assert projected['items'][0]['items'] == []
    assert projected['items'][0]['sourceCoverage'] == 'unknown'
    assert projected['items'][0]['coverage'] == 'incomplete'
    assert projected['items'][0]['index'] == 0
    assert restore_defaults(projected['items'][0]) == first


@pytest.mark.parametrize('flag', ['truncated', 'outputTruncated'])
def test_partial_page_keeps_all_defaults_and_exact_signed_continuation(flag):
    value = page(**{flag: True, 'nextCursor': 'trq1.signature:opaque',
                    'resumeCursor': '0', 'unavailable': []})
    projected = json.loads(project_artifact_result('search_tool_artifact', raw(value)))
    assert projected == value


def test_nondefault_gaps_resume_and_child_refs_all_survive():
    matches = [{'offset': 12, 'text': 'Exact\nquote "', 'artifactRef': REF},
               {'offset': 9007199254740993, 'text': 'Different child', 'artifactRef': OTHER}]
    value = page(items=matches, resumeCursor='trq1.original', nextCursor='trq1.next',
                 unavailable=[{'artifactRef': OTHER, 'reason': 'expired'}])
    projected = json.loads(project_artifact_result('search_tool_artifact', raw(value)))
    assert restore_defaults(projected) == value
    assert projected['items'] == matches


def test_read_keeps_media_descriptors_and_source_retention_evidence():
    value = page('read_tool_artifact', media=[{'reference': 'tofu-media:owned',
                 'mimeType': 'image/png'}], sourceCoverage='partial', retentionCoverage='complete')
    projected = json.loads(project_artifact_result('read_tool_artifact', raw(value)))
    assert restore_defaults(projected) == value
    assert projected['media'] == value['media']


@pytest.mark.parametrize('value', [
    {'status': 'error', 'error': {'code': 'scope_denied'}},
    {'status': 'ok', 'final_answer': 'A\nB', 'nextCursor': 'opaque'},
    {'status': 'ok', 'items': []},
    {'status': 'ok', 'items': [{'index': 0, 'status': 'ok', 'output': 'custom'}]},
])
def test_unknown_or_error_only_shapes_pass_through(value):
    assert project_artifact_result('search_tool_artifact', raw(value)) is None


@pytest.mark.parametrize('text', [
    '{"status":"ok","status":"error"}', '[1,2]', 'not json',
    '{"status":"ok","x":NaN}',
])
def test_duplicate_keys_non_json_and_nonfinite_are_not_normalized(text):
    assert project_artifact_result('search_tool_artifact', text) is None


def test_unknown_numeric_tokens_keep_every_digit_exponent_and_negative_zero():
    source = raw(page()).replace('"items": []', '"items": [],"unknown":{"decimal":1.234567890123456789,"small":1e-999,"zero":-0,"big":9007199254740993123456789}')
    result = project_artifact_result('search_tool_artifact', source)
    for token in ('1.234567890123456789', '1e-999', '-0', '9007199254740993123456789'):
        assert token in result


def test_unknown_tool_does_not_gain_artifact_projection():
    assert project_artifact_result('custom_tool', raw(page())) is None


def test_budget_delivery_retains_original_decoded_source_and_complete_view(monkeypatch):
    value = page()
    saved = []
    def persist(canonical, *, user_id):
        saved.append((user_id, canonical.text()))
        return 'tool-result:' + 'c' * 64
    monkeypatch.setattr('lib.tools.result_value.persist_result', persist)
    source = raw(value)
    envelope = budget_tool_result_v2('search_tool_artifact', source, user_id=42)
    delivery = split_tool_result_delivery(envelope)
    assert restore_defaults(json.loads(delivery.model_text)) == value
    assert saved == [(42, source)]
    assert json.loads(envelope.canonical.text()) == value
    assert delivery.evidence['viewCoverage'] == 'complete'
    assert not delivery.evidence['truncated']
