"""Project persistence ports preserve task identity, delegation and transient mode."""
import builtins

import pytest

from lib.tasks_pkg.persistence_store import DefaultConversationStore

pytestmark = pytest.mark.unit


def test_host_project_ports_delegate_exact_owned_objects(monkeypatch):
    from lib.conversations import project_messages, project_brain
    store=DefaultConversationStore();task={'id':'task','_userId':7};messages=[];seen=[]
    monkeypatch.setattr(project_messages,'inject',lambda *args:seen.append(('inject',args)))
    monkeypatch.setattr(project_messages,'confirm',lambda *args:seen.append(('confirm',args)))
    def consume(value):
        assert value is task
        seen.append(('consume',(value,)))
        return False
    monkeypatch.setattr(project_brain,'confirm_project_context_delivery',consume)
    store.inject_project_messages(task,messages,3)
    store.confirm_project_messages(task,3)
    assert store.confirm_project_context_consumption(task) is False
    assert seen==[('inject',(task,messages,3)),('confirm',(task,3)),('consume',(task,))]
    assert seen[0][1][0] is task and seen[0][1][1] is messages


def test_transient_project_ports_never_activate_host_persistence(monkeypatch):
    original=builtins.__import__
    def guarded(name,*args,**kwargs):
        if name.startswith(('lib.conversations','lib.storage')):
            raise AssertionError('transient project port activated host persistence')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',guarded)
    store=DefaultConversationStore();task={'_transientRuntime':True,'_projectNarrativeDelivery':{'deliveryToken':'pending'}}
    messages=[]
    store.inject_project_messages(task,messages,0)
    store.confirm_project_messages(task,0)
    assert store.confirm_project_context_consumption(task) is False
    assert messages==[] and task['_projectNarrativeDelivery']['deliveryToken']=='pending'


def test_project_port_failures_keep_original_exception(monkeypatch):
    from lib.conversations import project_brain
    failure=RuntimeError('confirmation unavailable')
    def confirm(_task):raise failure
    monkeypatch.setattr(project_brain,'confirm_project_context_delivery',confirm)
    with pytest.raises(RuntimeError) as raised:DefaultConversationStore().confirm_project_context_consumption({'_userId':7})
    assert raised.value is failure
