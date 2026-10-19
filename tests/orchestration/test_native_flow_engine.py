"""Run the retained control-flow specifications against the native cursor."""
import hashlib
import inspect
import os
from pathlib import Path
import unittest
import pytest
from lib.identity import PrincipalContext
from lib.orchestration.engine import FlowExecutor
from lib.orchestration.native_flow import NativeFlowLibrary
from tests.orchestration import test_orchestration_engine as oracle

pytestmark=pytest.mark.unit
CASES=[(cls,name) for _,cls in inspect.getmembers(oracle,inspect.isclass)
    if issubclass(cls,unittest.TestCase)
    for name in unittest.defaultTestLoader.getTestCaseNames(cls)]


@pytest.fixture(scope='module')
def native_library():
    archive=os.environ.get('TOFU_NATIVE_FLOW_TEST_ARCHIVE')
    value=os.environ.get('TOFU_NATIVE_FLOW_TEST_LIBRARY')
    if archive:
        from scripts.build_native_runtime import verify
        path=Path(archive);manifest=verify(path,run=False)
        library=NativeFlowLibrary.from_archive(archive=path,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            application_version=manifest['build']['application_version'],maximum_bytes=64<<20)
    else:
        if not value:pytest.skip('explicit native Flow library or archive required')
        path=Path(value)
        library=NativeFlowLibrary(path=path,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),maximum_bytes=64<<20)
    yield library
    assert not library._graphs
    library.close()


@pytest.mark.parametrize('case',CASES,ids=[cls.__name__+'.'+name for cls,name in CASES])
def test_retained_graph_specification_uses_native_cursor(case,native_library,monkeypatch):
    original=FlowExecutor.__init__
    engines=[]
    principal=PrincipalContext.user(subject_id='graph-oracle',owner_user_id=7,tenant_id='oracle')
    def initialize(self,definition,**options):
        if not options.get('parent_task'):
            owner=options.get('human_gate_owner_user_id') or 7
            identity=PrincipalContext.user(subject_id='graph-oracle',owner_user_id=owner,tenant_id='oracle')
            options['parent_task']=dict(id='graph-oracle-task',_userId=owner,_principalContext=identity.to_payload())
        options['native_flow_library']=native_library
        original(self,definition,**options)
        engines.append(self)
    monkeypatch.setattr(FlowExecutor,'__init__',initialize)
    cls,name=case
    result=unittest.TestResult();cls(name).run(result)
    assert not result.errors and not result.failures,result.errors+result.failures
    assert all(engine._native_flow_library is native_library for engine in engines)
    assert not native_library._graphs
