"""Model reference inheritance is reversible, source-scoped and lossless.

Exercise native query projection with an independent consumer; canonical storage
and provider delivery are covered by the companion backend specification.
"""

from __future__ import annotations

import json

import pytest

from lib.tools.artifact_result_projection import project_artifact_result
from tests.tools.test_artifact_read_text_projection import decode_read_view
from tests.tools.test_artifact_result_projection import OTHER, REF, page, raw

pytestmark = pytest.mark.unit


def restore_references(value, operation):
    """Resolve only the documented page-local reference default."""
    value = json.loads(json.dumps(value))
    if 'artifactRef' not in value:
        value['items'] = [restore_references(item, operation) if item.get('status') == 'ok'
                          else item for item in value['items']]
        return value
    key = 'provenance' if operation == 'read_tool_artifact' else 'items'
    for item in value.get(key, []):
        item.setdefault('artifactRef', value['artifactRef'])
    return value


def native_rows(operation, reference=REF):
    if operation == 'read_tool_artifact':
        return 'provenance', [
            {'artifactRef': reference, 'path': ['/parts/0'], 'segment': [0],
             'start': 0, 'end': 5},
            {'artifactRef': OTHER, 'path': [{'name': 'child', 'callId': 'same'}],
             'segment': [1], 'start': 5, 'end': 9},
            {'artifactRef': reference, 'path': ['/parts/2'], 'segment': [2],
             'start': 9, 'end': 9007199254740993},
        ]
    return 'items', [
        {'artifactRef': reference, 'path': ['/parts/0'], 'offset': 0, 'text': 'first'},
        {'artifactRef': OTHER, 'path': ['/parts/0'], 'offset': 1, 'text': 'child'},
        {'artifactRef': reference, 'path': ['/parts/2'], 'offset': 9007199254740993,
         'text': 'last'},
    ]


@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
@pytest.mark.parametrize('partial', [False, True])
def test_equal_parent_references_are_inherited_without_losing_source_identity(
        operation, partial):
    key, rows = native_rows(operation)
    source = page(operation, **{key: rows}, truncated=partial,
                  nextCursor='trq3.next.signature' if partial else None,
                  sourceCoverage='unknown', retentionCoverage='partial',
                  unavailable=[{'artifactRef': OTHER, 'reason': 'expired'}])
    model = decode_read_view(project_artifact_result(operation, raw(source)))

    assert 'artifactRef' not in model[key][0]
    assert model[key][1]['artifactRef'] == OTHER
    assert 'artifactRef' not in model[key][2]
    assert restore_references(model, operation)[key] == rows
    assert model['artifactRef'] == REF
    assert model['sourceCoverage'] == 'unknown' and model['retentionCoverage'] == 'partial'
    assert model['unavailable'] == source['unavailable']
    assert model.get('nextCursor') == source['nextCursor']


@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_each_batch_page_binds_its_own_parent_and_keeps_error_rows(operation):
    key, first = native_rows(operation)
    _, second = native_rows(operation, reference='tool-result:' + 'c' * 64)
    failed = {'index': 1, 'status': 'error', 'artifactRef': OTHER,
              'error': {'code': 'artifact_unavailable', 'retryable': False}}
    pages = [page(operation, index=0, **{key: first}), failed,
             page(operation, artifactRef='tool-result:' + 'c' * 64, index=2,
                  **{key: second})]
    model = decode_read_view(project_artifact_result(operation, raw({
        'status': 'partial_failure', 'items': pages})))
    restored = restore_references(model, operation)

    assert restored['items'][0][key] == first
    assert restored['items'][2][key] == second
    assert model['items'][1] == failed
    assert [item['index'] for item in model['items']] == [0, 1, 2]
    assert 'artifactRef' not in model['items'][0][key][0]
    assert 'artifactRef' not in model['items'][2][key][0]


@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
@pytest.mark.parametrize('parent', ['tr:read:abcdef12', '', OTHER])
def test_different_or_unknown_parent_never_relabels_a_digest(operation, parent):
    key, rows = native_rows(operation)
    source = page(operation, artifactRef=parent, **{key: rows[:1]})
    model = decode_read_view(project_artifact_result(operation, raw(source)))
    assert model[key] == rows[:1]
    assert model['artifactRef'] == parent


@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_unknown_row_fields_and_nested_application_references_stay_exact(operation):
    key, rows = native_rows(operation)
    rows[0]['future'] = {'artifactRef': REF, 'offset': -0.0}
    source = page(operation, **{key: rows[:1]},
                  unknown={'artifactRef': REF, 'provenance': rows})
    model = decode_read_view(project_artifact_result(operation, raw(source)))
    assert model[key] == rows[:1]
    assert model['unknown'] == source['unknown']


@pytest.mark.parametrize('change', [
    {'segment': ['0']}, {'start': '0'}, {'end': False}, {'path': None},
    {'artifactRef': ''}, {'future': 1},
])
def test_unsupported_provenance_is_not_normalized(change):
    _, rows = native_rows('read_tool_artifact')
    row = {**rows[0], **change}
    model = decode_read_view(project_artifact_result('read_tool_artifact', raw(
        page('read_tool_artifact', provenance=[row]))))
    assert model['provenance'] == [row]


def test_grouped_matches_keep_all_exact_offsets_and_inherit_one_source():
    matches = [{'artifactRef': REF, 'offset': offset, 'path': ['/parts/0'],
                'text': 'same exact quote'} for offset in [0, 0, 9007199254740993]]
    model = json.loads(project_artifact_result('search_tool_artifact', raw(page(items=matches))))
    assert model['items'] == [{'text': 'same exact quote', 'path': ['/parts/0'],
                               'offsets': [0, 0, 9007199254740993]}]
    row = restore_references(model, 'search_tool_artifact')['items'][0]
    assert [{key: value for key, value in row.items() if key != 'offsets'} | {'offset': offset}
            for offset in row['offsets']] == matches


@pytest.mark.parametrize('reference', [None, 17, False])
def test_unknown_provenance_reference_shape_does_not_gain_parent_identity(reference):
    _, rows = native_rows('read_tool_artifact')
    row = {**rows[0], 'artifactRef': reference}
    if reference is None:
        del row['artifactRef']
    source = raw(page('read_tool_artifact', provenance=[row]))
    assert project_artifact_result('read_tool_artifact', source) is None
    from lib.tools.result_surfaces import project_model_source
    assert project_model_source('read_tool_artifact', source) == source
