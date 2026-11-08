"""A caller's handled exception cannot hide a summary's own teardown failure."""
from copy import deepcopy

import pytest

from lib.llm.stream_result import ProviderStreamResult
from lib.llm_dispatch.provider_pin import get_pinned_provider
from tests.compaction.test_auxiliary_summary_integration import native as native

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('outer_exception', [False, True])
@pytest.mark.parametrize('provider_failure', [False, True])
def test_handled_outer_exception_never_suppresses_owned_cleanup_failure(
        native, monkeypatch, outer_exception, provider_failure):
    from lib.tasks_pkg.compaction import _auxiliary_route as routes
    from lib.tasks_pkg.compaction._summary_budget import summary_failure_reason

    native.task['_transientRuntime'] = True  # Exercise the issued attempt settlement too.
    source = deepcopy(native.source)
    original = deepcopy(source)
    released, calls = [], []
    dispose = routes._dispose_owned_auxiliary_group
    cleanup_error = RuntimeError('owned cleanup observer failed')

    def cleanup(group):
        dispose(group)
        released.append(group)
        raise cleanup_error

    def stream(*args, **kwargs):
        calls.append(kwargs)
        if provider_failure:
            raise RuntimeError('synthetic provider failure')
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': '### Objective\nKeep the complete source.'},
            'stop', {'prompt_tokens': 20, 'completion_tokens': 10})

    monkeypatch.setattr(routes, '_dispose_owned_auxiliary_group', cleanup)
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}

    def invoke():
        with pytest.raises(RuntimeError) as raised:
            native.summary._generate_query_aware_summary(
                source, 'continue', task=native.task, usage_out=usage,
                on_delta=lambda chunk: None)
        assert raised.value is cleanup_error

    if outer_exception:
        try:
            raise LookupError('already handled by the caller')
        except LookupError:
            invoke()
    else:
        invoke()
    assert source == original and len(calls) == 1
    assert len(released) == 1 and released[0].disposed
    assert all(handle.disposed and handle.slot.api_key == '' for handle in released[0].handles)
    assert native.pool.slots == [native.parent]
    assert get_pinned_provider() is None
    assert '_summaryFailure' not in usage or usage['_summaryFailure']['code'] == 'summary_dispatch_failed'
    assert summary_failure_reason(native.task) == 'summary_dispatch_failed'
