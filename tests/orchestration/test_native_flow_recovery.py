"""Run durable journal replay specifications through the native graph cursor."""
import inspect
import pytest
from lib.identity import PrincipalContext
from lib.orchestration.engine import FlowExecutor
from tests.orchestration import test_orchestration_execution_recovery as oracle
from tests.orchestration.test_native_flow_engine import native_library as native_library

pytestmark=pytest.mark.unit
CASES=[function for name,function in inspect.getmembers(oracle,inspect.isfunction)
    if name.startswith('test_') and not inspect.signature(function).parameters]


@pytest.mark.parametrize('specification',CASES,ids=[function.__name__ for function in CASES])
def test_durable_replay_specification_keeps_its_effect_owner(specification,native_library,monkeypatch):
    original=FlowExecutor.__init__
    def initialize(self,definition,**options):
        parent=options.get('parent_task')
        if parent is None:
            owner=options.get('human_gate_owner_user_id') or 7
            principal=PrincipalContext.user(subject_id='recovery-oracle',owner_user_id=owner)
            options['parent_task']=dict(id='recovery-oracle-task',_userId=owner,
                _principalContext=principal.to_payload())
        options['native_flow_library']=native_library
        return original(self,definition,**options)
    monkeypatch.setattr(FlowExecutor,'__init__',initialize)
    specification()
    assert not native_library._graphs
