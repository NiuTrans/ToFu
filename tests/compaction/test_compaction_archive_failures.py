"""Failed archive creation retains the original source and authentic counts."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.agent_core.storage_errors import StorageError
from lib.tasks_pkg.compaction._archive import _archive_transcript

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('emit_event', [False, True])
@pytest.mark.parametrize('trigger', ['working_set', 'window', 'dispatch_guard', 'manual'])
def test_failed_archive_creation_emits_one_unchanged_source_receipt(
        monkeypatch, emit_event, trigger):
    source = [{'role': 'user', 'content': f'Preserve source message {number}.'}
              for number in range(18)]
    original = deepcopy(source)
    task = {'id': 'archive-fixture-task', 'convId': 'archive-fixture-conversation',
            '_turnId': 'archive-fixture-turn', '_userId': 41, 'aborted': False,
            'model': 'observed-parent'}
    events, stored = [], []

    def fail_creation(conv_id, messages, **kwargs):
        stored.append((conv_id, deepcopy(messages), kwargs))
        return None

    monkeypatch.setattr('lib.agent_core.store.get_conversation_store', lambda: SimpleNamespace(
        archive_transcript=fail_creation))
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda actual, event: events.append(event))
    with pytest.raises(StorageError) as error:
        _archive_transcript(task['convId'], source, user_id=41, task=task,
                            trigger=trigger, round_num=7, tokens_before=387_901,
                            tokens_after=12_000, msgs_before=len(source), msgs_after=4,
                            emit_event=emit_event)
    assert source == original
    assert error.value.code == 'database_unavailable'
    assert error.value.retryable is True
    assert error.value.operation_id == 'compaction_archive.commit'
    assert len(stored) == 1 and stored[0][0] == task['convId']
    assert stored[0][1] == original and stored[0][2]['user_id'] == 41
    assert len(events) == 1
    event = events[0]
    assert event['type'] == 'compaction_done'
    assert event['tokensBefore'] == event['tokensAfter'] == 387_901
    assert event['msgsBefore'] == event['msgsAfter'] == len(source)
    assert event['tokenCountKind'] == 'estimated' and event['reductionPct'] == 0
    assert event['trigger'] == trigger and event['roundNum'] == 7
    assert not event['archiveId']
    receipt = event['receipt']
    assert receipt['status'] == 'failed' and receipt['strategy'] == 'unchanged'
    assert receipt['outcomeReason'] == 'compaction_archive_unavailable'
    assert not receipt['summary']['accepted'] and not receipt['summary']['generated']


def test_aborted_archive_creation_never_emits_a_second_terminal(monkeypatch):
    task = {'id': 'archive-fixture-task', 'convId': 'archive-fixture-conversation',
            '_turnId': 'archive-fixture-turn', '_userId': 41, 'aborted': True}
    source = [{'role': 'user', 'content': 'The owner stopped this task.'}]
    original = deepcopy(source)
    events = []
    monkeypatch.setattr('lib.agent_core.store.get_conversation_store', lambda: SimpleNamespace(
        archive_transcript=lambda *args, **kwargs: None))
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda *args: events.append(args))
    assert _archive_transcript(task['convId'], source, user_id=41, task=task,
                               tokens_before=100, msgs_before=1) is None
    assert source == original and events == []
