"""Native counted requests use the existing application tree account exactly once.

Entry: NativeModelAccountPolicy. The caller supplies an already-owned account,
principal/task and joined executor. Pricing/transcript publication remains with
its current owner; no tokenizer, account mirror, database or pool is created.
"""
import asyncio
from contextlib import contextmanager
import json
from concurrent.futures import Executor
from lib.agent_core.execution_budget import ExecutionBudget, ModelBudgetExceeded
from lib.agent_core.native_process import _joined


class NativeModelAccountPolicy:
    def __init__(self, *, principal, task: dict, executor: Executor, prepare_headers=None, prepare_pricing=None, pricing_resolver=None):
        self.owner = principal.require_owner(context='native model account')
        self.budget = task.get('_executionBudget')
        if (not isinstance(executor,Executor) or not isinstance(self.budget,ExecutionBudget)
                or not self.budget.authorizes_model_owner(task,self.owner)):
            raise ValueError('native model account requires the owned existing tree account')
        self.task, self.task_id, self.executor = task, task['id'], executor
        if prepare_headers is not None and not callable(prepare_headers):
            raise TypeError('native credential preparation must be callable')
        self.prepare_headers = prepare_headers
        if prepare_pricing is not None and not callable(prepare_pricing):
            raise TypeError('native price preparation must be callable')
        if pricing_resolver is not None and (not callable(pricing_resolver) or prepare_pricing is not None):
            raise TypeError('native pricing has one explicit resolver owner')
        self.pricing_resolver=pricing_resolver
        self.prepare_pricing=prepare_pricing
        self.pricing_result=None
        self.request = None
        self.lease = None
        self.settlement = None
        self.settled = False
        self.dispatch_entered = False

    @contextmanager
    def bind_pricing(self, *, principal, task_id, metadata):
        if (self.pricing_resolver is None or self.prepare_pricing is not None
                or self.request is not None or self.lease is not None
                or principal.require_owner(context='native price binding')!=self.owner
                or task_id!=self.task_id or metadata.get('owner_user_id')!=self.owner
                or (metadata.get('tenant_id') or '')!=(principal.tenant_id or '')
                or not self.budget.authorizes_model_owner(self.task,self.owner)):
            raise ValueError('native price binding differs')
        validate=getattr(self.pricing_resolver,'validate_binding',None)
        if callable(validate): validate(principal,metadata)
        encoded=json.dumps(metadata,allow_nan=False,ensure_ascii=False).encode()
        resolver=self.pricing_resolver
        def prepare(result):
            self._check(result.request)
            if (result.request.principal.tenant_id or '')!=(principal.tenant_id or ''):
                raise ValueError('native price tenant changed')
            return resolver(result,json.loads(encoded))
        self.prepare_pricing=prepare
        try:
            yield
        finally:
            if self.prepare_pricing is prepare:
                self.prepare_pricing=None
            self.pricing_result=None

    @contextmanager
    def bind_credentials(self, *, principal, task_id, metadata, subscription,
                         secret_reference, endpoint, extra_headers, resolver):
        """Bind one selected owner/destination port, never a global token store."""
        if (self.prepare_headers is not None or self.request is not None or self.lease is not None
                or principal.require_owner(context='native credential binding') != self.owner
                or task_id != self.task_id or metadata.get('owner_user_id') != self.owner
                or metadata.get('tenant_id', '') != (principal.tenant_id or '')
                or metadata.get('credential_kind') != 'oauth' or metadata.get('protocol') != 'openai_responses'
                or metadata.get('responses_profile') != 'codex' or subscription != 'codex'
                or not isinstance(secret_reference, str) or not secret_reference
                or not callable(resolver) or not self.budget.authorizes_model_owner(self.task,self.owner)):
            raise ValueError('native credential binding differs')
        from lib.model_routing.native_route import NativeCredentialRequest, route_binding_sha256
        from lib.llm.responses_outbound import responses_cache_affinity_key
        cache_key = responses_cache_affinity_key({'_conv_id':self.task.get('convId'), '_task_id':self.task_id})
        headers_json = json.dumps(extra_headers,ensure_ascii=False,allow_nan=False).encode()
        credential_id, wire_model = metadata['credential_id'], metadata['wire_model']
        identifiers = {key:metadata[key] for key in ('provider_id','offering_id','connection_id','deployment_id')}
        route_binding = route_binding_sha256(metadata)
        def prepare(request):
            self._check(request)
            if (request.principal.tenant_id or '') != (principal.tenant_id or ''):
                raise ValueError('native credential tenant changed')
            headers = resolver(NativeCredentialRequest(principal=request.principal,
                task_id=self.task_id, attempt_ordinal=request.attempt_ordinal,
                credential_id=credential_id, secret_reference=secret_reference, **identifiers,
                subscription=subscription, endpoint=endpoint, wire_model=wire_model,
                cache_affinity_key=cache_key, extra_headers_json=headers_json,
                cancelled=request.cancelled,route_binding_sha256=route_binding))
            if not isinstance(headers, dict):
                raise TypeError('native OAuth headers must be an object')
            bearer = next((value for name,value in headers.items() if isinstance(name,str) and name.lower() == 'authorization'), None)
            if not isinstance(bearer,str) or not bearer.startswith('Bearer ') or not bearer[7:].strip():
                raise ValueError('native OAuth resolver returned incomplete authorization')
            return headers
        self.prepare_headers = prepare
        try:
            yield cache_key
        finally:
            if self.prepare_headers is prepare:
                self.prepare_headers = None

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
        self.pricing_result=None
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
            if self.task.get('_nativePricingRequired') is True:
                from lib.billing.native_admission import protect_before_dispatch
                protect_before_dispatch(self.task, budget=self.budget, owner=self.owner)
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
            return self.pricing_result
        def settle():
            self._check(request)
            usage=result.usage
            if self.lease is not None:
                self.lease.settle(usage,unsent=result.unsent or not self.dispatch_entered)
                self.lease = None
            self.settled = True
            eligible=self.prepare_pricing is not None and not result.unsent and self.dispatch_entered
            if eligible:
                from lib.cost import observed_usage_totals
                eligible=observed_usage_totals(usage)[1]
            if eligible:
                from lib.agent_core.native_model_control import ModelPricing,NativePricingError
                try:
                    candidate=self.prepare_pricing(result)
                    self._check(request)
                    if candidate is not None and not isinstance(candidate,ModelPricing):
                        raise TypeError('native price preparation requires a resolved snapshot')
                    self.pricing_result=candidate
                except Exception as error:
                    raise NativePricingError('native price preparation failed') from error
            return self.pricing_result
        return await self._call(request,settle)

    async def capture_checkpoint(self, request, counter_policy):
        self._check(request)
        if self.lease is not None:
            raise ValueError('native checkpoint preceded model settlement')
        def capture():
            self._check(request)
            if self.task['id'] != self.budget.root_id:
                return self.budget.capture_actor_account(
                    owner=self.owner,task=self.task,counter_policy=counter_policy)
            return self.budget.capture_shared_account(
                owner=self.owner,task=self.task,counter_policy=counter_policy)
        return await self._call(request,capture)

    async def restore_checkpoint(self, request, state, counter_policy):
        self._check(request)
        if self.lease is not None or self.request is not None:
            raise ValueError('native shared recovery must precede model admission')
        def restore():
            self._check(request)
            if 'actor_task_id' in state:
                # The whole-set sink already moved this prefix once. Observe
                # that exact acknowledged account; never restore/add it again.
                checkpoint = request.state
                if isinstance(checkpoint, dict):
                    from lib.tasks_pkg.native_file_evidence import verify_recovered_actor_checkpoint
                    if checkpoint.get('shared_model_account') != state:
                        raise ValueError('Pending actor checkpoint account changed during restore')
                    verify_recovered_actor_checkpoint(self.budget,checkpoint,owner=self.owner,
                        task=self.task,counter_policy=counter_policy)
                else:
                    self.budget.verify_recovered_actor_account(
                        state,owner=self.owner,task=self.task,counter_policy=counter_policy)
            else:
                self.budget.restore_shared_account(
                    state,owner=self.owner,task=self.task,counter_policy=counter_policy)
        await self._call(request,restore)
