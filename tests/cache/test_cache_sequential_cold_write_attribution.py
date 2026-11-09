#!/usr/bin/env python3
"""A sequential round after a cold write is filed as an upstream miss, not a client timing race.

Recorded shape (2026-11-08, conversation worker-1e4aa7a38a9dec71cb04bbbd, claude-opus-5.5 through
your-llm-gateway.example.com): call 1 read 0 and wrote 176,464 tokens; call 2 was sent 12 s after call 1's
response ended, with identical wire bytes, key and Mt-Session-Id, read 0 and wrote 191,554. The
detector filed it as ``cache_write_unsettled``, a client-side write-visibility race that the
settle gate would fix. A cache entry is readable once its own request has started responding
(Bedrock prompt-caching guide); a sequential round is sent after the previous response ended,
and after a cold write the next round's read rate falls with the gap instead of recovering past
a settle window. Rationale and measurements: docs/LLM_COST_OPTIMIZATION.md, "Cache-break
attribution corrections".
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

PREV_COLD_WRITE = 176_464
THIS_WRITE = 191_554


def _round_pair(conv, second_extra=None):
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
    from lib.tasks_pkg.cache_tracking._state import _cache_states
    from lib.tasks_pkg.wire_fingerprint import (
        canonical_messages, routing_fingerprint, static_prefix_hash, wire_byte_prefix,
    )
    _cache_states.clear()
    msgs = [{'role': 'system', 'content': 'STATIC WORKER SYSTEM'},
            {'role': 'user', 'content': 'implement the assignment'},
            {'role': 'assistant', 'content': 'reading the owning module'}]
    wire = {'_wire_fp': canonical_messages(msgs), '_wire_static': static_prefix_hash(msgs),
            '_wire_bytes': wire_byte_prefix(msgs),
            '_wire_routing': routing_fingerprint(
                key_hash='cred_ec6300b9', endpoint='https://api.openai.com/v1/anthropic/v1/messages',
                affinity='Mt-Session-Id@0f1e2d3c4b5a')}
    first = {'cache_read_tokens': 0, 'cache_creation_input_tokens': PREV_COLD_WRITE, **wire}
    second = {'cache_read_tokens': 0, 'cache_creation_input_tokens': THIS_WRITE, **wire,
              **(second_extra or {})}
    detect_cache_break(conv, msgs, None, 'claude-opus-5.5', usage=dict(first), user_id=1)
    return detect_cache_break(conv, msgs, None, 'claude-opus-5.5', usage=dict(second), user_id=1)


@pytest.mark.parametrize('second_extra', [None, {'_prev_cold_write_gap_s': 12.0}],
                         ids=['derived-gap', 'explicit-gap'])
def test_sequential_round_after_cold_write_is_an_upstream_miss(second_extra):
    from lib.tasks_pkg.cache_tracking._detect import BUCKET_UPSTREAM, classify_verdict

    verdict = _round_pair('seq-cold-write', second_extra)

    assert verdict is not None, 'the read collapsed after a cold write: a break is expected'
    assert 'cache_write_unsettled' not in verdict, verdict
    assert classify_verdict(verdict) == BUCKET_UPSTREAM, verdict
    text = json.dumps(verdict).lower()
    assert 'client-side timing race' not in text, verdict
    assert 'settle gate' not in text, verdict


def test_legacy_unsettled_verdicts_keep_their_bucket():
    """Records written before this correction still count under their own bucket."""
    from lib.tasks_pkg.cache_tracking._detect import BUCKET_WRITE_UNSETTLED, classify_verdict

    assert classify_verdict({'cache_write_unsettled': 'x'}) == BUCKET_WRITE_UNSETTLED


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
