"""Existing application prompt admission over native-owned round history.

NativeContextPolicy binds the compiled route and actual output/tool projection,
runs the canonical prompt admission in the caller's executor, and delegates
reservation/settlement. It creates no loop, worker pool or history mirror.
"""
import asyncio
from concurrent.futures import Executor
import json
import time

from lib.agent_core.native_process import RoundRequest
from lib.identity import PrincipalContext


class NativeContextPolicy:
    def __init__(self, *, principal: PrincipalContext, task: dict,
                 executor: Executor, round_policy, project_path=None, project_enabled=False):
        self.owner = principal.require_owner(context='native context policy')
        if (type(task.get('_userId')) is not int or task['_userId'] != self.owner
                or not isinstance(task.get('id'), str) or not task['id']
                or not isinstance(executor, Executor)
                or not all(callable(getattr(task.get('abort_event'), name, None))
                           for name in ('set', 'is_set'))
                or not all(callable(getattr(round_policy, name, None))
                           for name in ('reserve', 'settle'))
                or callable(getattr(round_policy, 'prepare', None))):
            raise ValueError('native context requires one owned preparation/accounting policy')
        if type(project_enabled) is not bool or (project_path is not None and not isinstance(project_path, str)):
            raise ValueError('native context project configuration invalid')
        self.project_path, self.project_enabled = project_path, project_enabled
        self.task, self.task_id, self.executor = task, task['id'], executor
        self.policy = round_policy
        self._bound = None
        self._closed = False

    def bind_route(self, metadata, template, catalog):
        """Called only after the issued owner route clamps its generation output."""
        if self._closed or self._bound is not None or metadata.get('owner_user_id') != self.owner:
            raise ValueError('native context route binding differs')
        fields = [key for key in ('max_tokens', 'max_completion_tokens') if key in template]
        if len(fields) != 1:
            raise ValueError('native context output binding differs')
        output = template[fields[0]]
        from lib.llm_dispatch.native_context_scope import native_context_scope
        with native_context_scope(self.task, metadata, output):
            pass  # Validate before native acquisition or callbacks.
        from lib.agent_core.native_content_contract import native_model_tools
        if not isinstance(catalog, dict) or any(not isinstance(value, dict) for value in catalog.values()):
            raise ValueError('native context catalog binding differs')
        self._bound = json.dumps([metadata, output, native_model_tools(catalog)],
                                 ensure_ascii=False, allow_nan=False)

    def close(self):
        self._closed = True
        self._bound = None

    def _check(self, request: RoundRequest):
        if (self._closed or request.principal.require_owner(context='native context round') != self.owner
                or request.task_id != self.task_id or self.task.get('id') != self.task_id
                or type(self.task.get('_userId')) is not int or self.task['_userId'] != self.owner
                or self.task.get('status') not in {'pending', 'running'}
                or self._bound is None):
            raise ValueError('native context round ownership changed')

    def _prepare(self, request):
        self._check(request)
        if self.task['abort_event'].is_set():
            raise asyncio.CancelledError
        if request.deadline_monotonic_ns is not None and time.monotonic_ns() >= request.deadline_monotonic_ns:
            self.task['abort_event'].set()
            raise TimeoutError('native context deadline expired before worker entry')
        if not isinstance(request.messages, list) or request.history_messages != len(request.messages):
            raise ValueError('native context history binding differs')
        metadata, output, tools = json.loads(self._bound)
        model = (self.task.get('config') or {}).get('model') or self.task.get('model')
        if model and model not in (metadata['logical_model'], metadata['wire_model']):
            raise ValueError('native context model binding changed')
        from lib.llm_dispatch.native_context_scope import native_context_scope
        with native_context_scope(self.task, metadata, output, deadline_monotonic_ns=request.deadline_monotonic_ns):
            from lib.tasks_pkg.orchestrator._root_agent_loop import prepare_round_context, admit_round_context
            self.task.setdefault('_firstDispatchRound', request.round)
            prepare_round_context(self.task, request.messages, round_num=request.round,
                tid=self.task_id[:8], model=metadata['logical_model'],
                project_path=self.project_path, project_enabled=self.project_enabled,
                round_tools=tools)
            admit_round_context(self.task, request.messages, tools,
                round_num=request.round, model=metadata['logical_model'])
        self._check(request)
        if self.task['abort_event'].is_set():
            raise asyncio.CancelledError
        return request.messages

    async def prepare(self, request):
        self._check(request)
        if self.task['abort_event'].is_set():
            raise asyncio.CancelledError
        deadline = request.deadline_monotonic_ns
        if deadline is not None and (type(deadline) is not int or not 0 <= deadline <= 18446744073709551615):
            raise ValueError('native context deadline invalid')
        if deadline is not None and deadline <= time.monotonic_ns():
            self.task['abort_event'].set()
            raise TimeoutError('native context deadline expired')
        loop = asyncio.get_running_loop()
        until = None if deadline is None else loop.time() + max(0, (deadline - time.monotonic_ns()) / 1_000_000_000)
        worker = self.executor.submit(self._prepare, request)
        future = asyncio.wrap_future(worker, loop=loop)
        try:
            async with asyncio.timeout_at(until):
                return await asyncio.shield(future)
        except (asyncio.CancelledError, TimeoutError):
            worker.cancel()  # Remove queued work; running work stays owned below.
            # The existing compaction pipeline observes the owning task abort.
            event = self.task.get('abort_event')
            if event is not None:
                event.set()
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break  # Joined failed preparation; parent cancellation wins.
            if future.done() and not future.cancelled():
                future.exception()  # Retrieve joined failure without hiding cancellation.
            raise

    async def reserve(self, request):
        self._check(request)
        if self.task['abort_event'].is_set():
            raise asyncio.CancelledError
        return await self.policy.reserve(request)

    async def settle(self, result):
        # Settlement remains required even after the task becomes terminal.
        if (result.request.principal.require_owner(context='native context settlement') != self.owner
                or result.request.task_id != self.task_id):
            raise ValueError('native context settlement identity differs')
        settled = await self.policy.settle(result)
        if settled is not None:
            return settled  # Preserve the host's existing strict return validation.
        if (result.evidence.get('kind') == 'returned'
                and result.evidence.get('provider_state') == 'provider_finished'
                and not self.task['abort_event'].is_set()
                and not (result.cancelled is not None and result.cancelled.is_set())):
            from lib.tasks_pkg.orchestrator._deferred_inbox_flush import confirm_consumed_project_context, flush_deferred_peer_and_steer
            def confirm():
                if (not self.task['abort_event'].is_set()
                        and not (result.cancelled is not None and result.cancelled.is_set())):
                    confirm_consumed_project_context(self.task, tid=self.task_id[:8])
                    flush_deferred_peer_and_steer(self.task,
                        round_num=result.request.round, tid=self.task_id[:8])
            await asyncio.get_running_loop().run_in_executor(self.executor, confirm)
        return None
