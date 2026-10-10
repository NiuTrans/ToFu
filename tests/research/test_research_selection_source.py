"""Saved discovery identity is owner-validated and part of experiment basis."""
from copy import deepcopy
import pytest
from lib.research import records, workspace, program, action
from lib.storage import StorageSupervisor
from lib.storage.errors import StorageError
pytestmark = pytest.mark.unit
DIRECTION = 'Frozen original selection'


@pytest.fixture(params=['sqlite','tofudb'])
def storage(tmp_path,monkeypatch,request):
    monkeypatch.setenv('TOFUDB_OWNER_ID','12')
    supervisor = StorageSupervisor(project_root=tmp_path / 'storage',backend=request.param,startup_timeout=60)
    supervisor.start()
    monkeypatch.setattr(workspace,'_storage',lambda **kwargs:supervisor.client)
    try:
        yield supervisor, request.param
    finally:
        supervisor.stop()


def source(client, monkeypatch, task_id='original', owner=12):
    monkeypatch.setattr(records,'_storage',lambda **kwargs:client)
    records.persist_record({'task_id':task_id,'user_id':owner,'direction':DIRECTION,
        'lang':'en','status':'done','result':{'accepted':[{'id':'idea-1'}]}})


def test_saved_source_survives_legacy_update_without_field(storage,monkeypatch):
    supervisor, _ = storage
    source(supervisor.client,monkeypatch)
    draft = workspace.empty_workspace(DIRECTION)
    draft.update(selected_idea_id='idea-1',selected_idea_source_task_id='original')
    saved = workspace.save_workspace(DIRECTION,'en',draft,user_id=12,expected_revision=0)
    assert saved['selected_idea_source_task_id'] == 'original'
    legacy = deepcopy(saved);legacy.pop('selected_idea_source_task_id')
    updated = workspace.save_workspace(DIRECTION,'en',legacy,user_id=12,expected_revision=saved['revision'])
    assert updated['selected_idea_source_task_id'] == 'original'


@pytest.mark.parametrize('owner,idea',[(13,'idea-1'),(12,'missing')])
def test_foreign_or_missing_selection_cannot_publish(storage,monkeypatch,owner,idea):
    supervisor, _ = storage
    source(supervisor.client,monkeypatch)
    draft = workspace.empty_workspace(DIRECTION)
    draft.update(selected_idea_id=idea,selected_idea_source_task_id='original')
    with pytest.raises((ValueError,StorageError)):
        workspace.save_workspace(DIRECTION,'en',draft,user_id=owner,expected_revision=0)
    assert workspace.load_workspace(DIRECTION,'en',user_id=12)['revision'] == 0


def test_source_change_invalidates_basis_but_empty_legacy_keeps_hash():
    base = workspace.empty_workspace(DIRECTION);base['hypothesis'] = 'Measured hypothesis'
    legacy = program.dependency_digests(base)
    omitted = deepcopy(base);omitted.pop('selected_idea_source_task_id')
    assert program.dependency_digests(omitted) == legacy
    base.update(selected_idea_id='idea-1',selected_idea_source_task_id='original')
    bound = program.dependency_digests(base)
    assert bound['protocol_digest'] == legacy['protocol_digest']
    assert bound['hypothesis_digest'] != legacy['hypothesis_digest']
    assert action._frozen_hypothesis_identity(base) == bound['hypothesis_digest']
    base['selected_idea_source_task_id'] = 'different-original'
    assert program.dependency_digests(base)['hypothesis_digest'] != bound['hypothesis_digest']
