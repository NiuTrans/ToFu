"""Explicit native application execution over v2 routing and terminal commit.

Entry: run_native_task. Default context preparation reuses application policy;
callers supply tool/model policies, terminal projection and an owned executor. Rust runs model/tool rounds;
the canonical application writer acknowledges terminal durability. No implicit
route fallback, storage selection, worker pool or production default is changed.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Executor
from contextlib import nullcontext
import copy
from pathlib import Path
from typing import Any, Callable, Mapping

from lib.agent_core.native_process import ApprovalPolicy, NativeProcessClient, ProgressLimits, ProgressPolicy, CheckpointPolicy, RoundPolicy, TerminalRequest, TextDelta, ToolCallback
from lib.agent_core.native_process import valid_ack_budget
from lib.model_routing.native_route import Route, Result, run_native_route
from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy, project_native_terminal, task_lifetime_matches


class NativeTaskRounds:
    """Publish application round boundaries; the Rust controller owns iteration.

    Borrow the supplied policy and executor. Retain one open presentation round,
    without reserving model resources or copying round history/accounting.
    """

    __slots__ = ('principal', 'task', 'executor', 'policy', 'owner', 'task_id', 'open_round', 'response_policy')

    def __init__(self, *, principal, task, executor, policy):
        self.principal, self.task, self.executor, self.policy = principal, task, executor, policy
        self.owner = principal.require_owner(context='native round delivery')
        self.task_id = task['id']
        self.open_round = None
        self.response_policy = None

    @property
    def prepare(self):
        return getattr(self.policy, 'prepare', None)

    @property
    def accepts_native_cost(self):
        return getattr(self.policy, 'accepts_native_cost', False)

    @property
    def capture_fee_checkpoint(self):
        return getattr(self.policy, 'capture_fee_checkpoint', None)

    @property
    def prepare_fee_checkpoint_restore(self):
        return getattr(self.policy, 'prepare_fee_checkpoint_restore', None)

    def bind_pricing_route(self, metadata):
        bind = getattr(self.policy, 'bind_pricing_route', None)
        if callable(bind):
            return bind(metadata)

    def _check(self, request, *, live=True):
        if (request.task_id != self.task_id or self.task.get('id') != self.task_id
                or request.principal.require_owner(context='native round delivery') != self.owner
                or type(self.task.get('_userId')) is not int or self.task['_userId'] != self.owner
                or (live and self.task.get('status') not in {'pending', 'running'})):
            raise ValueError('native round delivery requires an owned live task')

    def finish(self, reason):
        from lib.agent_core.events import EventType, build_event
        from lib.tasks_pkg.manager import append_event
        if self.open_round is not None:
            number = self.open_round
            signal=self.task.get('abort_event')
            if self.task.get('aborted') or (signal is not None and signal.is_set()):
                # Stop withdraws ordinary presentation frames. The canonical
                # terminal writer still commits settled tool/account evidence;
                # a cancelled round-end must not strand that terminal ACK.
                self.open_round = None
                return
            append_event(self.task, build_event(EventType.ROUND_END, roundNum=number, reason=reason))
            self.open_round = None

    def _open(self, request):
        self._check(request)
        from lib.agent_core.events import EventType, build_event
        from lib.tasks_pkg.manager import append_event
        self.finish('tools')
        append_event(self.task, build_event(EventType.ROUND_START, roundNum=request.round))
        self.open_round = request.round

    async def reserve(self, request):
        self._check(request)
        accepted = True if self.policy is None else await self.policy.reserve(request)
        if accepted is True:
            from lib.agent_core.native_process import _joined
            work = asyncio.get_running_loop().run_in_executor(self.executor, self._open, request)
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                await _joined(work)
                raise
        return accepted

    async def settle(self, result):
        self._check(result.request, live=False)
        if self.policy is not None:
            await self.policy.settle(result)
        if self.response_policy is not None and result.evidence.get('kind')=='returned':
            work=asyncio.get_running_loop().run_in_executor(self.executor,self.response_policy,result)
            try:await asyncio.shield(work)
            except asyncio.CancelledError:
                from lib.agent_core.native_process import _joined
                await _joined(work)
                raise


def project_native_text(task: dict, delta: TextDelta) -> None:
    """Accumulate owned native text and publish through the ordinary event owner.

    The native text controller already owns ordering and byte admission. A text
    ACK acknowledges event-producer acceptance; terminal persistence joins any
    durable delivery debt. Preserve paid partial text if delivery fails.
    """
    from lib.tasks_pkg.manager import append_event
    from lib.agent_core.events import EventType, build_event

    if delta.channel not in {'content', 'reasoning'} or not isinstance(delta.text, str) or not delta.text:
        raise ValueError('native text projection requires a nonempty text channel')
    field = 'content' if delta.channel == 'content' else 'thinking'
    with task.get('content_lock') or nullcontext():
        if (delta.task_id != task.get('id') or type(task.get('_userId')) is not int
                or delta.principal.require_owner(context='native text projection') != task['_userId']
                or task.get('status') not in {'pending', 'running'}):
            raise ValueError('native text projection requires an owned live task')
        previous = task.get(field, '')
        if not isinstance(previous, str):
            raise ValueError('native text projection task text invalid')
        task[field] = previous + delta.text
    # Publication may snapshot text under the same non-reentrant lock.
    append_event(task, build_event(EventType.DELTA, **{field: delta.text}))


def compile_native_application_route(repository, principal, selection, *, policy=None, issued_group=None):
    """Compile through the owned production credential resolver, without I/O probes."""
    from lib.model_routing.native_route import compile_native_route
    from lib.model_routing.repository import OwnerBoundary
    from lib.oauth.native_credentials import NativeCodexCredentialResolver
    boundary=OwnerBoundary.create(principal.require_owner(context='native application route'), principal.tenant_id)
    expected = None
    if issued_group is not None:
        from lib.model_routing.dispatch_adapter import RoutedSlotGroup
        from lib.model_routing.native_route import NativeRouteError
        if (not isinstance(issued_group, RoutedSlotGroup) or issued_group.disposed
                or not issued_group.handles or not issued_group.candidates
                or not isinstance(issued_group.route_identity, Mapping)
                or issued_group.route_identity.get("owner_user_id") != boundary.owner_user_id
                or issued_group.route_identity.get("tenant_id") != boundary.tenant_id):
            raise NativeRouteError("native_route_owner")
        expected = issued_group.route_identity
    return compile_native_route(repository,boundary,selection,policy=policy,
        expected_route_identity=expected,
        credential_resolver=NativeCodexCredentialResolver(repository=repository,principal=principal))


def prepare_native_application_template(task: dict, metadata: Mapping[str, Any]) -> dict:
    """Compile generation intent against the issued route without copying history."""
    if (type(task.get('_userId')) is not int or type(metadata.get('owner_user_id')) is not int
            or metadata['owner_user_id'] != task['_userId']
            or not isinstance(task.get('id'), str) or not task['id']
            or not isinstance(metadata.get('logical_model'), str) or not metadata['logical_model']
            or not isinstance(metadata.get('wire_model'), str) or not metadata['wire_model']
            or not isinstance(metadata.get('inference_profile'), dict)):
        raise ValueError('native generation template requires an owned selected route')
    from lib.tasks_pkg.model_config import _resolve_model_config
    from lib.llm.reasoning import capture_intent, compile_reasoning
    config = task.get('config') or {}
    if not isinstance(config, dict):
        raise ValueError('native generation task configuration invalid')
    resolved = _resolve_model_config(dict(config, model=metadata['logical_model']), task['id'])
    if resolved['model'] != metadata['logical_model']:
        raise ValueError('native generation configuration changes the selected model')
    output = resolved['max_tokens']
    cap = metadata.get('max_output_tokens')
    if cap is not None and (type(cap) is not int or cap < 0):
        raise ValueError('native generation route output limit invalid')
    if cap:
        output = min(output, cap)
    body = {'model': metadata['logical_model'], 'max_tokens': output,
            '_reasoning_intent': capture_intent(
                thinking_enabled=resolved['thinking_enabled'], thinking_depth=resolved['thinking_depth'],
                preset=resolved['preset'], temperature=resolved['temperature'])}
    if resolved['response_format']:
        body['response_format'] = copy.deepcopy(resolved['response_format'])
    compile_reasoning(body, metadata['logical_model'], inference_profile=metadata['inference_profile'])
    template = {key: value for key, value in body.items() if not key.startswith('_')}
    template['model'] = metadata['wire_model']
    task['model'] = metadata['logical_model']
    return template


def prepare_native_application_counter(metadata, template, profile):
    """Bind declared counter bounds to the selected local tier and output field."""
    from lib.agent_core.native_content_contract import MODEL_COUNTER_SCHEMA
    from lib.token_counter.tiktoken_counter import encoding_for_model
    if (not isinstance(profile, dict) or not isinstance(metadata.get('logical_model'), str)
            or not metadata['logical_model']):
        raise ValueError('native application counter requires a selected model and bounds')
    fields = [name for name in ('max_tokens', 'max_completion_tokens') if name in template]
    if len(fields) != 1:
        raise ValueError('native application counter requires one output field')
    counter = dict(profile)
    counter.setdefault('encoding', encoding_for_model(metadata['logical_model']))
    counter.setdefault('output_field', fields[0])
    if (set(counter) != set(MODEL_COUNTER_SCHEMA['required'])
            or counter['output_field'] != fields[0]):
        raise ValueError('native application counter fields differ from generation')
    for name, rule in MODEL_COUNTER_SCHEMA['properties'].items():
        value = counter[name]
        if ('enum' in rule and value not in rule['enum']) or (
                rule.get('type') == 'integer' and (type(value) is not int
                or value < rule.get('minimum', 0) or value > rule.get('maximum', 18446744073709551615))):
            raise ValueError('native application counter bound invalid: ' + name)
    return counter


async def run_native_task(
    client: NativeProcessClient,
    route: Route,
    configuration: Mapping[str, Any],
    *,
    task: dict,
    executor: Executor,
    project_terminal: Callable[[dict, TerminalRequest], None] = project_native_terminal,
    prepare_template: Callable[[dict], Mapping[str, Any]] | None = None,
    prepare_wire_tools=None,
    wire_context_tools=None,
    callbacks: Mapping[str, ToolCallback],
    working_directory: Path,
    round_policy: RoundPolicy | None = None,
    progress_policy: ProgressPolicy | None = None,
    checkpoint_policy: CheckpointPolicy | None = None,
    project_text: Callable[[dict, TextDelta], None] | None = None,
    approval_policy: ApprovalPolicy | None = None,
    gateway=None,
    model_policy=None,
    pricing_resolver=None,
    after_terminal: Callable[[dict], None] | None = None,
    transient: bool = False,
) -> Result:
    """Run a task through its explicitly selected durable or transient terminal owner.

    The application retains preparation, routing, accounting and projection
    policy. Explicit pricing_resolver composes the existing task account and
    fee projection; supplied context preparation/binding/cleanup stay owned.
    The terminal profile supplies a caller-owned operation ID and frame
    bound. Invalid terminal composition fails before consuming the single-use route.
    """
    from lib.tasks_pkg.native_checkpoint import checkpoint_lifetime_matches
    if (not task_lifetime_matches(task, transient)
            or not checkpoint_lifetime_matches(task,checkpoint_policy,configuration,transient)):
        raise ValueError('native task requires its declared lifetime without durable transient ports')
    profile = configuration.get('terminal_persistence')
    if not callable(project_terminal):
        raise ValueError('native application terminal projector must be callable')
    if after_terminal is not None and not callable(after_terminal):
        raise ValueError('native application completion callback must be callable')
    if prepare_template is not None and not callable(prepare_template):
        raise ValueError('native application generation template must be callable')
    if prepare_template is None:
        prepare_template = lambda metadata: prepare_native_application_template(task, metadata)
    if (not isinstance(profile, Mapping)
            or configuration.get('task_id') != task.get('id')
            or type(configuration.get('owner_user_id')) is not int
            or configuration['owner_user_id'] != client.principal.require_owner(
                context='native application execution')
            or type(profile.get('frame_bytes')) is not int
            or profile['frame_bytes'] <= 0
            or profile['frame_bytes'] > client.budget.frame_bytes
            or type(profile.get('attempts')) is not int or profile['attempts'] <= 0
            or not valid_ack_budget(profile)):
        raise ValueError('native application execution requires owned terminal admission')
    from lib.agent_core.abort_signal import AbortSignal
    stop_signal=task.get('abort_event')
    if not isinstance(stop_signal,AbortSignal):
        raise ValueError('native task requires the owned task notification signal')
    from lib.agent_core.execution_budget import execution_deadline_unix_ms
    deadline = execution_deadline_unix_ms(task)
    provided_deadline = configuration.get('deadline_unix_ms')
    if 'deadline_unix_ms' in configuration and (
            type(provided_deadline) is not int or not 0 <= provided_deadline <= 18446744073709551615):
        raise ValueError('native application absolute deadline invalid')
    if deadline is not None:
        configuration = dict(configuration, deadline_unix_ms=(
            min(deadline, provided_deadline) if provided_deadline is not None else deadline))
    from lib.tasks_pkg.native_context import NativeContextPolicy
    if round_policy is None or not callable(getattr(round_policy, 'prepare', None)):
        round_policy = NativeContextPolicy(principal=client.principal, task=task,
            executor=executor, round_policy=round_policy,
            token_count_host=getattr(client,'_token_count_host',None))
        # Resolve project behavior from the selected route at binding time, using
        # the same configuration owner as the ordinary application invocation.
        default_context = round_policy
    else:
        default_context = None
    context_policy = round_policy if isinstance(round_policy, NativeContextPolicy) else None
    if prepare_wire_tools is not None:
        if context_policy is None:
            raise ValueError('native wire preparation requires the shared context owner')
        context_policy.bind_wire_projection(prepare_wire_tools,wire_context_tools)
        configuration=dict(configuration,wire_tool_preparation=True)
    if isinstance(round_policy, NativeContextPolicy) and (
            round_policy.task is not task or round_policy.executor is not executor
            or round_policy.owner != client.principal.require_owner(context='native context composition')):
        raise ValueError('native context composition differs')
    from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
    from lib.tasks_pkg.native_file_recovery import NativeFileToolRecovery
    file_recovery = (checkpoint_policy.tool_recovery
        if isinstance(checkpoint_policy, NativeCheckpointPolicy)
        and isinstance(checkpoint_policy.tool_recovery, NativeFileToolRecovery) else None)
    if file_recovery is not None and gateway is None:
        raise ValueError('native file checkpoint recovery requires its application gateway')
    if file_recovery is not None and file_recovery.registered_child != checkpoint_policy.registered_child:
        raise ValueError('native file checkpoint scope differs from its registered owner')
    if isinstance(checkpoint_policy, NativeCheckpointPolicy) and (
            checkpoint_policy.task is not task or checkpoint_policy.executor is not executor
            or checkpoint_policy.owner != client.principal.require_owner(context='native checkpoint composition')
            or checkpoint_policy.epoch != configuration.get('epoch') or checkpoint_policy.closed):
        raise ValueError('native checkpoint composition differs')
    if file_recovery is not None:
        file_recovery.bind_checkpoint_storage(checkpoint_policy.storage)
    if pricing_resolver is not None:
        from lib.tasks_pkg.native_pricing import NativeRoutePricing
        from lib.tasks_pkg.native_cost_projection import NativeTaskCostPolicy
        from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
        control = configuration.get('model_control')
        if (not isinstance(pricing_resolver, NativeRoutePricing)
                or not isinstance(control, Mapping) or control.get('pricing') is not True
                or round_policy is None or model_policy is not None
                or getattr(round_policy, 'accepts_native_cost', False) is True):
            raise ValueError('native priced task requires one model and cost owner')
        if (pricing_resolver.principal.require_owner(context='native task pricing') != client.principal.require_owner(context='native task pricing')
                or (pricing_resolver.principal.tenant_id or '') != (client.principal.tenant_id or '')):
            raise ValueError('native task pricing ownership differs')
        model_policy = NativeModelAccountPolicy(principal=client.principal, task=task,
            executor=executor, pricing_resolver=pricing_resolver)
        round_policy = NativeTaskCostPolicy(principal=client.principal, task=task, policy=round_policy)
    if model_policy is None and 'model_control' in configuration:
        control = configuration['model_control']
        if (not isinstance(control, Mapping) or control.get('pricing', False) is not False
                or not isinstance(configuration.get('model_counter'), dict)
                or 'token_admission' in configuration):
            raise ValueError('native default model account requires one unpriced counter authority')
        from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
        model_policy = NativeModelAccountPolicy(principal=client.principal, task=task,
            executor=executor)
    if model_policy is not None:
        from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
        if isinstance(model_policy, NativeModelAccountPolicy) and (
                model_policy.task is not task or model_policy.executor is not executor
                or model_policy.owner != client.principal.require_owner(context='native model account composition')):
            raise ValueError('native model account composition differs')
    rounds = NativeTaskRounds(principal=client.principal, task=task, executor=executor, policy=round_policy)
    round_policy = rounds
    # Capture the identity, not the policy which owns this callback. A policy
    # back-reference retains the completed invocation/context until cyclic GC.
    terminal_task_id = task['id']
    def project_outcome(owner_task, request):
        rounds._check(request)
        status = request.event.get('status')
        if request.event.get('type') != status or status not in {'done', 'error', 'aborted'}:
            raise ValueError('native round terminal outcome invalid')
        if gateway is not None:
            gateway.finish_approvals()
        result = project_terminal(owner_task, request)
        if result is not None:
            raise TypeError('native terminal projector must return None')
        if status=='done' and rounds.open_round is not None:
            from lib.tasks_pkg.orchestrator._completion_translation import submit_final_translation
            submit_final_translation(owner_task,rounds.open_round,owner_task.get('content') or '')
        rounds.finish('stop' if status == 'done' else status)
        if (status == 'done' and context_policy is not None
                and isinstance(owner_task.get('messages'), list)):
            from lib.tasks_pkg.orchestrator._completion_projection import publish_final_messages_snapshot
            from lib.tasks_pkg.manager import append_event
            model, tools = context_policy.completion_binding()
            publish_final_messages_snapshot(owner_task, owner_task.get('messages'),
                model=model, tid=terminal_task_id[:8], tools=tools, publish=append_event)
    terminal = NativeTaskTerminalPolicy(
        principal=client.principal, task=task, operation_id=profile.get('operation_id'),
        executor=executor, project=project_outcome, candidate_bytes=profile['frame_bytes'], transient=transient,
    )
    if gateway is not None:
        from lib.tasks_pkg.native_gateway import NativeGateway
        if isinstance(gateway, NativeGateway) and 'tools' not in configuration:
            configuration = dict(configuration, tools=gateway.native_catalog())
        if (not isinstance(gateway, NativeGateway) or gateway.task is not task
                or gateway.owner != terminal.owner or gateway.executor is not executor
                or gateway.multimodal != (configuration.get('tool_feedback','text')=='multimodal')
                or set(gateway.permissions) != set(configuration.get('tools') or {})
                or any(configuration['tools'][name].get('permission') != permission
                       for name, permission in gateway.permissions.items())):
            raise ValueError('native application gateway composition differs')
        if callbacks or approval_policy is not None or progress_policy is not None:
            raise ValueError('native application gateway has one callback/policy owner')
        parallel_names = configuration.get('parallel_tool_names', sorted(gateway.parallel_names))
        if (not isinstance(parallel_names, list) or any(not isinstance(name, str) for name in parallel_names)
                or not set(parallel_names).issubset(gateway.parallel_names)
                or len(parallel_names) != len(set(parallel_names))):
            raise ValueError('native application parallel tools differ from canonical scheduling policy')
        configuration = dict(configuration, parallel_tool_names=parallel_names)
        no_progress = configuration.get('no_progress_rounds', 0)
        terminal_failures = configuration.get('nonretryable_failure_rounds', 0)
        if configuration.get('progress_control') is True or no_progress != 0 or terminal_failures != 0:
            if rounds.policy is None:
                raise ValueError('native gateway progress requires round settlement')
            progress_policy = gateway.enable_progress(ProgressLimits(no_progress, terminal_failures), configuration.get('feedback_bytes'))
        callbacks = gateway.callbacks()
        if file_recovery is not None:
            file_recovery.bind_gateway(gateway)
        if prepare_wire_tools is not None or file_recovery is not None or parallel_names:
            configuration=dict(configuration,tool_sibling_control=True)
            rounds.response_policy=gateway.prepare_response
        approval_policy = gateway if ('approval_control' in configuration
            or any(value != 'read' for value in gateway.permissions.values())) else None
    if project_text is None and configuration.get('text_progress') is True:
        project_text = project_native_text
    if project_text is not None and not callable(project_text):
        raise ValueError('native application text requires a callable projector')
    async def on_text(delta):
        if (delta.task_id != task['id'] or type(task.get('_userId')) is not int
                or task['_userId'] != terminal.owner
                or task.get('status') not in {'pending', 'running'}):
            raise ValueError('native text projection task ownership changed')
        result = await asyncio.get_running_loop().run_in_executor(executor, project_text, task, delta)
        if result is not None:
            raise TypeError('native text projector must return None')
    def bind_context(metadata, template, catalog):
        if default_context is not None:
            from lib.tasks_pkg.model_config import _resolve_model_config
            resolved = _resolve_model_config(dict(task.get('config') or {},
                model=metadata['logical_model']), task['id'])
            if resolved['model'] != metadata['logical_model']:
                raise ValueError('native context configuration changes the selected model')
            default_context.project_path = resolved['project_path']
            default_context.project_enabled = resolved['project_enabled']
        if context_policy is not None:
            context_policy.bind_route(metadata, template, catalog)

    try:
        if gateway is not None and gateway.round_source is None:
            gateway.round_source = lambda: rounds.open_round
        return await run_native_route(
            client, route, configuration, prepare_template=prepare_template,
            prepare_wire_tools=None,
            prepare_model_counter=prepare_native_application_counter if 'model_counter' in configuration else None,
            callbacks=callbacks, working_directory=working_directory,
            round_policy=round_policy, progress_policy=progress_policy, checkpoint_policy=checkpoint_policy, terminal_policy=terminal, model_policy=model_policy,
            on_text=on_text if project_text is not None else None,
            approval_policy=approval_policy,
            stop_signal=stop_signal,
            bind_round_policy=(bind_context if default_context is not None else
                context_policy.bind_route if context_policy is not None else None),
        )
    finally:
        import sys
        primary = sys.exception()
        try:
            if isinstance(checkpoint_policy, NativeCheckpointPolicy):
                try:
                    await checkpoint_policy.close(terminal_saved=terminal.accepted)
                except BaseException as cleanup_error:
                    if primary is None:
                        raise
                    primary.add_note('native checkpoint cleanup failed: ' + type(cleanup_error).__name__)
        finally:
            if context_policy is not None:
                context_policy.close()
            if gateway is not None:
                gateway.close()
        if terminal.accepted and after_terminal is not None:
            from contextvars import copy_context
            from lib.agent_core.native_process import _joined
            work=asyncio.get_running_loop().run_in_executor(executor,copy_context().run,after_terminal,task)
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError as cancelled:
                try:await _joined(work)
                except BaseException as error:
                    cancelled.add_note('native post-commit callback failed: '+type(error).__name__)
                if primary is None:raise
                primary.add_note('native post-commit callback wait cancelled after join')
            except BaseException as error:
                if primary is None:raise
                primary.add_note('native post-commit callback failed: '+type(error).__name__)
