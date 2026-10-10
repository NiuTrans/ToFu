"""Tests for lib/tasks_pkg/wire_fingerprint.py — the envelope-agnostic
post-translation wire fingerprint that grounds cache-miss attribution.

The load-bearing contract:
  * BENIGN transforms the Anthropic server does not see are ERASED (so they
    never cry wolf): str ↔ single-text-block wrapping (moving cache marker),
    cache_control markers, tool-call `arguments` key reordering (the
    ensure_ascii=False re-dump on the anthropic path).
  * REAL content changes the server WOULD see are CAUGHT and named: a mutated
    tool result, a re-encoded image (e.g. the _downscale first-send shrink to
    the uniform 1568px cap).
  * OpenAI-shape and Anthropic-shape messages for the SAME conversation
    produce the SAME fingerprint (a protocol switch alone is not a change).

Each behaviour is paired with a negative control asserting the diff would
FLIP if the canonicalisation were removed — proving the erase/catch is real,
not vacuous.
"""

import copy
import json

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg.wire_fingerprint import (
    capture_wire_message_evidence,
    canonical_messages,
    diff_canonical,
    first_changed_byte_index,
    first_changed_index,
    static_prefix_hash,
    wire_byte_field_prefix,
    wire_byte_prefix,
)


def _diff(a, b):
    return diff_canonical(canonical_messages(a), canonical_messages(b))


def test_combined_capture_matches_all_standalone_message_fingerprints():
    """Live one-pass capture preserves every established fingerprint byte."""
    messages = [
        {'role': 'system', 'content': [
            {'type': 'text', 'text': 'policy',
             'cache_control': {'type': 'ephemeral'}}]},
        {'role': 'assistant', 'content': 'answer',
         'reasoning_details': [{'type': 'reasoning.text', 'text': 'thought'}],
         'tool_calls': [{
             'id': 'call-1', 'type': 'function',
             'function': {'name': 'read_file',
                          'arguments': '{"path":"a.py"}'},
         }]},
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'result'},
        'ignored-non-message',
    ]
    before = copy.deepcopy(messages)

    evidence = capture_wire_message_evidence(messages)

    assert evidence.canonical == canonical_messages(messages)
    assert evidence.message_bytes == wire_byte_prefix(messages)
    assert evidence.field_bytes == wire_byte_field_prefix(messages)
    assert messages == before


def test_combined_capture_strips_only_marker_bearing_messages(monkeypatch):
    """The shared owner does not traverse every marker-free message twice."""
    import lib.tasks_pkg.wire_fingerprint as fingerprint_module

    messages = [
        {'role': 'system', 'content': 'policy'},
        {'role': 'user', 'content': 'question'},
        {'role': 'assistant', 'content': [
            {'type': 'text', 'text': 'answer',
             'cache_control': {'type': 'ephemeral'}}]},
    ]
    stripped = []
    canonical_reads = []
    real_canonical_parts = fingerprint_module._canonical_fields_and_tool_key
    real_strip = fingerprint_module._strip_cache_control

    def _strip_once(message):
        if any(message is candidate for candidate in messages):
            stripped.append(message)
        return real_strip(message)

    def _canonical_parts_once(message):
        canonical_reads.append(message)
        return real_canonical_parts(message)

    monkeypatch.setattr(
        fingerprint_module, '_strip_cache_control', _strip_once)
    monkeypatch.setattr(
        fingerprint_module,
        '_canonical_fields_and_tool_key',
        _canonical_parts_once,
    )

    capture_wire_message_evidence(messages)

    assert stripped == [messages[-1]]
    assert canonical_reads == messages


def test_marker_like_text_does_not_trigger_recursive_strip(monkeypatch):
    """Only an encoded JSON key, not user text mentioning it, is a marker."""
    import lib.tasks_pkg.wire_fingerprint as fingerprint_module

    def _unexpected_strip(_message):
        raise AssertionError('marker-like user text caused a deep traversal')

    monkeypatch.setattr(
        fingerprint_module, '_strip_cache_control', _unexpected_strip)

    capture_wire_message_evidence([{
        'role': 'user',
        'content': 'literal JSON: {"cache_control": {"type": "ephemeral"}}',
    }])


def test_canonical_capture_retains_only_consumed_evidence():
    """Per-message state excludes construction-only role/preview duplicates."""
    evidence = capture_wire_message_evidence([
        {'role': 'user', 'content': 'question'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': 'call-1', 'type': 'function',
            'function': {'name': 'read_file', 'arguments': '{}'},
        }]},
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'result'},
    ])

    assert all(set(entry) == {'key', 'fields'}
               for entry in evidence.canonical)
    assert evidence.canonical[1]['key'] == 'assistant/tool_call(read_file)'
    assert evidence.canonical[2]['key'] == 'tool_result(call-1)'


def test_common_text_shapes_skip_generic_recursive_normalization(monkeypatch):
    """Known string/text-block lanes do not pay for generic shape dispatch."""
    import lib.tasks_pkg.wire_fingerprint as fingerprint_module

    def _unexpected_generic_text(_content):
        raise AssertionError('known text shape used generic normalization')

    monkeypatch.setattr(
        fingerprint_module, '_text_of', _unexpected_generic_text)

    messages = [
        {'type': 'function_call_output', 'call_id': 'call-0',
         'output': 'function result'},
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'tool result'},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'call-2',
             'content': 'anthropic result'}]},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': 'question'},
            {'type': 'input_text', 'text': 'continued'}]},
        {'role': 'assistant', 'content': 'answer'},
    ]

    for message in messages:
        fields, _tool_key = (
            fingerprint_module._canonical_fields_and_tool_key(message))
        assert fields


def test_combined_capture_serializes_each_top_level_value_once(monkeypatch):
    """Whole-message evidence reuses each complex field JSON exactly once."""
    import lib.tasks_pkg.wire_fingerprint as fingerprint_module

    message = {
        'role': 'tool',
        'tool_call_id': 'call-1',
        'content': 'result',
        'extra_content': {'provider': 'kept'},
    }
    real_encode = fingerprint_module._WIRE_JSON_ENCODER.encode
    encoded = []

    def _counted_encode(value):
        encoded.append(value)
        return real_encode(value)

    monkeypatch.setattr(
        fingerprint_module._WIRE_JSON_ENCODER, 'encode', _counted_encode)

    evidence = capture_wire_message_evidence([message])

    assert encoded == [message['extra_content']]
    assert all(type(value) is int
               for value in evidence.canonical[0]['fields'].values())
    assert type(evidence.message_bytes[0]['h']) is int
    assert all(type(value) is int
               for value in evidence.field_bytes[0]['fields'].values())


def test_combined_capture_joins_serialized_fields_without_preconcat(monkeypatch):
    """Large raw values stay as references until the one whole-message join."""
    import lib.tasks_pkg.wire_fingerprint as fingerprint_module

    real_encode = fingerprint_module.json.encoder.encode_basestring

    class _JoinOnlyString(str):
        def __add__(self, _other):
            raise AssertionError('serialized field was copied before join')

        def __radd__(self, _other):
            raise AssertionError('serialized field was copied before join')

    monkeypatch.setattr(
        fingerprint_module.json.encoder,
        'encode_basestring',
        lambda value: _JoinOnlyString(real_encode(value)),
    )

    evidence = capture_wire_message_evidence([{
        'role': 'user',
        'content': 'large payload placeholder',
        'metadata': {'source': 'research'},
    }])

    assert evidence.message_bytes and evidence.field_bytes


@pytest.mark.parametrize('value', [
    'quoted " unicode \u67e5\u770b\n',
    None,
    True,
    False,
    42,
    3.25,
    ['nested', {'value': 1}],
])
def test_wire_value_fast_path_matches_stdlib_json(value):
    """Primitive shortcuts must preserve the exact raw-byte comparison."""
    import lib.tasks_pkg.wire_fingerprint as fingerprint_module

    assert fingerprint_module._dump_wire_json_value(value) == json.dumps(
        value, ensure_ascii=False, sort_keys=False)


def test_shared_wire_json_encoder_is_parallel_reentrant():
    """The process-owned encoder keeps no payload state between callers."""
    from concurrent.futures import ThreadPoolExecutor
    import lib.tasks_pkg.wire_fingerprint as fingerprint_module

    values = [
        ['nested', {'value': index, 'unicode': '查看'}]
        for index in range(64)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        actual = list(pool.map(
            fingerprint_module._WIRE_JSON_ENCODER.encode, values))

    expected = [json.dumps(
        value, ensure_ascii=False, sort_keys=False) for value in values]
    assert actual == expected


def test_combined_capture_invalid_json_keeps_standalone_fallback_parity():
    """One malformed extension must not split combined/standalone evidence."""
    messages = [{
        'role': 'tool',
        'tool_call_id': 'call-1',
        'content': 'result',
        'invalid_extension': {'not-json'},
    }]

    evidence = capture_wire_message_evidence(messages)

    assert evidence.message_bytes == wire_byte_prefix(messages)
    assert evidence.field_bytes == wire_byte_field_prefix(messages)


def test_prepare_request_uses_one_combined_message_capture(monkeypatch):
    """The live transport boundary does not restore three history scans."""
    import lib.tasks_pkg.wire_fingerprint as fingerprint_module
    from lib.llm._sse_core import prepare_request

    real_capture = fingerprint_module.capture_wire_message_evidence
    captured = []

    def _capture(messages):
        captured.append(messages)
        return real_capture(messages)

    monkeypatch.setattr(
        fingerprint_module, 'capture_wire_message_evidence', _capture)
    monkeypatch.setattr(
        fingerprint_module, 'canonical_messages',
        lambda *_args, **_kwargs: pytest.fail(
            'prepare_request must use combined capture'))
    monkeypatch.setattr(
        fingerprint_module, 'wire_byte_prefix',
        lambda *_args, **_kwargs: pytest.fail(
            'prepare_request must use combined capture'))
    monkeypatch.setattr(
        fingerprint_module, 'wire_byte_field_prefix',
        lambda *_args, **_kwargs: pytest.fail(
            'prepare_request must use combined capture'))

    plan = prepare_request(
        {'model': 'gpt-4o', 'messages': [
            {'role': 'system', 'content': 'policy'},
            {'role': 'user', 'content': 'hello'},
        ]},
        log_prefix='[wire-evidence-test]',
        base_url='https://gateway.invalid/v1',
        api_protocol='openai',
    )

    assert len(captured) == 1
    expected = real_capture(captured[0])
    assert plan.raw_dumper.wire_fp == expected.canonical
    assert plan.raw_dumper.wire_bytes == expected.message_bytes
    assert plan.raw_dumper.wire_field_bytes == expected.field_bytes


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
    """A _downscale re-encode (shrinking an oversized image to the uniform
    1568px cap) produces new base64 bytes. The canonicaliser hashes image
    identity, so this shows."""
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


def test_byte_only_divergence_gets_honest_position():
    """The byte-aware index reports WHERE a <bytes>-only divergence landed,
    where the canonical index is blind.

    ``reasoning_details`` is intentionally NOT part of the canonical
    fingerprint (build_body synthesises it from reasoning_content/signature),
    so a message whose ONLY change is a rebuilt ``reasoning_details`` is
    canonical-identical yet byte-divergent. The canonical index must return -1
    (blind); the byte index must return the real position (here idx 1)."""
    old = [
        {'role': 'user', 'content': 'go'},
        {'role': 'assistant', 'content': 'a', 'reasoning_content': 't',
         'thinking_signature': 's',
         'reasoning_details': [{'type': 'reasoning.text', 'text': 't', 'v': 1}]},
        {'role': 'user', 'content': 'more'},
    ]
    new = copy.deepcopy(old)
    # Rebuild ONLY reasoning_details on msg[1] — canonical-invisible, byte-real.
    new[1]['reasoning_details'] = [{'type': 'reasoning.text', 'text': 't', 'v': 2}]

    co, cn = canonical_messages(old), canonical_messages(new)
    bo, bn = wire_byte_prefix(old), wire_byte_prefix(new)

    # Canonical is BLIND: no culprit, index -1.
    assert diff_canonical(co, cn) == []
    assert first_changed_index(co, cn) == -1
    # Byte-aware index finds the real position.
    assert first_changed_byte_index(bo, bn) == 1


def test_nc_canonical_index_is_blind_to_byte_only_change():
    """NEUTER: prove the byte index is load-bearing — the canonical index
    alone would collapse a byte-only divergence to -1 (→ the meaningless
    inside_prior_cached_prefix=False the fix repairs). If this ever starts
    returning a real index, canonical grew to cover reasoning_details and the
    byte fallback is no longer needed (update the caller)."""
    old = [{'role': 'assistant', 'content': 'a', 'reasoning_content': 't',
            'thinking_signature': 's',
            'reasoning_details': [{'v': 1}]}]
    new = [{'role': 'assistant', 'content': 'a', 'reasoning_content': 't',
            'thinking_signature': 's',
            'reasoning_details': [{'v': 2}]}]
    assert first_changed_index(canonical_messages(old),
                               canonical_messages(new)) == -1
    assert first_changed_byte_index(wire_byte_prefix(old),
                                    wire_byte_prefix(new)) == 0


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
