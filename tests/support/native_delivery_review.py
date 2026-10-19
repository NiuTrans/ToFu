"""Owner review ports for the real ROOT crash/continue rehearsal.

The model produces the file through the registered gateway. This fixture then
publishes the inspected bytes as an artifact and records explicit owner review;
it never fabricates tool recovery, Turn revisions or acceptance evidence.
"""
from pathlib import Path

from lib.acceptance import turns
from lib.artifacts import core, review as artifacts
from lib.identity import PrincipalContext
from lib.memory.working_facts import service as facts

OBJECTIVE = 'Deliver a region-specific report and preserve the once-only ledger'
CRITERIA = [
    {'id': 'ledger', 'kind': 'requirement', 'text': 'The ledger contains one publication'},
    {'id': 'region', 'kind': 'requirement', 'text': 'Report region: west'},
]


def principal(task):
    return PrincipalContext.from_payload(task['_principalContext'])


def read(task):
    return turns.read_review(task['convId'], task['_turnId'], principal=principal(task))


def configure(task, *, criteria, outputs):
    current = read(task)
    return turns.update_review(task['convId'], task['_turnId'], {
        'action': 'requirements', 'requirementsRevision': current['requirementsRevision'],
        'objective': OBJECTIVE, 'criteria': criteria, 'outputs': outputs,
    }, principal=principal(task))


def decide(task, criterion_id, output_ids, note, fact_bindings=None):
    view = read(task)
    criterion = next(row for row in view['criteria'] if row['id'] == criterion_id)
    return turns.update_review(task['convId'], task['_turnId'], {
        'action': 'decision', 'requirementsRevision': view['requirementsRevision'],
        'decisionsRevision': view['decisionsRevision'], 'criterionId': criterion_id,
        'criterionDigest': criterion['digest'], 'verdict': 'pass', 'note': note,
        'outputBindings': [{'id': row['id'], 'version': row['version']}
                           for row in view['outputs'] if row['id'] in output_ids],
        **({'factBindings': fact_bindings} if fact_bindings is not None else {}),
    }, principal=principal(task))


def region_fact(task, identifier=None):
    item = next(row for row in read(task)['criteria'] if row['id'] == 'region')
    current = facts.query_facts(task['convId'], principal=principal(task))
    result = facts.record_fact(task['convId'], {
        'action': 'record', 'revision': current['revision'], 'kind': 'observation',
        'text': item['text'], 'sources': [{'kind': 'criterion', 'targetKind': 'turn',
            'id': task['_turnId'], 'criterionId': item['id'],
            'version': item['digest'], 'quote': item['text']}],
        **({'id': identifier} if identifier else {}),
    }, principal=principal(task), task_id=task['id'])['facts'][0]
    return [{'id': result['id'], 'revision': result['revision']}]


def artifact_reviews(task):
    return {row['id']: artifacts.read_review(row['artifactId'], principal=principal(task))
            for row in read(task)['outputs'] if row['kind'] == 'artifact'}


def accept_artifact(task, artifact, text, fact_bindings=()):
    current = artifacts.read_review(artifact['id'], principal=principal(task))
    if not current['criteria']:
        current = artifacts.update_review(artifact['id'], {
            'action': 'requirements', 'requirementsRevision': current['requirementsRevision'],
            'objective': text, 'criteria': [{'id': 'content', 'kind': 'requirement', 'text': text}],
        }, principal=principal(task))
    return artifacts.update_review(artifact['id'], {
        'action': 'decision', 'requirementsRevision': current['requirementsRevision'],
        'decisionsRevision': current['decisionsRevision'], 'artifactDigest': current['artifactDigest'],
        'criterionId': 'content', 'criterionDigest': current['criteria'][0]['digest'],
        'verdict': 'pass', 'note': 'Owner inspected the published bytes against the current requirement',
        'factBindings': list(fact_bindings),
    }, principal=principal(task))


def publish(task, values, filename, expected, title):
    content = Path(values['directory'], 'project', filename).read_text()
    assert content == expected
    return core.create_artifact(conv_id=task['convId'], task_id=task['id'],
        content=content, format='markdown', source='write_file',
        source_ref={'path': filename}, title=title)


def prepare(task, values):
    """Keep the logical Turn's current owner scope across a new attempt."""
    if not values['resume']:
        return configure(task, criteria=CRITERIA, outputs=[])
    previous = read(task)
    assert previous['criteria'][0]['state'] == 'pass'
    if values.get('revise_accepted_output'):
        assert previous['criteria'][1]['state'] == 'pass'
        assert all(row['status'] == 'accepted' for row in artifact_reviews(task).values())
    outputs = [{key: row[key] for key in ('id', 'label', 'artifactId')}
               for row in previous['outputs'] if row['kind'] == 'artifact']
    criteria = [dict(CRITERIA[0]), dict(CRITERIA[1], text='Report region: east')]
    updated = configure(task, criteria=criteria, outputs=outputs)
    assert updated['criteria'][0]['state'] == 'pass'
    assert updated['criteria'][0]['evidence'] == previous['criteria'][0]['evidence']
    assert updated['criteria'][1]['digest'] != previous['criteria'][1]['digest']
    if values.get('revise_accepted_output'):
        updated['artifactReviews'] = artifact_reviews(task)
    return updated


def before_crash(task, values):
    path = Path(values['directory'])/'project/result.txt'
    content = path.read_text()
    assert content == 'published once\n'
    artifact = core.create_artifact(conv_id=task['convId'], task_id=task['id'],
        content=content, format='markdown', source='write_file',
        source_ref={'path': 'result.txt'}, title='Verified publication ledger')
    configure(task, criteria=CRITERIA, outputs=[{
        'id': 'ledger', 'label': 'Publication ledger', 'artifactId': artifact['id']}])
    view = decide(task, 'ledger', {'ledger'}, 'Inspected published bytes: exactly one ledger entry')
    if values.get('revise_accepted_output'):
        report = publish(task, values, 'report.md', 'Report region: west\n', 'Regional report')
        configure(task, criteria=CRITERIA, outputs=[
            {'id': 'ledger', 'label': 'Publication ledger', 'artifactId': artifact['id']},
            {'id': 'report', 'label': 'Regional report', 'artifactId': report['id']}])
        binding = region_fact(task)
        accept_artifact(task, artifact, 'The ledger contains one publication')
        accept_artifact(task, report, 'Meets the current task region', binding)
        view = decide(task, 'region', {'report'}, 'Inspected the west-region report', binding)
        view['artifactReviews'] = artifact_reviews(task)
    assert view['status'] == 'pending'
    return view


def after_continue(task, values):
    assert Path(values['directory'], 'project/result.txt').read_text() == 'published once\n'
    assert 'Report region: east' in task['content']
    before = read(task)
    assert before['criteria'][0]['state'] == 'pass'
    assert before['status'] == 'pending'
    if values.get('revise_accepted_output'):
        old = next(row for row in before['outputs'] if row['id'] == 'report')
        old_review = artifacts.read_review(old['artifactId'], principal=principal(task))
        assert old_review['criteria'][0]['state'] == 'stale'
        assert core.get_artifact(old['artifactId'], user_id=principal(task).owner_user_id)['content'] == 'Report region: west\n'
        report = publish(task, values, 'report.md', 'Report region: east\n', 'Regional report')
        assert report['parent_id'] == old['artifactId']
        outputs = [{key: row[key] for key in ('id', 'label', 'artifactId')}
                   for row in before['outputs'] if row['kind'] == 'artifact']
        next(row for row in outputs if row['id'] == 'report')['artifactId'] = report['id']
        configure(task, criteria=[dict(CRITERIA[0]), dict(CRITERIA[1], text='Report region: east')], outputs=outputs)
        binding = region_fact(task, old_review['criteria'][0]['evidence']['factBindings'][0]['id'])
        accept_artifact(task, report, 'Meets the current task region', binding)
        view = decide(task, 'region', {'answer', 'report'}, 'Inspected the revised report and answer', binding)
        assert view['status'] == 'accepted'
        assert all(row['covered'] for row in view['outputs'])
        return {'before_owner_review': before, 'accepted': view, 'artifactReviews': artifact_reviews(task),
            'old_report_review': artifacts.read_review(old['artifactId'], principal=principal(task))}
    view = decide(task, 'region', {'answer'}, 'Checked the completed answer against the revised east-region requirement')
    assert view['status'] == 'accepted'
    assert all(row['covered'] for row in view['outputs'])
    return {'before_owner_review': before, 'accepted': view}
