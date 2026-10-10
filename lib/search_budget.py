"""Owner-explicit content-filter budget binding on the existing search context.

Entry: content_filter_budget_scope. The caller owns task admission and waits
for workers before closing. No global reconfiguration or replay authority.
"""
from contextlib import contextmanager
from dataclasses import replace
import threading
from lib.identity import require_user_id
from lib.agent_core.execution_budget import ModelBudgetAdmissionDenied

@contextmanager
def content_filter_budget_scope(*,owner_user_id,model_budget_admit):
    from tofu_search.config import get_config,bind_call_config
    from lib.search_bridge import _chatui_llm
    owner=require_user_id(owner_user_id,context='search model budget owner')
    if not callable(model_budget_admit):
        raise ValueError('model_budget_admit must be callable')
    config=get_config()
    if config.llm_function is not _chatui_llm:
        raise ValueError('search budget requires the configured host model adapter')
    refused=[];lock=threading.Lock();closed=False
    def admitted_filter(messages,**kwargs):
        if 'owner_user_id' in kwargs or 'model_budget_admit' in kwargs:
            raise ValueError('filter cannot replace the task budget or owner')
        with lock:
            if closed:
                from lib.llm_errors import AbortedError
                raise AbortedError('search budget scope is closed')
            prior=refused[0] if refused else None
        if prior is not None:raise prior
        try:
            return _chatui_llm(messages,owner_user_id=owner,
                model_budget_admit=model_budget_admit,**kwargs)
        except ModelBudgetAdmissionDenied as error:
            with lock:
                if not refused:refused.append(error)
            raise
    try:
        with bind_call_config(replace(config,llm_function=admitted_filter)):
            yield
            with lock:
                error=refused[0] if refused else None
            if error is not None:raise error
    finally:
        with lock:
            closed=True


def budgeted_search_tool(function):
    """Keep policy refusal around a joined search batch, without changing other tools."""
    from functools import wraps
    @wraps(function)
    def wrapped(*args, **kwargs):
        port = kwargs.get('model_budget_admit')
        name = args[0] if args else kwargs.get('name')
        if port is None or name not in ('web_search', 'fetch_url'):
            return function(*args, **kwargs)
        with content_filter_budget_scope(owner_user_id=kwargs.get('owner_user_id'),
                                        model_budget_admit=port):
            return function(*args, **kwargs)
    return wrapped
