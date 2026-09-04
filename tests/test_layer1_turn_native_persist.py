"""L1 placeholders persist through normalized turn authority, never v1 PUT."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


class _TurnNativeStore:
    def __init__(self, messages):
        self.messages = messages
        self.notifications = 0
        self.load_calls = 0

    def load_transcript(self, _conv_id, *, user_id):
        self.load_calls += 1
        return self.messages, 10, 20

    def notify_conversation_changed(self, _conv_id, *, user_id):
        self.notifications += 1


def test_l1_patches_dirty_settled_turn_projection(monkeypatch):
    from lib.tasks_pkg.compaction.api import micro_compact

    cold_round = {
        'toolCallId': 'cold-call', 'toolName': 'read_files',
        'toolArgs': {}, 'toolContent': 'x' * 4000, 'status': 'done',
        'roundNum': 1,
    }
    owner = {
        'role': 'assistant', 'content': 'old answer',
        'toolRounds': [cold_round], '_turnId': 'turn-cold',
        '_projectionRevision': 7, '_turnStatus': 'completed',
        'segments': [{
            'type': 'tool_use', 'blockId': 'tool:custom-cold-call',
            'id': 'cold-call', 'name': 'read_files', 'input': {},
            'result': {
                'content': 'x' * 4000, 'status': 'done',
                'artifactId': 'artifact-cold',
            },
            'translatedText': '已读取',
        }],
    }
    store = _TurnNativeStore([owner])
    monkeypatch.setattr(
        'lib.agent_core.store.get_conversation_store', lambda: store)
    monkeypatch.setattr(
        'lib.tasks_pkg.cache_tracking._prefix.get_cache_prefix_count',
        lambda *_a, **_k: 0)
    updates = []

    class _TurnClient:
        def command(self, operation, payload, idempotency_key):
            updates.append({
                'operation': operation,
                'payload': payload,
                'idempotency_key': idempotency_key,
            })
            return {'turn': {'projectionRevision': 8},
                    'conversationRevision': 21}

    monkeypatch.setattr(
        'lib.turn_lifecycle.get_turn',
        lambda *_a, **_k: {'actor': 'assistant', 'status': 'completed'},
    )
    monkeypatch.setattr(
        'lib.turn_lifecycle._turn_client',
        lambda *, write=False: _TurnClient(),
    )

    api_messages = [
        {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'cold-call', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': '{}'},
        }]},
        {'role': 'tool', 'tool_call_id': 'cold-call', 'name': 'read_files',
         'content': 'x' * 4000},
        {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'hot-call', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': '{}'},
        }]},
        {'role': 'tool', 'tool_call_id': 'hot-call', 'name': 'read_files',
         'content': 'y' * 4000},
    ]

    saved = micro_compact(
        api_messages, conv_id='conv-turn-native',
        task={'_userId': 1, 'model': 'test-model'},
        constant_overrides={
            'MICRO_HOT_TAIL': 1,
            'MICRO_COMPACT_THRESHOLD': 100,
        })

    assert saved > 0
    assert store.load_calls == 1
    assert store.notifications == 1
    assert len(updates) == 1
    assert updates[0]['operation'] == 'turn.projection.update'
    assert updates[0]['payload']['turn_id'] == 'turn-cold'
    assert updates[0]['payload']['expected_projection_revision'] == 7
    persisted = updates[0]['payload']['projection']
    persisted_tool = next(
        segment for segment in persisted['segments']
        if segment['type'] == 'tool_use'
    )
    assert persisted_tool['blockId'] == 'tool:custom-cold-call'
    assert persisted_tool['input'] == {}
    assert persisted_tool['translatedText'] == '已读取'
    assert persisted_tool['result'] == {
        'content': cold_round['toolContent'],
        'status': 'done',
        'artifactId': 'artifact-cold',
    }
    assert owner['segments'][0]['result']['content'] == 'x' * 4000
    assert cold_round['compactionLayer'] == 'L1'
    assert cold_round['toolContent'].startswith('[read_files result compacted')
    assert owner['_projectionRevision'] == 8


def test_l1_noop_does_not_load_conversation_transcript(monkeypatch):
    """The per-round steady state is read-free when no placeholder is made."""
    from lib.tasks_pkg.compaction.api import micro_compact

    store = _TurnNativeStore([])
    monkeypatch.setattr(
        'lib.agent_core.store.get_conversation_store', lambda: store)
    monkeypatch.setattr(
        'lib.tasks_pkg.cache_tracking._prefix.get_cache_prefix_count',
        lambda *_a, **_k: 0)
    token_counts = []
    monkeypatch.setattr(
        'lib.tasks_pkg.compaction._tokens._estimate_total_tokens',
        lambda messages: token_counts.append(len(messages)) or 0)

    api_messages = [
        {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'cold-placeholder', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': '{}'},
        }]},
        {'role': 'tool', 'tool_call_id': 'cold-placeholder',
         'name': 'read_files',
         'content': '[read_files result compacted — was 4,000 chars]'},
        {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'hot-call', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': '{}'},
        }]},
        {'role': 'tool', 'tool_call_id': 'hot-call', 'name': 'read_files',
         'content': 'short result'},
    ]
    task = {'_userId': 1, 'model': 'test-model', 'toolRounds': []}

    assert micro_compact(
        api_messages, conv_id='conv-noop', task=task,
        constant_overrides={'MICRO_HOT_TAIL': 1}) == 0
    assert store.load_calls == 0
    assert token_counts == []


def test_l1_task_owned_placeholder_does_not_load_transcript(monkeypatch):
    """An in-flight round already carries the persistence/UX stamp owner."""
    from lib.tasks_pkg.compaction.api import micro_compact

    store = _TurnNativeStore([])
    monkeypatch.setattr(
        'lib.agent_core.store.get_conversation_store', lambda: store)
    monkeypatch.setattr(
        'lib.tasks_pkg.cache_tracking._prefix.get_cache_prefix_count',
        lambda *_a, **_k: 0)

    cold_round = {
        'toolCallId': 'cold-task-call', 'toolName': 'read_files',
        'toolContent': 'x' * 4000, 'roundNum': 1,
    }
    api_messages = [
        {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'cold-task-call', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': '{}'},
        }]},
        {'role': 'tool', 'tool_call_id': 'cold-task-call',
         'name': 'read_files', 'content': 'x' * 4000},
        {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'hot-task-call', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': '{}'},
        }]},
        {'role': 'tool', 'tool_call_id': 'hot-task-call',
         'name': 'read_files', 'content': 'y' * 4000},
    ]
    task = {
        '_userId': 1, 'model': 'test-model', 'toolRounds': [cold_round],
    }

    assert micro_compact(
        api_messages, conv_id='conv-task-owned', task=task,
        constant_overrides={
            'MICRO_HOT_TAIL': 1,
            'MICRO_COMPACT_THRESHOLD': 100,
        }) > 0
    assert store.load_calls == 0
    assert cold_round['compactionLayer'] == 'L1'
    assert cold_round['toolContent'].startswith('[read_files result compacted')


def test_l1_duplicate_legacy_id_never_rewrites_an_arbitrary_round(monkeypatch):
    """Ambiguous positional IDs fail closed at the durable write-back seam."""
    from lib.tasks_pkg.compaction.api import micro_compact

    first_round = {
        'toolCallId': 'read_files_0', 'toolName': 'read_files',
        'toolContent': 'first durable bytes', 'roundNum': 1,
    }
    second_round = {
        'toolCallId': 'read_files_0', 'toolName': 'read_files',
        'toolContent': 'second durable bytes', 'roundNum': 2,
    }
    owner = {
        'role': 'assistant', 'content': 'old answer',
        'toolRounds': [first_round, second_round], '_turnId': 'turn-duplicate',
        '_projectionRevision': 3, '_turnStatus': 'completed',
    }
    store = _TurnNativeStore([owner])
    monkeypatch.setattr(
        'lib.agent_core.store.get_conversation_store', lambda: store)
    monkeypatch.setattr(
        'lib.tasks_pkg.cache_tracking._prefix.get_cache_prefix_count',
        lambda *_a, **_k: 0)
    updates = []
    monkeypatch.setattr(
        'lib.turn_lifecycle.update_turn_projection',
        lambda *_a, **_k: updates.append((_a, _k)))

    api_messages = [
        {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'read_files_0', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': '{}'},
        }]},
        {'role': 'tool', 'tool_call_id': 'read_files_0',
         'name': 'read_files', 'content': 'x' * 4000},
        {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'hot-call', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': '{}'},
        }]},
        {'role': 'tool', 'tool_call_id': 'hot-call', 'name': 'read_files',
         'content': 'y' * 4000},
    ]

    assert micro_compact(
        api_messages, conv_id='conv-duplicate',
        task={'_userId': 1, 'model': 'test-model', 'toolRounds': []},
        constant_overrides={
            'MICRO_HOT_TAIL': 1,
            'MICRO_COMPACT_THRESHOLD': 100,
        }) > 0
    assert first_round['toolContent'] == 'first durable bytes'
    assert second_round['toolContent'] == 'second durable bytes'
    assert updates == []
    assert store.notifications == 0


def test_l1_cold_image_placeholder_is_durable(monkeypatch):
    """The image-tail step must not resurrect base64 on the next turn."""
    from lib.tasks_pkg.compaction.api import micro_compact

    cold_round = {
        'toolCallId': 'cold-image', 'toolName': 'browser_read_page',
        'toolContent': [], 'roundNum': 1,
    }
    owner = {
        'role': 'assistant', 'content': 'old answer',
        'toolRounds': [cold_round], '_turnId': 'turn-image',
        '_projectionRevision': 4, '_turnStatus': 'completed',
    }
    store = _TurnNativeStore([owner])
    monkeypatch.setattr(
        'lib.agent_core.store.get_conversation_store', lambda: store)
    monkeypatch.setattr(
        'lib.tasks_pkg.cache_tracking._prefix.get_cache_prefix_count',
        lambda *_a, **_k: 0)
    monkeypatch.setattr(
        'lib.turn_lifecycle.update_turn_projection',
        lambda *_a, **_k: {
            'turn': {'projectionRevision': 5}, 'conversationRevision': 9,
        })

    def image_content(char: str):
        return [
            {'type': 'text', 'text': 'page'},
            {'type': 'image_url', 'image_url': {
                'url': 'data:image/png;base64,' + char * 4000,
            }},
        ]

    api_messages = [
        {'role': 'tool', 'tool_call_id': tool_call_id,
         'name': 'browser_read_page', 'content': image_content(char)}
        for tool_call_id, char in (
            ('cold-image', 'A'), ('warm-image', 'B'), ('hot-image', 'C'))
    ]

    assert micro_compact(
        api_messages, conv_id='conv-image',
        task={'_userId': 1, 'model': 'test-model', 'toolRounds': []},
        constant_overrides={'MICRO_HOT_TAIL': 99}) > 0
    assert store.load_calls == 1
    assert store.notifications == 1
    assert cold_round['compactionLayer'] == 'L1'
    assert cold_round['toolContent'].startswith(
        '[browser_read_page image compacted')
