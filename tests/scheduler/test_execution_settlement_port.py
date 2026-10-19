"""Terminal scheduler callback delegates to the atomic owner/executor port."""
import pytest
pytestmark=pytest.mark.unit

@pytest.mark.parametrize('status,error,expected',[('done',None,'ok'),('done',{'message':'failure'},'error'),('aborted',None,'error')])
def test_terminal_does_not_build_scheduler_presentation(monkeypatch,status,error,expected):
    from lib.tasks_pkg.manager import _sync
    from lib.scheduler import manager
    calls=[]
    class Scheduler:
        def list_tasks(self,**kwargs):raise AssertionError('terminal must not hydrate schedules')
        def settle_execution(self,task_id,**kwargs):calls.append((task_id,kwargs))
    monkeypatch.setattr(manager,'get_scheduler',lambda:Scheduler())
    _sync._update_proactive_execution_status(dict(id='execution',_userId=7,status=status,error=error))
    assert len(calls)==1
    assert calls[0][0]=='execution' and calls[0][1]['user_id']==7 and calls[0][1]['status']==expected


@pytest.mark.parametrize('lookup,commands',[
    ({'indexed':True,'matched':False},0),
    ({'indexed':True,'matched':True},1),
    ({'indexed':False,'matched':False},1),
])
def test_only_proven_absence_skips_receipted_settlement(monkeypatch,lookup,commands):
    from lib.scheduler import manager
    calls=[]
    class Client:
        def query(self,operation,payload):
            assert operation=='scheduler.task.execution_lookup'
            assert payload=={'user_id':7,'execution_task_id':'execution'}
            return lookup
        def command(self,operation,payload,identity):
            calls.append((operation,payload,identity));return {'changed':1}
    monkeypatch.setattr(manager,'_scheduler_client',lambda **kwargs:Client())
    scheduler=object.__new__(manager.ScheduledTaskManager)
    result=scheduler.settle_execution('execution',user_id=7,status='ok',now='terminal')
    assert len(calls)==commands and result=={'changed':commands}
    if commands:
        assert calls[0][0]=='scheduler.task.settle_execution'
        assert calls[0][1]=={'user_id':7,'execution_task_id':'execution','status':'ok','now':'terminal'}


@pytest.mark.parametrize('lookup',[{},None,{'indexed':1,'matched':False},{'indexed':False,'matched':True}])
def test_unproven_lookup_shape_refuses_before_mutation(monkeypatch,lookup):
    from lib.scheduler import manager
    from lib.storage import StorageError
    class Client:
        def query(self,*args):return lookup
        def command(self,*args):raise AssertionError('invalid lookup cannot authorize mutation')
    monkeypatch.setattr(manager,'_scheduler_client',lambda **kwargs:Client())
    scheduler=object.__new__(manager.ScheduledTaskManager)
    with pytest.raises(StorageError):scheduler.settle_execution('execution',user_id=7,status='ok',now='terminal')
