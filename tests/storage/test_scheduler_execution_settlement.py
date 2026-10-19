"""Owner/executor atomic scheduler settlement and immutable command replay."""
import pytest
from tests.storage.test_storage_authority_sql_contract import storage as storage
pytestmark=pytest.mark.unit


def test_settlement_is_owner_scoped_skips_commands_and_replays_without_regression(storage):
    client=storage.client
    for identity,owner,kind in [('a',42,'agent'),('b',43,'agent'),('c',42,'command')]:
        client.command('scheduler.task.create',dict(task_id=identity,user_id=owner,name=identity,schedule='* * * * *',command='run',task_type=kind,created_at='now',updated_at='now',last_execution_task_id='old',last_execution_status='running'),identity)
    payload=dict(user_id=42,execution_task_id='old',status='ok',now='terminal')
    assert client.command('scheduler.task.settle_execution',payload,'settled')=={'changed':1}
    for identity,owner,status in [('a',42,'ok'),('b',43,'running'),('c',42,'running')]:
        assert client.query('scheduler.task.get',dict(task_id=identity,user_id=owner))['last_execution_status']==status
    client.command('scheduler.task.update',dict(task_id='a',user_id=42,last_execution_task_id='new',last_execution_status='running'),'new')
    assert client.command('scheduler.task.settle_execution',payload,'settled')=={'changed':1}
    assert client.command('scheduler.task.settle_execution',payload,'late')=={'changed':0}
    assert client.query('scheduler.task.get',dict(task_id='a',user_id=42))['last_execution_status']=='running'


@pytest.mark.parametrize('field,value',[('user_id',0),('execution_task_id',''),('status','running'),('now','')])
def test_invalid_settlement_is_rejected_without_changes(storage,field,value):
    from lib.storage import StorageError
    payload=dict(user_id=42,execution_task_id='old',status='ok',now='terminal');payload[field]=value
    with pytest.raises(StorageError):storage.client.command('scheduler.task.settle_execution',payload,'invalid-'+field)


def test_execution_lookup_is_readonly_owned_and_stale_match_still_fenced(storage,monkeypatch):
    from lib.scheduler import manager
    client=storage.client
    assert client.query('scheduler.task.execution_lookup',dict(user_id=42,execution_task_id='old'))=={'indexed':True,'matched':False}
    client.command('scheduler.task.create',dict(task_id='lookup',user_id=42,name='lookup',schedule='* * * * *',command='run',task_type='agent',created_at='now',updated_at='now',last_execution_task_id='old',last_execution_status='running'),'lookup-create')
    assert client.query('scheduler.task.execution_lookup',dict(user_id=43,execution_task_id='old'))=={'indexed':True,'matched':False}
    class ChangingClient:
        def query(self,operation,payload):
            result=client.query(operation,payload)
            client.command('scheduler.task.update',dict(task_id='lookup',user_id=42,last_execution_task_id='new',last_execution_status='running'),'lookup-new')
            return result
        def command(self,*args):return client.command(*args)
    monkeypatch.setattr(manager,'_scheduler_client',lambda **kwargs:ChangingClient())
    scheduler=object.__new__(manager.ScheduledTaskManager)
    assert scheduler.settle_execution('old',user_id=42,status='ok',now='terminal')=={'changed':0}
    assert client.query('scheduler.task.get',dict(task_id='lookup',user_id=42))['last_execution_status']=='running'
