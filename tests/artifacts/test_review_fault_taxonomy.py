"""Owner review separates the caller's command faults from server faults.

The shared review mechanics serve the artifact and ordinary Turn adapters
alike. Rejecting the caller's command (schema, duplicate IDs, blank text or
note, omitted or duplicate fact bindings) is a ``BadRequest`` naming the
command field. Validation of a server-built view, record capacity and an
unacknowledged write stay builtin server faults, so no HTTP boundary answers
them with 400.
"""
import pytest

from lib.error_envelope import BadRequest
from lib.identity import PrincipalContext

pytestmark = pytest.mark.unit
PRINCIPAL = PrincipalContext.user(subject_id='reviewer', owner_user_id=1)


@pytest.mark.parametrize('name', ['ArtifactReviewCommand', 'TurnReviewCommand'])
def test_command_schema_violation_is_the_callers_fault(name):
    from lib.acceptance.contract import validate

    with pytest.raises(BadRequest, match='Invalid owner review'):
        validate({'action': 'requirements', 'unexpected': True}, name)


@pytest.mark.parametrize('name', ['ArtifactReviewView', 'TurnReviewView'])
def test_view_schema_violation_stays_a_server_fault(name):
    from lib.acceptance.contract import validate

    with pytest.raises(ValueError, match='Invalid owner review') as caught:
        validate({'schemaVersion': 1}, name)
    assert not isinstance(caught.value, BadRequest)


@pytest.mark.parametrize('criteria', [
    [{'id': 'same', 'kind': 'requirement', 'text': 'A'},
     {'id': 'same', 'kind': 'requirement', 'text': 'B'}],
    [{'id': 'blank', 'kind': 'requirement', 'text': '  '}],
], ids=['duplicate-id', 'blank-text'])
def test_requirement_rejection_names_the_criteria_field(criteria):
    from lib.acceptance.review import generate_requirements

    with pytest.raises(BadRequest) as caught:
        generate_requirements({'objective': 'Ship', 'criteria': criteria}, {})
    assert caught.value.field == 'criteria'


class _DecisionsWithBoundFact:
    principal = PRINCIPAL

    def read(self, kind):
        assert kind == 'decisions'
        return {'items': {'answer': {'factBindings': [{'id': 'fact-a', 'revision': 1}]}}}, 3

    def put(self, *_args):
        pytest.fail('a rejected decision must not be written')


@pytest.mark.parametrize(('changes', 'field'), [
    ({'note': '  '}, 'note'),
    ({}, 'factBindings'),
    ({'factBindings': [{'id': 'fact-b', 'revision': 1}] * 2}, 'factBindings'),
], ids=['blank-note', 'omitted-required-bindings', 'duplicate-fact-ids'])
def test_decision_rejection_names_the_command_field(changes, field):
    from lib.acceptance.review import save_decision

    command = {'verdict': 'pass', 'note': 'Compared to the source', 'decisionsRevision': 3,
               **changes}
    with pytest.raises(BadRequest) as caught:
        save_decision(_DecisionsWithBoundFact(), command,
                      {'id': 'answer', 'digest': '0' * 64}, {}, 'conv-review')
    assert caught.value.field == field


def test_record_capacity_and_unacknowledged_writes_stay_server_faults():
    from lib.acceptance.contract import contract
    from lib.acceptance.review import ReviewRecords

    writes = []

    class Client:
        def command(self, *args):
            writes.append(args)
            return {'version': 9}

    records = ReviewRecords(PRINCIPAL, 'turn_review_v1', {'requirements': ['conv', 'turn']},
                            lambda write=False: Client())
    full = 'x' * contract()['x-limits']['maxRecordBytes']
    with pytest.raises(ValueError, match='Review record is full') as caught:
        records.put('requirements', {'objective': full}, 0)
    assert not isinstance(caught.value, BadRequest)
    assert writes == []
    with pytest.raises(ValueError, match='not acknowledged') as caught:
        records.put('requirements', {'objective': 'Ship'}, 0)
    assert not isinstance(caught.value, BadRequest)
    assert len(writes) == 1
