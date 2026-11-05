"""A prefix-mutation verdict must name the real culprit and the real bill.

Incident mvywcu5qqy5ocy round 3 (Claude over the Anthropic Messages wire): L1
compaction replaced the second of three image tool results inside the previous
round's merged tool-result user message. Provider usage was read 40,968 /
write 4,076, yet the round was reported as a whole-body re-bill blamed on the
merged message's first block, and the change in the previous round's final
wire message was judged "inside the prior cached prefix" because a wire index
was compared with an API-form message count.

The detector runs exactly as live: API-form messages drive the source
fallback while ``usage`` carries the post-translation Anthropic wire evidence.
"""
from __future__ import annotations

import copy
import uuid

import pytest

from lib.llm.anthropic_outbound import openai_body_to_anthropic
from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
from lib.tasks_pkg.cache_tracking._policy import (
    BUCKET_BODY_CHANGE,
    _resolve_break_cause,
    classify_verdict,
)
from lib.tasks_pkg.cache_tracking._state import (
    CacheState,
    _cache_states,
    _state_key,
)
from lib.tasks_pkg.cache_tracking._wire import (
    _wire_evidence_from_usage,
    compare_wire_evidence,
)
from lib.tasks_pkg.cache_tracking.replay import build_round_usage

pytestmark = pytest.mark.unit

_USER_ID = 1
_MODEL = 'claude-opus-4-8'
_SKILL_ID = 'toolu_01AbCd5yozUTuvU9X1'
_IMAGE_IDS = ('toolu_01EfGhKq3vB8xLm2Np', 'toolu_01IjKlZydohm1sPTGD',
              'toolu_01MnOpWt7cR4yHs9Fj')
_COMPACTED = '[image omitted by context compaction]'
_TOOLS = [{'type': 'function', 'function': {
    'name': name, 'description': name,
    'parameters': {'type': 'object', 'properties': {}}}}
    for name in ('load_skill', 'inspect_image', 'read_files')]


def _cause(**overrides) -> str:
    arguments = dict(
        client_changes={}, prefix_mutation_break=True, elapsed=43.1,
        cache_read=40968, cache_write=4076, prefix_mutated=True,
        prefix_culprits=['tool_result(Zydohm1sPTGD).tool_result'])
    arguments.update(overrides)
    return _resolve_break_cause(**arguments)


# ── Billing clause: built from the round's provider usage ──

def test_partial_reuse_states_read_and_written_tokens():
    cause = _cause()

    assert '40968 tokens were still read from cache' in cause
    assert '4076 tokens' in cause and 'written to cache' in cause
    assert 'whole' not in cause
    assert cause.endswith('[changed: tool_result(Zydohm1sPTGD).tool_result]')


def test_zero_read_states_the_whole_prompt_was_rebilled():
    cause = _cause(cache_read=0, cache_write=45044)

    assert 'the whole prompt was re-billed uncached' in cause
    assert 'still read from cache' not in cause


def test_unmetered_write_does_not_claim_a_cache_write():
    """OpenAI-compatible routes may report reads but never writes."""
    cause = _cause(cache_write=0)

    assert '40968 tokens were still read from cache' in cause
    assert 'no cache write reported' in cause
    assert 'written to cache' not in cause
    assert 'whole' not in cause


@pytest.mark.parametrize('cache_read,cache_write',
                         [(40968, 4076), (0, 45044), (40968, 0)])
def test_billing_clause_keeps_reason_code_and_bucket(cache_read, cache_write):
    cause = _cause(cache_read=cache_read, cache_write=cache_write)

    assert classify_verdict({'prefix_mutation': cause}) == BUCKET_BODY_CHANGE


# ── Detector: block culprit + like-for-like position evidence ──

def _image_result(index):
    return [{'type': 'text', 'text': f'image {index}'},
            {'type': 'image_url',
             'image_url': {'url': f'data:image/png;base64,IMG{index}'}}]


def _tool_turn(call_ids, results):
    calls = [{'id': call_id, 'type': 'function',
              'function': {'name': 'read_files', 'arguments': '{}'}}
             for call_id in call_ids]
    return ([{'role': 'assistant', 'content': '', 'tool_calls': calls}]
            + [{'role': 'tool', 'tool_call_id': call_id, 'content': result}
               for call_id, result in zip(call_ids, results)])


def _api_history(*, compacted=(), follow_up_rounds=0):
    """System + user + one merged batch of four results + follow-up rounds."""
    images = [(_COMPACTED if call_id in compacted else _image_result(index))
              for index, call_id in enumerate(_IMAGE_IDS, start=1)]
    messages = [{'role': 'system', 'content': 'You are Tofu.'},
                {'role': 'user', 'content': 'inspect these images'}]
    messages += _tool_turn((_SKILL_ID,) + _IMAGE_IDS,
                           ['skill body'] + images)
    for index in range(follow_up_rounds):
        messages += _tool_turn([f'toolu_01FollowUp{index:08d}'],
                               [f'follow-up {index}'])
    return messages


def _wire_messages(api_messages):
    body = openai_body_to_anthropic(
        {'model': _MODEL, 'max_tokens': 1024,
         'messages': copy.deepcopy(api_messages)})
    return body


@pytest.fixture
def conversation():
    conv_id = 'attr-' + uuid.uuid4().hex[:12]
    yield conv_id
    _cache_states.pop(_state_key(conv_id, user_id=_USER_ID), None)


def _observe(conv_id, api_messages, *, cache_read, cache_write):
    usage = build_round_usage(_wire_messages(api_messages),
                              cache_read=cache_read, cache_write=cache_write)
    return detect_cache_break(
        conv_id, api_messages, _TOOLS, _MODEL, usage=usage,
        user_id=_USER_ID, durable=False, prev_turn_cache_read=0)


def test_previous_final_wire_message_edit_is_not_inside_prior_prefix(
        conversation):
    """The incident round: the edit lands in the previous round's final wire
    message and the read did not drop, so it is an editable-tail re-bill."""
    assert _observe(conversation, _api_history()[:2],
                    cache_read=0, cache_write=40968) is None
    assert _observe(conversation, _api_history(),
                    cache_read=40968, cache_write=3624) is None

    verdict = _observe(
        conversation,
        _api_history(compacted=(_IMAGE_IDS[1],), follow_up_rounds=1),
        cache_read=40968, cache_write=4076)

    assert verdict is None


def test_edit_inside_prior_prefix_names_block_and_real_billing(conversation):
    _observe(conversation, _api_history()[:2], cache_read=0, cache_write=40968)
    _observe(conversation, _api_history(follow_up_rounds=1),
             cache_read=40968, cache_write=3624)

    verdict = _observe(
        conversation,
        _api_history(compacted=(_IMAGE_IDS[1],), follow_up_rounds=2),
        cache_read=40968, cache_write=4076)

    assert verdict is not None
    cause = verdict['prefix_mutation']
    assert cause.endswith('[changed: tool_result(Zydohm1sPTGD).tool_result]')
    assert '5yozUTuvU9X1' not in cause
    assert '40968 tokens were still read from cache' in cause
    assert '4076 tokens' in cause and 'whole' not in cause
    assert classify_verdict(verdict) == BUCKET_BODY_CHANGE
    assert [(reason['code'], reason['evidence'])
            for reason in verdict['reasons']] == [
        ('prefix_mutation', 'wire_verified')]


def _prior_state(api_messages, wire_messages, *, cache_read):
    usage = build_round_usage({'messages': wire_messages},
                              cache_read=cache_read, cache_write=0)
    state = CacheState()
    state.call_count = 2
    state.message_count = len(api_messages)
    state.last_cache_read_tokens = cache_read
    state.wire_fp = usage['_wire_fp']
    state.wire_bytes = usage['_wire_bytes']
    state.wire_field_bytes = usage['_wire_field_bytes']
    return state


def _compare(previous, wire_messages, api_messages):
    usage = build_round_usage({'messages': wire_messages},
                              cache_read=40968, cache_write=4076)
    return compare_wire_evidence(
        previous, _wire_evidence_from_usage(usage), conv_id='attr-unit',
        was_compaction=False, expected_covers=False,
        expected_rewrite_index=-1, api_first_changed=-1, client_changes={},
        message_count=len(api_messages), cache_read=40968)


def test_position_boundary_counts_previous_wire_messages():
    """API form carries the hoisted system message and one message per tool
    result; the wire merges them. The boundary must use wire messages."""
    previous_api = _api_history()
    previous_wire = _wire_messages(previous_api)['messages']
    assert len(previous_api) - len(previous_wire) >= 4
    previous = _prior_state(previous_api, previous_wire, cache_read=40968)

    tail_api = _api_history(compacted=(_IMAGE_IDS[1],), follow_up_rounds=1)
    tail_edit = _compare(previous, _wire_messages(tail_api)['messages'],
                         tail_api)
    assert tail_edit.culprits == ['tool_result(Zydohm1sPTGD).tool_result']
    assert tail_edit.first_changed_index == len(previous_wire) - 1
    assert tail_edit.mutation_inside_prior_prefix is False

    head_api = _api_history(follow_up_rounds=1)
    head_api[1] = {'role': 'user', 'content': 'inspect these images again'}
    head_edit = _compare(previous, _wire_messages(head_api)['messages'],
                         head_api)
    assert head_edit.first_changed_index == 0
    assert head_edit.mutation_inside_prior_prefix is True


@pytest.mark.parametrize('from_end,inside', [(1, False), (3, True)])
def test_byte_only_position_uses_the_same_wire_boundary(from_end, inside):
    """A canonical-invisible edit falls back to the raw-byte index, which is
    in the same wire-message space as its boundary."""
    previous_api = _api_history(follow_up_rounds=1)
    previous_wire = _wire_messages(previous_api)['messages']
    previous = _prior_state(previous_api, previous_wire, cache_read=40968)
    edited_index = len(previous_wire) - from_end

    current_api = _api_history(follow_up_rounds=2)
    current_wire = _wire_messages(current_api)['messages']
    current_wire[edited_index]['content'][0]['is_error'] = False
    comparison = _compare(previous, current_wire, current_api)

    assert comparison.culprits and all(
        culprit.startswith('<bytes>') for culprit in comparison.culprits)
    assert comparison.first_changed_index == edited_index
    assert comparison.mutation_inside_prior_prefix is inside
