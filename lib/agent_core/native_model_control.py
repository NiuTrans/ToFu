"""Counted native model callbacks over the existing owned stdio process.

Entry: ModelRequest, ModelSettlement, ModelPolicy; internal ModelControl joins
callbacks and cleanup. No counter, account mirror, backend, routing or retry.
"""
from dataclasses import dataclass, field, replace
from collections.abc import Mapping as Mapping
from typing import Protocol
import asyncio
import threading
import time
import json

from lib.identity import PrincipalContext
from lib.agent_core.native_process import NativeProcessError, _joined


@dataclass(frozen=True, slots=True)
class ModelRequest:
    principal: PrincipalContext
    task_id: str
    attempt_ordinal: int
    input_tokens: int
    max_output_tokens: int
    minimum_output_tokens: int
    cancelled: threading.Event


@dataclass(frozen=True, slots=True)
class ModelSettlement:
    request: ModelRequest
    usage_json: bytes | None
    unsent: bool

    @property
    def usage(self):
        return None if self.usage_json is None else json.loads(self.usage_json)


@dataclass(frozen=True, slots=True)
class ModelDispatch:
    """One complete host-prepared header set; repr never reveals credentials."""
    headers_json: bytes = field(repr=False)

    @staticmethod
    def _validate(values):
        from lib.agent_core.native_content_contract import MODEL_DISPATCH_FORBIDDEN_HEADERS
        if not isinstance(values, dict):
            raise TypeError('native dispatch headers must be an object')
        names = set()
        for name, value in values.items():
            if (not isinstance(name, str) or not name or name.lower() in names
                    or any(ch not in "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" for ch in name)
                    or name.lower() in MODEL_DISPATCH_FORBIDDEN_HEADERS
                    or not isinstance(value, str) or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
                raise ValueError('native dispatch headers invalid')
            names.add(name.lower())
    @classmethod
    def headers(cls, values):
        cls._validate(values)
        return cls(json.dumps(values,ensure_ascii=False,allow_nan=False).encode())

    def values(self):
        # Even manually constructed carriers cross the same closed validation.
        values = json.loads(self.headers_json)
        self._validate(values)
        return values


class NativePricingError(ValueError):
    """Resolved pricing failed after token settlement; caller cause is preserved."""


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """One bounded resolved numeric snapshot, never a wallet authorization."""
    snapshot_json: bytes

    @staticmethod
    def _validate(values):
        import math
        from lib.agent_core.native_content_contract import MODEL_PRICE_SCHEMA
        if not isinstance(values,dict) or set(values)!=set(MODEL_PRICE_SCHEMA['required']):
            raise ValueError('native resolved price invalid')
        for key,rule in MODEL_PRICE_SCHEMA['properties'].items():
            value=values[key]
            if 'enum' in rule:
                valid=value in rule['enum']
            elif rule['type']=='integer':
                valid=type(value) is int and rule['minimum']<=value<=rule['maximum']
            else:
                try: valid=type(value) in (int,float) and math.isfinite(value) and value>=rule.get('minimum',0) and value>rule.get('exclusiveMinimum',-1)
                except OverflowError: valid=False
            if not valid: raise ValueError('native resolved price invalid')

    @classmethod
    def resolved(cls,values):
        cls._validate(values)
        return cls(json.dumps(values,allow_nan=False,separators=(',',':')).encode())

    def values(self):
        if not isinstance(self.snapshot_json,bytes) or len(self.snapshot_json)>4096:
            raise ValueError('native resolved price exceeds bound')
        values=json.loads(self.snapshot_json)
        self._validate(values)
        return values


class ModelPolicy(Protocol):
    async def reserve(self, request: ModelRequest) -> int: ...
    async def dispatch(self, request: ModelRequest) -> ModelDispatch | None: ...
    async def settle(self, result: ModelSettlement) -> ModelPricing | None: ...


class ModelControl:
    def __init__(self, client, policy, task_id, *, pricing=False, unbounded_ack=False):
        if type(unbounded_ack) is not bool:
            raise TypeError('native model callback budget selection must be explicit')
        self.client, self.policy, self.task_id = client, policy, task_id
        self.pricing=pricing
        self.unbounded_ack=unbounded_ack
        self.pending = None
        self.dispatch_attempted = False
        self.next_ordinal = 1
        self.reserve_entered = False
        self.failure = None
        self.counter_policy = None
        self.settlement = None

    async def handle(self, value, process, *, closing=False):
        required = {'version','type','request_id','owner_user_id','task_id','attempt_ordinal','action',
                    'input_tokens','max_output_tokens','minimum_output_tokens','expires_monotonic_ns'}
        action = value.get('action')
        expires = value.get('expires_monotonic_ns')
        expected = required | ({'usage','unsent'} if action == 'settle' else set())
        if (set(value) != expected or value.get('version') != self.client.protocol_version
                or value.get('type') != 'model_request' or value.get('task_id') != self.task_id
                or type(value.get('owner_user_id')) is not int
                or value['owner_user_id'] != self.client.principal.require_owner(context='native model control')
                or not isinstance(value.get('request_id'),str) or not value['request_id']
                or any(type(value.get(key)) is not int or not 0 <= value[key] <= 18446744073709551615
                       for key in ('attempt_ordinal','input_tokens','max_output_tokens','minimum_output_tokens'))
                or any(value[key] <= 0 for key in ('attempt_ordinal','max_output_tokens','minimum_output_tokens'))
                or (expires is None and not self.unbounded_ack)
                or (expires is not None and (type(expires) is not int or not 0 < expires <= 18446744073709551615))
                or value['minimum_output_tokens'] > value['max_output_tokens']):
            raise NativeProcessError('native model control identity differs')
        if action == 'reserve':
            if self.pending is not None or value['attempt_ordinal'] != self.next_ordinal:
                raise NativeProcessError('native model reservation order differs')
            self.pending = ModelRequest(self.client.principal,self.task_id,value['attempt_ordinal'],
                value['input_tokens'],value['max_output_tokens'],value['minimum_output_tokens'],threading.Event())
            self.next_ordinal += 1
            self.dispatch_attempted = False
        request = self.pending
        if (request is None or (request.attempt_ordinal,request.input_tokens,request.max_output_tokens,request.minimum_output_tokens)
                != tuple(value[key] for key in ('attempt_ordinal','input_tokens','max_output_tokens','minimum_output_tokens'))
                or action not in {'reserve','dispatch','settle'}):
            raise NativeProcessError('native model lease changed')
        accepted, output = False, None
        headers = None
        price = None
        if closing:
            request.cancelled.set()
        try:
            if action == 'reserve':
                if closing or self.failure is not None:
                    raise NativeProcessError('native model reservation closing')
                self.reserve_entered = True
                call = self.policy.reserve(request)
            elif action == 'dispatch':
                if closing or self.failure is not None or self.dispatch_attempted:
                    raise NativeProcessError('native model dispatch refused')
                self.dispatch_attempted = True
                call = self.policy.dispatch(request)
            else:
                if type(value['unsent']) is not bool or (value['usage'] is not None and not isinstance(value['usage'],dict)):
                    raise NativeProcessError('native model settlement differs')
                if self.dispatch_attempted and value['unsent']:
                    raise NativeProcessError('native sent model cannot be voided')
                self.settlement = ModelSettlement(request,None if value['usage'] is None else json.dumps(value['usage'],allow_nan=False).encode(),value['unsent'] or not self.dispatch_attempted)
                call = self.policy.settle(self.settlement)
            work = asyncio.create_task(call)
            try:
                at = (None if expires is None else asyncio.get_running_loop().time()
                      + max(0,expires-time.monotonic_ns()) / 1e9)
                async with asyncio.timeout_at(at):
                    result = await asyncio.shield(work)
            except (TimeoutError, asyncio.CancelledError) as primary:
                request.cancelled.set()
                try:
                    await _joined(work)
                except (Exception, asyncio.CancelledError) as callback_error:
                    primary.add_note('joined native model callback failed: ' + type(callback_error).__name__)
                raise
            if action == 'reserve':
                if type(result) is not int or not request.minimum_output_tokens <= result <= request.max_output_tokens:
                    raise TypeError('native model output admission differs')
                output = result
            elif action == 'dispatch' and isinstance(result, ModelDispatch):
                headers = result.values()
            elif action == 'settle' and isinstance(result,ModelPricing):
                if not self.pricing or self.settlement.unsent:
                    raise TypeError('native pricing requires explicit sent settlement')
                try: price=result.values()
                except (ValueError,TypeError,OverflowError) as error:
                    raise NativePricingError('native resolved price validation failed') from error
            elif result is not None:
                raise TypeError('native model callback result differs')
            if action == 'settle':
                self.pending = None
                self.settlement = None
            accepted = not (action != 'settle' and request.cancelled.is_set())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self.failure is None:
                self.failure = error
        if expires is None or time.monotonic_ns() < expires:
            reply = dict(version=self.client.protocol_version,type='model_decision',request_id=value['request_id'],accepted=accepted)
            if accepted and action == 'reserve':
                reply['output_tokens'] = output
            if accepted and action == 'dispatch' and headers is not None:
                reply['headers'] = headers
            if accepted and action == 'settle' and price is not None:
                reply['price'] = price
            if not accepted and self.failure is not None:
                from lib.agent_core.execution_budget import ModelBudgetExceeded
                if isinstance(self.failure,ModelBudgetExceeded):
                    reply['error'] = {'kind':self.failure.reason}
                elif isinstance(self.failure,NativePricingError):
                    reply['error'] = {'kind':'model_pricing'}
            async with asyncio.timeout(self.client.budget.shutdown_seconds):
                process.stdin.write(self.client._encode(reply,self.client.budget.frame_bytes))
                await process.stdin.drain()

    async def abandon(self):
        if self.pending is not None:
            request = self.pending
            request.cancelled.set()
            if self.reserve_entered:
                await self.policy.settle(self.settlement or ModelSettlement(request,None,not self.dispatch_attempted))
            self.pending = None

    async def capture_checkpoint(self, request):
        if self.pending is not None or self.failure is not None:
            raise NativeProcessError('shared checkpoint preceded model settlement')
        return await self.policy.capture_checkpoint(request,self.counter_policy)

    async def restore_checkpoint(self, request, state):
        if self.pending is not None or self.failure is not None or self.next_ordinal != 1:
            raise NativeProcessError('shared checkpoint load must precede model admission')
        if not isinstance(state,dict) or state.get('model_account') is not None or not isinstance(state.get('shared_model_account'),dict):
            raise NativeProcessError('shared checkpoint account missing or mixed')
        await self.policy.restore_checkpoint(replace(request,state=state),state['shared_model_account'],self.counter_policy)
