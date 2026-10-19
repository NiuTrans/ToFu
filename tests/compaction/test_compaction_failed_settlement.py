"""Failed working_set/dispatch_guard compaction settles as an authentic terminal fact.

The muu1klptjtwd3t incident: the working_set pass emitted a durable COMPACTION
archive row, then the summary route failed (Codex shared credential
unavailable) and the attempt returned False without any settlement — the row
stayed `running` forever, and the dispatch_guard pass repeated it, leaving two
stuck "compressing" ghosts and a fatal ContextCompactionError. These tests pin
the settlement contract: a failed attempt now emits ONE terminal
``compaction_done`` whose receipt carries ``status='failed'`` and the real
failure reason, and bridges the live phase row through ``_reactiveCompactions``.
"""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


def _messages():
    return [
        {'role': 'system', 'content': 'system'},
        {'role': 'user', 'content': 'old objective'},
        {'role': 'assistant', 'content': 'old answer'},
        {'role': 'user', 'content': 'current request'},
        {'role': 'assistant', 'content': 'current answer'},
    ]


def _decline(summary_usage):
    def inner(source, task=None, **kwargs):
        meta = kwargs['_result_meta']
        meta.update(
            compacted=False,
            tokens_before=1000,
            msgs_before=len(source),
            summary_usage=summary_usage,
            summary_duration_ms=42,
            summaryFailureReason='summary_dispatch_failed')
        return 'Context compaction attempted but summary generation failed.'
    return inner


def _wire(monkeypatch, *, trigger, archived_row, summary_usage=None):
    from lib.tasks_pkg.compaction._layer2 import _compact as compact
    from lib.tasks_pkg.compaction import _summary_budget, _tokens
    import lib.agent_core.store as agent_store
    import lib.tasks_pkg.manager as manager

    task = {'id': 'settle-failed', 'convId': 'isolated-settle',
            '_userId': 1, 'config': {'model': 'mock'}}
    messages = _messages()
    original = deepcopy(messages)
    archived, events = [], []
    store = MagicMock()

    def archive(_conv_id, source, **kwargs):
        archived.append(deepcopy(source))
        return 'settle-archive'

    monkeypatch.setattr(compact, 'execute_compact_tool',
                        _decline(summary_usage))
    if archived_row:
        monkeypatch.setattr(compact, '_archive_transcript', archive)
    monkeypatch.setattr(compact, '_get_context_limit', lambda task: 100)
    monkeypatch.setattr(compact, '_usable_context', lambda limit: 100)
    monkeypatch.setattr(_tokens, '_count_tokens_authoritative',
                        lambda source, task: (200, 'mock'))
    monkeypatch.setattr(agent_store, 'get_conversation_store', lambda: store)
    monkeypatch.setattr(manager, 'append_event',
                        lambda task, event: events.append(event))
    # A live/transient bounded task must keep its no-lossy-recovery discipline;
    # scope the test to the explicit-utility failure surface.
    monkeypatch.setattr(_summary_budget, 'bounded_summary_task',
                        lambda task: False)
    # Keep the deterministic head-truncate net out of the way so the attempt
    # reaches the settlement tail.
    monkeypatch.setattr(compact, 'force_compact_if_needed',
                        compact.force_compact_if_needed)
    return task, messages, original, archived, events, store


@pytest.mark.parametrize('trigger', ['working_set', 'dispatch_guard'])
def test_failed_attempt_settles_terminal_done(monkeypatch, trigger):
    from lib.tasks_pkg.compaction._layer2 import _compact as compact

    usage = {'input_tokens': 10, 'output_tokens': 0, '_summaryCalls': [
        {'usage': {'_dispatch': {'model': 'luna', 'provider_id': 'oauth_codex'}}},
    ]}
    task, messages, original, _archived, events, _store = _wire(
        monkeypatch, trigger=trigger, archived_row=False, summary_usage=usage)

    result = compact.force_compact_if_needed(
        messages, task=task, force=True,
        _compaction_trigger=trigger, _compaction_round=2)

    assert result is False
    assert messages == original  # No mutation on a failed summary.
    done = [event for event in events if event.get('type') == 'compaction_done']
    assert len(done) == 1
    receipt = done[0]['receipt']
    assert receipt['status'] == 'failed'
    assert receipt['trigger'] == trigger
    assert receipt['outcomeReason'] == 'summary_dispatch_failed'
    # The live phase row is bridged so it settles instead of ghosting.
    bridge = task.get('_reactiveCompactions') or {}
    assert done[0]['archiveId'] in bridge
    assert bridge[done[0]['archiveId']]['trigger'] == trigger
    # Observed summary dispatch attribution is preserved, never a credential.
    assert done[0]['summaryDispatchCalls'] == [
        {'model': 'luna', 'providerId': 'oauth_codex'}]


def test_reactive_trigger_is_not_double_settled(monkeypatch):
    """The reactive path owns its own terminal accounting; the settlement
    helper must not fire for it (scoped to working_set/window/dispatch_guard).
    """
    from lib.tasks_pkg.compaction._layer2 import _compact as compact

    task, messages, original, _archived, events, _store = _wire(
        monkeypatch, trigger='reactive', archived_row=False)

    result = compact.force_compact_if_needed(
        messages, task=task, force=True,
        _compaction_trigger='reactive', _compaction_round=2)

    assert result is False
    done = [event for event in events if event.get('type') == 'compaction_done']
    assert done == []
    assert '_reactiveCompactions' not in task


def test_preflight_refusal_records_its_actual_boundary_for_live_and_replay(monkeypatch):
    from lib.tasks_pkg.compaction._layer2 import _compact as compact
    from lib.turn_activity_timeline import fold_activity_timeline

    task, messages, original, _archived, events, _store = _wire(
        monkeypatch, trigger='dispatch_guard', archived_row=False)
    task['_attemptId'] = 'failed-attempt'
    task['segments'] = [{'type': 'tool_use', 'blockId': 'last-edit',
                         'taskId': task['id'], 'attemptId': 'failed-attempt',
                         'llmRound': 22, 'roundNum': 48}]
    assert not compact.force_compact_if_needed(
        messages, task=task, force=True,
        _compaction_trigger='dispatch_guard', _compaction_round=23)
    done = next(event for event in events if event['type'] == 'compaction_done')
    assert done.get('anchorBlockId') == 'last-edit'
    assert done.get('anchorPosition') == 'after'
    assert messages == original
    # The event itself must carry the witness. Reload may see later output;
    # neither a future segment nor producer-start may move the refusal.
    for segments in (task['segments'], [*task['segments'], {
            'type': 'text', 'blockId': 'future', 'taskId': task['id'],
            'attemptId': 'failed-attempt', 'text': 'later output'}]):
        replay_task = {**task, 'segments': segments}
        timeline = fold_activity_timeline(None, {**done, 'seq': 100, 'emittedAt': 1000}, replay_task)
        entry = timeline['entries'][0]
        assert entry['anchorBlockId'] == 'last-edit'
        assert entry['anchorPosition'] == 'after'
        assert entry['status'] == 'failed'
        assert entry['llmRound'] == 23


def test_economic_decline_is_not_settled_as_failure(monkeypatch):
    """A proactive low-yield decline is an expected no-op, not a failure: it
    must not emit a terminal failed compaction_done."""
    from lib.tasks_pkg.compaction._layer2 import _compact as compact
    from lib.tasks_pkg.compaction import _summary_budget
    import lib.tasks_pkg.manager as manager

    task = {'id': 'decline', 'convId': 'isolated-decline', '_userId': 1,
            'config': {'model': 'mock'}}
    messages = _messages()
    events = []

    def decline(source, task=None, **kwargs):
        kwargs['_result_meta'].update(
            compacted=False, reason='low_yield', msgs_before=len(source))
        return 'low yield'

    monkeypatch.setattr(compact, 'execute_compact_tool', decline)
    monkeypatch.setattr(_summary_budget, 'bounded_summary_task',
                        lambda task: False)
    monkeypatch.setattr(manager, 'append_event',
                        lambda task, event: events.append(event))

    result = compact.force_compact_if_needed(
        messages, task=task, force=True,
        _compaction_trigger='working_set', _compaction_round=0)

    assert result is False
    assert not any(e.get('type') == 'compaction_done' for e in events)
