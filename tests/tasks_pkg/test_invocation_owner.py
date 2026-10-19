"""The root API enters one synchronous owner without weakening isolation."""
from dataclasses import replace
import threading

import pytest

from lib.tasks_pkg import invocation
from lib.tasks_pkg.orchestrator import _run

pytestmark = pytest.mark.unit


def test_root_calls_declared_owner_with_unchanged_carrier(monkeypatch):
    task = {'id': 'invocation-owner-root', '_userId': 71,
            'config': {'model': 'fixture-model'}, 'messages': []}
    calls = []
    caller = threading.get_ident()
    monkeypatch.setattr(
        _run, '_execute_task_invocation',
        lambda carrier, *, policy: calls.append(
            (carrier, policy, threading.get_ident())))
    assert _run.run_task(task) is None
    assert calls == [(task, invocation.ROOT_INVOCATION_POLICY, caller)]
    assert calls[0][0] is task


def test_child_policy_cannot_borrow_root_carrier_before_any_mutation(monkeypatch):
    task = {'id': 'invocation-owner-child', '_userId': 72}
    original = dict(task)
    monkeypatch.setattr(
        _run, 'check_autopilot_kick',
        lambda _: pytest.fail('child reached kickoff'))
    with pytest.raises(ValueError, match='task-local evidence port'):
        invocation.execute_invocation(
            task, policy=replace(invocation.ROOT_INVOCATION_POLICY, parent_task_id='parent'))
    assert task == original


def test_explicit_owner_preserves_root_kickoff_and_cleanup(monkeypatch):
    calls = []
    monkeypatch.setattr(_run, 'check_autopilot_kick',
                        lambda task: calls.append('kick') or True)
    monkeypatch.setattr(_run, 'set_req_id',
                        lambda value: calls.append(('request', value)))
    monkeypatch.setattr(_run, 'clear_log_context',
                        lambda: calls.append('clear'))
    invocation.execute_invocation(
        {'id': 'owner-kickoff'}, policy=invocation.ROOT_INVOCATION_POLICY)
    assert calls == [('request', 'owner-ki'), 'kick', ('request', ''), 'clear']


def test_owner_rejects_missing_carrier_id():
    with pytest.raises(ValueError, match="missing 'id'"):
        invocation.execute_invocation({}, policy=invocation.ROOT_INVOCATION_POLICY)


@pytest.mark.parametrize('failure', [False, True])
def test_root_invocation_retires_outer_worker_correlation(monkeypatch, failure):
    from lib.log import clear_log_context, log_fields, req_id, set_log_context, set_req_id

    def execute(task, *, policy):
        set_req_id('current-root')
        set_log_context(task_id=task['id'])
        if failure:
            raise RuntimeError('root fixture failure')

    monkeypatch.setattr(_run, '_execute_task_invocation', execute)
    set_req_id('stale-root')
    set_log_context(task_id='stale-root')
    try:
        task = {'id': 'current-root'}
        if failure:
            with pytest.raises(RuntimeError, match='root fixture failure'):
                _run.run_task(task)
        else:
            _run.run_task(task)
        assert req_id() == ''
        assert log_fields() == {}
    finally:
        set_req_id('')
        clear_log_context()


def test_rejected_root_invocation_also_retires_worker_correlation():
    from lib.log import clear_log_context, log_fields, req_id, set_log_context, set_req_id

    set_req_id('stale-root')
    set_log_context(task_id='stale-root')
    try:
        with pytest.raises(ValueError, match="missing 'id'"):
            invocation.execute_invocation({}, policy=invocation.ROOT_INVOCATION_POLICY)
        assert req_id() == ''
        assert log_fields() == {}
    finally:
        set_req_id('')
        clear_log_context()


@pytest.mark.parametrize('failure', [None, RuntimeError, KeyboardInterrupt])
def test_native_preparation_scope_is_owned_and_restored_on_every_exit(monkeypatch,failure):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from lib.token_counter.native import scoped_native_encoder,native_token_scope
    from tests.tasks_pkg.test_native_invocation import binding
    from tests.tasks_pkg.test_native_host import image
    from lib.tasks_pkg.native_host import NativeHost
    async def run():
        parent=NativeHost(image(),token_count=True)
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor,token_count=True)
            task={'id':'native-scope','_userId':port.principal.owner_user_id,
                  '_nativePreparedInvocation':port,'config':{}}
            def body(carrier,*,policy):
                encoder=scoped_native_encoder('cl100k_base')
                assert encoder.host is port.host
                if failure:raise failure('injected')
            monkeypatch.setattr(_run,'_execute_task_invocation',body)
            try:
                with native_token_scope(parent):
                    if failure:
                        with pytest.raises(failure):
                            invocation.execute_invocation(task,policy=invocation.ROOT_INVOCATION_POLICY)
                    else:invocation.execute_invocation(task,policy=invocation.ROOT_INVOCATION_POLICY)
                    assert scoped_native_encoder('cl100k_base').host is parent
                assert scoped_native_encoder('cl100k_base') is None
                assert port.host._token_counter is None  # Binding a scope performs no admission IO.
            finally:await port.host.close();await parent.close()
    asyncio.run(run())


def test_foreign_native_preparation_never_enters_body(monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from tests.tasks_pkg.test_native_invocation import binding
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor,token_count=True)
            monkeypatch.setattr(_run,'_execute_task_invocation',lambda *a,**k:pytest.fail('foreign task entered'))
            try:
                with pytest.raises(ValueError,match='task-owned'):
                    invocation.execute_invocation({'id':'foreign','_userId':999,
                        '_nativePreparedInvocation':port,'config':{}},policy=invocation.ROOT_INVOCATION_POLICY)
                assert port.host._token_counter is None
            finally:await port.host.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure', [False, True])
def test_python_invocation_preserves_explicit_caller_token_scope(monkeypatch, failure):
    import asyncio
    from lib.token_counter.native import scoped_native_encoder, native_token_scope
    from lib.tasks_pkg.native_host import NativeHost
    from tests.tasks_pkg.test_native_host import image
    async def run():
        host=NativeHost(image(),token_count=True)
        def body(carrier,*,policy):
            assert scoped_native_encoder('cl100k_base').host is host
            if failure:raise RuntimeError('injected scope exit')
        monkeypatch.setattr(_run,'_execute_task_invocation',body)
        try:
            with native_token_scope(host):
                if failure:
                    with pytest.raises(RuntimeError,match='injected scope exit'):
                        invocation.execute_invocation({'id':'python-scope'},policy=invocation.ROOT_INVOCATION_POLICY)
                else:
                    invocation.execute_invocation({'id':'python-scope'},policy=invocation.ROOT_INVOCATION_POLICY)
                assert scoped_native_encoder('cl100k_base').host is host
            assert scoped_native_encoder('cl100k_base') is None
            assert host._token_counter is None
        finally:await host.close()
    asyncio.run(run())
