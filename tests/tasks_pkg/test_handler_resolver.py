"""HandlerResolver (D1): task-local first, immutable fallback, graceful env failure."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


class FakeEnv:
    def __init__(self, handlers, *, raises=False):
        self._handlers = handlers
        self._raises = raises

    def resolve(self, fn_name):
        if self._raises:
            raise RuntimeError('env exploded')
        return self._handlers.get(fn_name)


class FakeRegistry:
    def __init__(self, handlers):
        self._handlers = handlers
        self.calls = []

    def lookup(self, fn_name, round_entry=None):
        self.calls.append((fn_name, round_entry))
        if round_entry and round_entry.get('toolName') == 'code_exec':
            return self._handlers.get('__code_exec__')
        return self._handlers.get(fn_name)


def _handlers():
    return {
        'local_tool': object(),
        'builtin_tool': object(),
        '__code_exec__': object(),
    }


def test_task_local_env_wins_over_fallback():
    from lib.tasks_pkg.executor._resolver import resolver_for
    handlers = _handlers()
    resolver = resolver_for(
        {'_tool_env': FakeEnv({'shared': 'local'})},
        registry=FakeRegistry({'shared': 'builtin'}))
    assert resolver.lookup('shared') == 'local'


def test_fallback_registry_serves_builtin_and_special_key():
    from lib.tasks_pkg.executor._resolver import resolver_for
    registry = FakeRegistry(_handlers())
    resolver = resolver_for({'_tool_env': FakeEnv({})}, registry=registry)
    assert resolver.lookup('builtin_tool') is not None
    special = resolver.lookup('code_exec', {'toolName': 'code_exec'})
    assert special is not None
    assert ('code_exec', {'toolName': 'code_exec'}) in registry.calls


def test_plain_task_resolves_through_fallback_only():
    from lib.tasks_pkg.executor._resolver import resolver_for
    registry = FakeRegistry(_handlers())
    resolver = resolver_for({}, registry=registry)
    assert resolver.lookup('builtin_tool') is not None
    assert resolver.lookup('missing') is None


def test_exploding_env_degrades_to_fallback_with_warning():
    from lib.tasks_pkg.executor._resolver import resolver_for
    registry = FakeRegistry(_handlers())
    resolver = resolver_for({'_tool_env': FakeEnv({}, raises=True)},
                            registry=registry)
    assert resolver.lookup('builtin_tool') is not None


def test_execute_dispatches_through_the_seam(monkeypatch):
    from types import SimpleNamespace
    from lib.tasks_pkg.executor import _execute, _resolver
    from lib.tasks_pkg.tool_runtime.context import ToolExecutionContext
    calls=[];released=[]
    def handler(*args):return 'seam-call','seam-result',False
    def lookup(name,entry):calls.append((name,entry));return handler
    monkeypatch.setattr(_resolver,'resolver_for',lambda task:SimpleNamespace(lookup=lookup))
    task={'id':'seam-task','config':{},'_userId':71}
    entry={'tStart':0}
    context=ToolExecutionContext(
        task=task,round_num=1,tool_call_id='seam-call',tool_name='seam-tool',
        owner_user_id=71,round_entry=entry)
    monkeypatch.setattr(_execute,'context_for_task',lambda *args,**kwargs:context)
    monkeypatch.setattr(_execute,'unregister_context',released.append)
    result=_execute._execute_tool_one(task, {},'seam-tool','seam-call',{},1,entry,{},None,False)
    assert result==('seam-call','seam-result',False)
    assert calls==[('seam-tool',entry)] and released==[context]
    assert context.terminal_state==entry['_runtimeState']=='done'
