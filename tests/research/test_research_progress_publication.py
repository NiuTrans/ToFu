"""Checkpoint head/node authority is published atomically with paged receipts."""
from copy import deepcopy
import pytest

from lib.research.progress_checkpoint import ProgressCheckpointRepository, NAMESPACE
from lib.research.receipt_pages import ReceiptPages
from lib.research.persistence import research_direction_hash
from lib.research.workspace import load_workspace, save_workspace
from lib.storage.errors import StorageError
from tests.research.test_research_pending_paging import save, apply, initial, receipt, DIRECTION
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def repository(client, run):
    return ProgressCheckpointRepository(client,user_id=1,paper_hash=research_direction_hash(DIRECTION),lang='en',run=run)


def test_workspace_receipt_and_progress_heads_survive_selected_storage_restart(storage):
    supervisor,_=storage;saved=save(initial());run=saved['runs'][0]
    checkpoint=repository(supervisor.client,run).read(run)
    assert checkpoint['index']['receipts']==2 and checkpoint['index']['sources']['count']==2
    assert repository(supervisor.client,run).indexes.status(checkpoint['index'])=='running'
    supervisor.stop();supervisor.start()
    assert load_workspace(DIRECTION,user_id=1)==saved
    updated=save(apply(saved,'next',[receipt('job-B','completed','poll','new')],supervisor.client))
    checkpoint=repository(supervisor.client,updated['runs'][0]).read(updated['runs'][0])
    assert checkpoint['index']['receipts']==3 and checkpoint['receipt_history']==updated['runs'][0]['receipt_history']


def test_checkpoint_advance_reads_only_new_page_suffix(storage,monkeypatch):
    supervisor,_=storage;saved=save(initial())
    for n in range(12):saved=save(apply(saved,f'poll-{n}',[receipt('job-B','completed','poll',str(n))],supervisor.client))
    candidate=apply(saved,'last',[receipt('job-B','completed','poll','last')],supervisor.client)
    observed=[];original=ReceiptPages.open_receipts
    def tracked(self,reference,inline=(),*,base=None):
        if base is not None:observed.append((reference['count'],base['count']))
        return original(self,reference,inline,base=base)
    monkeypatch.setattr(ReceiptPages,'open_receipts',tracked)
    updated=save(candidate)
    assert observed==[(15,14)]
    assert repository(supervisor.client,updated['runs'][0]).read(updated['runs'][0])['index']['receipts']==15


def test_raw_foreign_progress_pointer_is_rejected(storage):
    supervisor,_=storage;saved=save(initial());changed=deepcopy(saved)
    changed['runs'][0]['receipt_progress']='f'*64
    with pytest.raises(ValueError,match='server-owned'):
        save_workspace(DIRECTION,'en',changed,expected_revision=saved['revision'],user_id=1)
    assert load_workspace(DIRECTION,user_id=1)==saved


def test_failed_workspace_cas_does_not_publish_progress_candidate(storage,monkeypatch):
    supervisor,_=storage;saved=save(initial());candidate=apply(saved,'next',[receipt('job-B','completed','poll','new')],supervisor.client)
    command=supervisor.client.command
    def refuse(operation,*args,**kwargs):
        if operation=='research.workspace.put':raise StorageError('database_conflict','refused checkpoint publication')
        return command(operation,*args,**kwargs)
    monkeypatch.setattr(supervisor.client,'command',refuse)
    with pytest.raises(StorageError,match='refused checkpoint publication'):save(candidate)
    assert load_workspace(DIRECTION,user_id=1)==saved
    monkeypatch.setattr(supervisor.client,'command',command)
    committed=save(candidate)
    assert repository(supervisor.client,committed['runs'][0]).read(committed['runs'][0])['index']['receipts']==3


def test_checkpoint_cannot_be_read_against_another_receipt_boundary(storage):
    supervisor,_=storage;saved=save(initial());run=deepcopy(saved['runs'][0])
    run['receipt_history']['count']+=1
    with pytest.raises(StorageError,match='does not match published receipt head'):
        repository(supervisor.client,run).read(run)


def test_success_shaped_checkpoint_ack_without_record_cannot_publish(storage,monkeypatch):
    supervisor,_=storage;saved=save(initial());candidate=apply(saved,'next',[receipt('job-B','completed','poll','new')],supervisor.client)
    command=supervisor.client.command
    def fake(operation,payload,*args,**kwargs):
        if operation=='record.put' and payload.get('namespace')==NAMESPACE:return {'version':1}
        return command(operation,payload,*args,**kwargs)
    monkeypatch.setattr(supervisor.client,'command',fake)
    with pytest.raises(StorageError,match='checkpoint is missing or invalid'):save(candidate)
    assert load_workspace(DIRECTION,user_id=1)==saved


def test_delta_reader_rejects_an_unreachable_published_base(storage):
    supervisor,_=storage;saved=save(initial());run=saved['runs'][0]
    pages=repository(supervisor.client,run).pages
    fake={'head':'f'*64,'count':run['receipt_history']['count']-1}
    with pytest.raises(StorageError,match='does not reach its published base'):
        with pages.open_receipts(run['receipt_history'],base=fake):pass


def test_success_shaped_pending_node_ack_without_node_cannot_publish(storage,monkeypatch):
    from lib.research.progress_index import SOURCE_NAMESPACE
    supervisor,_=storage;saved=save(initial());candidate=apply(saved,'next',[receipt('job-B','completed','poll','new')],supervisor.client)
    command=supervisor.client.command
    def fake(operation,payload,*args,**kwargs):
        if operation=='record.put' and payload.get('namespace')==SOURCE_NAMESPACE:return {'version':1}
        return command(operation,payload,*args,**kwargs)
    monkeypatch.setattr(supervisor.client,'command',fake)
    with pytest.raises(StorageError,match='node is missing or invalid'):save(candidate)
    assert load_workspace(DIRECTION,user_id=1)==saved
