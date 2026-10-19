"""Compose accepted pending Goals through the ordinary Flow execution boundary.

NativeGoalRecoveryConfiguration is trusted application policy. Byte bounds use
the issued native transport; I/O deadlines use selected storage policy or an
explicit override. Storage is resolved once per preflight/execution scope.
execute_pending_goal_flow borrows the existing loop, executor, journal, children
and completion path. It creates no worker, model loop or retry mechanism.
"""
from contextlib import contextmanager
from concurrent.futures import CancelledError as FutureCancelledError
from dataclasses import dataclass
import asyncio
import json
import math
import sys

from lib.orchestration.execution_journal import RecoveryRefused


def _storage():
    from lib.storage import get_storage_client
    return get_storage_client(write=True)


@dataclass(frozen=True)
class NativeGoalRecoveryConfiguration:
    storage_factory: object = _storage
    header_bytes: int | None = None
    snapshot_bytes: int | None = None
    identity_bytes: int | None = None
    evidence_bytes: int | None = None
    io_timeout_ms: int | None = None

    def __post_init__(self):
        from tofu_db.generated_checkpoint_control import CHECKPOINT_DOCUMENT_MAX_BYTES
        if (not callable(self.storage_factory) or asyncio.iscoroutinefunction(self.storage_factory)
                or any(value is not None and (type(value) is not int or not 0 < value <= 18446744073709551615)
                       for value in (self.header_bytes, self.snapshot_bytes, self.identity_bytes,
                                     self.evidence_bytes, self.io_timeout_ms))):
            raise ValueError('native Goal recovery requires synchronous storage and positive bounds')
        if any(value is not None and value > CHECKPOINT_DOCUMENT_MAX_BYTES
               for value in (self.header_bytes, self.snapshot_bytes, self.evidence_bytes)):
            raise ValueError('native Goal recovery exceeds checkpoint document capacity')

    def bounds(self, transport, *, storage):
        from tofu_db.generated_checkpoint_control import CHECKPOINT_DOCUMENT_MAX_BYTES
        timeout = self.io_timeout_ms
        if timeout is None:
            seconds = getattr(storage, 'request_timeout_seconds', None)
            if (type(seconds) not in (int, float)
                    or not 0 < seconds <= 18446744073709551615 / 1000 or not math.isfinite(seconds)):
                raise ValueError('native Goal recovery requires selected storage I/O policy')
            timeout = math.ceil(seconds * 1000)
            if timeout > 18446744073709551615:
                raise ValueError('native Goal recovery storage I/O policy exceeds protocol range')
        document_bytes = min(transport.frame_bytes, CHECKPOINT_DOCUMENT_MAX_BYTES)
        return dict(header_bytes=self.header_bytes or document_bytes,
            snapshot_bytes=self.snapshot_bytes or document_bytes,
            identity_bytes=self.identity_bytes or transport.frame_bytes,
            evidence_bytes=self.evidence_bytes or min(transport.configuration_bytes, CHECKPOINT_DOCUMENT_MAX_BYTES),
            io_timeout_ms=timeout)


def recovery_options(task=None):
    """Read explicit application policy for command preflight or bound execution."""
    from lib.tasks_pkg.native_invocation import NativePreparedInvocation
    if task is not None:
        port = task.get('_nativePreparedInvocation')
        if not isinstance(port, NativePreparedInvocation) or port.goal_recovery is None:
            return {}
        configuration, transport = port.goal_recovery, port.budget
    else:
        from quart import current_app, has_app_context
        from lib.tasks_pkg.native_application import NativeApplicationBinding
        if not has_app_context():
            return {}
        binding = current_app.extensions.get('tofu_native_application')
        if not isinstance(binding, NativeApplicationBinding) or binding.closed:
            return {}
        configuration = binding.configuration.goal_recovery
        if configuration is None:
            return {}
        transport = binding.host.image.budget
    storage = configuration.storage_factory()
    bounds = configuration.bounds(transport, storage=storage)
    bounds.pop('evidence_bytes')
    return dict(pending_actor_bounds=bounds, storage=storage)


def _on_loop(port, coroutine, abort):
    """Join a scope transition on the same serving loop as its native children."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        coroutine.close()
        raise RecoveryRefused('Goal recovery requires the ordinary Flow worker')
    if not port.loop.is_running() or port.loop.is_closed():
        coroutine.close()
        raise RecoveryRefused('Goal recovery serving loop is unavailable')
    try:
        future = asyncio.run_coroutine_threadsafe(coroutine, port.loop)
    except BaseException:
        coroutine.close()
        raise
    # This private future is never cancelled: completion includes native teardown.
    try:
        return future.result()
    except FutureCancelledError:
        raise asyncio.CancelledError from None
    except BaseException:
        if not future.done():
            abort.set()
            while not future.done():
                try:
                    future.result()
                except BaseException:
                    if future.done():
                        break
        raise


@contextmanager
def _prepared_scope(port, handoff, bindings, authorization):
    # Ordinary child completion drops task resource references before this outer
    # scope closes. Retain only these exact borrowed ports until joined teardown.
    from lib.tasks_pkg.native_invocation import NativePreparedInvocation
    ports = [child.get('_nativePreparedInvocation') for child, _ in bindings]
    if any(not isinstance(child_port, NativePreparedInvocation)
           or child_port.checkpoint_preparation is not None or child_port.loop is not port.loop
           or child_port.executor is not port.executor for child_port in ports):
        raise RecoveryRefused('Goal recipients require unused native preparation ports')
    scope = handoff.prepare_recipients(bindings, authorization=authorization, executor=port.executor)
    preparation = _on_loop(port, scope.__aenter__(), handoff.task['abort_event'])
    try:
        for child_port in ports:
            child_port.checkpoint_preparation = preparation
        yield
    except BaseException:
        _on_loop(port, scope.__aexit__(*sys.exc_info()), handoff.task['abort_event'])
        raise
    else:
        _on_loop(port, scope.__aexit__(None, None, None), handoff.task['abort_event'])
    finally:
        for child_port in ports:
            child_port.checkpoint_preparation = None


class _FlowStopped(Exception):
    def __init__(self, outcome):
        self.outcome = outcome


def execute_pending_goal_flow(task, definition, launch, *, journal_factory, on_event, execution_kwargs):
    """Claim once, prepare the whole set, then use the same Flow and event adapter."""
    from lib.billing.native_admission import protect_before_dispatch
    from lib.goal_runs.pending_claim import claim_pending_goal_actors
    from lib.orchestration.agent_runner import OrchestrationAgentRunnerConfig
    from lib.orchestration.prepared_recipients import prepare_flow_recipients
    from lib.orchestration.runtime_service import execute_flow
    from lib.tasks_pkg.invocation_registration import owned_child_record
    from lib.tasks_pkg.native_goal_handoff import NativeGoalHandoff
    from lib.tasks_pkg.native_invocation import NativePreparedInvocation

    seed = task['_goalRecoverySeed']
    port = task.get('_nativePreparedInvocation')
    if (not isinstance(port, NativePreparedInvocation) or port.goal_recovery is None
            or port.checkpoint_factory is None or port._transient or not callable(journal_factory)
            or json.loads(port._profile).get('checkpoint_actor_account') is not True):
        raise RecoveryRefused('Pending Goal requires its issued native recovery configuration')
    port.require_worker()
    owner = port.principal.require_owner(context='Goal recovery dispatch')
    storage = port.goal_recovery.storage_factory()
    bounds = port.goal_recovery.bounds(port.budget, storage=storage)
    inputs = seed.inspect_pending_inputs(maximum_bytes=bounds['evidence_bytes'], task=task,
        abort_check=execution_kwargs['abort_check'], native_flow_library=port.flow_scheduler)
    from lib.goal_runs.fee_account import original_reservation
    wallet, reserved = original_reservation(task, seed.metadata, storage=storage,
                                           deadline=bounds['io_timeout_ms']/1000)
    claim = claim_pending_goal_actors(task, seed, principal=port.principal, storage=storage,
        wallet_user_id=wallet, reserved_micro=reserved,
        **{key: value for key, value in bounds.items() if key != 'identity_bytes'})
    handoff = None
    def handoff_factory(journal):
        nonlocal handoff
        handoff = NativeGoalHandoff(principal=port.principal, task=task, storage=storage,
            evidence_bytes=bounds['evidence_bytes'], io_timeout_ms=bounds['io_timeout_ms'], journal=journal)
        return handoff
    try:
        journal = journal_factory(task, definition, launch, native_claim=claim,
                                  account_handoff_factory=handoff_factory)
        protect_before_dispatch(task, budget=task['_executionBudget'], owner=owner)
        options = execution_kwargs['executor_options']
        config = OrchestrationAgentRunnerConfig(**{key: options[key]
            for key in OrchestrationAgentRunnerConfig.__dataclass_fields__})
        with prepare_flow_recipients(inputs, config=config, emit=on_event,
                abort_check=execution_kwargs['abort_check']) as recipients:
            bindings = recipients.source_bindings({key: row['admission']
                for key, row in seed.pending_actors.actors.items()})
            authorization = dict(format='tofu.native-goal-actor-handoff/v1', claim_request=claim.request,
                target_reserved_micro=task.get('_billing_reservation_micro', 0),
                actors=[dict(source_task_id=source, target_task_id=child['id'],
                    target_registration_version=owned_child_record(storage, user_id=owner,
                        conversation_id=task['convId'], invocation_id=child['id'])['version'])
                    for child, source in bindings])
            recipients.bind(journal)
            with _prepared_scope(port, handoff, bindings, authorization):
                options['execution_journal'] = journal
                outcome = execute_flow(definition, on_event=on_event, **execution_kwargs)
                if not outcome.result['ok']:
                    raise _FlowStopped(outcome)
                journal.verify_consumed()
                return outcome
    except _FlowStopped as stopped:
        return stopped.outcome
    finally:
        if handoff is not None:
            handoff.close()
