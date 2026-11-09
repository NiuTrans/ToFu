"""Request provenance remains discoverable without exposing provider bodies."""

import json

import pytest

from debug.inspect_conversation import _render_request_evidence, DiagnosticAPIError

pytestmark = pytest.mark.unit


def test_request_archive_metadata_is_discoverable_without_body_or_prompt():
    paths = []
    class API:
        def get(self, path, **kwargs):
            paths.append(path)
            if path.endswith('/requests'):
                return {'requests': [{'roundNum': 3, 'turn': 'request:span'}]}
            return {'messages': [{'content': 'private-prompt'}],
                    'rawArchives': [{'archiveId': 'raw-123', 'taskId': 'task-1',
                                     'attemptId': 'attempt-1', 'integrity': 'partial',
                                     'truncationReason': 'transport_interrupted',
                                     'responseAvailable': True,
                                     'details': {'traceId': 'trace-1', 'statusCode': 200}}]}
    document = {'messages': [{'toolRounds': [
        {'inspectorRef': {'taskId': 'task-1'}}, {'taskId': 'task-1'}]}]}
    output = '\n'.join(_render_request_evidence(API(), document))
    assert 'raw-123' in output and 'trace-1' in output and 'attempt-1' in output
    assert 'private-prompt' not in output
    assert len(paths) == 2
    assert paths[-1].endswith('/requests/3?turn=request%3Aspan')


def test_request_evidence_is_bounded_and_reports_read_failure():
    paths = []
    class API:
        def get(self, path, **kwargs):
            paths.append(path)
            raise DiagnosticAPIError('read budget exhausted')
    document = {'messages': [{'toolRounds': [
        {'taskId': f'task-{index}'} for index in range(100)]}]}
    output = _render_request_evidence(API(), document)
    assert len(paths) == len(output) == 4
    assert all('read budget exhausted' in line for line in output)


def test_request_evidence_does_not_accept_another_tasks_archive():
    class API:
        def get(self, path, **kwargs):
            if path.endswith('/requests'):
                return {'requests': [{'roundNum': 1}]}
            return {'rawArchives': [{'taskId': 'foreign', 'archiveId': 'foreign-archive'}]}
    output = json.dumps(_render_request_evidence(API(), {
        'messages': [{'toolRounds': [{'taskId': 'owned-task'}]}]}))
    assert 'ownership mismatch' in output
    assert 'foreign-archive' not in output


@pytest.mark.parametrize('owned_source', [None, 'parent'])
def test_parent_evidence_selects_own_request_from_merged_child_catalog(owned_source):
    paths = []

    class API:
        def get(self, path, **kwargs):
            paths.append(path)
            if path.endswith('/requests'):
                return {'requests': [
                    {'roundNum': 14, 'turn': 'request:parent',
                     'sourceTaskId': owned_source},
                    {'roundNum': 25, 'turn': 'swarm-agent:child',
                     'sourceTaskId': 'parent#agent:child'},
                ]}
            if path.endswith('/requests/14?turn=request%3Aparent'):
                return {'rawArchives': [
                    {'taskId': 'parent', 'archiveId': 'parent-archive'}]}
            raise DiagnosticAPIError('wrong request owner')

    output = '\n'.join(_render_request_evidence(API(), {
        'messages': [{'toolRounds': [{'taskId': 'parent'}]}]}))
    assert 'parent-archive' in output
    assert 'wrong request owner' not in output
    assert len(paths) == 2
    assert paths[-1].endswith('/requests/14?turn=request%3Aparent')


def test_child_only_catalog_does_not_fetch_child_request_as_parent():
    paths = []

    class API:
        def get(self, path, **kwargs):
            paths.append(path)
            if path.endswith('/requests'):
                return {'requests': [
                    {'roundNum': 25, 'turn': 'swarm-agent:child',
                     'sourceTaskId': 'parent#agent:child'}]}
            raise DiagnosticAPIError('wrong request owner')

    output = '\n'.join(_render_request_evidence(API(), {
        'messages': [{'toolRounds': [{'taskId': 'parent'}]}]}))
    assert 'no task-owned request' in output
    assert paths == ['/api/v4/tasks/parent/requests']
