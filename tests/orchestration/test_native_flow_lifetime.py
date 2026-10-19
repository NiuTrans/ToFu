"""Pinned code and graph close protect actual accepted calls and borrowers."""
from concurrent.futures import ThreadPoolExecutor,TimeoutError
import hashlib
import os
from pathlib import Path
import threading
import pytest
from lib.identity import PrincipalContext
from lib.orchestration.graph import FlowExecutionError
from lib.orchestration.native_flow import NativeFlowLibrary,NativeFlowGraph

pytestmark=pytest.mark.unit


def library():
    value=os.environ.get('TOFU_NATIVE_FLOW_TEST_LIBRARY')
    if not value:pytest.skip('explicit native Flow library required')
    path=Path(value)
    return NativeFlowLibrary(path=path,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),maximum_bytes=64<<20)


def test_library_close_cannot_unload_a_graph_opened_by_an_accepted_call(monkeypatch):
    runtime=library();entered=threading.Event();release=threading.Event();closing=threading.Event()
    original=NativeFlowGraph.__init__
    def paused(self,*args):
        original(self,*args);entered.set();assert release.wait(5)
    monkeypatch.setattr(NativeFlowGraph,'__init__',paused)
    principal=PrincipalContext.user(subject_id='lifetime',owner_user_id=7)
    def close():closing.set();runtime.close()
    graph=None
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            opened=workers.submit(runtime.open_graph,principal=principal,task_id='lifetime',
                nodes={'a':dict(id='a',type='role')},edges=[])
            assert entered.wait(5)
            closed=workers.submit(close);assert closing.wait(5)
            with pytest.raises(TimeoutError):closed.result(timeout=.03)
            release.set();graph=opened.result(timeout=5)
            with pytest.raises(FlowExecutionError,match='opened during library close'):closed.result(timeout=5)
        assert graph.find_start()=='a'
        graph.close();runtime.close()
        with pytest.raises(FlowExecutionError,match='closed'):
            runtime.open_graph(principal=principal,task_id='retired',nodes={},edges=[])
    finally:
        release.set()
        if graph is not None:graph.close()
        runtime.close()


def test_wrong_code_digest_refuses_before_loading_or_graph_allocation(monkeypatch):
    value=os.environ.get('TOFU_NATIVE_FLOW_TEST_LIBRARY')
    if not value:pytest.skip('explicit native Flow library required')
    import ctypes
    original=ctypes.CDLL
    def deny_unverified(path,*args,**kwargs):
        if path is not None:pytest.fail('unverified code loaded')
        return original(path,*args,**kwargs)
    monkeypatch.setattr('lib.orchestration.native_flow.c.CDLL',deny_unverified)
    with pytest.raises(FlowExecutionError,match='digest differs'):
        NativeFlowLibrary(path=Path(value),sha256='0'*64,maximum_bytes=64<<20)


def test_joined_close_retires_admission_but_keeps_borrowers_usable(monkeypatch):
    runtime = library()
    principal = PrincipalContext.user(subject_id='joined-lifetime', owner_user_id=7)
    graph = runtime.open_graph(principal=principal, task_id='accepted',
        nodes={'a': dict(id='a', type='role')}, edges=[])
    waiting = threading.Event()
    original_wait = runtime._condition.wait
    def observed_wait(*args, **kwargs):
        waiting.set()
        return original_wait(*args, **kwargs)
    monkeypatch.setattr(runtime._condition, 'wait', observed_wait)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            closing = workers.submit(runtime.close, wait_for_graphs=True)
            try:
                assert waiting.wait(5)
                assert not closing.done() and runtime._library is not None
                with pytest.raises(FlowExecutionError, match='closed'):
                    runtime.open_graph(principal=principal, task_id='late', nodes={}, edges=[])
                assert graph.find_start() == 'a'
            finally:
                graph.close()
            closing.result(timeout=5)
        assert runtime._library is None and runtime._fd is None
        runtime.close(wait_for_graphs=True)
    finally:
        graph.close(); runtime.close()
