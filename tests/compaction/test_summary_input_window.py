"""One summary request never exceeds the summarizer's real input window.

Human evidence stays verbatim and is never silently elided, so an oversized
transcript cannot always fit one request. The external constraint is the
summarizer's context window: every dispatched request must leave room for the
receipt there, every human message must reach a summary request verbatim, and
a fitting input keeps its single request.
"""
from copy import deepcopy

import pytest

pytestmark = pytest.mark.unit

WINDOW = 32_000
FILLER = 'keep every stated constraint verbatim '


def _human(index: int) -> str:
    return f'HUMAN_REQUEST_{index:03d} ' + FILLER * 70


def _transcript(count: int) -> list:
    messages = [{'role': 'system', 'content': 'system policy'}]
    for index in range(count):
        messages += [
            {'role': 'user', 'content': _human(index)},
            {'role': 'assistant', 'content': f'Handled request {index:03d}.'},
        ]
    return messages


class _Requests(list):
    """Accepted request texts; ``attempts`` also counts refused requests."""


@pytest.fixture
def provider(monkeypatch):
    """A summarizer whose window is WINDOW tokens; larger requests are refused."""
    import lib.llm_dispatch as dispatch
    from lib.llm.stream_result import ProviderStreamResult
    from lib.tasks_pkg.compaction._constants import _SUMMARY_MAX_TOKENS
    from lib.tasks_pkg.compaction._layer2 import _prompt, _summary
    from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens

    monkeypatch.setattr(_prompt, '_get_context_limit', lambda task: WINDOW)
    monkeypatch.setattr(_summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(_summary, '_archived_prior_receipt', lambda task: '')
    requests = _Requests()
    attempts = []

    def complete(body, **kwargs):
        prompt = body['messages'] if isinstance(body, dict) else body
        tokens = _estimate_total_tokens(prompt)
        attempts.append(tokens)
        if tokens + _SUMMARY_MAX_TOKENS > WINDOW:
            raise RuntimeError(f'prompt is too long: {tokens} tokens')
        requests.append(prompt[-1]['content'])
        content = f'### Objective\nKeep every constraint.\nRECEIPT_{len(requests)}'
        usage = {'prompt_tokens': tokens, 'completion_tokens': 20}
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': content}, 'stop', usage)

    def chat(messages, **kwargs):
        result = complete({'messages': messages}, **kwargs)
        return result.message['content'], {**result.usage, 'finish_reason': 'stop'}

    monkeypatch.setattr(dispatch, 'dispatch_stream', complete)
    monkeypatch.setattr(dispatch, 'dispatch_chat', chat)
    requests.attempts = attempts
    return requests


def _task():
    return {'_transientRuntime': True, '_userId': 1, 'config': {'model': 'test'}}


def test_oversized_human_evidence_is_summarized_in_window_sized_passes(provider):
    from lib.tasks_pkg.compaction._layer2 import _summary
    from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens

    messages = _transcript(48)
    assert _estimate_total_tokens(messages) > WINDOW
    original = deepcopy(messages)
    usage = {}

    receipt = _summary._generate_query_aware_summary(
        messages, 'continue', task=_task(), usage_out=usage)

    assert receipt is not None
    assert len(provider) > 1
    for index in range(48):
        assert any(_human(index) in request for request in provider)
    # Each later pass carries the previous pass's receipt forward.
    for number, request in enumerate(provider[1:], start=1):
        assert f'RECEIPT_{number}' in request
    assert receipt.endswith(f'RECEIPT_{len(provider)}')
    assert usage['completion_tokens'] == 20 * len(provider)
    assert len(usage['_summaryCalls']) == len(provider)
    assert messages == original


def test_single_human_message_larger_than_the_window_is_split_verbatim(provider):
    from lib.tasks_pkg.compaction._layer2 import _summary

    clauses = [f'clause-{n:05d}' for n in range(14_000)]
    giant = ' '.join(['GIANT_START', *clauses, 'GIANT_END'])
    messages = [{'role': 'user', 'content': giant},
                {'role': 'assistant', 'content': 'Read it.'}]

    assert _summary._generate_query_aware_summary(
        messages, 'continue', task=_task()) is not None

    assert len(provider) > 1
    assert 'GIANT_START' in provider[0] and 'GIANT_END' in provider[-1]
    assert all(any(f'{clause} ' in request for request in provider)
               for clause in clauses)


def test_fitting_input_keeps_its_single_request(provider):
    from lib.tasks_pkg.compaction._layer2 import _summary

    assert _summary._generate_query_aware_summary(
        _transcript(4), 'continue', task=_task()) is not None
    assert len(provider) == 1
    assert all(_human(index) in provider[0] for index in range(4))


def test_irreducible_fixed_evidence_is_refused_without_dispatch(provider):
    from lib.tasks_pkg.compaction._layer2 import _summary

    messages = _transcript(2)
    original = deepcopy(messages)
    query = 'CURRENT ' + FILLER * 4_000

    assert _summary._generate_query_aware_summary(
        messages, query, task=_task()) is None
    assert provider.attempts == []
    assert messages == original


def test_dispatch_guard_compacts_a_transcript_larger_than_the_summarizer(provider, monkeypatch):
    from lib.tasks_pkg.compaction._layer2 import _compact as l2
    from lib.tasks_pkg.compaction._layer2 import _summary
    import lib.tasks_pkg.autopilot_state as autopilot

    monkeypatch.setattr(l2, '_archive_transcript', lambda *a, **k: None)
    monkeypatch.setattr(l2, '_extract_recently_accessed_files', lambda *a: [])
    monkeypatch.setattr(l2, '_generate_query_aware_summary', _summary._generate_query_aware_summary)
    monkeypatch.setattr(autopilot, '_update_objective_from_receipt', lambda *a, **k: None)
    messages = _transcript(48) + [{'role': 'user', 'content': 'Finish the release.'}]
    meta = {}

    assert l2.force_compact_if_needed(
        messages, task={'id': 'window-task', '_userId': 1, '_transientRuntime': True,
                        'config': {'model': 'test'}},
        force=True, preserve_budget_tokens=1000, _compaction_skip_archive=True,
        _compaction_trigger='dispatch_guard', _result_meta=meta)

    assert meta.get('compacted') is True
    assert len(provider) > 1
    assert messages[-1]['content'] == 'Finish the release.'
