"""Complete-record validation protects nested evidence and live ownership."""
from copy import deepcopy
import uuid

import pytest

from lib.project_dispatch import repository
from lib.project_dispatch.repository import ProjectDispatchError
from tests.project_mod.test_project_dispatch_repository import (
    authorize, context as context, model_attempt,
)

pytestmark = pytest.mark.unit


def record_for(c):
    assignment = authorize(c)
    assignment['modelAttempts'] = [model_attempt('model-root')]
    return {'binding': deepcopy(c.repo.binding), 'assignments': [assignment]}


@pytest.mark.parametrize('segmented', [False, True])
def test_valid_record_preserves_complete_evidence(context, segmented):
    value = record_for(context)
    expected = deepcopy(value)
    context.repo._check_record(value, segmented=segmented)
    assert value == expected


@pytest.mark.parametrize('segmented', [False, True])
@pytest.mark.parametrize('mutation', [
    'request-type', 'unknown-request-authority', 'attempt-type', 'attempt-nonce',
    'nonfinite', 'invalid-unicode', 'foreign-owner', 'duplicate-occurrence',
])
def test_complete_record_rejects_invalid_nested_or_semantic_evidence(context, segmented, mutation):
    value = record_for(context)
    assignment = value['assignments'][0]
    attempt = assignment['modelAttempts'][0]
    if mutation == 'request-type':
        assignment['request']['brief'] = 7
    elif mutation == 'unknown-request-authority':
        assignment['request']['ungrantedAuthority'] = True
    elif mutation == 'attempt-type':
        attempt['attempt_ordinal'] = True
    elif mutation == 'attempt-nonce':
        attempt['account_nonce'] = 'malformed'
    elif mutation == 'nonfinite':
        attempt['reported_tokens'] = float('nan')
    elif mutation == 'invalid-unicode':
        assignment['request']['brief'] = '\ud800'
    elif mutation == 'foreign-owner':
        assignment['grant']['owner'] += 1
    else:
        assignment['modelAttempts'].append(deepcopy(attempt))
    with pytest.raises(ProjectDispatchError):
        context.repo._check_record(value, segmented=segmented)


@pytest.fixture
def schema_walks(monkeypatch):
    """Every jsonschema walk, by definition, through the repository's validator port."""
    walks, validator = [], repository._validator

    class Counted:
        def __init__(self, definition):
            self.definition, self.validator = definition, validator(definition)

        def is_valid(self, value):
            walks.append(self.definition)
            return self.validator.is_valid(value)
    monkeypatch.setattr(repository, '_validator', Counted)
    return walks


def fresh_record(c, walks):
    # A new stored identity per test: process-wide verdicts never decide another test.
    value = record_for(c)
    value['assignments'][0]['id'] = str(uuid.uuid4())
    walks.clear()
    return value


@pytest.mark.parametrize('segmented', [False, True])
def test_unchanged_stored_rows_skip_repeated_schema_walk(context, schema_walks, segmented):
    value = fresh_record(context, schema_walks)
    definition = 'assignment' if segmented else 'record'
    for _ in range(2):
        context.repo._check_record(deepcopy(value), segmented=segmented)
    assert schema_walks.count(definition) == 1
    value['assignments'][0]['modelAttempts'].append(model_attempt('another-root'))
    context.repo._check_record(deepcopy(value), segmented=segmented)
    assert schema_walks.count(definition) == 2
    # The derived admission projection is not stored evidence and is walked on every read.
    assert schema_walks.count('admission') == 3


@pytest.mark.parametrize('segmented', [False, True])
def test_changed_invalid_stored_row_is_walked_and_refused_every_time(context, schema_walks, segmented):
    value = fresh_record(context, schema_walks)
    definition = 'assignment' if segmented else 'record'
    context.repo._check_record(deepcopy(value), segmented=segmented)
    value['assignments'][0]['request']['brief'] = 7
    for _ in range(2):
        with pytest.raises(ProjectDispatchError, match='Invalid dispatch ' + definition):
            context.repo._check_record(deepcopy(value), segmented=segmented)
    assert schema_walks.count(definition) == 3


@pytest.mark.parametrize('segmented', [False, True])
def test_python_only_value_never_shares_its_canonical_json_verdict(context, schema_walks, segmented):
    value = fresh_record(context, schema_walks)
    value['assignments'][0]['toolAttempts'] = []
    context.repo._check_record(deepcopy(value), segmented=segmented)
    # A tuple encodes exactly like the validated list, but JSON Schema refuses it.
    value['assignments'][0]['toolAttempts'] = ()
    with pytest.raises(ProjectDispatchError, match='Invalid dispatch'):
        context.repo._check_record(value, segmented=segmented)


def test_stored_verdicts_are_not_shared_across_definitions(context, schema_walks):
    value = fresh_record(context, schema_walks)
    context.repo._check_record(deepcopy(value), segmented=True)
    value['requestBudgets'] = [deepcopy(value['assignments'][0])]
    with pytest.raises(ProjectDispatchError, match='Invalid dispatch coordinatorRequestBudget'):
        context.repo._check_record(deepcopy(value), segmented=True)
    assert schema_walks.count('assignment') == 1
    assert schema_walks.count('coordinatorRequestBudget') == 1
