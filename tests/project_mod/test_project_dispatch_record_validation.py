"""Complete-record validation protects nested evidence and live ownership."""
from copy import deepcopy

import pytest

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
