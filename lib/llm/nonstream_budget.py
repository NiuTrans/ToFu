"""Explicit per-send budget lifecycle for synchronous non-stream completions.

The execution owner supplies admission. This module retains one active lease,
not prompts or credentials; failed attempts settle before retries. It does not
create accounts, grant ownership, or authorize replay.
"""
from functools import wraps
from lib.llm.request import seal_request

class NonStreamBudgetScope:
    def __init__(self, admission):
        self.admission=admission
        self.lease=None

    def prepare(self, body):
        self.settle()
        wire=body.wire_body()
        key=next((k for k in ('max_output_tokens','max_completion_tokens','max_tokens') if k in wire),'max_tokens')
        limit=wire.get(key)
        limit=limit if type(limit) is int and limit>0 else 0
        thinking=wire.get('thinking') or {}
        minimum=thinking.get('budget_tokens',0) if isinstance(thinking,dict) else 0
        minimum=minimum+1 if type(minimum) is int and minimum>0 else 1
        self.lease=self.admission(input_tokens=body.input_tokens,max_output_tokens=limit,
                                  minimum_output_tokens=minimum)
        if limit:wire[key]=self.lease.max_output_tokens
        return seal_request(wire,model=body.model,provider_id=body.provider_id,
            protocol=body.protocol,endpoint=body.endpoint,input_tokens=body.input_tokens,
            semantic_id=body.semantic_id)

    def send(self, body, send, *args, **kwargs):
        prepare = getattr(self.lease, 'prepare_input', None)
        if prepare is not None:
            prepare(body)
        self.lease.mark_dispatched()
        return send(*args,**kwargs)

    def settle(self, usage=None, *, successful=False):
        lease,self.lease=self.lease,None
        if lease is not None:
            lease.settle(usage,unsent=not lease.dispatched and usage is None)
            confirm = getattr(lease, 'confirm_input', None)
            if confirm is not None:
                confirm(successful=successful)


def budgeted_nonstream_chat(function):
    @wraps(function)
    def wrapped(*args,**kwargs):
        admission=kwargs.get('model_budget_admit')
        if admission is None:return function(*args,**kwargs)
        if not callable(admission):raise ValueError('model_budget_admit must be callable')
        scope=NonStreamBudgetScope(admission)
        kwargs['_budget_scope']=scope
        try:
            result=function(*args,**kwargs)
            scope.settle(result[1], successful=True)
            return result
        except BaseException as error:
            scope.settle(getattr(error,'usage',None))
            raise
    return wrapped
