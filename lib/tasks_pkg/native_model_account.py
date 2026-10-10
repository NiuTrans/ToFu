"""Native counted requests use the existing application tree account exactly once.

Entry: NativeModelAccountPolicy. The caller supplies an already-owned account,
principal/task and joined executor. Pricing/transcript publication remains with
its current owner; no tokenizer, account mirror, database or pool is created.
"""
import asyncio
from concurrent.futures import Executor
from lib.agent_core.execution_budget import ExecutionBudget, ModelBudgetExceeded
from lib.agent_core.native_process import _joined


class NativeModelAccountPolicy:
    def __init__(self, *, principal, task: dict, executor: Executor, prepare_headers=None):
        self.owner = principal.require_owner(context='native model account')
        self.budget = task.get('_executionBudget')
        if (not isinstance(executor,Executor) or not isinstance(self.budget,ExecutionBudget)
                or not self.budget.authorizes_model_owner(task,self.owner)):
            raise ValueError('native model account requires the owned existing tree account')
        self.task, self.task_id, self.executor = task, task['id'], executor
        if prepare_headers is not None and not callable(prepare_headers):
            raise TypeError('native credential preparation must be callable')
        self.prepare_headers = prepare_headers
        self.request = None
        self.lease = None
        self.settlement = None
        self.settled = False
        self.dispatch_entered = False

    def _check(self, request):
        if (request.principal.require_owner(context='native model account callback') != self.owner
                or request.task_id != self.task_id or self.task.get('id') != self.task_id
                or self.task.get('_executionBudget') is not self.budget
                or not self.budget.authorizes_model_owner(self.task,self.owner)):
            raise ValueError('native model account ownership changed')

    async def _call(self, request, function):
        work = asyncio.ensure_future(asyncio.get_running_loop().run_in_executor(self.executor,function))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            request.cancelled.set()
            await _joined(work)
            raise

    async def reserve(self, request):
        self._check(request)
        if self.lease is not None:
            raise ValueError('native model account already reserved')
        self.request = request
        self.settlement, self.settled = None, False
        self.dispatch_entered = False
        def reserve():
            self._check(request)
            self.lease = self.budget.reserve_model(self.task,input_tokens=request.input_tokens,
                max_output_tokens=request.max_output_tokens,minimum_output_tokens=request.minimum_output_tokens,
                cancelled=lambda: request.cancelled.is_set() or bool(self.task.get('aborted')) or self.task['abort_event'].is_set())
            return self.lease.max_output_tokens
        try:
            return await self._call(request,reserve)
        except ModelBudgetExceeded as error:
            if error.reason != 'execution_owner_cancelled':
                self.task['_executionLimitReason'] = error.reason
            raise

    async def dispatch(self, request):
        self._check(request)
        if self.request is not request or self.lease is None:
            raise ValueError('native model account reservation missing')
        def dispatch():
            self._check(request)
            if request.cancelled.is_set() or self.task['abort_event'].is_set():
                raise asyncio.CancelledError
            prepared = None
            if self.prepare_headers is not None:
                from lib.agent_core.native_model_control import ModelDispatch
                prepared = ModelDispatch.headers(self.prepare_headers(request))
                self._check(request)
                if request.cancelled.is_set() or self.task['abort_event'].is_set():
                    raise asyncio.CancelledError
            self.dispatch_entered = True
            self.lease.mark_dispatched()
            return prepared
        return await self._call(request,dispatch)

    async def settle(self, result):
        request = result.request
        self._check(request)
        if self.request is not request:
            raise ValueError('native model account settlement identity changed')
        candidate = (result.usage_json,result.unsent)
        if self.settlement is not None and self.settlement != candidate:
            raise ValueError('native model account settlement candidate changed')
        self.settlement = candidate
        if self.settled:
            return
        def settle():
            self._check(request)
            if self.lease is not None:
                self.lease.settle(result.usage,unsent=result.unsent or not self.dispatch_entered)
                self.lease = None
            self.settled = True
        await self._call(request,settle)

    async def capture_checkpoint(self, request, counter_policy):
        self._check(request)
        if self.lease is not None:
            raise ValueError('native checkpoint preceded model settlement')
        def capture():
            self._check(request)
            return self.budget.capture_shared_account(
                owner=self.owner,task=self.task,counter_policy=counter_policy)
        return await self._call(request,capture)

    async def restore_checkpoint(self, request, state, counter_policy):
        self._check(request)
        if self.lease is not None or self.request is not None:
            raise ValueError('native shared recovery must precede model admission')
        def restore():
            self._check(request)
            self.budget.restore_shared_account(
                state,owner=self.owner,task=self.task,counter_policy=counter_policy)
        await self._call(request,restore)
