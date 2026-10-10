"""Old cached audit successes cannot bypass observation consistency checks."""
from copy import deepcopy
import pytest
from lib.research.observation_audit import AUDIT_VERSION
from lib.research.workspace import load_workspace, save_workspace
from lib.research.persistence import research_direction_hash
from tests.research.test_research_history import DIRECTION, draft_with_runs, save
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark=pytest.mark.unit


@pytest.mark.parametrize('legacy_version',[0,1,2,3,4,5,6])
@pytest.mark.parametrize('has_observations',[False,True])
def test_legacy_aggregate_fails_closed_then_replays_committed_pages(storage,has_observations,legacy_version):
    draft=draft_with_runs(24)
    if not has_observations:
        import json
        for row in draft['runs']:
            payload=json.loads(row['tool_receipts'][0]['result_excerpt'])
            payload.pop('scientific_observations')
            row['tool_receipts'][0]['result_excerpt']=json.dumps(payload)
    saved=save(draft)
    legacy=deepcopy(saved)
    legacy['history']['audit_version']=legacy_version
    legacy['history']['audit_failures']={key:0 for key in legacy['history']['audit_failures']}
    storage[0].client.command('research.workspace.put',{
        'user_id':1,'paper_hash':research_direction_hash(DIRECTION),'lang':'en',
        'expected_revision':saved['revision'],'workspace':legacy,'updated_at':0},'fixture-legacy-cached-audit')
    before=load_workspace(DIRECTION,'en',user_id=1)
    assert before['history']['audit_version']==legacy_version
    assert not before['manuscript_audit']['passed']
    restored=save_workspace(DIRECTION,'en',before,user_id=1,expected_revision=before['revision'])
    assert restored['history']['audit_version']==AUDIT_VERSION
    assert restored['history']['head']==saved['history']['head']
    assert restored['history']['count']==8
    assert all(value==(0 if has_observations else 8) for value in restored['history']['audit_failures'].values())
    scientific_checks=[check['ok'] for check in restored['manuscript_audit']['checks'] if check['id']!='claim_traceability']
    assert all(scientific_checks)==has_observations
