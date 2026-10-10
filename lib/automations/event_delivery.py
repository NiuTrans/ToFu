"""Bounded indexed event fanout using the canonical worker's durable suspension.

Entry point: AutomationEventHandler. Each accepted run retains the frozen event;
only the queue-owned delivery cursor changes. Typed completion retires pure
transport after the worker heartbeat has joined. Application composition remains
explicit; importing this module creates no worker, timer, or model call.
"""
from copy import deepcopy

from lib.durable_worker import (WorkerJobOutcome,WorkerJobSuspension,WorkerKindRegistration,
    WorkerKindSafety,WorkerLeaseLost,WorkerProtocolError)
from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from .contract import LIMITS,STORAGE_NAMESPACES,storage_schema
from .errors import AutomationConflict
from .events import initial_delivery_cursor,validate_event_job
from .execution_repository import AutomationExecutionRepository
from .publication import subscription_key
from .transitions import ExecutionFence


class AutomationEventHandler:
    def __init__(self, *, client_factory, now_ms):
        self._factory=client_factory;self._now_ms=now_ms

    def _clock(self):
        value=self._now_ms()
        if type(value) is not int or not 0<value<=LIMITS['safeIntegerMax']:
            raise WorkerProtocolError('Invalid event execution clock')
        return value

    def _job(self,context):
        job=self._factory().query('worker_job.get',{'task_id':context.claim.task_id,
            'user_id':context.principal.require_owner(context='automation event delivery')})
        if job is None:raise WorkerLeaseLost('Event transport was retired')
        payload=deepcopy(job['payload'])
        prefixes=validate_event_job(job['userId'],job['tenantId'],job['taskId'],payload)
        if (job['taskKind']!='automation-event' or payload['principal']!=context.principal.to_payload()
                or job['fencingToken']!=context.fencing_token or job['claimOwner']!=context.claim.worker_id):
            raise WorkerLeaseLost('Event transport claim was replaced')
        payload.setdefault('delivery',initial_delivery_cursor())
        return payload,prefixes

    def _settle(self,context,payload, *, mode,next_delivery=None,error_kind=''):
        result=self._factory(write=True).command('automation.event.settle',{
            'user_id':context.principal.owner_user_id,'tenant_id':context.principal.tenant_id or '',
            'job_id':context.claim.task_id,'worker_id':context.claim.worker_id,
            'fencing_token':context.fencing_token,'expected_payload':payload,
            'next_delivery':next_delivery,'mode':mode,'error_kind':error_kind,'now_ms':self._clock()},None)
        if not result.get('ok'):raise WorkerLeaseLost('Event settlement lost its live producer authority')
        return result

    def __call__(self,context,payload):
        context.checkpoint()
        current,prefixes=self._job(context);cursor=current['delivery']
        if cursor['index']==len(prefixes):return WorkerJobOutcome('succeeded')
        prefix=prefixes[cursor['index']];limit=storage_schema()['x-event']['batchRows']
        rows=self._factory().query('record.list',{'namespace':STORAGE_NAMESPACES['subscription'],
            'prefix':prefix,'after_key':cursor['afterKey'],'limit':limit})
        event=current['event']
        for row in rows:
            context.checkpoint()
            pointer=row['value'];definition_id=pointer['id']
            repo=AutomationExecutionRepository(context.principal,client_factory=self._factory)
            definition=repo.get('definition',definition_id)
            if definition is None or definition_id in event['automationChain']:continue
            value=definition.value
            if (value.get('deleted') or not value['definition']['enabled']
                    or subscription_key(repo._prefix,value['definition'])!=row['key']
                    or definition.version!=pointer['definitionRevision']
                    or value['compiled']['digest']!=pointer['definitionDigest']
                    or value['publicationCommand']['requestedAt']>event['occurredAt']):continue
            grant=PrincipalContext.from_payload(value['grant']['principal'])
            if (grant.owner_user_id!=context.principal.owner_user_id
                    or grant.tenant_id!=context.principal.tenant_id or 'admin' in grant.scopes):
                raise WorkerProtocolError('Event definition grant escaped its owner')
            target=AutomationExecutionRepository(grant,client_factory=self._factory)
            try:
                target.admit(definition_id,event,now=self._clock()/1000,
                    producer_fence=ExecutionFence(context.claim.task_id,context.claim.worker_id,context.fencing_token))
            except AutomationConflict:
                refreshed=repo.get('definition',definition_id)
                if refreshed is not None and refreshed.version==definition.version:
                    raise
                # A management edit revoked/replaced this unaccepted subscription.
        next_cursor={'index':cursor['index']+(len(rows)<limit),
            'afterKey':'' if len(rows)<limit else rows[-1]['key'],
            'processed':cursor['processed']+len(rows)}
        context.checkpoint()
        self._settle(context,current,mode='advance',next_delivery=next_cursor)
        return WorkerJobSuspension(self._clock(),replay_cursor=next_cursor['processed'])

    def cancel(self,context,payload):
        context.checkpoint(allow_cancelled=True)
        return WorkerJobOutcome('cancelled')

    def failure(self,context,payload,error):
        context.checkpoint()
        if isinstance(error,StorageError) and error.code in {'database_timeout','database_unavailable','database_busy'}:
            return WorkerJobSuspension(self._clock()+storage_schema()['x-event']['failureRetryMs'],
                replay_cursor=context.replay_cursor)
        if isinstance(error,AutomationConflict):
            proof=self._factory().query('worker_job.claim_state',{'task_id':context.claim.task_id,
                'worker_id':context.claim.worker_id,'fencing_token':context.fencing_token,'now_ms':self._clock()})
            if not proof.get('ok'):raise WorkerLeaseLost('Event admission lost its producer fence')
        return WorkerJobOutcome('failed',error={'kind':'automation_event_failed'})

    def complete(self,context,payload,outcome,now_ms):
        # The runner calls this only after the monitor has joined. Re-read the
        # cursor because this claim may have checkpointed a batch already.
        job=self._factory().query('worker_job.get',{'task_id':context.claim.task_id,
            'user_id':context.principal.owner_user_id})
        if job is None:return {'ok':True,'retired':True,'applied':False}
        current=deepcopy(job['payload']);current.setdefault('delivery',initial_delivery_cursor())
        return self._settle(context,current,mode=outcome.status,
            error_kind=str(outcome.error.get('kind','automation_event_failed')) if outcome.status=='failed' else '')

    def registration(self):
        return WorkerKindRegistration('automation-event',self,WorkerKindSafety(True,True,True,True),
            cancel_handler=self.cancel,failure_handler=self.failure,completion_handler=self.complete)
