"""Explicit native application execution over v2 routing and terminal commit.

Entry: run_native_task. Callers supply existing prepared context, tool/model
policies, terminal projection and an owned executor. Rust runs model/tool rounds;
the canonical application writer acknowledges terminal durability. No implicit
route fallback, storage selection, worker pool or production default is changed.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Executor
from pathlib import Path
from typing import Any, Callable, Mapping

from lib.agent_core.native_process import ApprovalPolicy, NativeProcessClient, ProgressLimits, ProgressPolicy, CheckpointPolicy, RoundPolicy, TerminalRequest, TextDelta, ToolCallback
from lib.model_routing.native_route import Route, Result, run_native_route
from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy


async def run_native_task(
    client: NativeProcessClient,
    route: Route,
    configuration: Mapping[str, Any],
    *,
    task: dict,
    executor: Executor,
    project_terminal: Callable[[dict, TerminalRequest], None],
    prepare_template: Callable[[dict], Mapping[str, Any]],
    callbacks: Mapping[str, ToolCallback],
    working_directory: Path,
    round_policy: RoundPolicy | None = None,
    progress_policy: ProgressPolicy | None = None,
    checkpoint_policy: CheckpointPolicy | None = None,
    project_text: Callable[[dict, TextDelta], None] | None = None,
    approval_policy: ApprovalPolicy | None = None,
    gateway=None,
    model_policy=None,
) -> Result:
    """Run a prepared durable task; success requires the existing commit path.

    The application retains preparation, routing, accounting and projection
    policy. The terminal profile supplies a caller-owned operation ID and frame
    bound. Invalid terminal composition fails before consuming the single-use route.
    """
    profile = configuration.get('terminal_persistence')
    if (not isinstance(profile, Mapping)
            or configuration.get('task_id') != task.get('id')
            or type(configuration.get('owner_user_id')) is not int
            or configuration['owner_user_id'] != client.principal.require_owner(
                context='native application execution')
            or type(profile.get('frame_bytes')) is not int
            or profile['frame_bytes'] <= 0
            or profile['frame_bytes'] > client.budget.frame_bytes
            or any(type(profile.get(key)) is not int or profile[key] <= 0
                   for key in ('attempts', 'ack_ms'))):
        raise ValueError('native application execution requires owned terminal admission')
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
    if isinstance(round_policy, NativeContextPolicy) and (
            round_policy.task is not task or round_policy.executor is not executor
            or round_policy.owner != client.principal.require_owner(context='native context composition')):
        raise ValueError('native context composition differs')
    from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
    if isinstance(checkpoint_policy, NativeCheckpointPolicy) and (
            checkpoint_policy.task is not task or checkpoint_policy.executor is not executor
            or checkpoint_policy.owner != client.principal.require_owner(context='native checkpoint composition')
            or checkpoint_policy.epoch != configuration.get('epoch') or checkpoint_policy.closed):
        raise ValueError('native checkpoint composition differs')
    if model_policy is not None:
        from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
        if isinstance(model_policy, NativeModelAccountPolicy) and (
                model_policy.task is not task or model_policy.executor is not executor
                or model_policy.owner != client.principal.require_owner(context='native model account composition')):
            raise ValueError('native model account composition differs')
    terminal = NativeTaskTerminalPolicy(
        principal=client.principal, task=task, operation_id=profile.get('operation_id'),
        executor=executor, project=project_terminal, candidate_bytes=profile['frame_bytes'],
    )
    if gateway is not None:
        from lib.tasks_pkg.native_gateway import NativeGateway
        if (not isinstance(gateway, NativeGateway) or gateway.task is not task
                or gateway.owner != terminal.owner or gateway.executor is not executor
                or gateway.multimodal != (configuration.get('tool_feedback','text')=='multimodal')
                or set(gateway.permissions) != set(configuration.get('tools') or {})
                or any(configuration['tools'][name].get('permission') != permission
                       for name, permission in gateway.permissions.items())):
            raise ValueError('native application gateway composition differs')
        if callbacks or approval_policy is not None or progress_policy is not None:
            raise ValueError('native application gateway has one callback/policy owner')
        no_progress = configuration.get('no_progress_rounds', 0)
        terminal_failures = configuration.get('nonretryable_failure_rounds', 0)
        if configuration.get('progress_control') is True or no_progress != 0 or terminal_failures != 0:
            if round_policy is None:
                raise ValueError('native gateway progress requires round settlement')
            progress_policy = gateway.enable_progress(ProgressLimits(no_progress, terminal_failures), configuration.get('feedback_bytes'))
        callbacks = gateway.callbacks()
        approval_policy = gateway if gateway.decision_policy is not None else None
    if (project_text is not None and not callable(project_text)) or (
            configuration.get('text_progress') is True and project_text is None):
        raise ValueError('native application text requires an explicit projector')
    async def on_text(delta):
        if (delta.task_id != task['id'] or type(task.get('_userId')) is not int
                or task['_userId'] != terminal.owner
                or task.get('status') not in {'pending', 'running'}):
            raise ValueError('native text projection task ownership changed')
        result = await asyncio.get_running_loop().run_in_executor(executor, project_text, task, delta)
        if result is not None:
            raise TypeError('native text projector must return None')
    try:
        return await run_native_route(
            client, route, configuration, prepare_template=prepare_template,
            callbacks=callbacks, working_directory=working_directory,
            round_policy=round_policy, progress_policy=progress_policy, checkpoint_policy=checkpoint_policy, terminal_policy=terminal, model_policy=model_policy,
            on_text=on_text if project_text is not None else None,
            approval_policy=approval_policy,
            bind_round_policy=round_policy.bind_route if isinstance(round_policy, NativeContextPolicy) else None,
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
            if isinstance(round_policy, NativeContextPolicy):
                round_policy.close()
            if gateway is not None:
                gateway.close()
