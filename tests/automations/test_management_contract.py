"""Management wire admission and self-contained canonical schema projection."""
from copy import deepcopy
import pytest
from jsonschema import Draft202012Validator

from lib.automations.errors import AutomationError
from lib.automations.management_contract import fragment, schema, validate
from tests.support.automation_fixtures import clock_definition

pytestmark = pytest.mark.unit


def test_contract_embeds_canonical_definition_without_network_resolution():
    Draft202012Validator.check_schema(schema())
    body = {'expectedRevision': 0, 'requestId': 'publish-1', 'definition': clock_definition()}
    validate('PublishRequest', body)
    Draft202012Validator(fragment('PublishRequest')).validate(body)
    bad = deepcopy(body)
    bad['definition']['steps'][0]['after'] = 'show'
    with pytest.raises(AutomationError):
        validate('PublishRequest', bad)


@pytest.mark.parametrize('change', [
    {'expectedRevision': True}, {'expectedRevision': -1}, {'requestId': ''},
    {'requestId': 'x' * 129}, {'ownerId': 42}, {'principal': {}}, {'occurredAt': 1},
])
def test_request_rejects_identity_forgery_and_invalid_cas(change):
    with pytest.raises(AutomationError):
        validate('RevisionRequest', {'expectedRevision': 1, 'requestId': 'run-1', **change})


def test_publication_bounds_nested_conditions_before_recursive_validation():
    rule = clock_definition()
    condition = True
    for _ in range(100):
        condition = {'not': condition}
    rule['steps'][0]['when'] = condition
    with pytest.raises(AutomationError) as exc:
        validate('PublishRequest', {'expectedRevision': 0, 'requestId': 'nested', 'definition': rule})
    assert exc.value.code == 'automation_document_too_complex'


@pytest.mark.parametrize('change', [{'stepId':''},{'stepId':'../write'}, {'stepId':'x'*65},
    {'arguments':{}},{'actor_principal':{}},{'expectedRevision':True},{'expectedRevision':0},{'requestId':'approval\n'},{'stepId':'write\n'}])
def test_approval_request_accepts_only_owned_revision_step_and_request_identity(change):
    body = {'expectedRevision':1,'requestId':'approval','stepId':'write'}
    validate('ApproveRequest',body)
    with pytest.raises(AutomationError):
        validate('ApproveRequest',{**body,**change})


def test_time_presentation_output_derives_the_registered_action_contract():
    from lib.automations.registry import builtin_registry
    assert schema()['$defs']['Presentation']['properties']['output']==builtin_registry().get('task.show_time').output_schema
