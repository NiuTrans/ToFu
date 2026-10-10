"""Guard: the ttl-flip detector must not fire on marker-slot COLLISIONS.

Production evidence (2026-07-26, 7-day apiRounds scan, epic pt_6ac5febf):
``markers_ttl_flipped`` reported 358 "cache TTL marker flipped" breaks, ALL on
one slot key ``msg:tool_result(toolu_bdrk)`` and ALL on aws.claude-opus-4.8.

Root cause is NOT a ttl flip. The tool_result slot key took
``tool_use_id[:10]``, and AWS Bedrock mints ids shaped ``toolu_bdrk_01ABC…`` —
so the first 10 chars are IDENTICAL for every tool_result on that line and all
of them collapse into ONE slot key. The stable mid marker (ttl='1h') and the
rolling tail marker (ttl='') then land on two DIFFERENT tool_result messages
that share the collapsed key, so the slot's value SET is ``{'1h',''}``. As the
tail rolls forward the set membership churns ('1h' → {'1h',''} → '' …) and the
set-compare reads that as a ttl VALUE flip on a surviving slot.

Measured: 790 rounds carried a multi-valued slot and in **100% of them** the
value set was exactly ``{'1h',''}`` — i.e. one stable + one tail marker, never
a real 5m↔1h reconfiguration.

Cost of the bug is diagnostic, not billing: it mislabels real misses (which
have their own cause) as a client-side "cache re-keyed" verdict, and — because
``<ttl-flip>`` is a CULPRIT token — it also blocks the honest "server-side
PROVEN" verdict. That is exactly the attribution channel epic pt_a475804a
depends on, so a poisoned label here costs future investigations.

Fix under test: slot keys must be collision-resistant (full tool id, not a
10-char prefix), so two distinct tool_results never share a slot.
"""

import pytest

from lib.tasks_pkg.wire_fingerprint import (
    _canonical_message_entry,
    marker_signature,
    markers_ttl_flipped,
)

pytestmark = pytest.mark.unit


# AWS Bedrock tool ids: the discriminating part starts AFTER 'toolu_bdrk_'.
_AWS_ID_A = 'toolu_bdrk_01AAAAAAAAAAAAAAAAAAAAAA'
_AWS_ID_B = 'toolu_bdrk_01BBBBBBBBBBBBBBBBBBBBBB'


def _tool_msg(tool_id, *, ttl=None):
    """An OpenAI-wire tool message, optionally carrying a cache_control ttl."""
    block = {'type': 'text', 'text': 'result body'}
    if ttl is not None:
        cc = {'type': 'ephemeral'}
        if ttl:
            cc['ttl'] = ttl
        block['cache_control'] = cc
    return {'role': 'tool', 'tool_call_id': tool_id, 'content': [block]}


def test_distinct_aws_tool_ids_get_distinct_slot_keys():
    """Two different AWS tool_results must not collapse into one slot key.

    Uses ``_canonical_message_entry`` — the exact key builder
    ``marker_signature`` slots message markers on.
    """
    key_a = _canonical_message_entry(_tool_msg(_AWS_ID_A))['key']
    key_b = _canonical_message_entry(_tool_msg(_AWS_ID_B))['key']
    assert key_a != key_b, (
        f'slot-key collision: both AWS tool_results keyed as {key_a!r} — '
        'the 10-char id prefix is entirely consumed by the vendor prefix '
        '"toolu_bdrk", so every tool_result on that line shares a slot'
    )


def test_stable_and_tail_markers_on_distinct_tools_are_not_a_ttl_flip():
    """The real production shape: mid marker (1h) on one tool_result, rolling
    tail marker (no ttl) on another, then the tail advances.

    Nothing was RECONFIGURED — each marker kept its own ttl — so this must not
    be reported as a ttl flip.
    """
    # Round N: stable marker on tool A, tail marker on tool B.
    prev = marker_signature({
        'messages': [
            _tool_msg(_AWS_ID_A, ttl='1h'),
            _tool_msg(_AWS_ID_B, ttl=''),
        ],
    })
    # Round N+1: the tail rolled forward; stable marker still on A, and the
    # tail marker moved OFF B (B is now plain buried prefix).
    cur = marker_signature({
        'messages': [
            _tool_msg(_AWS_ID_A, ttl='1h'),
            _tool_msg(_AWS_ID_B),
        ],
    })
    assert not markers_ttl_flipped(prev, cur), (
        'false ttl-flip: the stable marker never changed ttl; only the rolling '
        'tail marker moved between two DIFFERENT tool_results that share a '
        'collapsed slot key'
    )


def test_genuine_ttl_value_flip_still_fires():
    """The detector must keep working: a real 1h→5m change on the SAME message
    is still a client-caused cache re-key."""
    prev = marker_signature({'messages': [_tool_msg(_AWS_ID_A, ttl='1h')]})
    cur = marker_signature({'messages': [_tool_msg(_AWS_ID_A, ttl='')]})
    assert markers_ttl_flipped(prev, cur), (
        'regression: a genuine ttl VALUE flip on one and the same message must '
        'still be detected'
    )
