"""Project owned native fee evidence into existing task persistence fields.

Entry NativeTaskCostPolicy: wraps the existing round policy, freezes current
settlement identity for idempotent joined retries, and writes only apiRounds/cost.
The ordinary terminal writer persists them. No wallet, price lookup, secondary
ledger/store, model replay, worker or background publication is introduced.
"""
import asyncio
import hashlib
import json
from copy import deepcopy
from functools import lru_cache
from contextlib import nullcontext

from lib.billing.native_actor_fees import quote_from_api_round, validate_quote


@lru_cache(maxsize=1)
def _checkpoint_validator():
    """One immutable schema per process; every instance is still validated."""
    from jsonschema import Draft202012Validator
    from lib.agent_core.native_content_contract import FEE_CHECKPOINT_SCHEMA
    Draft202012Validator.check_schema(FEE_CHECKPOINT_SCHEMA)
    return Draft202012Validator(FEE_CHECKPOINT_SCHEMA)


class NativeTaskCostPolicy:
    accepts_native_cost=True

    @property
    def prepare(self):
        """Preserve the supplied context owner's optional preparation port."""
        return getattr(self.policy, 'prepare', None)

    def __init__(self, *, principal, task, policy):
        self.owner=principal.require_owner(context='native cost projection')
        if (type(task.get('_userId')) is not int or task['_userId']!=self.owner
                or not isinstance(task.get('id'),str) or not task['id']
                or not all(callable(getattr(policy,name,None)) for name in ('reserve','settle'))):
            raise ValueError('native cost projection requires an owned task/round policy')
        self.principal,self.task,self.task_id,self.policy=principal,task,task['id'],policy
        task['_nativePricingRequired'] = True
        self._bound=None;self._latest=None;self._lock=asyncio.Lock()

    def bind_pricing_route(self,metadata):
        if self._bound is not None or metadata.get('owner_user_id')!=self.owner or (metadata.get('tenant_id') or '')!=(self.principal.tenant_id or ''):
            raise ValueError('native cost route binding differs')
        self._bound=(metadata['logical_model'],metadata['provider_id'])

    def _check(self,request):
        if (self._bound is None or request.principal.require_owner(context='native cost settlement')!=self.owner
                or (request.principal.tenant_id or '')!=(self.principal.tenant_id or '')
                or request.task_id!=self.task_id or self.task.get('id')!=self.task_id
                or type(self.task.get('_userId')) is not int or self.task['_userId']!=self.owner):
            raise ValueError('native cost settlement ownership differs')

    async def reserve(self,request):
        self._check(request)
        if self._latest is not None and not self._latest[2]:
            raise ValueError('native cost delivery debt precedes model admission')
        return await self.policy.reserve(request)

    def _row(self,number,evidence,*,bound_route=None):
        if evidence.get('kind')!='returned' or not isinstance(evidence.get('usage'),dict):
            raise ValueError('native fee requires returned usage evidence')
        price,amounts,credits=validate_quote(evidence['cost'])
        if evidence.get('usage_observation',{}).get('complete') is not True:
            raise ValueError('native fee projection requires known usage')
        if amounts is not None:
            model,provider=bound_route or self._bound
            amounts=dict(amounts,pricingSource='native_resolved_price',pricingSnapshot={
                'kind':'nativeResolvedPrice','model':model,'provider':provider,'source':'native_resolved_price',
                'currency':price['currency'],'selectedPromptTokens':price['prompt_tokens'],
                'rates':{'input':price['input'],'output':price['output'],'cacheWriteMul':price['cache_write_multiplier'],'cacheReadMul':price['cache_read_multiplier']},
                'usdToCny':price['usd_to_cny']})
        return {'round':number+1,'taskId':self.task_id,'model':(bound_route or self._bound)[0],'provider_id':(bound_route or self._bound)[1],
                'usage':evidence['usage'],'cost':amounts,'nativeCredits':credits,'nativePrice':price}

    def _checkpoint_rows(self, request, state, projection):
        self._check(request)
        _checkpoint_validator().validate(projection)
        if (type(projection['owner_user_id']) is not int or projection['owner_user_id'] != self.owner
                or projection['task_id'] != self.task_id
                or projection['tenant_id'] != (self.principal.tenant_id or '')
                or (projection['logical_model'], projection['provider_id']) != self._bound
                or projection['wallet_user_id'] != (self.task.get('_billingUserId') or '')
                or type(projection['reserved_micro']) is not int
                or projection['reserved_micro'] != self.task.get('_billing_reservation_micro', 0)
                or type(state.get('owner_user_id')) is not int or state['owner_user_id'] != self.owner
                or state.get('task_id') != self.task_id or type(state.get('next_round')) is not int
                or state['next_round'] != len(projection['rounds'])):
            raise ValueError('native fee checkpoint binding differs')
        if 'wallet_settlement' in projection:
            from lib.billing.native_settlement import decode
            capsule = projection['wallet_settlement']
            decode(capsule, task_id=self.task_id, owner_user_id=self.owner,
                wallet_user_id=projection['wallet_user_id'])
            if capsule['reserved_micro'] != projection['reserved_micro']:
                raise ValueError('native checkpoint invoice reservation differs')
        account = state.get('shared_model_account')
        if (not isinstance(account, dict) or type(account.get('owner_user_id')) is not int
                or account['owner_user_id'] != self.owner
                or account.get('actor_task_id', account.get('root_task_id')) != self.task_id
                or account.get('complete') is not True or type(account.get('tokens')) is not int):
            raise ValueError('native fee checkpoint requires complete shared account')
        if 'actor_task_id' in account and (
                account['root_task_id'] == self.task_id or projection['wallet_user_id']
                or getattr(self.task.get('_executionBudget'), 'root_id', None) != account['root_task_id']
                or projection['reserved_micro'] or projection.get('child_rounds')
                or 'wallet_settlement' in projection or 'root_tokens' in account):
            raise ValueError('actor checkpoint cannot claim root wallet or tree fees')
        actor = 'actor_task_id' in account
        native_usage = state.get('usage')
        if not isinstance(native_usage, list) or len(native_usage) != len(projection['rounds']):
            raise ValueError('native fee checkpoint usage frontier differs')
        from lib.cost import accumulate_usage_into, observed_usage_totals, compute_api_rounds_cost
        rows = []; root_tokens = 0; child_tokens = 0; usage = {}
        def restore_row(saved, observed, public_round):
            row = self._row(public_round - 1, dict(kind='returned', usage=observed,
                usage_observation={'complete': True}, cost=saved['cost']),
                bound_route=(saved['route']['logical_model'],saved['route']['provider_id']) if 'route' in saved else None)
            row['taskId'] = saved.get('task_id', projection['task_id'])
            actual, complete = observed_usage_totals(observed)
            amounts = saved['cost']['amounts']
            quoted = saved['cost']['price']['prompt_tokens'] + (amounts['outputTokens'] if amounts else 0)
            if complete is not True or actual != quoted:
                raise ValueError('native fee checkpoint quote does not cover usage')
            return row, quoted
        current_seen = False
        for number, saved in enumerate(projection['rounds']):
            if type(saved['round']) is not int or saved['round'] != number + 1:
                raise ValueError('native fee checkpoint round evidence differs')
            row, quoted = restore_row(saved, native_usage[number], saved.get('public_round', number + 1))
            if actor:
                producer, payer = row['taskId'], saved.get('billable_task_id')
                if (not isinstance(payer, str) or not payer or producer == account['root_task_id']
                        or payer in {producer, self.task_id} or 'public_round' in saved):
                    raise ValueError('native actor fee payer or producer differs')
                if producer == self.task_id:
                    current_seen = True
                    if payer != account['root_task_id']:
                        raise ValueError('native fresh actor fee payer differs')
                elif current_seen or payer == account['root_task_id']:
                    raise ValueError('native historical actor fee payer differs')
                else:
                    row['billableTaskId'] = payer
            elif 'billable_task_id' in saved:
                raise ValueError('root fee rounds cannot claim actor payer identities')
            rows.append(row); root_tokens += quoted
            accumulate_usage_into(usage, native_usage[number])
        native_origins = {row['taskId'] for row in rows} | {self.task_id}
        child_identities = set()
        for saved in projection.get('child_rounds', []):
            identity = (saved['task_id'], saved['source_round'])
            if saved['task_id'] in native_origins or identity in child_identities:
                raise ValueError('native checkpoint child executor duplicated or confused with root')
            child_identities.add(identity)
            row, quoted = restore_row(saved, saved['usage'], saved['round'])
            row.update(sourceRound=saved['source_round'], billableTaskId=saved['billable_task_id'])
            if row['billableTaskId'] != self.task_id:
                prefix = self.task.get('_checkpointApiRounds') or []
                position = row['round'] - 1
                if position >= len(prefix) or row != prefix[position]:
                    raise ValueError('native checkpoint child fee receiver differs from accepted prefix')
            rows.append(row); child_tokens += quoted
        rows.sort(key=lambda row: row['round'])
        if [row['round'] for row in rows] != list(range(1, len(rows) + 1)):
            raise ValueError('native checkpoint public fee frontier has gaps or duplicates')
        if (root_tokens + child_tokens != account['tokens']
                or root_tokens != account.get('root_tokens', account['tokens'])):
            raise ValueError('native fee checkpoint does not cover shared consumption')
        return rows, usage, compute_api_rounds_cost(rows, use_frozen_costs=True)

    def capture_fee_checkpoint(self, request, state):
        self._check(request)
        if self._latest is None or self._latest[2] is not True:
            raise ValueError('native fee checkpoint preceded accepted cost delivery')
        projection = dict(format='tofu.native-fee-checkpoint/v1', owner_user_id=self.owner,
            tenant_id=self.principal.tenant_id or '', task_id=self.task_id,
            logical_model=self._bound[0], provider_id=self._bound[1],
            wallet_user_id=self.task.get('_billingUserId') or '',
            reserved_micro=self.task.get('_billing_reservation_micro', 0), rounds=[])
        ledger = self.task.get('apiRounds') or []
        account = state.get('shared_model_account') or {}
        actor = 'actor_task_id' in account
        # Transferred descendants have sourceRound; a recovered actor's own
        # inherited prefix only has its original billableTaskId.
        has_children = any('sourceRound' in row for row in ledger)
        for row in ledger:
            if 'sourceRound' not in row and (row.get('model'), row.get('provider_id')) != self._bound:
                raise ValueError('native fee checkpoint round route differs')
            quote = quote_from_api_round(row)
            if 'sourceRound' in row:
                projection.setdefault('child_rounds', []).append(dict(round=row['round'],
                    task_id=row['taskId'], source_round=row['sourceRound'],
                    billable_task_id=row['billableTaskId'], usage=row['usage'], cost=quote))
                if (row['model'],row['provider_id'])!=self._bound:
                    projection['child_rounds'][-1]['route']=dict(logical_model=row['model'],provider_id=row['provider_id'])
            else:
                number = len(projection['rounds'])
                if (number >= len(state.get('usage') or []) or json.dumps(row['usage'], sort_keys=True, allow_nan=False)
                        != json.dumps(state['usage'][number], sort_keys=True, allow_nan=False)):
                    raise ValueError('native fee capture usage differs from native frontier')
                saved = dict(round=number + 1, task_id=row.get('taskId', self.task_id), cost=quote)
                if actor:
                    saved['billable_task_id'] = row.get('billableTaskId', account['root_task_id'])
                elif 'billableTaskId' in row:
                    raise ValueError('native root fee round has an unwitnessed actor payer')
                if has_children:
                    saved['public_round'] = row['round']
                projection['rounds'].append(saved)
        # Live transfers must still be witnessed by the original actor account.
        from lib.billing.cost import native_task_cost
        if projection['wallet_user_id']:
            from lib.billing.native_settlement import project
            projection['wallet_settlement'] = project(self.task)
        elif has_children:
            native_task_cost(self.task, model=self._bound[0])
        restored, _, _ = self._checkpoint_rows(request, state, projection)
        if actor:
            prefix = [row for row in restored if row['taskId'] != self.task_id]
            if prefix != (self.task.get('_checkpointApiRounds') or []):
                raise ValueError('native actor fee capture changed its accepted prefix')
            if prefix:
                budget = self.task.get('_executionBudget')
                inherited = budget.recovered_actor_prefix_tokens(self.task, owner=self.owner)
                quoted = sum(row['nativePrice']['prompt_tokens'] +
                    (row['cost']['outputTokens'] if row['cost'] else 0) for row in prefix)
                if inherited is None or inherited != quoted:
                    raise ValueError('native actor fee prefix has no acknowledged account rebind')
        return projection

    def prepare_fee_checkpoint_restore(self,request,state):
        if self._latest is not None:
            raise ValueError('native fee restore requires a pristine task projection')
        projection=state.get('host_fee_projection')
        rows,usage,cost=self._checkpoint_rows(request,state,projection)
        def encoded(value):
            return json.dumps(value,sort_keys=True,allow_nan=False,separators=(',',':'))
        fields=('apiRounds','usage','cost','_checkpointApiRounds','_checkpointUsage')
        def current_projection():
            return encoded({key:self.task.get(key) for key in fields})
        original=current_projection()
        if any(self.task.get(key) for key in ('apiRounds','usage','cost')):
            # Ordinary Continue already hydrates the accepted Turn prefix.
            # Its paid rows and usage must match the journal exactly; neither
            # a display estimate nor arbitrary occupied fields grant recovery.
            if (any(encoded(self.task.get(key))!=encoded(rows)
                    for key in ('apiRounds','_checkpointApiRounds'))
                    or any(encoded(self.task.get(key))!=encoded(usage)
                    for key in ('usage','_checkpointUsage'))
                    or (self.task.get('cost') is not None
                        and encoded(self.task['cost'])!=encoded(cost))):
                raise ValueError('native fee restore differs from hydrated checkpoint projection')
        from lib.agent_core.execution_budget import ExecutionBudget
        budget=self.task.get('_executionBudget')
        if not isinstance(budget,ExecutionBudget) or not budget.authorizes_model_owner(self.task,self.owner):
            raise ValueError('native fee restore requires the owned shared account')
        account=state['shared_model_account'];next_round=state['next_round']
        billing_prefix = self.task.get('_checkpointApiRounds') or []
        actor = 'actor_task_id' in account
        if actor:
            billing_prefix = [deepcopy(row) for row in rows if row['taskId'] != self.task_id]
        elif billing_prefix:
            # Same-executor recovery still owes its own uncommitted work.
            # Only an older executor's contiguous prefix remains paid elsewhere.
            foreign_prefix = []
            current_seen = False
            for row in billing_prefix:
                receiver = row.get('billableTaskId') or row.get('taskId', self.task_id)
                if receiver == self.task_id:
                    current_seen = True
                elif current_seen:
                    raise ValueError('native retained invoice interleaves foreign billing after current work')
                else:
                    foreign_prefix.append(row)
            billing_prefix = foreign_prefix
        def commit():
            self._check(request)
            if (self._latest is not None or current_projection()!=original
                    or self.task.get('_executionBudget') is not budget):
                raise ValueError('native fee restore preceded account restore or task changed')
            if actor and billing_prefix:
                from lib.tasks_pkg.native_file_evidence import verify_recovered_actor_checkpoint
                verify_recovered_actor_checkpoint(budget,state,owner=self.owner,
                    task=self.task,counter_policy=account['counter_policy'])
            else:
                capture_account = budget.capture_actor_account if actor else budget.capture_shared_account
                if capture_account(owner=self.owner,task=self.task,counter_policy=account['counter_policy'])!=account:
                    raise ValueError('native fee restore preceded account restore or task changed')
            if actor and billing_prefix:
                inherited = budget.recovered_actor_prefix_tokens(self.task, owner=self.owner)
                quoted = sum(row['nativePrice']['prompt_tokens'] +
                    (row['cost']['outputTokens'] if row['cost'] else 0) for row in billing_prefix)
                if inherited is None or inherited != quoted:
                    raise ValueError('native actor fee prefix has no acknowledged account rebind')
            grants = self.task.get('_nativeChildFeeGrants', ())
            child_rows = [row for row in rows if 'sourceRound' in row and row.get('billableTaskId') == self.task_id]
            if child_rows:
                from lib.billing.cost import NativeRecoveredChildFeeGrant
                grants = (*grants, NativeRecoveredChildFeeGrant(self.task, child_rows, account))
            from lib.billing.cost import native_task_cost
            candidate = dict(self.task, apiRounds=rows, usage=usage, cost=cost,
                _nativeFrozenCostProjection=True, _nativeChildFeeGrants=grants,
                _checkpointApiRounds=billing_prefix)
            if 'wallet_settlement' in projection:
                from lib.billing.native_settlement import project
                if project(candidate) != projection['wallet_settlement']:
                    raise ValueError('native checkpoint invoice differs from original wallet evidence')
            else:
                # An actor proves its local quote coverage above; siblings may
                # still own other shared-account consumption. Never treat this
                # local read as a root invoice or tree-quiescence witness.
                if actor:
                    candidate = dict(candidate, _nativePricingRequired=False, _executionBudget=None)
                native_task_cost(candidate, model=self._bound[0])
            self.task.update(apiRounds=rows,usage=usage,cost=cost,_nativeFrozenCostProjection=True,
                _nativeChildFeeGrants=grants, _checkpointApiRounds=billing_prefix)
            self._latest=(next_round-1,b'',True)
        return commit

    async def settle(self,result):
        self._check(result.request)
        if 'cost' not in result.evidence:
            return await self.policy.settle(result)
        raw=json.dumps(result.evidence,allow_nan=False,sort_keys=True,separators=(',',':')).encode()
        digest=hashlib.sha256(raw).digest();number=result.request.round
        async with self._lock:
            if self._latest is not None:
                old,identity,accepted=self._latest
                if number<old or (number==old and identity!=digest):
                    raise ValueError('native priced settlement candidate changed')
                if number==old and accepted:return None
                if number>old and not accepted:raise ValueError('native cost delivery debt precedes next round')
            if self._latest is None or number>self._latest[0]:
                evidence=json.loads(raw)
                with self.task.get('content_lock') or nullcontext():
                    rounds=self.task.get('apiRounds',[])
                    if not isinstance(rounds,list):raise ValueError('native task round ledger invalid')
                    # A public Continue creates a new native execution ordinal,
                    # while the accepted paid prefix belongs to the same Turn.
                    # Keep settlement identity local; number its appended public
                    # row after that prefix, including native journal restores.
                    row=self._row(max(number,len(rounds)),evidence)
                    from lib.cost import compute_api_rounds_cost
                    updated_rounds=[*rounds,row]
                    aggregate=compute_api_rounds_cost(updated_rounds,use_frozen_costs=True)
                    from lib.cost import accumulate_usage_into
                    usage=dict(self.task.get('usage') or {})
                    accumulate_usage_into(usage,evidence['usage'])
                    # Accepted Turn projections may borrow the previous prefix.
                    # Reuse the candidate list already built for the fold instead
                    # of mutating that durable patch baseline through an alias.
                    self.task['apiRounds']=updated_rounds
                    self.task['usage']=usage
                    self.task['cost']=aggregate
                    self.task['_nativeFrozenCostProjection']=True
                    self._latest=(number,digest,False)
            answer=await self.policy.settle(result)
            if answer is not None:raise TypeError('native round cost consumer must return None')
            self._latest=(number,digest,True)
        return None
