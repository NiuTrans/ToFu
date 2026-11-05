"""tests/cache/test_cache_prefill_conversion_no_marker.py — prefill-conversion cache safety.

The THIRD live→replay drift on the prefix-cache path (after str↔block wrap
ab161bf and raw↔stripped 1274cee). This one is RARE but real: it fires only
when a request genuinely ends on a bare assistant turn (regenerate-from-a-past-
assistant, resume/continue, interrupted-turn recovery) — NOT on the normal
``[…, assistant, user]`` shape (post-restart production: 1 conversion / 167
requests). But when it DOES fire it is a full prefix break, so "flawless" needs
it closed.

Mechanism (proven end-to-end on real mrojzb3t):
``_strip_trailing_assistant_for_claude`` converts a TRAILING bare assistant turn
into ``{'role':'user','content':'[Your previous response for context]:\\n'+X}``
(Anthropic rejects a conversation ending on assistant). The SAME turn, once the
user sends the next message, is BURIED → NOT converted → sent as
``{'role':'assistant','content':X}``. So the turn flips BOTH ``role`` and
``content`` between its tail round and its buried round.

That alone would be a tail-only cost (the tail is volatile), EXCEPT
``add_cache_breakpoints`` places the tail cache breakpoint ON the converted turn
(verified: idx marked in round N) — so round N WRITES a cache entry covering the
converted ``user`` form that round N+1's buried ``assistant`` form cannot read
back.

FIX: the prefill-converted turn is a volatile synthetic representation whose
bytes are guaranteed round-dependent — ``add_cache_breakpoints`` must NEVER
anchor a cache breakpoint on it. The tail/mid marker loops skip it and fall back
to the previous stable turn. Cost: at most that one turn's tokens re-billed for
the single round it was the tail; the turn caches cleanly (as assistant) from
the next round on.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/cache/test_cache_prefill_conversion_no_marker.py -p no:cacheprovider
"""

import copy

import pytest

pytestmark = pytest.mark.unit

from lib.llm.cache import add_cache_breakpoints
from lib.llm.body._model_tweaks import (
    CLAUDE_PREFILL_SENTINEL, _strip_trailing_assistant_for_claude)

MODEL = 'aws.claude-opus-4.8'


def _has_marker(msg):
    c = msg.get('content')
    if isinstance(c, list):
        return any(isinstance(b, dict) and b.get('cache_control') for b in c)
    return False


def _text_of(msg):
    c = msg.get('content')
    if isinstance(c, str):
        return c
    if isinstance(c, list) and c and isinstance(c[0], dict):
        return c[0].get('text', '')
    return ''


# ═══════════════════════════════════════════════════════════════════════════
#  The drift is real: converted (tail) vs buried differ in role AND content
# ═══════════════════════════════════════════════════════════════════════════

def test_prefill_conversion_flips_role_and_content():
    """Document the drift: the same turn is user+sentinel when trailing,
    assistant+bare when buried."""
    turn = {'role': 'assistant', 'content': 'Here is my analysis.'}
    tail = [{'role': 'user', 'content': 'go'}, copy.deepcopy(turn)]
    tail = _strip_trailing_assistant_for_claude(tail, MODEL)      # turn is the tail
    buried = [{'role': 'user', 'content': 'go'}, copy.deepcopy(turn),
              {'role': 'user', 'content': 'next'}]
    buried = _strip_trailing_assistant_for_claude(buried, MODEL)    # turn is buried

    conv = tail[1]
    still = buried[1]
    assert conv['role'] == 'user' and conv['content'].startswith(
        CLAUDE_PREFILL_SENTINEL)
    assert still['role'] == 'assistant' and still['content'] == 'Here is my analysis.'
    # role + content both differ → a prefix break IF the converted turn is cached.
    assert conv != still


# ═══════════════════════════════════════════════════════════════════════════
#  THE FIX: no cache breakpoint lands on a prefill-converted turn
# ═══════════════════════════════════════════════════════════════════════════

def test_no_marker_on_prefill_converted_tail():
    """When the trailing turn is a prefill-converted assistant→user, the tail
    breakpoint must NOT land on it (its form is round-dependent). It should
    fall back to the previous stable turn instead."""
    msgs = [{'role': 'system', 'content': 'S' * 60},
            {'role': 'user', 'content': 'question one'},
            {'role': 'assistant', 'content': 'stable earlier answer'},
            {'role': 'user', 'content': 'question two'},
            {'role': 'assistant', 'content': 'the trailing answer being regenerated'}]
    msgs = _strip_trailing_assistant_for_claude(msgs, MODEL)   # last → user+sentinel
    assert msgs[-1]['role'] == 'user' and _text_of(msgs[-1]).startswith(
        CLAUDE_PREFILL_SENTINEL)

    body = {'model': MODEL, 'messages': msgs, 'max_tokens': 100, 'stream': True}
    add_cache_breakpoints(body, log_prefix='[test]')

    conv_turn = body['messages'][-1]
    assert not _has_marker(conv_turn), (
        'the prefill-converted volatile turn must NOT carry a cache breakpoint '
        '— its bytes flip (user+sentinel → buried assistant) next round, so '
        'caching it writes an entry the next round cannot read back.')


def test_marker_falls_back_to_stable_turn():
    """With the converted tail skipped, the tail marker must still be placed —
    on the previous stable (non-converted) turn — so caching is not disabled."""
    msgs = [{'role': 'system', 'content': 'S' * 60},
            {'role': 'user', 'content': 'q1'},
            {'role': 'assistant', 'content': 'stable answer that stays put'},
            {'role': 'user', 'content': 'q2 with enough text to be a real turn'},
            {'role': 'assistant', 'content': 'regenerated trailing answer'}]
    msgs = _strip_trailing_assistant_for_claude(msgs, MODEL)
    body = {'model': MODEL, 'messages': msgs, 'max_tokens': 100, 'stream': True}
    add_cache_breakpoints(body, log_prefix='[test]')
    # SOME message still carries a breakpoint (caching not disabled), just not
    # the converted turn.
    assert any(_has_marker(m) for m in body['messages']), (
        'skipping the converted turn must not disable caching entirely — a '
        'breakpoint should fall back to a stable turn')


# ═══════════════════════════════════════════════════════════════════════════
#  NEUTER: prove the skip is load-bearing
# ═══════════════════════════════════════════════════════════════════════════

def test_nc_without_skip_marker_lands_on_converted_turn():
    """NEUTER: a normal (NON-converted) trailing assistant turn DOES get the
    tail marker — proving the skip is specific to the prefill sentinel, not a
    blanket 'never mark the tail'. If this ever stops marking, the tail cache
    is broken for the common case."""
    msgs = [{'role': 'system', 'content': 'S' * 60},
            {'role': 'user', 'content': 'q1'},
            {'role': 'user', 'content': 'ordinary trailing user turn'}]
    body = {'model': MODEL, 'messages': msgs, 'max_tokens': 100, 'stream': True}
    add_cache_breakpoints(body, log_prefix='[test]')
    # The ordinary trailing user turn (no sentinel) MUST be markable.
    assert _has_marker(body['messages'][-1]), (
        'an ordinary trailing turn must still receive the tail breakpoint — '
        'the skip must be specific to the prefill-converted sentinel')


# ═══════════════════════════════════════════════════════════════════════════
#  Block (list) content on the trailing assistant turn
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('content, text', [
    ([{'type': 'text', 'text': 'partial answer'}], 'partial answer'),
    ([{'type': 'text', 'text': 'partial'}, {'type': 'text', 'text': ' answer'}],
     'partial answer'),
    ([{'type': 'thinking', 'thinking': 'scratch'}, {'type': 'text', 'text': 'kept'}],
     'kept'),
])
def test_trailing_block_assistant_converts_its_text_like_a_string_turn(content, text):
    """A trailing assistant turn whose content is a list of blocks converts to
    the same sentinel user turn as its text would; it must not crash request
    preparation. Only text parts can stand in a user turn."""
    msgs = [{'role': 'user', 'content': 'q1'},
            {'role': 'assistant', 'content': content}]
    original = copy.deepcopy(msgs)
    result = _strip_trailing_assistant_for_claude(msgs, MODEL)
    assert msgs == original
    assert result[:-1] == msgs[:-1]
    assert result[-1] == {'role': 'user',
                          'content': f'{CLAUDE_PREFILL_SENTINEL}\n{text}'}
    assert result[-1] == _strip_trailing_assistant_for_claude(
        [msgs[0], {'role': 'assistant', 'content': text}], MODEL)[-1]


@pytest.mark.parametrize('content', [
    [],
    [{'type': 'text', 'text': '   '}],
    [{'type': 'thinking', 'thinking': 'scratch only'}],
])
def test_trailing_block_assistant_without_text_is_stripped_like_an_empty_turn(content):
    msgs = [{'role': 'user', 'content': 'q1'},
            {'role': 'assistant', 'content': content}]
    assert _strip_trailing_assistant_for_claude(msgs, MODEL) == [msgs[0]]


def test_converted_block_turn_never_carries_a_cache_breakpoint():
    msgs = [{'role': 'system', 'content': 'S' * 60},
            {'role': 'user', 'content': 'q1'},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'partial'}]}]
    body = {'model': MODEL, 'max_tokens': 100, 'stream': True,
            'messages': _strip_trailing_assistant_for_claude(msgs, MODEL)}
    add_cache_breakpoints(body, log_prefix='[test]')
    assert _text_of(body['messages'][-1]).startswith(CLAUDE_PREFILL_SENTINEL)
    assert not _has_marker(body['messages'][-1])
