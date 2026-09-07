"""tests/test_cache_content_wrap_invariance.py — the {content} byte-flip fix.

THE dominant residual prefix-cache floor-miss (field-level tracer, commit
a353842, proved it): an ``assistant/tool_call(run_command)`` turn carrying
NON-EMPTY prose ``content`` flips its serialized bytes round-over-round while
its canonical fingerprint is unchanged — logged as
``WIRE BYTES DIVERGED while canonical matched … field=[…(run_command){content}]``.

Root cause (proven end-to-end):
``add_cache_breakpoints`` Phase 0.5 ("representation invariance") normalizes a
markable ``str`` content into the single-block ``[{"type":"text",…}]`` form so
that ADDING/REMOVING the rolling tail marker only toggles a ``cache_control``
key on an already-list block — never flips ``str`` ↔ ``list``. But it carved
out ``assistant`` turns that carry ``tool_calls``
(``… and not msg.get('tool_calls')``). So a ``run_command`` turn with prose:
  * as the TAIL round → the tail phase wraps its ``str`` content into a block;
  * as a BURIED prefix round the next turn → left a bare ``str``.
That str↔block flip is exactly the ``str`` ↔ single-text-block wrapping the
canonical fingerprint deliberately erases (so canonical says "identical") while
the real bytes differ → a fresh ``{content}`` divergence every round on the
turn that just left the tail → the cached prefix cannot extend.

The carve-out's stated fear ("content is usually empty; the tool_use blocks
drive the marker logic") only holds for EMPTY content. The fix normalizes the
turn ONLY when content is a non-empty ``str`` — empty-content tool_call turns
are still left alone, so the ``_assistant_blocks`` last-block marker path is
undisturbed.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/test_cache_content_wrap_invariance.py -p no:cacheprovider
"""

import copy
import json

import pytest

pytestmark = pytest.mark.unit

from lib.llm.cache import add_cache_breakpoints

MODEL = 'aws.claude-opus-4.8'


def _tools():
    return [{'type': 'function',
             'function': {'name': 'run_command', 'description': 'run',
                          'parameters': {'type': 'object'}}}]


def _run_command_turn(prose='Let me check the logs.'):
    return {'role': 'assistant', 'content': prose,
            'tool_calls': [{'id': 'c1', 'type': 'function',
                            'function': {'name': 'run_command',
                                         'arguments': '{"command": "ls"}'}}]}


def _annotate(messages):
    body = {'model': MODEL, 'messages': copy.deepcopy(messages),
            'max_tokens': 100, 'tools': copy.deepcopy(_tools())}
    add_cache_breakpoints(body, log_prefix='')
    return body['messages']


def _content_shape(msg):
    c = msg.get('content')
    return 'list' if isinstance(c, list) else ('str' if isinstance(c, str) else type(c).__name__)


def _first_asst(messages):
    for m in messages:
        if m.get('role') == 'assistant':
            return m
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  THE FIX: content representation is invariant to tail-marker position
# ═══════════════════════════════════════════════════════════════════════════

def test_run_command_content_shape_invariant_tail_vs_buried():
    """A run_command turn with prose content must serialize its ``content`` to
    the SAME representation (single-text-block) whether it is the tail (marker
    on) or a buried prefix round (marker off). Before the fix, buried→str /
    tail→list flips the bytes → the {content} floor-miss."""
    head = [{'role': 'system', 'content': 'S' * 60},
            {'role': 'user', 'content': 'go'}]

    # TAIL: the run_command turn is the last message (gets the tail marker).
    tail_msgs = _annotate(head + [_run_command_turn()])
    # BURIED: many later turns push the run_command turn deep into the prefix.
    buried = head + [_run_command_turn(), {'role': 'tool', 'tool_call_id': 'c1',
                                           'content': 'r'}]
    for k in range(6):
        buried += [{'role': 'assistant', 'content': f'step {k}',
                    'tool_calls': [{'id': f't{k}', 'type': 'function',
                                    'function': {'name': 'run_command',
                                                 'arguments': '{}'}}]},
                   {'role': 'tool', 'tool_call_id': f't{k}', 'content': 'x'}]
    buried_msgs = _annotate(buried)

    tail_asst = _first_asst(tail_msgs)
    buried_asst = _first_asst(buried_msgs)
    assert tail_asst is not None and buried_asst is not None

    assert _content_shape(tail_asst) == _content_shape(buried_asst), (
        f'run_command content shape flips with tail position: '
        f'tail={_content_shape(tail_asst)} buried={_content_shape(buried_asst)} '
        '— this is the canonical-invisible str↔block flip that re-bills the '
        'cached prefix every round.')


def test_run_command_content_bytes_stable_stripping_marker():
    """The DIRECT byte assertion: with cache_control stripped (the only
    legitimately-mobile key), a run_command turn's ``content`` is byte-identical
    in the tail and buried positions — no str↔block flip survives."""
    head = [{'role': 'system', 'content': 'S' * 60},
            {'role': 'user', 'content': 'go'}]
    tail_msgs = _annotate(head + [_run_command_turn()])
    buried = head + [_run_command_turn(), {'role': 'tool', 'tool_call_id': 'c1',
                                           'content': 'r'},
                     {'role': 'user', 'content': 'next'}]
    buried_msgs = _annotate(buried)

    def _content_bytes(msg):
        c = msg.get('content')
        # Strip cache_control (mobile marker) from any block form.
        if isinstance(c, list):
            c = [{k: v for k, v in b.items() if k != 'cache_control'}
                 if isinstance(b, dict) else b for b in c]
        return json.dumps(c, ensure_ascii=False, sort_keys=False)

    tb = _content_bytes(_first_asst(tail_msgs))
    bb = _content_bytes(_first_asst(buried_msgs))
    assert tb == bb, (
        f'run_command content bytes differ tail vs buried:\n tail  ={tb}\n '
        f'buried={bb}\nThe str↔block flip changes the wire bytes for an '
        'already-cached turn.')


# ═══════════════════════════════════════════════════════════════════════════
#  PRESERVE: empty-content tool_call turns are still left alone
# ═══════════════════════════════════════════════════════════════════════════

def test_empty_content_tool_call_turn_not_wrapped():
    """The carve-out's valid case: an assistant tool_call turn with EMPTY (or
    absent) content must NOT gain a fabricated ``[{type:text,text:''}]`` block
    — the tool_use blocks drive the marker logic and a wrap would disturb it.
    The fix only normalizes NON-EMPTY str content."""
    head = [{'role': 'system', 'content': 'S' * 60},
            {'role': 'user', 'content': 'go'}]
    empty_turn = {'role': 'assistant',  # no 'content' key at all
                  'tool_calls': [{'id': 'c1', 'type': 'function',
                                  'function': {'name': 'run_command',
                                               'arguments': '{}'}}]}
    msgs = _annotate(head + [empty_turn, {'role': 'tool', 'tool_call_id': 'c1',
                                          'content': 'r'},
                             {'role': 'user', 'content': 'next'}])
    asst = _first_asst(msgs)
    # content must remain absent/empty — NOT a synthesized text block.
    c = asst.get('content')
    assert not (isinstance(c, list) and c), (
        f'empty-content tool_call turn gained a fabricated content block: {c}')


# ═══════════════════════════════════════════════════════════════════════════
#  NEUTER: prove the invariance is load-bearing
# ═══════════════════════════════════════════════════════════════════════════

def test_nc_flip_is_real_without_the_fix():
    """NEUTER / negative control: reproduce the PRE-FIX behaviour by directly
    building the two shapes the old carve-out produced (buried=str, tail=block)
    and assert they are canonical-INVISIBLE yet byte-DIVERGENT — proving the
    flip the fix removes is real and is exactly the {content} floor-miss.

    If canonical ever starts SEEING this (returns a culprit), the str↔block
    erase was removed and the whole premise changed — revisit."""
    from lib.tasks_pkg.wire_fingerprint import (
        canonical_messages, diff_canonical,
        wire_byte_field_prefix, diff_byte_field_prefix)

    buried = [{'role': 'assistant', 'content': 'Let me check the logs.',
               'tool_calls': [{'id': 'c1', 'type': 'function',
                               'function': {'name': 'run_command',
                                            'arguments': '{}'}}]}]
    tail = [{'role': 'assistant',
             'content': [{'type': 'text', 'text': 'Let me check the logs.'}],
             'tool_calls': [{'id': 'c1', 'type': 'function',
                             'function': {'name': 'run_command',
                                          'arguments': '{}'}}]}]

    # Canonical is BLIND to the str↔block wrap (the false "identical").
    assert diff_canonical(canonical_messages(buried),
                          canonical_messages(tail)) == [], (
        'str↔block wrap should be canonical-invisible; if canonical now sees '
        'it, the premise changed')
    # Field-byte diff NAMES {content} (the real divergence the tracer logs).
    fc = diff_byte_field_prefix(wire_byte_field_prefix(buried),
                                wire_byte_field_prefix(tail))
    assert any(c.endswith('{content}') for c in fc), (
        f'expected a {{content}} field culprit for the str↔block flip, got {fc}')
