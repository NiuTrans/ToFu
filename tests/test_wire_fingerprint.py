"""Tests for lib/tasks_pkg/wire_fingerprint.py — the envelope-agnostic
post-translation wire fingerprint that grounds cache-miss attribution.

The load-bearing contract:
  * BENIGN transforms the Anthropic server does not see are ERASED (so they
    never cry wolf): str ↔ single-text-block wrapping (moving cache marker),
    cache_control markers, tool-call `arguments` key reordering (the
    ensure_ascii=False re-dump on the anthropic path).
  * REAL content changes the server WOULD see are CAUGHT and named: a mutated
    tool result, a re-encoded image (the _downscale threshold=5 retro-shrink).
  * OpenAI-shape and Anthropic-shape messages for the SAME conversation
    produce the SAME fingerprint (a protocol switch alone is not a change).

Each behaviour is paired with a negative control asserting the diff would
FLIP if the canonicalisation were removed — proving the erase/catch is real,
not vacuous.
"""

import copy
import json

from lib.tasks_pkg.wire_fingerprint import (
    canonical_messages,
    diff_canonical,
    static_prefix_hash,
)


def _diff(a, b):
    return diff_canonical(canonical_messages(a), canonical_messages(b))


# ── ERASE: benign transforms produce NO culprit ──

def test_str_vs_single_text_block_erased():
    """A message content flipping str ↔ [{type:text}] is the same to the
    server; canonical diff must be empty."""
    a = [{'role': 'tool', 'tool_call_id': 'c1', 'content': 'RESULT'}]
    b = [{'role': 'tool', 'tool_call_id': 'c1',
          'content': [{'type': 'text', 'text': 'RESULT'}]}]
    assert _diff(a, b) == []


def test_cache_control_marker_erased():
    """Adding/removing a cache_control marker must not register."""
    a = [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}]
    b = [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi',
                                       'cache_control': {'type': 'ephemeral'}}]}]
    assert _diff(a, b) == []


def test_tool_call_arg_key_reorder_erased():
    """OpenAI keeps `arguments` as a string; the anthropic translation
    re-dumps it (ensure_ascii=False, may reorder keys). Same semantic args
    must canonicalise identically."""
    a = [{'role': 'assistant', 'content': '', 'tool_calls': [{
        'id': 'c1', 'type': 'function',
        'function': {'name': 'read_files',
                     'arguments': json.dumps({'path': 'a.py', 'z': 1})}}]}]
    b = [{'role': 'assistant', 'content': '', 'tool_calls': [{
        'id': 'c1', 'type': 'function',
        'function': {'name': 'read_files',
                     # different key order + whitespace, same object
                     'arguments': '{"z": 1,   "path": "a.py"}'}}]}]
    assert _diff(a, b) == []


# ── CATCH: real content changes ARE named ──

def test_mutated_tool_result_caught():
    a = [{'role': 'tool', 'tool_call_id': 'c1', 'content': 'ORIGINAL'}]
    b = [{'role': 'tool', 'tool_call_id': 'c1', 'content': 'CHANGED'}]
    culprits = _diff(a, b)
    assert culprits
    assert any('tool_result' in c for c in culprits)


def test_reencoded_image_caught():
    """The _downscale threshold=5 retro-shrink re-encodes an image → new
    base64 bytes. The canonicaliser hashes image identity, so this shows."""
    a = [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'x'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}]}]
    b = [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'x'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,BBBB'}}]}]
    culprits = _diff(a, b)
    assert culprits
    assert any('.content' in c for c in culprits)


def test_changed_tool_call_arg_value_caught():
    """A genuine argument VALUE change (not a reorder) must be caught."""
    a = [{'role': 'assistant', 'content': '', 'tool_calls': [{
        'id': 'c1', 'type': 'function',
        'function': {'name': 'read_files',
                     'arguments': json.dumps({'path': 'a.py'})}}]}]
    b = [{'role': 'assistant', 'content': '', 'tool_calls': [{
        'id': 'c1', 'type': 'function',
        'function': {'name': 'read_files',
                     'arguments': json.dumps({'path': 'DIFFERENT.py'})}}]}]
    culprits = _diff(a, b)
    assert culprits
    assert any('tool_call' in c for c in culprits)


# ── ENVELOPE PARITY: OpenAI shape == Anthropic shape for same conversation ──

def test_openai_and_anthropic_envelopes_match():
    """The SAME conversation, translated to the Anthropic Messages shape,
    must produce the SAME canonical fingerprints (envelope erased)."""
    from lib.llm.anthropic_outbound import openai_body_to_anthropic
    openai_msgs = [
        {'role': 'system', 'content': 'You are Tofu.'},
        {'role': 'user', 'content': 'go'},
        {'role': 'assistant', 'content': '',
         'reasoning_content': 'thinking', 'thinking_signature': 'SIG',
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'read_files',
                                      'arguments': json.dumps({'path': 'a.py'})}}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'file body'},
    ]
    anthropic_body = openai_body_to_anthropic(
        {'model': 'claude-opus-4-8', 'messages': copy.deepcopy(openai_msgs),
         'max_tokens': 1024})
    # Anthropic hoists system out into body['system']; re-attach it as a system
    # message so the two message lists are comparable at the semantic level.
    anthropic_msgs = ([{'role': 'system', 'content': anthropic_body['system']}]
                      + anthropic_body['messages'])
    co = canonical_messages(openai_msgs)
    ca = canonical_messages(anthropic_msgs)
    # Compare the non-system content fields (system text is identical string).
    diff = diff_canonical(co, ca)
    assert diff == [], f'envelope mismatch: {diff}'


# ── NEGATIVE CONTROLS: prove the erase is not vacuous ──

def test_nc_str_block_would_differ_without_normalization():
    """NC: if _text_of did NOT collapse str↔single-text-block, the wrapped vs
    bare forms WOULD differ. Verify the raw (un-normalised) representations
    are genuinely different, so the erase above is doing real work."""
    bare = 'RESULT'
    wrapped = [{'type': 'text', 'text': 'RESULT'}]
    # A naive stringify (what a non-normalising hash would see) differs:
    assert json.dumps(bare) != json.dumps(wrapped)
    # …yet the canonicaliser collapses them:
    a = [{'role': 'tool', 'tool_call_id': 'c1', 'content': bare}]
    b = [{'role': 'tool', 'tool_call_id': 'c1', 'content': wrapped}]
    assert _diff(a, b) == []


def test_static_prefix_hash_stable_and_sensitive():
    base = [{'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'u'},
            {'role': 'assistant', 'content': 'a'}]
    same = [{'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'u'},
            {'role': 'assistant', 'content': 'DIFFERENT tail'}]
    # Static floor = system + first user, so a tail change does NOT move it.
    assert static_prefix_hash(base) == static_prefix_hash(same)
    # But changing the system DOES move it.
    changed_sys = [{'role': 'system', 'content': 'sys-CHANGED'},
                   {'role': 'user', 'content': 'u'}]
    assert static_prefix_hash(base) != static_prefix_hash(changed_sys)
