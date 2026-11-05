"""Task-backed HTTP billing parity without storage, services or a provider.

The real request_flow and ExecutionSession own money/lifecycle behavior; only
wallet/pricing, task publication and authorized routing ports are synthetic.
"""

from __future__ import annotations

import asyncio
from collections import Counter
import importlib
import inspect
import subprocess
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from quart import Quart

from lib.agent_core.execution_session import (
    ExecutionPhase, ExecutionSession, ResourceDisposition,
)

pytestmark = pytest.mark.unit


@pytest.fixture(params=['chat', 'agent_run'])
def surface(request, monkeypatch):
    import lib.billing as billing
    import lib.conv_config as conv_config
    import lib.storage as storage
    import lib.tasks_pkg.entry as entry
    import lib.tasks_pkg.manager as manager
    import lib.tasks_pkg.spawn as spawn
    import routes.api_v1.auth as auth

    route = importlib.import_module(f'routes.api_v1.{request.param}')
    session = ExecutionSession(
        execution_id=f'billing-spec-{uuid4().hex}', kind='billing-spec',
        owner_user_id=7,
    )
    state = SimpleNamespace(
        route=route, name=request.param, session=session, calls=[], faults={},
        account='opaque-wallet', reserve_micro=73, enabled=True, rejected=[],
        config={'model': 'configured-model'}, task=None, messages=None,
        body={'messages': [{'role': 'user', 'content': 'eight chars'}],
              'model': {'creator_id': 'test', 'model_id': 'routed-model'},
              'conversation_id': 'caller-job', 'stream': True},
    )
    state.model = 'configured-model' if state.name == 'chat' else 'routed-model'

    def counted(name, function):
        def call(*args, **kwargs):
            state.calls.append((name, args, kwargs, threading.get_ident()))
            if name in state.faults:
                raise state.faults[name]
            return function(*args, **kwargs)
        return call

    def no_storage(*_args, **_kwargs):
        pytest.fail('billing preparation reached application storage')

    monkeypatch.setattr(storage, 'get_storage_client', no_storage)
    monkeypatch.setattr(storage, 'start_storage', no_storage)
    monkeypatch.setattr('lib.relay_config.billing_enabled', lambda: state.enabled)
    monkeypatch.setattr(billing, 'estimate_request_cost', counted(
        'wallet.estimate', lambda *_args, **_kwargs: state.reserve_micro))
    monkeypatch.setattr(billing, 'compute_request_cost', counted(
        'wallet.compute', lambda model, **_kwargs: SimpleNamespace(
            micro=11, matched_model=model, snapshot={})))
    for name in ('reserve', 'settle', 'debit', 'reserve_release'):
        monkeypatch.setattr(billing, name, counted(
            f'wallet.{name}', lambda *_args, **_kwargs: SimpleNamespace(balance_micro=989)))
    monkeypatch.setattr(billing, 'get_balance', no_storage)
    monkeypatch.setattr(billing, 'get_wallet', no_storage)

    # Patch the concrete HTTP preparation owner; lower-level money and
    # lifecycle operations remain real, including their failure semantics.
    billing_ports = importlib.import_module('routes.api_v1._task_billing')
    for name in ('estimate_prompt_tokens', 'reserve_for_task', 'settle_task',
                 'release_reservation', 'bind_billing_reservation'):
        monkeypatch.setattr(billing_ports, name, counted(name, getattr(billing_ports, name)))

    def create(_conversation_id, messages, config, **kwargs):
        assert _conversation_id == 'caller-job'
        assert kwargs == {'user_id': 7, 'saved_conversation': False}
        assert config is state.config
        state.messages = messages
        state.task = {'id': session.execution_id, 'status': 'pending',
                      '_executionSession': session}
        return state.task

    def reject(task, error, *, cause, conv_id):
        state.rejected.append((task, error, cause, conv_id))
        session.settle(ExecutionPhase.FAILED, cause=cause)

    monkeypatch.setattr(manager, 'create_task', counted('create', create))
    monkeypatch.setattr(manager, 'reject_unstarted_chat_task', counted('reject', reject))
    monkeypatch.setattr(spawn, 'spawn_task', counted('spawn', lambda _task: None))
    monkeypatch.setattr(auth, 'request_user_id', lambda: 7)
    if state.name == 'chat':
        monkeypatch.setattr(route, 'request_user_id', lambda: 7)
    monkeypatch.setattr(route, 'current_auth', lambda: SimpleNamespace(
        account_user_id=state.account, owner_user_id=7, tenant_id='tenant', key_id='', name='spec'))
    monkeypatch.setattr(route, 'audit_log', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(route, 'guard_model_relay_or_dispose', lambda _group: None)
    group = SimpleNamespace(pin_id='authorized-route', candidates=[SimpleNamespace(provider_id='p')])
    selection = SimpleNamespace(model=SimpleNamespace(public_dict=lambda: {'model_id': 'routed-model'}))
    monkeypatch.setattr(route, 'mint_native_request_route', counted(
        'route.mint', lambda *_args, **_kwargs: ('routed-model', selection, group)))
    monkeypatch.setattr(route, 'dispose_routed_slot_group', counted('route.dispose', lambda _group: None))
    monkeypatch.setattr(entry, 'build_chat_config', lambda *_args, **_kwargs: state.config)
    if state.name == 'agent_run':
        monkeypatch.setattr(route, '_build_cfg', lambda *_args: state.config)
    monkeypatch.setattr(conv_config, 'resolve_auto_translate', lambda _cfg: False)
    monkeypatch.setattr(route, 'controller', SimpleNamespace(
        acquire=counted('admission.acquire', lambda: 'exact-lease'),
        release=counted('admission.release', lambda _lease: True),
        in_flight=1, capacity=0,
    ))
    monkeypatch.setattr(route, 'register_waiter', counted('waiter.register', lambda _id: None))
    monkeypatch.setattr(route, 'unregister_waiter', counted('waiter.unregister', lambda _id: None))
    monkeypatch.setattr(route, 'sse_response', lambda *_args, **_kwargs: 'accepted')
    handler = route.chat_completions if state.name == 'chat' else route.agent_run
    app = Quart(__name__, static_folder=None)
    suffix = 'chat/completions' if state.name == 'chat' else 'agent/run'

    def invoke(major='v1'):
        async def run():
            async with app.test_request_context(f'/api/{major}/{suffix}', method='POST', json=state.body):
                state.request_thread = threading.get_ident()
                result = await inspect.unwrap(handler)()
                if isinstance(result, tuple):
                    response, status = result
                    return status, await response.get_json(), response.content_type
                return result
        return asyncio.run(run())

    state.invoke = invoke
    state.counts = lambda: Counter(call[0] for call in state.calls)
    state.call = lambda name: [call for call in state.calls if call[0] == name]
    try:
        yield state
    finally:
        session.settle(ExecutionPhase.CANCELLED, cause='test cleanup')


def _assert_rejected(surface, error, cause):
    assert surface.rejected == [(surface.task, error, cause, 'caller-job')]
    assert surface.counts()['bind_billing_reservation'] == 0
    assert surface.counts()['admission.acquire'] == surface.counts()['spawn'] == 0
    assert surface.counts()['route.dispose'] == 1
    assert surface.session.is_terminal


@pytest.mark.parametrize(('account', 'reserved'), [('', 73), ('opaque-wallet', 73), ('opaque-wallet', 0)])
def test_reserve_bind_counts_and_request_thread_are_unchanged(surface, account, reserved):
    surface.account, surface.reserve_micro = account, reserved
    assert surface.invoke() == 'accepted'
    counts = surface.counts()
    expected = int(bool(account))
    for name in ('estimate_prompt_tokens', 'reserve_for_task', 'bind_billing_reservation', 'wallet.estimate'):
        assert counts[name] == expected
    assert counts['wallet.reserve'] == int(bool(account) and reserved > 0)
    assert counts['wallet.settle'] == counts['wallet.debit'] == counts['wallet.reserve_release'] == 0
    assert counts['admission.acquire'] == counts['spawn'] == 1
    if account:
        reserve = surface.call('reserve_for_task')[0]
        assert reserve[1] == (surface.task,)
        assert reserve[2] == {'user_id': account, 'model': surface.model,
                              'prompt_tokens': 2, 'max_completion_tokens': 1024}
        assert surface.call('estimate_prompt_tokens')[0][1][0] is surface.messages
        assert surface.call('bind_billing_reservation')[0][1] == (surface.session,)
    # Routing is already offloaded; billing preparation must NOT add another
    # handoff or move estimation/reservation/binding off the request loop.
    assert surface.call('route.mint')[0][3] != surface.request_thread
    for name in ('estimate_prompt_tokens', 'reserve_for_task', 'wallet.estimate',
                 'wallet.reserve', 'bind_billing_reservation'):
        assert all(call[3] == surface.request_thread for call in surface.call(name))
    surface.session.settle(ExecutionPhase.FAILED, cause='unstarted request')
    surface.session.settle(ExecutionPhase.FAILED, cause='repeat')
    assert surface.counts()['release_reservation'] == expected
    assert surface.counts()['wallet.reserve_release'] == int(bool(account) and reserved > 0)
    assert surface.counts()['wallet.settle'] == surface.counts()['wallet.debit'] == 0
    assert surface.counts()['route.dispose'] == surface.counts()['admission.release'] == 1


@pytest.mark.parametrize(('config_hint', 'body_hint', 'expected'), [
    ('17', '29', 17), (0, '29', 29), (None, 0, 1024), ('0', 29, 0), (-7, 29, -7),
])
def test_completion_hint_precedence_and_integer_conversion(surface, config_hint, body_hint, expected):
    surface.config['maxTokens'] = config_hint
    surface.body['max_tokens'] = body_hint
    assert surface.invoke() == 'accepted'
    assert surface.call('reserve_for_task')[0][2]['max_completion_tokens'] == expected


def test_empty_account_does_not_convert_invalid_completion_hint(surface):
    surface.account = ''
    surface.config['maxTokens'] = 'not-an-integer'
    assert surface.invoke() == 'accepted'
    assert surface.counts()['reserve_for_task'] == surface.counts()['bind_billing_reservation'] == 0
    assert surface.counts()['wallet.estimate'] == 0


@pytest.mark.parametrize('fault', ['conversion', 'estimate_prompt_tokens', 'wallet.estimate', 'wallet.reserve'])
def test_reservation_fault_rejects_and_rethrows_the_same_exception(surface, fault):
    error = RuntimeError('synthetic reserve fault')
    if fault == 'conversion':
        class BrokenInt:
            def __int__(self):
                raise error
        surface.config['maxTokens'] = BrokenInt()
    else:
        surface.faults[fault] = error
    with pytest.raises(RuntimeError) as caught:
        surface.invoke()
    assert caught.value is error
    _assert_rejected(surface, error, 'billing_reservation_failed')
    assert surface.counts()['wallet.reserve_release'] == surface.counts()['wallet.settle'] == 0


@pytest.mark.parametrize('major', ['v1', 'v4'])
def test_insufficient_funds_keeps_http_projection_and_never_dispatches(surface, major):
    from lib.billing import InsufficientFunds
    error = InsufficientFunds(surface.account, balance_micro=1234567, needed_micro=2345678)
    surface.faults['wallet.reserve'] = error
    status, body, content_type = surface.invoke(major)
    assert status == 402
    detail = 'Insufficient credits. Estimated cost 2.3457 credits, balance 1.2346.'
    if major == 'v1':
        body.pop('request_id', None)
        assert body == {'ok': False, 'error': detail, 'error_kind': 'insufficient_funds',
                        'balance_micro': 1234567, 'needed_micro': 2345678}
        assert content_type.startswith('application/json')
    else:
        assert body['status'] == 402 and body['detail'] == detail
        assert body['title'] == 'HTTP error' and body['code'] == 'http_error'
        assert body['instance'].startswith('/api/v4/')
        assert 'balance_micro' not in body and 'needed_micro' not in body
        assert content_type.startswith('application/problem+json')
    _assert_rejected(surface, error, 'billing_reservation_refused')
    assert surface.counts()['wallet.reserve'] == 1


@pytest.mark.parametrize('stage', ['hold', 'phase'])
def test_billing_bind_fault_rolls_back_once_and_preserves_identity(surface, monkeypatch, stage):
    error = RuntimeError('synthetic bind fault')
    if stage == 'hold':
        original = surface.session.hold_resource
        def fail_billing(name, *args, **kwargs):
            if name == 'billing':
                raise error
            return original(name, *args, **kwargs)
        monkeypatch.setattr(surface.session, 'hold_resource', fail_billing)
    else:
        def fail_phase():
            raise error
        monkeypatch.setattr(surface.session, 'mark_reserved', fail_phase)
    with pytest.raises(RuntimeError) as caught:
        surface.invoke()
    assert caught.value is error
    assert surface.rejected == [(surface.task, error, 'billing_bind_failed', 'caller-job')]
    assert surface.counts()['admission.acquire'] == surface.counts()['spawn'] == 0
    surface.session.settle(ExecutionPhase.FAILED)
    assert surface.counts()['wallet.reserve_release'] == surface.counts()['route.dispose'] == 1
    assert surface.counts()['wallet.settle'] == 0


@pytest.mark.parametrize('fault', ['admission.acquire', 'spawn'])
def test_failure_after_billing_bind_releases_without_settling(surface, fault):
    error = RuntimeError('synthetic pre-dispatch failure')
    surface.faults[fault] = error
    if fault == 'admission.acquire':
        with pytest.raises(RuntimeError) as caught:
            surface.invoke()
        assert caught.value is error
    else:
        assert surface.invoke()[0] == 500
    cause = 'admission_acquire_failed' if fault == 'admission.acquire' else 'task_spawn_failed'
    assert surface.rejected == [(surface.task, error, cause, 'caller-job')]
    surface.session.settle(ExecutionPhase.FAILED)
    assert surface.counts()['wallet.reserve_release'] == surface.counts()['route.dispose'] == 1
    assert surface.counts()['wallet.settle'] == 0
    assert surface.counts()['admission.release'] == int(fault == 'spawn')


@pytest.mark.parametrize('reserved', [73, 0])
def test_terminal_settlement_uses_owned_thread_and_late_model_lookup(surface, reserved):
    surface.reserve_micro = reserved
    assert surface.invoke() == 'accepted'
    surface.config['model'] = 'changed-after-reserve'
    surface.task['usage'] = {'input_tokens': 5, 'output_tokens': 2}
    surface.session.mark_dispatch_started()
    worker = threading.Thread(target=lambda: surface.session.settle(ExecutionPhase.COMPLETED))
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    surface.session.settle(ExecutionPhase.COMPLETED)
    settle = surface.call('settle_task')
    assert len(settle) == 1 and settle[0][3] == worker.ident
    model = 'changed-after-reserve' if surface.name == 'chat' else 'routed-model'
    assert settle[0][2] == {'user_id': surface.account, 'model': model, 'raise_on_error': True}
    assert surface.call('reserve_for_task')[0][2]['model'] == surface.model
    assert surface.counts()['wallet.settle'] == int(reserved > 0)
    assert surface.counts()['wallet.debit'] == int(reserved == 0)
    assert surface.counts()['wallet.reserve_release'] == 0
    assert surface.counts()['route.dispose'] == surface.counts()['admission.release'] == 1


@pytest.mark.parametrize('dispatched', [False, True])
def test_cleanup_fault_remains_deferred_and_is_not_retried(surface, dispatched):
    assert surface.invoke() == 'accepted'
    port = 'wallet.settle' if dispatched else 'wallet.reserve_release'
    surface.faults[port] = RuntimeError('synthetic wallet cleanup failure')
    if dispatched:
        surface.session.mark_dispatch_started()
    first = surface.session.settle(ExecutionPhase.FAILED)
    assert surface.session.settle(ExecutionPhase.FAILED) is first
    assert dict(first.resource_dispositions)['billing'] is ResourceDisposition.DEFERRED
    assert surface.counts()[port] == 1
    assert surface.counts()['route.dispose'] == surface.counts()['admission.release'] == 1
    callback = 'settle_task' if dispatched else 'release_reservation'
    assert surface.call(callback)[0][2]['raise_on_error'] is True


def test_billing_disabled_still_binds_without_wallet_operations(surface):
    surface.enabled = False
    assert surface.invoke() == 'accepted'
    surface.session.mark_dispatch_started()
    surface.session.settle(ExecutionPhase.COMPLETED)
    assert surface.counts()['reserve_for_task'] == surface.counts()['bind_billing_reservation'] == 1
    assert surface.counts()['settle_task'] == 1
    assert not any(name.startswith('wallet.') for name in surface.counts())


@pytest.mark.parametrize('surface', ['chat'], indirect=True)
def test_chat_estimates_final_translated_messages(surface, monkeypatch):
    import lib.chat.turn_builder as turn_builder
    monkeypatch.setattr('lib.conv_config.resolve_auto_translate', lambda _cfg: True)
    monkeypatch.setattr(turn_builder, 'translate_user_text_to_english',
                        lambda text, _cfg: ('translated English text', text, {'input_tokens': 3}, None))
    assert surface.invoke() == 'accepted'
    estimate = surface.call('estimate_prompt_tokens')[0][1][0]
    assert estimate is surface.messages
    assert estimate[-1]['content'] == 'translated English text'
    assert surface.call('reserve_for_task')[0][2]['prompt_tokens'] == len('translated English text') // 4
    assert surface.task['_translate_usage'] == {'input_tokens': 3}


def test_preparation_import_and_empty_account_keep_task_rejection_lazy():
    # A fresh interpreter also checks module loading. Limit the guard to this
    # adapter: sibling routes legitimately import their own billing exceptions.
    script = """
import builtins
original_import = builtins.__import__
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if (globals or {}).get('__name__') == 'routes.api_v1._task_billing':
        if name == 'lib.tasks_pkg.manager':
            raise AssertionError('task rejection loaded eagerly')
        if name == 'lib.billing' and 'InsufficientFunds' in fromlist:
            raise AssertionError('reservation exception imported outside billed path')
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded_import
from routes.api_v1._task_billing import prepare_task_billing
class Unreadable:
    def get(self, *args):
        raise AssertionError('empty account inspected config')
def unused_model():
    raise AssertionError('empty account resolved a model')
assert prepare_task_billing(
    {}, account_user_id='', execution_session=None, conversation_id='label',
    messages=None, config=Unreadable(), completion_hint=Unreadable(),
    model_for_billing=unused_model,
) is None
"""
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
