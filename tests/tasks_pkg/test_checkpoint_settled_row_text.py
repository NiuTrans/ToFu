"""Executor-recovery checkpoints encode each settled row once.

``checkpoint_task_partial`` runs about twice per round. Its tool-round and
segment texts equal the former whole-list ``json.dumps``; settled tool rounds
and settled segments keep their encoded text in their own memo slots, so a
checkpoint encodes only rows that can still change.
"""
import json

import pytest

from lib.conversation_sync import settled_rows
from lib.conversation_sync.settled_rows import SettledDict, settle
from lib.tasks_pkg.manager import _sync
from lib.tasks_pkg.manager._persist import _merge_tool_rounds
from lib.tasks_pkg.segments import segments_to_json

pytestmark = pytest.mark.unit


def _task():
    rounds = [settle({
        'roundNum': n, 'llmRound': n, 'toolCallId': f'call-{n}', 'toolName': 'read_file',
        'toolArgs': '{"path": "a.py"}', 'status': 'done', 'ratio': 1e-05 * n,
        'toolContent': f'结果 {n} "quoted"\n' * 20}) for n in range(1, 6)]
    rounds.append({'roundNum': 6, 'llmRound': 6, 'toolCallId': 'call-6',
                   'toolName': 'run_command', 'toolArgs': '{"command": "ls"}',
                   'status': 'running'})
    return {'id': 'checkpoint-text-task', 'status': 'running', 'content': 'working',
            'thinking': '', 'toolRounds': rounds, 'config': {}}


def test_checkpoint_texts_equal_the_full_encoding_and_settled_rows_encode_once(monkeypatch):
    sent = []
    monkeypatch.setattr(_sync, '_upsert_task_row',
                        lambda task, conv_id, **payload: sent.append(payload) or True)
    encoded = []
    real_json = settled_rows.json

    class CountingJson:
        @staticmethod
        def dumps(value, **kwargs):
            encoded.append(value)
            return real_json.dumps(value, **kwargs)

    monkeypatch.setattr(settled_rows, 'json', CountingJson)
    task = _task()
    assert _sync.checkpoint_task_partial(task, force=True) is True
    first = len(encoded)
    assert _sync.checkpoint_task_partial(task, force=True) is True
    merged = _merge_tool_rounds(task)
    for payload in sent:
        assert payload['tr_json'] == json.dumps(merged, ensure_ascii=False)
        assert payload['segments_json'] == json.dumps(
            segments_to_json(task['segments']), ensure_ascii=False)
    assert sum(type(row) is SettledDict for row in merged) == 5
    # The repeat checkpoint encodes only the live round; every settled round
    # and every settled segment reuses its text.
    assert all(type(segment) is SettledDict for segment in task['segments'])
    assert len(encoded) - first == 1
