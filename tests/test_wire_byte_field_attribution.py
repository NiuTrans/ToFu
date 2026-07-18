"""tests/test_wire_byte_field_attribution.py — field-level wire-byte culprit.

The dominant remaining prefix-cache miss (post the 2026-07-17 wire-freeze +
settle-gate work) is a canonical-INVISIBLE byte divergence on an already-cached
``assistant/tool_call(...)`` message: ``canonical_messages`` matches (same
tokenized content) yet the RAW serialized bytes differ round-over-round. The
live detector (``detect_cache_break``) names only the MESSAGE
(``<bytes>assistant/tool_call(read_files)``) via ``diff_byte_prefix`` — it
cannot say WHICH FIELD of that message flipped (``reasoning_details`` rebuild /
``tool_calls`` arg re-serialization / a field-insertion-order change / a
``content`` edit). That leaves the root cause a CATEGORY name, not a proof —
exactly the "many unverified things in the tracing system" the owner flagged.

``wire_byte_field_prefix`` closes that: it hashes each message's TOP-LEVEL
fields individually (only ``cache_control`` stripped, insertion order
preserved) so ``diff_byte_field_prefix`` can name the exact ``key.field`` that
diverged. The next live trigger then logs
``<bytes>assistant/tool_call(read_files){reasoning_details}`` — a proven field,
not a guessed cause.

Contract mirrors the message-level pair (``wire_byte_prefix`` /
``diff_byte_prefix``): stable-key aligned (a benign reindex does not explode),
overlapping-prefix only (fresh tail is not diffed), ``cache_control`` stripped
(the rolling marker is not a culprit).

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/test_wire_byte_field_attribution.py -p no:cacheprovider
"""

import copy
import json

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg.wire_fingerprint import (
    canonical_messages,
    diff_byte_field_prefix,
    diff_byte_prefix,
    diff_canonical,
    wire_byte_field_prefix,
    wire_byte_prefix,
)


def _asst_tool_call(args_str, *, reasoning_details=None, content='calling',
                    reasoning='thinking', sig='SIG'):
    m = {'role': 'assistant', 'content': content,
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'read_files',
                                      'arguments': args_str}}]}
    if reasoning:
        m['reasoning_content'] = reasoning
    if sig:
        m['thinking_signature'] = sig
    if reasoning_details is not None:
        m['reasoning_details'] = reasoning_details
    return m


# ═══════════════════════════════════════════════════════════════════════════
#  CATCH: the exact field that byte-diverged is NAMED
# ═══════════════════════════════════════════════════════════════════════════

def test_reasoning_details_rebuild_named_as_field_culprit():
    """A canonical-invisible ``reasoning_details`` rebuild (same
    reasoning_content+signature, different reasoning_details bytes) is the
    prime dominant-miss suspect. The message-level diff names only the message;
    the field-level diff must name ``{reasoning_details}`` specifically."""
    old = [{'role': 'user', 'content': 'go'},
           _asst_tool_call('{"path":"a.py"}',
                           reasoning_details=[{'type': 'thinking',
                                               'thinking': 'thinking',
                                               'signature': 'SIG'}])]
    new = copy.deepcopy(old)
    # Rebuild ONLY reasoning_details on the assistant turn — canonical-blind.
    new[1]['reasoning_details'] = [{'type': 'thinking', 'thinking': 'thinking',
                                    'signature': 'SIG', 'id': 'rd_1'}]

    # Canonical is blind (proves this is the canonical-invisible class).
    assert diff_canonical(canonical_messages(old), canonical_messages(new)) == []
    # Message-level byte diff sees THAT it changed, but not which field.
    msg_culprits = diff_byte_prefix(wire_byte_prefix(old), wire_byte_prefix(new))
    assert msg_culprits and all('{' not in c for c in msg_culprits)
    # Field-level diff names the EXACT field.
    field_culprits = diff_byte_field_prefix(
        wire_byte_field_prefix(old), wire_byte_field_prefix(new))
    assert any(c.endswith('{reasoning_details}') for c in field_culprits), (
        f'expected a {{reasoning_details}} field culprit, got {field_culprits}')


def test_tool_call_arg_reserialization_named():
    """The other dominant suspect: ``arguments`` re-serialized with different
    whitespace / ensure_ascii escaping (raw non-ASCII → ``\\uXXXX``) while the
    parsed object is identical — canonical erases it (sort_keys re-dump), the
    field-level byte diff names ``{tool_calls}``."""
    raw_utf8 = '{"command": "git log", "description": "\u67e5\u770b\u63d0\u4ea4"}'
    escaped = json.dumps(json.loads(raw_utf8))  # ensure_ascii=True → \uXXXX
    old = [_asst_tool_call(raw_utf8)]
    new = [_asst_tool_call(escaped)]

    # Canonical erases the re-serialization (proves the class).
    assert diff_canonical(canonical_messages(old), canonical_messages(new)) == []
    field_culprits = diff_byte_field_prefix(
        wire_byte_field_prefix(old), wire_byte_field_prefix(new))
    assert any(c.endswith('{tool_calls}') for c in field_culprits), (
        f'expected a {{tool_calls}} field culprit, got {field_culprits}')


def test_anthropic_input_dict_arg_escaping_named():
    """On the Anthropic wire the tool arg lives as ``input`` (a dict). A
    per-round re-serialization difference (ensure_ascii on the transport) is
    the real production shape. Naming ``{tool_calls}`` (OpenAI) or the
    Anthropic ``content`` block is enough to localize — assert the field diff
    is NON-empty and message-scoped, not a false whole-message-only report."""
    old = [{'role': 'assistant',
            'content': [{'type': 'tool_use', 'id': 'c1', 'name': 'read_files',
                         'input': {'command': '\u67e5\u770b'}}]}]
    new = [{'role': 'assistant',
            'content': [{'type': 'tool_use', 'id': 'c1', 'name': 'read_files',
                         # same object, but stored differently on the wire
                         'input': {'command': '\u67e5\u770b', '_pad': None}}]}]
    field_culprits = diff_byte_field_prefix(
        wire_byte_field_prefix(old), wire_byte_field_prefix(new))
    assert field_culprits and any('{content}' in c for c in field_culprits)


def test_field_insertion_order_flip_named():
    """A pure field-INSERTION-ORDER flip (same keys+values, different order)
    changes the serialized bytes but nothing semantic. The field-level diff
    reports a stable ``{__order__}`` culprit so this class is named, not
    laundered into an eviction."""
    old = [{'role': 'assistant', 'content': 'x', 'reasoning_content': 't',
            'thinking_signature': 's'}]
    # Same fields+values, different insertion order.
    new = [{'role': 'assistant', 'thinking_signature': 's',
            'reasoning_content': 't', 'content': 'x'}]
    field_culprits = diff_byte_field_prefix(
        wire_byte_field_prefix(old), wire_byte_field_prefix(new))
    assert any('{__order__}' in c for c in field_culprits), (
        f'expected an ordering culprit, got {field_culprits}')


# ═══════════════════════════════════════════════════════════════════════════
#  ERASE / STABILITY: benign transforms produce NO field culprit
# ═══════════════════════════════════════════════════════════════════════════

def test_cache_control_marker_not_a_field_culprit():
    """The rolling tail ``cache_control`` marker is the ONE legitimately-mobile
    element — it must be stripped, never a field culprit."""
    old = [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}]
    new = [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi',
                                         'cache_control': {'type': 'ephemeral'}}]}]
    assert diff_byte_field_prefix(
        wire_byte_field_prefix(old), wire_byte_field_prefix(new)) == []


def test_identical_messages_no_culprit():
    msgs = [{'role': 'user', 'content': 'go'},
            _asst_tool_call('{"path":"a.py"}')]
    assert diff_byte_field_prefix(
        wire_byte_field_prefix(msgs),
        wire_byte_field_prefix(copy.deepcopy(msgs))) == []


def test_fresh_tail_not_diffed():
    """The caller slices to the SHARED prefix before diffing (this round
    appends a fresh tail we do not diff), mirroring ``detect_cache_break``'s
    ``prev.wire_bytes[:shared]`` usage. Sliced that way, an appended tail
    yields NO field culprit — the prior prefix is untouched."""
    old = [{'role': 'user', 'content': 'go'}]
    new = [{'role': 'user', 'content': 'go'},
           _asst_tool_call('{"path":"a.py"}')]
    bo = wire_byte_field_prefix(old)
    bn = wire_byte_field_prefix(new)
    shared = len(bo)  # only the region that existed last round is a prefix now
    assert diff_byte_field_prefix(bo[:shared], bn[:shared]) == []


# ═══════════════════════════════════════════════════════════════════════════
#  NEUTER: prove the field granularity is load-bearing
# ═══════════════════════════════════════════════════════════════════════════

def test_nc_message_level_diff_cannot_name_the_field():
    """NEUTER / negative control: the message-level ``diff_byte_prefix`` (the
    CURRENT production instrument) reports the divergence but is STRUCTURALLY
    incapable of naming the field — its culprits never carry a ``{field}``
    suffix. This proves the field-level function adds real attribution, not a
    cosmetic rename. If message-level ever starts naming fields, this control
    must be revisited."""
    old = [_asst_tool_call('{"path":"a.py"}',
                           reasoning_details=[{'type': 'thinking', 'v': 1}])]
    new = copy.deepcopy(old)
    new[0]['reasoning_details'] = [{'type': 'thinking', 'v': 2}]
    msg_culprits = diff_byte_prefix(wire_byte_prefix(old), wire_byte_prefix(new))
    assert msg_culprits, 'divergence should be detected at the message level'
    assert all('{' not in c for c in msg_culprits), (
        'message-level diff must NOT carry field granularity — that is the '
        'gap the field-level function fills')


def test_nc_field_hash_reduction_would_hide_the_field():
    """NEUTER: if ``wire_byte_field_prefix`` collapsed a message to a single
    whole-message hash (i.e. behaved like ``wire_byte_prefix``), the field diff
    would degrade to a message-only culprit with no ``{field}``. Assert the
    per-field structure is present so a future refactor that flattens it fails
    here."""
    entry = wire_byte_field_prefix([_asst_tool_call('{"path":"a.py"}')])[0]
    assert 'fields' in entry and isinstance(entry['fields'], dict), (
        'wire_byte_field_prefix must expose a per-field hash map; a single '
        'whole-message hash would defeat field attribution')
    # The tool-bearing assistant turn must decompose into >1 field.
    assert len(entry['fields']) >= 3, (
        f'expected multiple top-level fields, got {list(entry["fields"])}')
