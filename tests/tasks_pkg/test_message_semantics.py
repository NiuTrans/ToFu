"""Source identity must survive rebuild, counting and protocol projection."""
import pytest

from lib.tasks_pkg.message_semantics import count_messages, is_human_message
from lib.tasks_pkg.compaction._history import is_human_message as retention_human
from lib.tasks_pkg.conv_message_builder._transform import _transform_messages
from lib.tasks_pkg.context_composer.task_state import derive_task_state_snapshot
from lib.tasks_pkg.tool_orchestration_policy import genuine_user_message_count, latest_user_text
from lib.tasks_pkg.wire_messages import apply_wire_sanitize

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('metadata,human', [
    ({}, True), ({'_initiator': 'operator'}, True), ({'_initiator': None}, True),
    ({'_initiator': 'timer'}, False), ({'_initiator': 'unknown'}, False),
    ({'_isVirtualUser': True}, False), ({'_sourceActor': 'critic'}, False),
    ({'_isInboxInject': True}, False),
    ({'_isInboxInject': True, '_containsHumanSteer': True}, True),
    ({'_contextComposer': True}, False), ({'_isMeta': True}, False),
])
def test_all_context_consumers_agree_after_cold_rebuild(metadata, human):
    source = {'role': 'user', 'content': 'must retain this request', **metadata}
    for messages in ([source], _transform_messages([source], {})):
        assert is_human_message(messages[0]) is human
        assert retention_human(messages[0]) is human
        assert genuine_user_message_count(messages) == int(human)
        assert count_messages(messages).human_messages == int(human)
        assert bool(latest_user_text(messages)) is human
        assert bool(derive_task_state_snapshot(messages).goal) is human
    assert apply_wire_sanitize(_transform_messages([source], {})) == [
        {'role': 'user', 'content': source['content']}]


def test_distinct_turns_with_identical_timestamp_keep_both_requests():
    raw = [dict(role='user', content=text, timestamp=123, _turnId=tid)
           for tid, text in [('a', 'first'), ('b', 'second')]]
    messages = _transform_messages(raw, {})
    assert [m['content'] for m in messages] == ['first', 'second']
    assert count_messages(messages).human_messages == 2
    assert 'first' in apply_wire_sanitize(messages)[0]['content']


def test_repeated_same_turn_is_deduplicated():
    raw = [dict(role='user', content=text, timestamp=123, _turnId='a')
           for text in ['optimistic', 'authoritative']]
    assert [m['content'] for m in _transform_messages(raw, {})] == ['authoritative']


def test_automatic_and_human_messages_do_not_merge_before_budgeting():
    messages = _transform_messages([
        {'role': 'user', 'content': 'timer', '_initiator': 'timer'},
        {'role': 'user', 'content': 'human'},
    ], {})
    assert len(messages) == 2
    assert latest_user_text(messages) == 'human'
    assert derive_task_state_snapshot(messages).goal == 'human'


def test_text_block_and_native_phase_are_eligible_but_media_is_not():
    from lib.tasks_pkg.message_semantics import has_model_media
    text = {'role': 'assistant', 'content': [{'type': 'text', 'text': 'done'}],
            '_responses_items': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'done'}]}]}
    assert not has_model_media(text)
    assert has_model_media(text | {'_modelImageSources': ['tofu-media:owner-source']})
    assert has_model_media(text | {'_anthropic_content_blocks': [
        {'type': 'tool_result', 'content': [{'type': 'image', 'source': {}}]}]})


def test_same_turn_identity_wins_even_if_legacy_timestamps_differ():
    messages = _transform_messages([
        {'role': 'user', '_turnId': 'one', 'timestamp': 1, 'content': 'optimistic'},
        {'role': 'user', '_turnId': 'one', 'timestamp': 2, 'content': 'authoritative'},
    ], {})
    assert len(messages) == 1
    assert messages[0]['content'] == 'authoritative'


def test_equal_timestamps_do_not_merge_different_sources_without_turn_ids():
    source = [
        {'role': 'user', 'timestamp': 123, 'content': 'automated', '_initiator': 'timer'},
        {'role': 'user', 'timestamp': 123, 'content': 'human'},
    ]
    assert [row['content'] for row in _transform_messages(source, {})] == ['automated', 'human']


def test_explicit_identity_cannot_deduplicate_an_unidentified_row():
    source = [
        {'role': 'user', 'timestamp': 123, 'content': 'legacy'},
        {'role': 'user', 'timestamp': 123, 'content': 'known turn', '_turnId': 'a'},
    ]
    assert [row['content'] for row in _transform_messages(source, {})] == ['legacy', 'known turn']


def test_rebuild_preserves_source_without_mutating_input_or_leaking_private_fields():
    from copy import deepcopy
    raw = [
        {'role': 'user', 'content': 'first', '_turnId': 'one', '_initiator': 'operator', '_secret': 'private'},
        {'role': 'assistant', 'content': 'answer', '_turnId': 'one', '_sourceActor': 'assistant'},
        {'role': 'assistant', 'content': 'followup', '_turnId': 'two', '_sourceActor': 'assistant'},
    ]
    before = deepcopy(raw)
    rebuilt = _transform_messages(raw, {})
    assert raw == before
    assert [row['_turnId'] for row in rebuilt] == ['one', 'one', 'two']
    assert '_secret' not in rebuilt[0]
    assert all(not any(key.startswith('_') for key in row) for row in apply_wire_sanitize(rebuilt))
