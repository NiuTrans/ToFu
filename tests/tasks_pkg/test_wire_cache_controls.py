"""Request controls that invalidate cached message blocks are part of the gate.

Anthropic prompt caching ("What invalidates the cache"): changing ``speed``,
``tool_choice``, the ``thinking`` configuration (mode, ``budget_tokens``) or
``output_config.effort`` invalidates cached message blocks even when every
system/tools/message byte is identical. The byte region hashed only system and
tools, so a client-side change of one of these controls was indistinguishable
from an upstream miss and would be filed as ``upstream_identical``.
"""
from __future__ import annotations

import pytest

from lib.tasks_pkg.wire_fingerprint import (
    cache_relevant_controls,
    canonical_messages,
    diff_byte_region,
    wire_byte_prefix,
    wire_byte_region,
)

pytestmark = pytest.mark.unit

_BASE = {'thinking': {'type': 'enabled', 'budget_tokens': 8000},
         'tool_choice': {'type': 'auto'}}


def _region(body):
    return wire_byte_region(None, [], cache_relevant_controls(body))


@pytest.mark.parametrize('changed', [
    {**_BASE, 'thinking': {'type': 'enabled', 'budget_tokens': 4000}},
    {**_BASE, 'thinking': {'type': 'adaptive'}},
    {**_BASE, 'tool_choice': {'type': 'any'}},
    {**_BASE, 'output_config': {'effort': 'high'}},
    {**_BASE, 'speed': 'fast'},
])
def test_cache_relevant_control_change_is_a_region_divergence(changed):
    assert diff_byte_region(_region(_BASE), _region(changed)) == [
        '<bytes>controls']


def test_parameters_outside_the_cache_key_are_not_controls():
    body = {**_BASE, 'max_tokens': 1, 'temperature': 1, 'stream': True,
            'metadata': {'user_id': 'u'},
            'output_config': {'format': {'type': 'json_schema'}}}
    assert diff_byte_region(_region(_BASE), _region(body)) == []


def test_key_order_inside_a_control_is_not_a_divergence():
    reordered = {'thinking': {'budget_tokens': 8000, 'type': 'enabled'},
                 'tool_choice': {'type': 'auto'}}
    assert diff_byte_region(_region(_BASE), _region(reordered)) == []


def test_a_region_captured_without_controls_is_inert():
    old = {k: v for k, v in _region(_BASE).items() if k != 'controls'}
    assert diff_byte_region(old, _region({**_BASE, 'speed': 'fast'})) == []


def _usage(msgs, controls, *, cache_read, cache_write):
    return {
        'cache_read_tokens': cache_read,
        'cache_write_tokens': cache_write,
        '_wire_fp': canonical_messages(msgs),
        '_wire_static': '',
        '_wire_bytes': wire_byte_prefix(msgs),
        '_wire_region': wire_byte_region('SYS ' + 'x' * 200, [], controls),
    }


def _drive(conv, controls_by_round, *, read=0, write=92000):
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
    from lib.tasks_pkg.cache_tracking._state import _cache_states

    r1 = [{'role': 'user', 'content': 'u1'}]
    r2 = r1 + [{'role': 'assistant', 'content': 'a1'}]
    detect_cache_break(conv, r1, None, 'claude-opus-4', usage=_usage(
        r1, controls_by_round[0], cache_read=90000, cache_write=2000),
        user_id=1)
    out = detect_cache_break(conv, r2, None, 'claude-opus-4', usage=_usage(
        r2, controls_by_round[1], cache_read=read, cache_write=write),
        user_id=1)
    _cache_states.clear()
    return out


def test_detector_names_a_thinking_change_instead_of_blaming_upstream():
    before = cache_relevant_controls(_BASE)
    after = cache_relevant_controls(
        {**_BASE, 'thinking': {'type': 'enabled', 'budget_tokens': 4000}})
    out = _drive('controls-gate', (before, after))
    assert out is not None
    assert 'upstream cache miss' not in str(out), out
    assert '<bytes>controls' in str(out)


def test_a_control_change_that_still_reads_the_prefix_is_not_a_break():
    """Explicit default effort is equivalent to omitting it: when the read
    covers the whole cached prefix the change invalidated nothing."""
    before = cache_relevant_controls(_BASE)
    after = cache_relevant_controls(
        {**_BASE, 'output_config': {'effort': 'high'}})
    out = _drive('controls-gate-hit', (before, after), read=92000,
                 write=3000)
    assert out is None or '<bytes>controls' not in str(out), out


def test_detector_NEUTER_without_controls_blames_upstream():
    """The same round with the controls left out of the region is laundered
    into the byte-identical verdict, so the controls field is load-bearing."""
    out = _drive('controls-gate-neuter', (None, None))
    assert out is not None
    assert 'upstream cache miss' in str(out), out
    assert '<bytes>controls' not in str(out)
