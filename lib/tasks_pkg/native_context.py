"""Existing application prompt admission over native-owned round history.

NativeContextPolicy binds the compiled route and actual output/tool projection,
runs the canonical prompt admission in the caller's executor, and delegates
reservation/settlement. It creates no loop, worker pool or history mirror.
"""
import asyncio
from contextvars import copy_context
from concurrent.futures import Executor
import json
import time

from lib.agent_core.native_process import RoundRequest, RoundPreparation
from lib.identity import PrincipalContext

from lib.log import get_logger

logger = get_logger(__name__)


class NativeContextPolicy:
    __slots__ = ('owner', 'project_path', 'project_enabled', 'task', 'task_id',
                 'executor', 'policy', '_bound', '_closed', '_projection', '_context_tools', '_wire_binding', '_wire_tools', '_token_count_host')

    def __init__(self, *, principal: PrincipalContext, task: dict,
                 executor: Executor, round_policy=None, project_path=None, project_enabled=False, token_count_host=None):
        if token_count_host is not None:
            from lib.tasks_pkg.native_host import NativeHost
            if not isinstance(token_count_host,NativeHost):
                raise TypeError('native context token host invalid')
        self._token_count_host=token_count_host
        self.owner = principal.require_owner(context='native context policy')
        if (type(task.get('_userId')) is not int or task['_userId'] != self.owner
                or not isinstance(task.get('id'), str) or not task['id']
                or not isinstance(executor, Executor)
                or not all(callable(getattr(task.get('abort_event'), name, None))
                           for name in ('set', 'is_set'))
                or (round_policy is not None and not all(callable(getattr(round_policy, name, None))
                           for name in ('reserve', 'settle')))
                or callable(getattr(round_policy, 'prepare', None))):
            raise ValueError('native context requires one owned preparation/accounting policy')
        if type(project_enabled) is not bool or (project_path is not None and not isinstance(project_path, str)):
            raise ValueError('native context project configuration invalid')
        self.project_path, self.project_enabled = project_path, project_enabled
        self.task, self.task_id, self.executor = task, task['id'], executor
        self.policy = round_policy
        self._bound = None
        self._closed = False
        self._projection = self._context_tools = self._wire_binding = self._wire_tools = None

    def bind_wire_projection(self, projection, context_tools):
        if self._closed or self._bound is not None or self._projection is not None or not callable(projection) or not isinstance(context_tools,list):
            raise ValueError('native context wire preparation differs')
        self._projection=projection
        self._context_tools=json.dumps(context_tools,ensure_ascii=False,allow_nan=False)

    @property
    def accepts_native_cost(self):
        return not self._closed and getattr(self.policy,'accepts_native_cost',False) is True

    @property
    def capture_fee_checkpoint(self):
        return getattr(self.policy,'capture_fee_checkpoint',None)

    @property
    def prepare_fee_checkpoint_restore(self):
        return getattr(self.policy,'prepare_fee_checkpoint_restore',None)

    def bind_pricing_route(self, metadata):
        bind=getattr(self.policy,'bind_pricing_route',None)
        if callable(bind): return bind(metadata)
        return None

    def bind_route(self, metadata, template, catalog):
        from lib.token_counter.native import native_token_scope
        with native_token_scope(self._token_count_host):
            return self._bind_route(metadata,template,catalog)

    def _bind_route(self, metadata, template, catalog):
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
        if self._context_tools is not None:
            tools=json.loads(self._context_tools)
        else:
            tools = template['tools'] if 'tools' in template else native_model_tools(catalog)
        if not isinstance(tools,list) or any((tool.get('function') or {}).get('name') not in catalog for tool in tools):
            raise ValueError('native context visible tools differ from execution authority')
        self._bound = (
            json.dumps([metadata, output], ensure_ascii=False, allow_nan=False),
            json.dumps(tools, ensure_ascii=False, allow_nan=False),
        )
        if self._projection is not None:
            from lib.tasks_pkg.native_tool_projection import NativeToolProjection
            if type(self._projection) is NativeToolProjection:
                # Preserve full JSON refusal before narrowing this built-in
                # observer's input. Rust and execution retain complete schemas;
                # custom callbacks/subclasses still receive the full catalog.
                json.dumps(catalog, ensure_ascii=False, allow_nan=False)
                catalog = self._projection.catalog_for_projection(catalog)
            self._wire_binding=json.dumps([metadata,template,catalog],ensure_ascii=False,allow_nan=False)

    def completion_binding(self):
        """Read the existing issued binding for final presentation, without history."""
        if (self._closed or self._bound is None or self.task.get('id') != self.task_id
                or type(self.task.get('_userId')) is not int or self.task['_userId'] != self.owner):
            raise ValueError('native context completion ownership changed')
        metadata, _output = json.loads(self._bound[0])
        tools = self._wire_tools if self._wire_tools is not None else self._bound[1]
        return metadata['logical_model'], json.loads(tools)

    def close(self):
        self._closed = True
        self._bound = None
        self._projection = self._context_tools = self._wire_binding = self._wire_tools = None

    def _check(self, request: RoundRequest):
        if (self._closed or request.principal.require_owner(context='native context round') != self.owner
                or request.task_id != self.task_id or self.task.get('id') != self.task_id
                or type(self.task.get('_userId')) is not int or self.task['_userId'] != self.owner
                or self.task.get('status') not in {'pending', 'running'}
                or self._bound is None):
            raise ValueError('native context round ownership changed')

    def _prepare(self, request):
        from lib.token_counter.native import native_token_scope
        with native_token_scope(self._token_count_host):
            return self._prepare_owned(request)

    def _prepare_owned(self, request):
        self._check(request)
        if self.task['abort_event'].is_set():
            raise asyncio.CancelledError
        if request.deadline_monotonic_ns is not None and time.monotonic_ns() >= request.deadline_monotonic_ns:
            self.task['abort_event'].set()
            raise TimeoutError('native context deadline expired before worker entry')
        if not isinstance(request.messages, list) or request.history_messages != len(request.messages):
            raise ValueError('native context history binding differs')
        metadata, output = json.loads(self._bound[0])
        tools = json.loads(self._bound[1])
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
        if self._projection is not None:
            metadata,template,catalog=json.loads(self._wire_binding)
            projected=self._projection(metadata,template,catalog,request.messages,request.round)
            if not isinstance(projected,list) or any(not isinstance(tool,dict) for tool in projected):
                raise ValueError('native context wire projection is not canonical')
            self._check(request)
            if self.task['abort_event'].is_set():raise asyncio.CancelledError
            self._wire_tools=json.dumps(projected,ensure_ascii=False,allow_nan=False)
            from lib.tasks_pkg.context_dependencies import observe_wire
            observe_wire(self.task, request.messages)
            return RoundPreparation(request.messages,projected)
        from lib.tasks_pkg.context_dependencies import observe_wire
        observe_wire(self.task, request.messages)
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
        worker = self.executor.submit(copy_context().run, self._prepare, request)
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
                except Exception as diagnostic_error:
                    logger.debug('prepare failure handled: %s', type(diagnostic_error).__name__)
                    break  # Joined failed preparation; parent cancellation wins.
            if future.done() and not future.cancelled():
                future.exception()  # Retrieve joined failure without hiding cancellation.
            raise

    async def reserve(self, request):
        self._check(request)
        if self.task['abort_event'].is_set():
            raise asyncio.CancelledError
        return True if self.policy is None else await self.policy.reserve(request)

    async def settle(self, result):
        # Settlement remains required even after the task becomes terminal.
        if (result.request.principal.require_owner(context='native context settlement') != self.owner
                or result.request.task_id != self.task_id):
            raise ValueError('native context settlement identity differs')
        settled = None if self.policy is None else await self.policy.settle(result)
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
            work = asyncio.get_running_loop().run_in_executor(self.executor, copy_context().run, confirm)
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                from lib.agent_core.native_process import _joined
                await _joined(work)
                raise
        return None
